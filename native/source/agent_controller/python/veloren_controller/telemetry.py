from __future__ import annotations

import copy
import json
import queue
import threading
import time
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from .controller import VelorenController
from .errors import BridgeTimeoutError


TELEMETRY_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class TelemetryFrame:
    sequence: int
    captured_at: float
    session_id: str | None
    observation: dict[str, Any]
    events: list[dict[str, Any]]
    event_gap: dict[str, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "telemetry_frame",
            "telemetry_schema_version": TELEMETRY_SCHEMA_VERSION,
            "sequence": self.sequence,
            "captured_at": self.captured_at,
            "session_id": self.session_id,
            "observation": copy.deepcopy(self.observation),
            "events": copy.deepcopy(self.events),
            "event_gap": copy.deepcopy(self.event_gap),
        }


class TelemetryCollector:
    """Periodically samples controller state for visualization consumers."""

    def __init__(
        self,
        controller: VelorenController,
        *,
        sample_hz: float = 10.0,
        trajectory_limit: int = 2_048,
        event_history_limit: int = 1_024,
        message_queue_limit: int = 256,
        event_page_size: int = 256,
    ) -> None:
        if not 0.1 <= sample_hz <= 60.0:
            raise ValueError("sample_hz must be between 0.1 and 60")
        for name, value in (
            ("trajectory_limit", trajectory_limit),
            ("event_history_limit", event_history_limit),
            ("message_queue_limit", message_queue_limit),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if not 1 <= event_page_size <= 256:
            raise ValueError("event_page_size must be between 1 and 256")

        self.controller = controller
        self.sample_hz = sample_hz
        self.event_page_size = event_page_size

        self._trajectory: deque[dict[str, Any]] = deque(maxlen=trajectory_limit)
        self._event_history: deque[dict[str, Any]] = deque(
            maxlen=event_history_limit
        )
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue(
            maxsize=message_queue_limit
        )
        self._lock = threading.RLock()
        self._sample_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._latest_frame: TelemetryFrame | None = None
        self._event_cursor = 0
        self._frame_sequence = 0
        self._dropped_messages = 0
        self._sample_errors = 0
        self._recording_errors = 0
        self._recorder: TextIO | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def dropped_messages(self) -> int:
        with self._lock:
            return self._dropped_messages

    @property
    def sample_errors(self) -> int:
        with self._lock:
            return self._sample_errors

    def _collect_events(self) -> tuple[list[dict[str, Any]], dict[str, int] | None]:
        events: list[dict[str, Any]] = []
        gap: dict[str, int] | None = None
        for _ in range(256):
            requested_after = self._event_cursor
            batch = self.controller.events_since(
                requested_after, limit=self.event_page_size
            )
            if batch.truncated and gap is None:
                gap = {
                    "requested_after": requested_after,
                    "oldest_available": batch.oldest_seq,
                }
            if batch.events:
                events.extend(batch.events)
                self._event_cursor = max(
                    self._event_cursor,
                    max(int(event["seq"]) for event in batch.events),
                )
            if not batch.has_more or not batch.events:
                break
        else:
            raise RuntimeError("event pagination did not converge")
        return events, gap

    @staticmethod
    def _trajectory_point(observation: dict[str, Any]) -> dict[str, Any] | None:
        position = observation.get("position")
        if (
            not isinstance(position, list)
            or len(position) != 3
            or not all(isinstance(value, (int, float)) for value in position)
        ):
            return None
        return {
            "tick": observation.get("tick"),
            "position": [float(value) for value in position],
        }

    def sample_once(self) -> TelemetryFrame:
        with self._sample_lock:
            observation = self.controller.observe()
            events, event_gap = self._collect_events()
            self.controller.drain_event_queue()
            point = self._trajectory_point(observation)
            with self._lock:
                self._frame_sequence += 1
                if point is not None:
                    self._trajectory.append(point)
                self._event_history.extend(copy.deepcopy(events))
                frame = TelemetryFrame(
                    sequence=self._frame_sequence,
                    captured_at=time.time(),
                    session_id=self.controller.session_id,
                    observation=copy.deepcopy(observation),
                    events=copy.deepcopy(events),
                    event_gap=event_gap,
                )
                self._latest_frame = frame
            self._publish(frame.to_dict())
            return frame

    def _publish(self, message: dict[str, Any]) -> None:
        try:
            self._messages.put_nowait(copy.deepcopy(message))
        except queue.Full:
            try:
                self._messages.get_nowait()
            except queue.Empty:
                pass
            with self._lock:
                self._dropped_messages += 1
            self._messages.put_nowait(copy.deepcopy(message))
        with self._lock:
            if self._recorder is not None:
                try:
                    self._recorder.write(
                        json.dumps(
                            message,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                    self._recorder.flush()
                except OSError:
                    self._recording_errors += 1
                    self._recorder.close()
                    self._recorder = None

    def _run(self) -> None:
        interval = 1.0 / self.sample_hz
        next_sample = time.monotonic()
        while not self._stop.is_set():
            try:
                self.sample_once()
            except Exception as error:
                with self._lock:
                    self._sample_errors += 1
                self._publish(
                    {
                        "type": "telemetry_error",
                        "telemetry_schema_version": TELEMETRY_SCHEMA_VERSION,
                        "captured_at": time.time(),
                        "session_id": self.controller.session_id,
                        "error": {
                            "type": type(error).__name__,
                            "message": str(error),
                        },
                    }
                )
                if not self.controller.is_running:
                    break
            next_sample += interval
            delay = max(0.0, next_sample - time.monotonic())
            if self._stop.wait(delay):
                break
            if delay == 0.0:
                next_sample = time.monotonic()

    def start(self) -> None:
        if self.is_running:
            return
        if not self.controller.is_running:
            raise RuntimeError("controller must be running before telemetry starts")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="veloren-telemetry",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            if timeout is None:
                timeout = float(getattr(self.controller, "timeout", 5.0)) + 2.0
            thread.join(timeout)
            if thread.is_alive():
                raise TimeoutError("telemetry thread did not stop")
        self.stop_recording()

    def get_message(self, timeout: float | None = None) -> dict[str, Any]:
        try:
            return self._messages.get(timeout=timeout)
        except queue.Empty as error:
            raise BridgeTimeoutError("telemetry message queue timed out") from error

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "telemetry_schema_version": TELEMETRY_SCHEMA_VERSION,
                "session_id": self.controller.session_id,
                "latest_frame": (
                    self._latest_frame.to_dict()
                    if self._latest_frame is not None
                    else None
                ),
                "trajectory": copy.deepcopy(list(self._trajectory)),
                "event_history": copy.deepcopy(list(self._event_history)),
                "stats": {
                    "dropped_messages": self._dropped_messages,
                    "sample_errors": self._sample_errors,
                    "recording_errors": self._recording_errors,
                    "controller_event_queue_drops": self.controller.event_queue_drops,
                    "controller_event_sequence_gaps": (
                        self.controller.event_sequence_gaps
                    ),
                },
            }

    def start_recording(self, path: str | Path, *, append: bool = False) -> None:
        target = Path(path)
        if not target.parent.exists():
            raise FileNotFoundError(
                f"recording directory does not exist: {target.parent}"
            )
        with self._lock:
            if self._recorder is not None:
                raise RuntimeError("telemetry recording is already active")
            self._recorder = target.open(
                "a" if append else "w",
                encoding="utf-8",
                newline="\n",
            )

    def stop_recording(self) -> None:
        with self._lock:
            recorder, self._recorder = self._recorder, None
            if recorder is not None:
                recorder.close()

    def __enter__(self) -> TelemetryCollector:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()


def read_telemetry_jsonl(path: str | Path) -> Iterator[dict[str, Any]]:
    with Path(path).open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"invalid telemetry JSON on line {line_number}: {error.msg}"
                ) from error
            if not isinstance(value, dict):
                raise ValueError(
                    f"telemetry line {line_number} must contain a JSON object"
                )
            yield value
