"""Same-player renderer process and the sole public/private mediation boundary."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import select
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
    def __init__(self, *args, renderer: dict, frames: Path, **kwargs):
        super().__init__(*args, **kwargs)
        self.renderer, self.frames = renderer, frames
        self.renderer_ready = None
        self.display_process = None
        self.display_log = None
        self.display_cleanup = {"started": False, "stopped": True}

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
            readable, _, _ = await asyncio.to_thread(select.select, [read_fd], [], [], 15)
            if not readable:
                raise TimeoutError("owned Xvfb display startup timeout")
            display = ":" + os.read(read_fd, 64).decode().strip()
            if not display[1:].isdigit() or self.display_process.returncode is not None:
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

    async def close(self) -> None:
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
        public = {
            **project_image(ready),
            "renderer": {
                "actor": native["actor"], "native_tick": None,
                **({"visual_ui": native["visual_ui"]} if "visual_ui" in native else {}),
                "ui_panels": native["ui_panels"],
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
        if self.mode == "image":
            return project_image(response, frame=request.get("cmd") == "frame" and response.get("ok") is True)
        return response
