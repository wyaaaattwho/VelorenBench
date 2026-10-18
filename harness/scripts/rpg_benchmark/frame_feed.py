"""Public, read-only renderer media feed. No protocol/control socket access.

The small publisher runs only on already-requested public frame responses.
PNG validation reuses the benchmark's existing attachment validator.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from uuid import uuid4

from .observations import verify_png
from .output_paths import validate_root

SCHEMA = "velorenbench-public-frames/1"
MAX_JSON = 32768
MAX_PNG = 32 * 1024 * 1024
KEY = re.compile(r"[0-9a-f]{64}")


def safe_path(path: Path) -> Path:
    path = Path(path)
    validate_root(path.parent)
    if not path.is_absolute() or ".." in path.parts or path.is_symlink():
        raise ValueError("absolute non-symlink path without traversal required")
    return path


def open_read(path: Path):
    """Walk with no-follow openat on POSIX, not just a racy resolve check."""
    path = safe_path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = None
    try:
        if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
            fd = os.open(path.anchor, flags | os.O_DIRECTORY)
            for part in path.parts[1:-1]:
                child = os.open(part, flags | os.O_DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = child
            child = os.open(path.name, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        else:
            fd = os.open(path, flags)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("not a regular file")
        handle = os.fdopen(fd, "rb")
        fd = None
        return handle
    finally:
        if fd is not None:
            os.close(fd)


def read_bytes(path: Path, limit: int) -> bytes:
    with open_read(path) as handle:
        if os.fstat(handle.fileno()).st_size > limit:
            raise ValueError("file exceeds bound")
        data = handle.read(limit + 1)
        if len(data) > limit:
            raise ValueError("file exceeds bound")
        return data


def read_json(path: Path) -> dict:
    value = json.loads(read_bytes(path, MAX_JSON))
    if not isinstance(value, dict):
        raise ValueError("public media metadata must be an object")
    return value


def atomic_json(path: Path, value: dict) -> None:
    data = json.dumps(value, allow_nan=False, sort_keys=True).encode()
    if len(data) > MAX_JSON:
        raise ValueError("public media metadata exceeds bound")
    temporary = path.with_name("." + uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def frame_key(row: dict) -> str:
    return hashlib.sha256(json.dumps(
        [row["session_id"], row["actor"], row["frame_id"]],
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def frame_time(row: dict) -> int:
    """No equivalence between native readback and passive X11 sampling clocks."""
    passive = row.get("capture_origin", {}).get("kind") == "passive-renderer-x11"
    return int(row["capture_host"]["sample_pts_ns" if passive else "begin_ns"])


def overlaps_source(source, path: Path) -> bool:
    return any(path == root or path.is_relative_to(root) or root.is_relative_to(path)
               for root in getattr(source, "protected_directories", [source.directory]))


class Publisher:
    """Only public identities, capture times and attachment descriptors leave here."""
    def __init__(self, directory: Path, run_id: str, task_id: str, terrain: str):
        self.directory = directory
        self.root = directory / "public-view"
        self.root.mkdir(mode=0o700)
        (self.root / "frames").mkdir()
        self.run = {"schema": SCHEMA, "run_id": run_id, "task_id": task_id,
                    "terrain": terrain, "status": "running", "started_at": time.time(),
                    "provenance": "already-requested same-player public renderer frame responses",
                    "frame_policy": "no autonomous captures; no structured/private fallback"}
        atomic_json(self.root / "run.json", self.run)

    def publish(self, frame: dict) -> None:
        image = frame["image"]
        path = Path(image["path"])
        if path.parent != self.directory or path.name in ("", ".", ".."):
            raise ValueError("renderer PNG is outside the selected frame directory")
        row = {
            "schema": SCHEMA, "run_id": self.run["run_id"], "task_id": self.run["task_id"],
            "session_id": frame["session_id"], "actor": frame["actor"],
            "frame_id": frame["frame_id"], "capture_host": frame["capture_host"],
            "client_update_counter": frame.get("client_update_counter"),
            "native_tick": None, "action_effect_ordering": "unconfirmed",
            "published_at": time.time(),
            "image": {key: image[key] for key in (
                "mime_type", "sha256", "byte_length", "width", "height")},
        }
        row["image"]["file"] = path.name
        row["capture_origin"] = frame.get("capture_origin", {"kind": "agent-requested-native-frame"})
        row["native_frame_id"] = frame.get("native_frame_id", frame["frame_id"])
        row["frame_id_scope"] = frame.get("frame_id_scope", "native-session-actor-frame")
        key = frame_key(row)
        data = json.dumps(row, allow_nan=False, sort_keys=True).encode()
        if len(data) > MAX_JSON:
            raise ValueError("public frame metadata exceeds bound")
        with (self.root / "frames" / (key + ".json")).open("xb") as handle:
            handle.write(data)
        atomic_json(self.root / "latest.json", row)

    def fail(self, error: Exception) -> None:
        # No exception text: it can include private native response contents.
        self.run.update(status="error", error="public media publication failed",
                        error_type=type(error).__name__)
        atomic_json(self.root / "run.json", self.run)

    def close(self) -> None:
        self.run.update(status="ended" if "error" not in self.run else "error",
                        ended_at=time.time())
        atomic_json(self.root / "run.json", self.run)


class Source:
    """An explicit renderer recording directory; never discovers other observers."""
    def __init__(self, directory: Path, *, live: bool = False, stale_seconds: float = 3):
        self.directory = safe_path(Path(directory))
        self.root = self.directory / "public-view"
        self.live = live
        if not math.isfinite(stale_seconds) or stale_seconds <= 0:
            raise ValueError("stale seconds must be finite and positive")
        self.stale_seconds = stale_seconds
        self.identity = self.run()

    def run(self) -> dict:
        run = read_json(self.root / "run.json")
        if (run.get("schema") != SCHEMA or run.get("status") not in {"running", "ended", "error"}
                or any(not isinstance(run.get(k), str) or not 0 < len(run[k]) <= 256
                       for k in ("run_id", "task_id"))):
            raise ValueError("invalid public renderer run metadata")
        if hasattr(self, "identity") and any(
                run[k] != self.identity[k] for k in ("run_id", "task_id", "started_at")):
            raise ValueError("selected run identity changed")
        return run

    def validate(self, row: dict) -> dict:
        if (row.get("schema") != SCHEMA
                or any(row.get(k) != self.identity[k] for k in ("run_id", "task_id"))
                or not isinstance(row.get("actor"), dict)
                or any(not isinstance(row.get(k), str) or not 0 < len(row[k]) <= 256
                       for k in ("session_id", "frame_id"))):
            raise ValueError("frame provenance differs from selected run")
        image = row["image"]
        name = image["file"]
        if (not isinstance(name, str) or not name.endswith(".png") or "/" in name
                or "\\" in name or name in {".", ".."}
                or image.get("mime_type") != "image/png"
                or not isinstance(image.get("sha256"), str)
                or not KEY.fullmatch(image["sha256"])
                or any(type(image.get(k)) is not int or not 1 <= image[k] <= bound
                       for k, bound in (("width", 7680), ("height", 4320),
                                        ("byte_length", MAX_PNG)))):
            raise ValueError("invalid public PNG descriptor/path")
        passive = row.get("capture_origin", {}).get("kind") == "passive-renderer-x11"
        begin = frame_time(row)
        end = int(row["capture_host"]["received_ns" if passive else "readback_complete_ns"])
        if passive and (row.get("native_frame_id") is not None
                        or row.get("frame_id_scope") != "passive-capture-session-sequence"
                        or not KEY.fullmatch(row["capture_origin"].get("receipt_sha256", ""))):
            raise ValueError("passive sample must not claim native frame/UI identity")
        if (not 0 < begin <= end < 2 ** 64 or row["capture_host"].get("clock")
                != "unix-realtime-nanoseconds-as-decimal-string"):
            raise ValueError("invalid source capture timestamps")
        return row

    def latest(self) -> dict | None:
        try:
            return self.validate(read_json(self.root / "latest.json"))
        except FileNotFoundError:
            return None

    def receipt(self, key: str) -> dict:
        if not KEY.fullmatch(key):
            raise ValueError("unregistered frame identity")
        row = self.validate(read_json(self.root / "frames" / (key + ".json")))
        if frame_key(row) != key:
            raise ValueError("frame receipt identity mismatch")
        return row

    def catalog(self) -> list[dict]:
        rows = [self.receipt(path.stem) for path in (self.root / "frames").iterdir()
                if path.suffix == ".json"]
        return sorted(rows, key=lambda row: (frame_time(row), frame_key(row)))

    def pixels(self, row: dict) -> bytes:
        self.validate(row)
        image = row["image"]
        data = read_bytes(self.directory / image["file"], MAX_PNG)
        if len(data) != image["byte_length"] or hashlib.sha256(data).hexdigest() != image["sha256"]:
            raise ValueError("source PNG byte length/hash mismatch")
        verify_png(data, image["width"], image["height"])
        return data

    def state(self, row: dict | None = None) -> dict:
        run = self.run()
        row = self.latest() if row is None else row
        age = None if row is None else time.time() - frame_time(row) / 1e9
        state = ("error" if run["status"] == "error" else
                 "ended" if run["status"] == "ended" else
                 "empty" if row is None else
                 "stale" if age < -1 or age > self.stale_seconds else "live")
        return {"run": run, "frame": row, "feed_state": state,
                "mode": "live" if self.live else "saved", "age_seconds": age,
                "claim": "received public frames only; not gameplay/model/score proof"}
