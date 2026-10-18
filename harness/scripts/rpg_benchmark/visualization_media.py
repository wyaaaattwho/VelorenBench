"""PNG and genuine MP4 export; owns only its encoder child, never a renderer.

Recording start/stop/probe lifecycle is adapted from the prior observer
dashboard; capture lives in the optional passive source, not an X11 game controller.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time

from .frame_feed import Source, frame_key, frame_time, overlaps_source, safe_path


def preflight() -> dict:
    tools = {name: shutil.which(name) for name in ("ffmpeg", "ffprobe")}
    if not all(tools.values()):
        raise RuntimeError("MP4 requires ffmpeg and ffprobe on PATH; no format substitution")
    result = subprocess.run([tools["ffmpeg"], "-hide_banner", "-encoders"],
                            capture_output=True, text=True, timeout=15, check=True)
    if not any(line.split()[1:2] == ["libx264"] for line in result.stdout.splitlines()):
        raise RuntimeError("MP4 requires the ffmpeg libx264 encoder")
    return tools


def reserve(source: Source, out: Path) -> Path:
    out = safe_path(Path(out))
    if overlaps_source(source, out):
        raise ValueError("export directory must not overlap the read-only source")
    out.parent.mkdir(parents=True, exist_ok=True)
    safe_path(out)
    out.mkdir(mode=0o700, exist_ok=False)
    return out


def metadata(out: Path, value: dict) -> None:
    # Export directory is exclusively reserved by this operation.
    (out / "metadata.json").write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def screenshot(source: Source, out: Path, row: dict | None = None) -> dict:
    row = source.latest() if row is None else row
    if row is None:
        raise ValueError("empty source: no screenshot")
    pixels = source.pixels(row)
    out = reserve(source, out)
    record = {"schema": "velorenbench-media/1", "kind": "screenshot", "status": "writing",
              "source": source.state(row), "frames": [row], "transformation": "identity PNG bytes"}
    try:
        with (out / "frame.png").open("xb") as handle:
            handle.write(pixels)
        record.update(status="saved", file="frame.png",
                      sha256=hashlib.sha256(pixels).hexdigest())
        metadata(out, record)
        return record
    except BaseException as error:
        record.update(status="error", error=f"{type(error).__name__}: {error}")
        metadata(out, record)
        raise


def owned_command(command: list[str], log: Path, cancel: threading.Event,
                  *, timeout: float = 300) -> None:
    """Popen ownership is sufficient: never signal a PID read from source metadata."""
    with log.open("xb") as stderr:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=stderr)
        try:
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if cancel.wait(0.05):
                    raise RuntimeError("media export cancelled; encoder child retired")
                if time.monotonic() >= deadline:
                    raise TimeoutError("encoder timeout")
            if process.returncode:
                raise RuntimeError(f"encoder failed with exit {process.returncode}; see encoder.log")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)


def movie(source: Source, out: Path, rows: list[dict] | None = None, *, fps: int = 30,
          cancel: threading.Event | None = None, reserved: bool = False,
          recording: dict | None = None) -> dict:
    if type(fps) is not int or not 1 <= fps <= 120:
        raise ValueError("MP4 fps must be 1..120")
    tools = preflight()
    rows = source.catalog() if rows is None else rows
    if not rows:
        raise ValueError("empty source: no MP4")
    out = out if reserved else reserve(source, out)
    cancel = cancel or threading.Event()
    record = {"schema": "velorenbench-media/1", "kind": "mp4", "status": "encoding",
              "source": source.state(rows[-1]), "frames": rows, "tools": tools,
              "recording": recording, "fps": fps,
              "timing": "source timestamp intervals (native begin_ns or explicitly marked passive "
                        "X11 sample_pts_ns), millisecond concat timebase; "
                        "CFR resampling holds prior PNG, no interpolated/fresh frames; "
                        "final source PNG held for 1/fps second",
              "transformation": "libx264, yuv420p (lossy), even-size black padding; no overlays"}
    try:
        begins = [frame_time(row) for row in rows]
        identity = [(row["run_id"], row["session_id"], row["actor"],
                     row["image"]["width"], row["image"]["height"],
                     row.get("capture_origin", {}).get("kind", "agent-requested-native-frame"),
                     row.get("capture_origin", {}).get("capture_session_id"),
                     row.get("capture_origin", {}).get("receipt_sha256")) for row in rows]
        if any(item != identity[0] for item in identity):
            raise ValueError("movie cannot mix run/session/actor identities or PNG dimensions")
        intervals = [(b - a) / 1e9 for a, b in zip(begins, begins[1:])]
        if any(not math.isfinite(item) or item <= 0 for item in intervals):
            raise ValueError("source frame times must be strictly increasing")
        durations = intervals + [1 / fps]
        record["source_duration_seconds"] = sum(durations)
        # Disk spool, one decoded/validated PNG at a time. No viewer/agent frame queue.
        with tempfile.TemporaryDirectory(prefix=".encode-", dir=out) as temporary:
            temporary = Path(temporary)
            lines = ["ffconcat version 1.0"]
            for index, (row, duration) in enumerate(zip(rows, durations)):
                if cancel.is_set():
                    raise RuntimeError("media export cancelled")
                name = f"{index:09}.png"
                (temporary / name).write_bytes(source.pixels(row))
                lines += [f"file '{name}'", "option framerate 1000",
                          f"duration {duration:.9f}"]
            lines += [f"file '{len(rows) - 1:09}.png'", "option framerate 1000"]
            concat = temporary / "frames.ffconcat"
            concat.write_text("\n".join(lines) + "\n")
            partial = out / "video.partial.mp4"
            command = [tools["ffmpeg"], "-nostdin", "-hide_banner", "-loglevel", "error",
                       "-n", "-f", "concat", "-safe", "0", "-i", str(concat),
                       "-an", "-vf", f"fps={fps},pad=ceil(iw/2)*2:ceil(ih/2)*2",
                       "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p",
                       "-t", f"{sum(durations):.9f}", "-movflags", "+faststart",
                       "-f", "mp4", str(partial)]
            record["encoder_argv"] = command
            owned_command(command, out / "encoder.log", cancel)
            probe = subprocess.run(
                [tools["ffprobe"], "-v", "error", "-count_frames", "-select_streams", "v:0",
                 "-show_entries", "format=format_name,duration:stream=codec_name,nb_read_frames,width,height",
                 "-of", "json", str(partial)], capture_output=True, text=True, timeout=30, check=True)
            checked = json.loads(probe.stdout)
            stream = checked["streams"][0]
            if ("mp4" not in checked["format"]["format_name"].split(",")
                    or stream["codec_name"] != "h264" or int(stream["nb_read_frames"]) < 1
                    or float(checked["format"]["duration"]) <= 0):
                raise RuntimeError("encoded output failed MP4/frame probe")
            # A full decode, not only a container label, is required for success.
            owned_command([tools["ffmpeg"], "-nostdin", "-v", "error", "-xerror",
                           "-i", str(partial), "-f", "null", "-"],
                          out / "decode.log", cancel)
            final = out / "video.mp4"
            # Hard-link publication refuses collisions, unlike replace/rename.
            os.link(partial, final)
            partial.unlink()
        with final.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        record.update(status="saved", file="video.mp4", probe=checked, sha256=digest)
        metadata(out, record)
        return record
    except BaseException as error:
        record.update(status="error", error=f"{type(error).__name__}: {error}")
        metadata(out, record)
        raise


class Recording:
    """One start/stop worker, using persisted public receipts rather than a queue.

    Start includes the currently displayed source frame. Stop exports all source
    receipts through the then-current frame. No wall-time padding is fabricated.
    A stale/ended feed at either boundary is a failure, not a successful live clip.
    """
    def __init__(self, source: Source, output_root: Path):
        self.source, self.output_root = source, output_root
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.thread = None
        self.state = {"state": "idle"}
        self.out = None
        self.start_frame = None
        self.closed = False

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state)

    def require_live(self) -> dict:
        state = self.source.state()
        if not self.source.live or state["feed_state"] != "live" or state["frame"] is None:
            raise RuntimeError("live recording requires a fresh running public renderer feed; "
                               "use export for saved/stale/ended frames")
        return state["frame"]

    def start(self, out: Path | None = None) -> dict:
        with self.lock:
            if self.closed or self.state["state"] in {"recording", "encoding"}:
                raise RuntimeError("recording already active or recorder closed")
            tools = preflight()
            self.start_frame = self.require_live()
            self.out = reserve(self.source, out if out is not None else
                               self.output_root / ("clip-" + str(time.time_ns())))
            self.state = {"state": "recording", "path": str(self.out),
                          "start_frame": frame_key(self.start_frame), "started_at": time.time(),
                          "source_frame": self.start_frame, "tools": tools}
            metadata(self.out, self.state)
            return self.snapshot()

    def stop(self) -> dict:
        with self.lock:
            if self.state["state"] != "recording":
                raise RuntimeError("no active recording")
            try:
                last = self.require_live()
                begin = frame_time(self.start_frame)
                end = frame_time(last)
                rows = [row for row in self.source.catalog()
                        if begin <= frame_time(row) <= end]
                if (not rows or frame_key(rows[0]) != frame_key(self.start_frame)
                        or frame_key(rows[-1]) != frame_key(last)):
                    raise ValueError("recording source window changed")
            except Exception as error:
                self.state.update(state="error", error=str(error))
                metadata(self.out, self.state)
                raise
            self.state.update(state="encoding", stopped_at=time.time())
            self.thread = threading.Thread(target=self._encode, args=(rows,), name="benchmark-mp4")
            self.thread.start()
            return self.snapshot()

    def _encode(self, rows: list[dict]) -> None:
        try:
            result = movie(self.source, self.out, rows, cancel=self.cancel, reserved=True,
                           recording=self.snapshot())
            with self.lock:
                self.state.update(state="saved", file=result["file"])
        except Exception as error:
            with self.lock:
                self.state.update(state="error", error=str(error))
            try:
                failure = json.loads((self.out / "metadata.json").read_text())
                failure.update(status="error", error=str(error), recording=self.snapshot())
                metadata(self.out, failure)
            except (OSError, ValueError) as metadata_error:
                with self.lock:
                    self.state["error"] += f"; error metadata write failed: {metadata_error}"

    def close(self) -> None:
        with self.lock:
            self.closed = True
            self.cancel.set()
            if self.state["state"] == "recording":
                self.state.update(state="error", error="viewer closed before recording stop")
                metadata(self.out, self.state)
        if self.thread:
            self.thread.join()
