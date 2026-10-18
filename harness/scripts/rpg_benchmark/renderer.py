"""Same-player renderer process and the sole public/private mediation boundary."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import time

from . import lifecycle
from .native import EpisodeBoundary, Journal, NativeBridge, allowed, stop_owned
from .observations import image_allowed, project_image


def temporal_artifact(value):
    if isinstance(value, list):
        return [temporal_artifact(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: temporal_artifact(item) for key, item in value.items()}
    if result.get("native_tick") is not None:
        result["client_update_counter"] = result["native_tick"]
        result["native_tick"] = None
    return result


class RendererJournal(Journal):
    def write(self, **row):
        row = temporal_artifact(row)
        row["timing_profile"] = "voxygen-variable-dt-client-update-v1"
        row["tick_semantics"] = "actor tick is local client update counter; administrator has its own clock"
        super().write(**row)


async def read_display_number(read_fd):
    """Consume Xserver's complete displayfd line before closing the read end.

    Xserver can write the digits and newline separately. Accepting the first
    readable bytes then closing the pipe races its final write (EPIPE is fatal
    to Xvfb), even though the digits already name a listening display.
    """
    loop = asyncio.get_running_loop()
    result = loop.create_future()
    payload = bytearray()

    def readable():
        if result.done():
            return
        try:
            chunk = os.read(read_fd, 64)
            payload.extend(chunk)
            if len(payload) > 64 or (not chunk and b"\n" not in payload):
                raise RuntimeError("owned Xvfb failed to publish a complete display number")
            if b"\n" in payload:
                number, rest = bytes(payload).split(b"\n", 1)
                if not number or not number.isdigit() or rest:
                    raise RuntimeError("owned Xvfb published an invalid display number")
                result.set_result(":" + number.decode("ascii"))
        except Exception as error:
            result.set_exception(error)

    loop.add_reader(read_fd, readable)
    try:
        return await asyncio.wait_for(result, 15)
    finally:
        loop.remove_reader(read_fd)


async def check_display(display, journal):
    check = await asyncio.create_subprocess_exec(
        "xdpyinfo", "-display", display, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL)
    start, cleanup = None, {}
    try:
        try:
            start = lifecycle.process_start_ticks(check.pid)
        except FileNotFoundError:
            code = await asyncio.wait_for(check.wait(), 10)
            cleanup.update(pid=check.pid, exit_code=code, stopped=True,
                           identity="owned child exited before fingerprint")
        else:
            journal.write(origin="process", role="display-check", pid=check.pid, start_ticks=start)
            code = await asyncio.wait_for(check.wait(), 10)
        if code:
            raise RuntimeError("owned Xvfb display is not responsive")
    finally:
        if start is not None:
            await stop_owned(check, start, cleanup)
        journal.write(origin="process", role="display-check", cleanup=cleanup)


class RendererBridge(NativeBridge):
    def __init__(self, *args, renderer: dict, frames: Path, public_run: dict | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.renderer, self.frames = renderer, frames
        self.renderer_ready = None
        self.display_process = None
        self.display_log = None
        self.display_cleanup = {"started": False, "stopped": True}
        self.observation_sequence = 0
        self.public_media = None
        if public_run is not None:
            from .frame_feed import Publisher
            self.public_media = Publisher(frames, **public_run)

    async def request(self, command, args=None, *, origin="setup"):
        result = await super().request(command, args, origin=origin)
        if command == "observe":
            self.observation_sequence += 1
            result["sample_identity"] = {
                "kind": "host-received-observation-sequence", "session_id": result["session_id"],
                "sequence": self.observation_sequence}
            result["received_host_monotonic"] = time.monotonic()
        return result

    def on_event(self, row: dict) -> None:
        if row.get("event") == "native_renderer_ready" and not self.renderer_ready.done():
            self.renderer_ready.set_result(row)

    def measured_receipt(self, request: dict, response: dict) -> dict:
        from .visual_ui import private_receipt
        return private_receipt(request, response, self.ready.result()["uid"], time.monotonic())

    async def start(self) -> None:
        self.renderer_ready = asyncio.get_running_loop().create_future()
        home, xdg, settings = (self.out / name for name in ("renderer-home", "renderer-xdg", "renderer-config"))
        for path in (home, xdg, settings):
            path.mkdir(mode=0o700)
        (settings / "settings.ron").write_text('''(
show_disclaimer:false,
networking:(use_srv:false,use_quic:false,enable_discord_integration:false),
audio:(output:Off),
graphics:(terrain_view_distance:4,entity_view_distance:4,lod_distance:0,
max_fps:Max(60),max_background_fps:Max(60),
window:(size:(960,540),maximised:false),fullscreen:(enabled:false),
render_mode:(shadow:None,cloud:Minimal,bloom:Off,profiler_enabled:false)),
)\n''')
        read_fd, write_fd = os.pipe()
        try:
            self.display_log = (self.out / "xvfb.log").open("x")
            display_command = ["Xvfb", "-displayfd", str(write_fd), "-screen", "0", "960x540x24",
                               "-nolisten", "tcp"]
            self.display_process = await asyncio.create_subprocess_exec(
                *display_command, pass_fds=(write_fd,), stdout=self.display_log,
                stderr=asyncio.subprocess.STDOUT)
            self.display_start = lifecycle.process_start_ticks(self.display_process.pid)
            self.display_cleanup = {"started": True, "stopped": False}
            self.journal.write(origin="process", role="xvfb", command=display_command,
                               pid=self.display_process.pid, start_ticks=self.display_start)
            os.close(write_fd)
            write_fd = None
            display = await read_display_number(read_fd)
            if self.display_process.returncode is not None:
                raise RuntimeError("owned Xvfb failed to publish a display")
        finally:
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)
        await check_display(display, self.journal)
        self.journal.write(origin="process", role="xvfb", responsive=True)
        self.ready = asyncio.get_running_loop().create_future()
        self.stderr = (self.out / f"{self.name}-bridge.log").open("x")
        environment = {
            **os.environ, "HOME": str(home), "XDG_RUNTIME_DIR": str(xdg), "DISPLAY": display,
            "VELOREN_ASSETS": self.renderer["paths"]["assets"],
            "VELOREN_SESSION_ID": self.name, "VOXYGEN_CONFIG": str(settings),
            "VELOREN_USERDATA": str(self.out / "renderer-userdata"), "WGPU_BACKEND": "vulkan",
            "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
        }
        command = [self.renderer["paths"]["executable"], "--server", f"127.0.0.1:{self.port}",
                   "--client-type", "game", "--agent-bridge", "--agent-frame-dir", str(self.frames),
                   "--agent-observation", "hybrid", "--agent-offline-user", self.name,
                   "--agent-character", self.name, "--agent-create-character"]
        self.process = await asyncio.create_subprocess_exec(
            *command, env=environment, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=self.stderr, limit=16 * 1024 * 1024)
        self.start_ticks = lifecycle.process_start_ticks(self.process.pid)
        self.journal.write(origin="process", role="renderer", command=command,
                           pid=self.process.pid, start_ticks=self.start_ticks,
                           backend_visibility="trusted-private-hybrid-pipe")
        self.reader_task = asyncio.create_task(self._read())
        await asyncio.wait_for(asyncio.shield(self.ready), 90)
        ready_wait = asyncio.ensure_future(asyncio.shield(self.renderer_ready))
        done, _ = await asyncio.wait({ready_wait, self.reader_task},
                                     timeout=120, return_when=asyncio.FIRST_COMPLETED)
        if ready_wait not in done:
            ready_wait.cancel()
            raise RuntimeError("renderer readiness unavailable; inspect private native journal")
        ready = ready_wait.result()
        if (ready["session_id"] != self.ready.result()["session_id"]
                or ready["data"]["same_client"] is not True
                or ready["data"]["client_type"] != "game"):
            raise RuntimeError("renderer is not the same ordinary player")
        if self.public_media is not None:
            try:
                from .passive_capture import register
                register(self.public_media, display=display, xvfb_pid=self.display_process.pid,
                         xvfb_start=self.display_start, renderer_pid=self.process.pid,
                         renderer_start=self.start_ticks, session_id=ready["session_id"],
                         actor=ready["data"]["actor"])
            except Exception as error:
                # Optional registration must not mutate gameplay/public frame fences.
                from .frame_feed import atomic_json
                self.public_media.run["passive_capture_error"] = type(error).__name__
                atomic_json(self.public_media.root / "run.json", self.public_media.run)
                self.journal.write(origin="media", event="passive_capture_registration_error",
                                   error_type=type(error).__name__)

    async def close(self) -> None:
        # Revoke passive attachment before retiring its registered processes.
        if self.public_media is not None:
            try:
                self.public_media.close()
            except (OSError, ValueError) as error:
                self.journal.write(origin="media", event="public_feed_close_error",
                                   error_type=type(error).__name__)
        try:
            await super().close()
        finally:
            if self.display_process is not None:
                await stop_owned(self.display_process, self.display_start, self.display_cleanup)
            if self.display_log is not None:
                self.display_log.close()
            self.cleanup["display"] = self.display_cleanup


class RendererBoundary(EpisodeBoundary):
    def __init__(self, bridge: RendererBridge, socket_path: Path, mode: str):
        super().__init__(bridge, socket_path)
        self.mode = mode

    def request_allowed(self, request: dict) -> bool:
        if request.get("cmd") == "step" and request.get("args", {}).get("action", {}).get("type") == "visual_input":
            from scripts.rpg_sdk_agent import validate_request
            validate_request("step", request["args"], renderer=True)
            return self.bridge.renderer_ready.result()["data"].get("visual_ui", {}).get("schema") == "native-visual-ui/1"
        if request.get("cmd") in {"frame", "ui"}:
            return True
        if self.mode == "image":
            return image_allowed(request)
        return allowed(request)

    def public_ready(self) -> dict:
        ready = self.bridge.ready.result()
        native = self.bridge.renderer_ready.result()["data"]
        selected_renderer = getattr(self.bridge, "renderer", {})
        public = {
            **project_image(ready),
            "renderer": {
                "actor": native["actor"], "native_tick": None,
                **({"visual_ui": native["visual_ui"]} if "visual_ui" in native else {}),
                "ui_panels": native["ui_panels"],
                **({"benchmark_features": selected_renderer["benchmark_features"]}
                   if "benchmark_features" in selected_renderer else {}),
                "visibility_profile": "image-receipts-v1" if self.mode == "image" else "structured-native-v2",
                "control": {key: native["actuator"]["control"][key] for key in (
                    "owner", "generation", "sequence", "protection_generation")
                    if key in native["actuator"]["control"]},
            },
        }
        if self.mode != "image":
            public["capabilities"] = ready.get("capabilities", {})
        return public

    def public_response(self, response: dict, request: dict) -> dict:
        observer = getattr(self, "public_observer", None)
        if observer is not None:
            observer(request, response)
        public = (project_image(response, frame=request.get("cmd") == "frame" and response.get("ok") is True)
                  if self.mode == "image" else response)
        publisher = getattr(self.bridge, "public_media", None)
        if publisher is not None and request.get("cmd") == "frame" and public.get("ok") is True:
            try:
                publisher.publish(public["result"])
            except (OSError, ValueError, KeyError, TypeError) as error:
                # Optional media must not change the native/public response or agent semantics.
                self.bridge.journal.write(origin="media", event="public_feed_error",
                                          error_type=type(error).__name__)
                try:
                    publisher.fail(error)
                except (OSError, ValueError):
                    pass
        return public
