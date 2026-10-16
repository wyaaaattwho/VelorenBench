"""Synchronous Python client for the Rust NDJSON agent bridge."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .errors import (
    BridgeCommandError,
    BridgeStartupError,
    BridgeTimeoutError,
    VelorenControllerError,
)
from .models import ActionResult, EventBatch


def _env_flag(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


class VelorenController:
    """Synchronous Python client for the Rust NDJSON bridge."""

    PROTOCOL_VERSION = "0.2"
    OBSERVATION_SCHEMA_VERSION = "1.1"

    def __init__(
        self,
        *,
        repository: str | Path | None = None,
        command: Sequence[str] | None = None,
        server: str = "127.0.0.1:14004",
        username: str = "agent",
        password: str = "",
        character: str | None = None,
        timeout: float = 10.0,
        startup_timeout: float | None = None,
        visual_pace: bool | None = None,
        pace_seconds: float | None = None,
        hold_open_seconds: float | None = None,
    ) -> None:
        self.repository = Path(repository).resolve() if repository else self._default_repository()
        self.server = server
        self.username = username
        self.password = password
        self.character = character
        self.timeout = timeout
        self.startup_timeout = (
            float(startup_timeout)
            if startup_timeout is not None
            else self._default_startup_timeout()
        )
        self.visual_pace = (
            bool(visual_pace)
            if visual_pace is not None
            else self._default_visual_pace()
        )
        self.pace_seconds = (
            max(0.0, float(pace_seconds))
            if pace_seconds is not None
            else _env_float(
                "VELOREN_PACE_SECONDS",
                2.5 if self.visual_pace else 1.0,
            )
        )
        self.hold_open_seconds = (
            max(0.0, float(hold_open_seconds))
            if hold_open_seconds is not None
            else _env_float(
                "VELOREN_HOLD_OPEN_SECONDS",
                10.0 if self.visual_pace else 0.0,
            )
        )
        self.command = list(command) if command else self._default_command()

        self._process: subprocess.Popen[str] | None = None
        self._pending: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._events: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1024)
        self._event_queue_drops = 0
        self._event_sequence_gaps = 0
        self._last_event_seq = 0
        self._stderr: list[str] = []
        self._ready = threading.Event()
        self._fatal: str | None = None
        self.session_id: str | None = None

    @staticmethod
    def _default_repository() -> Path:
        return Path(__file__).resolve().parents[3]

    @staticmethod
    def _agent_mode() -> str:
        return os.environ.get("VELOREN_AGENT_MODE", "headless").strip().lower()

    @classmethod
    def _default_visual_pace(cls) -> bool:
        flagged = _env_flag("VELOREN_VISUAL_PACE")
        if flagged is not None:
            return flagged
        # Visual/Voxygen demos should be human-followable by default.
        return cls._agent_mode() in {"voxygen", "visual", "fpv"}

    @classmethod
    def _default_startup_timeout(cls) -> float:
        """Allow a long interactive login window for visual/Voxygen mode."""
        raw = os.environ.get("VELOREN_STARTUP_TIMEOUT")
        if raw:
            try:
                return max(1.0, float(raw))
            except ValueError:
                pass
        if cls._agent_mode() in {"voxygen", "visual", "fpv"}:
            # Manual login + character select can take several minutes.
            return 1800.0
        return 45.0

    def _default_executable(self) -> Path:
        suffix = ".exe" if os.name == "nt" else ""
        configured_target = os.environ.get("CARGO_TARGET_DIR")
        target = Path(configured_target) if configured_target else self.repository / "target"
        if not target.is_absolute():
            target = self.repository / target
        mode = self._agent_mode()
        if mode in {"voxygen", "visual", "fpv"}:
            return target / "debug" / f"veloren-voxygen{suffix}"
        return target / "debug" / "examples" / f"agent_bridge{suffix}"

    def _default_command(self) -> list[str]:
        executable = str(self._default_executable())
        mode = self._agent_mode()
        if mode in {"voxygen", "visual", "fpv"}:
            command = [executable, "--agent-bridge"]
            if self.character:
                command.extend(["--character", self.character])
            if self.username:
                command.extend(["--username", self.username])
            # Empty password is still a valid local-auth value.
            command.extend(["--password", self.password])
            if self.server:
                command.extend(["--server", self.server])
            return command
        return [executable]

    def _pace(self, *, after_close: bool = False) -> None:
        seconds = self.hold_open_seconds if after_close else self.pace_seconds
        if not self.visual_pace or seconds <= 0:
            return
        time.sleep(seconds)

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def stderr_lines(self) -> tuple[str, ...]:
        return tuple(self._stderr)

    @property
    def event_queue_drops(self) -> int:
        return self._event_queue_drops

    @property
    def event_sequence_gaps(self) -> int:
        return self._event_sequence_gaps

    def start(self) -> None:
        if self.is_running:
            return
        if not self.command:
            raise BridgeStartupError("bridge command is empty")
        if len(self.command) == 1 and not Path(self.command[0]).exists():
            raise BridgeStartupError(
                f"bridge executable not found: {self.command[0]}; build it with "
                "`cargo +nightly-2026-06-13 build --example agent_bridge "
                "-p veloren-client --features bin_bot`"
            )

        environment = os.environ.copy()
        environment.update(
            {
                "VELOREN_SERVER": self.server,
                "VELOREN_USERNAME": self.username,
                "VELOREN_PASSWORD": self.password,
            }
        )
        if self.character:
            environment["VELOREN_CHARACTER"] = self.character

        self._ready.clear()
        self._fatal = None
        self.session_id = None
        self._stderr.clear()
        self._event_queue_drops = 0
        self._event_sequence_gaps = 0
        self._last_event_seq = 0
        while not self._events.empty():
            try:
                self._events.get_nowait()
            except queue.Empty:
                break
        self._process = subprocess.Popen(
            self.command,
            cwd=self.repository,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

        if not self._ready.wait(self.startup_timeout):
            message = self._fatal or (
                f"bridge did not emit a ready event within {self.startup_timeout:.0f}s"
            )
            self.terminate()
            raise BridgeStartupError(message)
        if self._fatal:
            message = self._fatal
            self.terminate()
            raise BridgeStartupError(message)

    def _fail_pending(self, message: str) -> None:
        with self._pending_lock:
            pending = list(self._pending.items())
        for request_id, target in pending:
            try:
                target.put_nowait(
                    {
                        "type": "response",
                        "protocol_version": self.PROTOCOL_VERSION,
                        "session_id": self.session_id,
                        "id": request_id,
                        "ok": False,
                        "result": None,
                        "error": {"code": "bridge_exited", "message": message},
                    }
                )
            except queue.Full:
                pass

    def _read_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        for raw_line in self._process.stdout:
            try:
                message = json.loads(raw_line)
            except json.JSONDecodeError:
                self._fatal = f"bridge wrote non-JSON data to stdout: {raw_line.rstrip()}"
                self._ready.set()
                continue

            if message.get("type") == "response":
                request_id = str(message.get("id"))
                with self._pending_lock:
                    target = self._pending.get(request_id)
                if target is not None:
                    target.put(message)
                continue

            try:
                seq = message.get("seq")
                if isinstance(seq, int) and seq > 0:
                    if self._last_event_seq and seq > self._last_event_seq + 1:
                        self._event_sequence_gaps += seq - self._last_event_seq - 1
                    self._last_event_seq = max(self._last_event_seq, seq)
                self._events.put_nowait(message)
            except queue.Full:
                try:
                    self._events.get_nowait()
                except queue.Empty:
                    pass
                self._event_queue_drops += 1
                self._events.put_nowait(message)
            if message.get("event") == "ready":
                version = message.get("protocol_version")
                if version != self.PROTOCOL_VERSION:
                    self._fatal = (
                        f"protocol mismatch: expected {self.PROTOCOL_VERSION}, got {version}"
                    )
                self.session_id = message.get("session_id")
                self._ready.set()
            elif message.get("event") == "fatal":
                self._fatal = str(message.get("error", "unknown bridge error"))
                self._ready.set()

        if not self._ready.is_set():
            self._fatal = "bridge exited before becoming ready"
            self._ready.set()
        self._fail_pending(self._fatal or "bridge process exited")

    def _read_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        for line in self._process.stderr:
            self._stderr.append(line.rstrip())

    def request(
        self,
        command: str,
        args: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        if not self.is_running or self._process is None or self._process.stdin is None:
            raise VelorenControllerError("bridge is not running")

        request_id = uuid.uuid4().hex
        target: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[request_id] = target

        try:
            payload = {
                "id": request_id,
                "protocol_version": self.PROTOCOL_VERSION,
                "cmd": command,
                "args": args or {},
            }
            try:
                with self._write_lock:
                    self._process.stdin.write(
                        json.dumps(payload, separators=(",", ":")) + "\n"
                    )
                    self._process.stdin.flush()
            except (BrokenPipeError, OSError) as error:
                raise VelorenControllerError(
                    f"failed to write command to bridge: {command}"
                ) from error
            try:
                response = target.get(timeout=self.timeout if timeout is None else timeout)
            except queue.Empty as error:
                raise BridgeTimeoutError(f"command timed out: {command}") from error
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

        if response.get("protocol_version") != self.PROTOCOL_VERSION:
            raise BridgeCommandError(
                "protocol_mismatch",
                f"expected response protocol {self.PROTOCOL_VERSION}, "
                f"got {response.get('protocol_version')}",
            )
        response_session = response.get("session_id")
        if self.session_id is not None and response_session != self.session_id:
            raise BridgeCommandError(
                "session_mismatch",
                f"expected session {self.session_id}, got {response_session}",
            )
        if not response.get("ok"):
            details = response.get("error") or {}
            raise BridgeCommandError(
                str(details.get("code", "unknown_error")),
                str(details.get("message", "bridge rejected the command")),
            )
        return response.get("result")

    def observe(self) -> dict[str, Any]:
        return self.request("observe")

    def health(self) -> dict[str, Any]:
        return self.request("health")

    def step(
        self,
        action: dict[str, Any],
        *,
        action_id: str | None = None,
        duration_ticks: int | None = None,
        timeout_ticks: int | None = None,
        timeout: float | None = None,
    ) -> ActionResult:
        actual_action_id = action_id or uuid.uuid4().hex
        payload: dict[str, Any] = {
            "action": {**action, "action_id": actual_action_id},
        }
        if duration_ticks is not None:
            payload["duration_ticks"] = duration_ticks
        if timeout_ticks is not None:
            payload["timeout_ticks"] = timeout_ticks

        if timeout is None:
            expected_ticks = max(duration_ticks or 0, timeout_ticks or 0)
            if expected_ticks:
                timeout = max(self.timeout, expected_ticks / 60.0 + 5.0)
        result = self.request("step", payload, timeout=timeout)
        action_result = ActionResult.from_dict(result)
        # Give the renderer time to catch up during human demos.
        self._pace()
        return action_result

    def move_forward(
        self, speed: float = 1.0, duration_ticks: int = 60
    ) -> ActionResult:
        return self.step(
            {"type": "move_forward", "speed": speed},
            duration_ticks=duration_ticks,
        )

    def move_backward(
        self, speed: float = 1.0, duration_ticks: int = 60
    ) -> ActionResult:
        return self.step(
            {"type": "move_backward", "speed": speed},
            duration_ticks=duration_ticks,
        )

    def move_left(self, speed: float = 1.0, duration_ticks: int = 60) -> ActionResult:
        return self.step(
            {"type": "move_left", "speed": speed},
            duration_ticks=duration_ticks,
        )

    def move_right(self, speed: float = 1.0, duration_ticks: int = 60) -> ActionResult:
        return self.step(
            {"type": "move_right", "speed": speed},
            duration_ticks=duration_ticks,
        )

    def move_vector(
        self,
        x: float,
        y: float,
        *,
        speed: float = 1.0,
        duration_ticks: int = 60,
    ) -> ActionResult:
        return self.step(
            {"type": "move_vector", "direction": [x, y], "speed": speed},
            duration_ticks=duration_ticks,
        )

    def move_to(
        self,
        position: Sequence[float],
        *,
        speed: float = 1.0,
        tolerance: float = 1.0,
        timeout_ticks: int = 600,
    ) -> ActionResult:
        return self.step(
            {
                "type": "move_to",
                "position": list(position),
                "speed": speed,
                "tolerance": tolerance,
            },
            timeout_ticks=timeout_ticks,
        )

    def look_at(self, position: Sequence[float]) -> ActionResult:
        return self.step({"type": "look_at", "position": list(position)})

    def turn(self, degrees: float) -> ActionResult:
        return self.step({"type": "turn", "degrees": degrees})

    def stop(self) -> ActionResult:
        return self.step({"type": "stop"})

    def jump(self, duration_ticks: int = 12) -> ActionResult:
        return self.step({"type": "jump"}, duration_ticks=duration_ticks)

    def attack(
        self,
        *,
        target_uid: int | None = None,
        secondary: bool = False,
        duration_ticks: int = 30,
        timeout_ticks: int = 180,
    ) -> ActionResult:
        action: dict[str, Any] = {
            "type": "attack_secondary" if secondary else "attack",
        }
        if target_uid is not None:
            action["target_uid"] = target_uid
        return self.step(
            action,
            duration_ticks=duration_ticks,
            timeout_ticks=timeout_ticks,
        )

    def block(self, duration_ticks: int = 30) -> ActionResult:
        return self.step({"type": "block"}, duration_ticks=duration_ticks)

    def roll(self, duration_ticks: int = 20) -> ActionResult:
        return self.step({"type": "roll"}, duration_ticks=duration_ticks)

    def toggle_wield(self, wield: bool | None = None) -> ActionResult:
        action: dict[str, Any] = {"type": "toggle_wield"}
        if wield is not None:
            action["wield"] = wield
        return self.step(action, timeout_ticks=180)

    def pickup(
        self,
        uid: int | None = None,
        *,
        timeout_ticks: int = 180,
    ) -> ActionResult:
        action: dict[str, Any] = {"type": "pickup"}
        if uid is not None:
            action["uid"] = uid
        return self.step(action, timeout_ticks=timeout_ticks)

    def collect(
        self,
        position: Sequence[float | int],
        *,
        timeout_ticks: int = 180,
    ) -> ActionResult:
        return self.step(
            {"type": "collect", "position": [int(v) for v in position]},
            timeout_ticks=timeout_ticks,
        )

    def mine(
        self,
        position: Sequence[float | int],
        *,
        duration_ticks: int = 120,
        timeout_ticks: int = 600,
    ) -> ActionResult:
        return self.step(
            {"type": "mine", "position": [int(v) for v in position]},
            duration_ticks=duration_ticks,
            timeout_ticks=timeout_ticks,
        )

    def use_item(
        self,
        slot: int | None = None,
        *,
        loadout_idx: int | None = None,
        slot_idx: int | None = None,
        timeout_ticks: int = 180,
    ) -> ActionResult:
        action: dict[str, Any] = {"type": "use_item"}
        if slot is not None:
            action["slot"] = slot
        if loadout_idx is not None:
            action["loadout_idx"] = loadout_idx
        if slot_idx is not None:
            action["slot_idx"] = slot_idx
        return self.step(action, timeout_ticks=timeout_ticks)

    def drop_item(
        self,
        slot: int | None = None,
        *,
        loadout_idx: int | None = None,
        slot_idx: int | None = None,
        equip_slot: str | None = None,
        timeout_ticks: int = 180,
    ) -> ActionResult:
        action: dict[str, Any] = {"type": "drop"}
        if slot is not None:
            action["slot"] = slot
        if loadout_idx is not None:
            action["loadout_idx"] = loadout_idx
        if slot_idx is not None:
            action["slot_idx"] = slot_idx
        if equip_slot is not None:
            action["equip_slot"] = equip_slot
        return self.step(action, timeout_ticks=timeout_ticks)

    def equip(
        self,
        slot: int | None = None,
        *,
        loadout_idx: int | None = None,
        slot_idx: int | None = None,
        equip_slot: str = "active_mainhand",
        timeout_ticks: int = 180,
    ) -> ActionResult:
        action: dict[str, Any] = {"type": "equip", "equip_slot": equip_slot}
        if slot is not None:
            action["slot"] = slot
        if loadout_idx is not None:
            action["loadout_idx"] = loadout_idx
        if slot_idx is not None:
            action["slot_idx"] = slot_idx
        return self.step(action, timeout_ticks=timeout_ticks)

    def swap_weapons(self) -> ActionResult:
        return self.step({"type": "swap_weapons"})

    def events(self) -> list[dict[str, Any]]:
        return self.request("events")

    def events_since(self, after_seq: int, *, limit: int = 256) -> EventBatch:
        if after_seq < 0:
            raise ValueError("after_seq must be non-negative")
        if not 1 <= limit <= 256:
            raise ValueError("limit must be between 1 and 256")
        result = self.request("events", {"after_seq": after_seq, "limit": limit})
        return EventBatch.from_dict(result)

    def get_event(self, timeout: float | None = None) -> dict[str, Any]:
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty as error:
            raise BridgeTimeoutError("event queue timed out") from error

    def drain_event_queue(self, limit: int = 1_024) -> list[dict[str, Any]]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        events: list[dict[str, Any]] = []
        while len(events) < limit:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                break
        return events

    def close(self) -> None:
        if not self.is_running:
            self.terminate()
            return
        # Keep the window up briefly so the last action is visible in demos.
        self._pace(after_close=True)
        try:
            self.request("close", timeout=2.0)
        except VelorenControllerError:
            pass
        finally:
            self.terminate()

    def terminate(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        self._fail_pending("bridge was terminated")
        if process.stdin:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()

    def __enter__(self) -> VelorenController:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
