"""Optional Linux X11 pixels from one registered ordinary renderer, not a camera.

Adapts the original observer dashboard's Camera identity checks, Preview pipe
pump and owned-child cleanup. Unlike its camera-only receipts, the normal-run
hook registers the existing same-player Xvfb/renderer; no processes are started
here except this source's ffmpeg encoder (and bounded read-only xdpyinfo checks).
No native/control socket or model interface exists in this module.
"""
from __future__ import annotations

from collections import deque
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import stat
import struct
import subprocess
import threading
import time
from uuid import uuid4

from .frame_feed import (MAX_JSON, MAX_PNG, Publisher, Source, atomic_json,
                         read_bytes, safe_path)
from .observations import verify_png
from .visualization_media import reserve

SCHEMA = "velorenbench-passive-renderer/1"


class Ended(RuntimeError):
    pass


def process_identity(pid: int) -> dict:
    if type(pid) is not int or pid <= 1:
        raise ValueError("invalid registered process PID")
    root = Path("/proc") / str(pid)
    text = (root / "stat").read_text()
    fields = text[text.rfind(")") + 2:].split()
    if fields[0] in {"Z", "X", "T", "t"}:
        raise RuntimeError("registered source process is stopped/zombie/dead")
    if root.stat().st_uid != os.getuid():
        raise ValueError("registered source must belong to the current user")
    argv = (root / "cmdline").read_bytes()
    return {"pid": pid, "start_ticks": int(fields[19]), "uid": root.stat().st_uid,
            "argv_sha256": hashlib.sha256(argv).hexdigest(),
            "executable": os.readlink(root / "exe")}


def display_identity(display: str, xvfb_pid: int) -> dict:
    if not re.fullmatch(r":[0-9]{1,5}", display):
        raise ValueError("requires an explicit local X11 display, no host/screen/offset")
    socket_path = "/tmp/.X11-unix/X" + display[1:]
    info = Path(socket_path).lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("registered X11 socket is not an owned Unix socket")
    owned = set()
    for fd in (Path("/proc") / str(xvfb_pid) / "fd").iterdir():
        try:
            target = os.readlink(fd)
        except FileNotFoundError:
            continue
        match = re.fullmatch(r"socket:\[([0-9]+)\]", target)
        if match:
            owned.add(match[1])
    # -displayfd Xvfb does not necessarily create .Xn-lock. Verify the actual
    # listening socket inode belongs to the registered Xvfb, not only its name.
    listeners = []
    for line in Path("/proc/net/unix").read_text().splitlines()[1:]:
        fields = line.split()
        if (len(fields) == 8 and fields[7] in {socket_path, "@" + socket_path}
                and fields[6] in owned and fields[3] == "00010000"):
            listeners.append({"path": fields[7], "socket_inode": fields[6]})
    if not listeners:
        raise ValueError("registered Xvfb does not own the selected display listener")
    return {"device": info.st_dev, "inode": info.st_ino,
            "listeners": sorted(listeners, key=lambda row: row["path"])}


def geometry(display: str) -> tuple[int, int]:
    tool = shutil.which("xdpyinfo")
    if not tool:
        raise RuntimeError("passive X11 capture requires xdpyinfo for geometry verification")
    result = subprocess.run([tool, "-display", display], capture_output=True, text=True,
                            timeout=5, check=True)
    match = re.search(r"dimensions:\s+([0-9]+)x([0-9]+) pixels", result.stdout)
    if not match:
        raise ValueError("selected display geometry unavailable")
    width, height = map(int, match.groups())
    if not 1 <= width <= 7680 or not 1 <= height <= 4320:
        raise ValueError("selected display geometry exceeds capture bounds")
    return width, height


def renderer_display(pid: int) -> str:
    # Inspect only DISPLAY; never retain/log the process environment/credentials.
    values = [part[len(b"DISPLAY="):] for part in
              (Path("/proc") / str(pid) / "environ").read_bytes().split(b"\0")
              if part.startswith(b"DISPLAY=")]
    if len(values) != 1:
        raise ValueError("registered renderer has no unique DISPLAY environment")
    return values[0].decode("ascii")


def register(publisher: Publisher, *, display: str, xvfb_pid: int, xvfb_start: int,
             renderer_pid: int, renderer_start: int, session_id: str, actor: dict) -> dict:
    """Normal-run hook after native_renderer_ready has verified same-client identity."""
    if os.name != "posix" or not Path("/proc/sys/kernel/random/boot_id").is_file():
        raise RuntimeError("passive capture registration requires Linux procfs/X11")
    xvfb, renderer = process_identity(xvfb_pid), process_identity(renderer_pid)
    if xvfb["start_ticks"] != xvfb_start or renderer["start_ticks"] != renderer_start:
        raise ValueError("registered process start identity changed")
    if renderer_display(renderer_pid) != display:
        raise ValueError("renderer DISPLAY differs from registered Xvfb")
    socket_identity = display_identity(display, xvfb_pid)
    width, height = geometry(display)
    receipt = {
        "schema": SCHEMA, "run_id": publisher.run["run_id"], "task_id": publisher.run["task_id"],
        "source_run_started_at": publisher.run["started_at"], "session_id": session_id,
        "actor": {key: actor[key] for key in ("uid", "lifecycle")},
        "display": display, "width": width, "height": height,
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "xvfb": xvfb, "renderer": renderer, "display_socket": socket_identity,
        "origin": "passive-renderer-x11",
        "attribution": "native_renderer_ready same ordinary actor; not a native frame/UI receipt",
        "lifetime": "only while source run is running and all registered identities match",
    }
    if process_identity(xvfb_pid) != xvfb or process_identity(renderer_pid) != renderer:
        raise ValueError("source process changed during registration")
    data = json.dumps(receipt, sort_keys=True, allow_nan=False).encode()
    if len(data) > MAX_JSON:
        raise ValueError("capture registration exceeds bound")
    with (publisher.root / "capture.json").open("xb") as handle:
        handle.write(data)
    return receipt


class Attachment:
    """Pin one receipt; changes are terminal, never silently attach elsewhere."""
    def __init__(self, directory: Path):
        self.source = Source(directory)
        self.path = self.source.root / "capture.json"
        self.raw = read_bytes(self.path, MAX_JSON)
        self.receipt = json.loads(self.raw)
        self.sha256 = hashlib.sha256(self.raw).hexdigest()
        row = self.receipt
        if (row.get("schema") != SCHEMA or row.get("origin") != "passive-renderer-x11"
                or any(row.get(key) != self.source.identity[key] for key in ("run_id", "task_id"))
                or row.get("source_run_started_at") != self.source.identity["started_at"]
                or not isinstance(row.get("session_id"), str) or not row["session_id"]
                or not isinstance(row.get("actor"), dict)):
            raise ValueError("capture registration differs from selected public run")
        self.lock = threading.RLock()
        self.geometry_at = 0
        self.check()

    def check(self) -> None:
        with self.lock:
            row = self.receipt
            run = self.source.run()
            if read_bytes(self.path, MAX_JSON) != self.raw:
                raise ValueError("registered capture receipt identity changed")
            if run["status"] == "ended":
                raise Ended("registered renderer run ended")
            if run["status"] != "running":
                raise RuntimeError("registered renderer run is in error")
            if Path("/proc/sys/kernel/random/boot_id").read_text().strip() != row["boot_id"]:
                raise ValueError("registered capture belongs to a different system boot")
            for role in ("xvfb", "renderer"):
                if process_identity(row[role]["pid"]) != row[role]:
                    raise ValueError(f"registered {role} process identity changed")
            if renderer_display(row["renderer"]["pid"]) != row["display"]:
                raise ValueError("registered renderer display identity changed")
            if display_identity(row["display"], row["xvfb"]["pid"]) != row["display_socket"]:
                raise ValueError("registered display socket identity changed")
            if time.monotonic() - self.geometry_at >= 1:
                if geometry(row["display"]) != (row["width"], row["height"]):
                    raise ValueError("registered display geometry changed")
                self.geometry_at = time.monotonic()


def pop_png(buffer: bytes):
    """Chunk-aware framing; IEND byte patterns inside compressed pixels are not delimiters."""
    if len(buffer) < 8:
        return None, buffer
    if not buffer.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("passive encoder returned non-PNG bytes")
    offset = 8
    while len(buffer) >= offset + 12:
        size = struct.unpack_from(">I", buffer, offset)[0]
        end = offset + size + 12
        if end > MAX_PNG:
            raise ValueError("passive PNG exceeds bound")
        if end > len(buffer):
            break
        if buffer[offset + 4:offset + 8] == b"IEND":
            return buffer[:end], buffer[end:]
        offset = end
    return None, buffer


def stop_encoder(process, *, timeout=3):
    """Reap our Popen child while draining BOTH pipes, without retaining pixels.

    ffmpeg can finish a pending PNG write before handling SIGTERM. Waiting while
    no longer reading that pipe deadlocks a large frame. Closing read ends first
    instead causes a broken pipe and loses useful terminal diagnostics.
    Called only by the capture worker, after its read loop has stopped: no
    competing readers or cross-thread pipe closes. The unreaped child cannot
    have its PID reused; never signal registered X11 or renderer processes.
    Forced termination remains explicit evidence.
    """
    streams = {process.stdout: "stdout", process.stderr: "stderr"}
    eof = {"stdout": False, "stderr": False}
    tail = b""
    kill_attempted = False
    deadline = time.monotonic() + timeout
    try:
        if process.poll() is None:
            process.terminate()
        while streams:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if not kill_attempted and process.poll() is None:
                    process.kill()
                    kill_attempted = True
                    deadline = time.monotonic() + timeout
                    continue
                break
            ready, _, _ = select.select(list(streams), [], [], min(0.2, remaining))
            for stream in ready:
                block = os.read(stream.fileno(), 65536)
                if not block:
                    eof[streams.pop(stream)] = True
                elif stream is process.stderr:
                    tail = (tail + block)[-65536:]
        # EOF is not process exit: allow a child which closed its pipes to
        # complete its own cleanup within the same graceful-stop deadline.
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            if process.poll() is None:
                process.kill()
                kill_attempted = True
            process.wait(timeout=timeout)
    finally:
        # A select/read failure must not strand our child. Preserve that
        # exception after bounded fallback reaping and closing both read ends.
        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=timeout)
        finally:
            try:
                process.stdout.close()
            finally:
                process.stderr.close()
    return {
        "pid": process.pid, "exit_code": process.returncode,
        "stopped": process.poll() is not None, "kill_attempted": kill_attempted,
        # Popen.kill re-polls internally: a terminal exit may win the race
        # between our poll and kill. An attempt alone is not a SIGKILL exit.
        "forced_termination": kill_attempted and process.returncode == -signal.SIGKILL,
        "stdout_eof": eof["stdout"], "stderr_eof": eof["stderr"],
        "pipes_closed": process.stdout.closed and process.stderr.closed,
    }, tail.decode(errors="replace")


class PassiveSource(Source):
    """Existing viewer Source seam, backed by an explicit new passive PNG archive.

    Archive disk grows only when explicitly requested. RAM holds one latest PNG
    plus bounded encoder pipes/timestamps, never a per-viewer capture queue.
    """
    def __init__(self, directory: Path, out: Path, *, fps: int = 10, stale_seconds: float = 3):
        if type(fps) is not int or not 1 <= fps <= 30:
            raise ValueError("passive capture fps must be 1..30")
        if not shutil.which("ffmpeg"):
            raise RuntimeError("passive capture requires ffmpeg with X11 input and PNG encoder")
        self.attachment = Attachment(directory)
        self.archive = reserve(self.attachment.source, out)
        registered = self.attachment.receipt
        self.publisher = Publisher(self.archive, registered["run_id"], registered["task_id"],
                                   self.attachment.source.identity.get("terrain", "unknown"))
        self.publisher.run.update(
            provenance="passive X11 samples of explicitly registered same-run ordinary renderer",
            frame_policy="independent of agent/native frame requests; native_frame_id is null",
            capture_source=registered, source_receipt_sha256=self.attachment.sha256)
        atomic_json(self.publisher.root / "run.json", self.publisher.run)
        super().__init__(self.archive, live=True, stale_seconds=stale_seconds)
        self.protected_directories = [self.directory, self.attachment.source.directory]
        self.fps = fps
        self.capture_session = uuid4().hex
        self.stopping = threading.Event()
        self.ready = threading.Event()
        self.closed = False
        self.error = None
        self.process = None
        self.thread = threading.Thread(target=self._capture, name="benchmark-passive-x11")
        try:
            self.thread.start()
        except BaseException:
            if self.thread.ident:
                self.close()
            else:
                self.publisher.close()
            raise

    def guard(self) -> None:
        if self.error:
            raise RuntimeError(self.error)
        if self.closed or self.stopping.is_set():
            raise Ended("passive capture source closed")
        self.attachment.check()

    def state(self, row=None) -> dict:
        state = super().state(row)
        state["capture_mode"] = "passive-renderer-x11"
        try:
            self.guard()
        except Ended as error:
            state.update(feed_state="ended", error=str(error))
        except Exception as error:
            state.update(feed_state="error", error=f"{type(error).__name__}: {error}")
        return state

    def pixels(self, row: dict) -> bytes:
        self.guard()
        return super().pixels(row)

    def wait_ready(self, timeout=15) -> None:
        if not self.ready.wait(timeout):
            self.close()
            raise TimeoutError("passive capture produced no PNG within readiness timeout")
        self.guard()
        if self.latest() is None:
            raise RuntimeError("passive capture ended without a frame")

    def _publish(self, data: bytes, sequence: int, pts_us: int) -> None:
        self.guard()
        row = self.attachment.receipt
        received = time.time_ns()
        if pts_us <= 0 or pts_us * 1000 > received:
            raise ValueError("passive encoder wallclock PTS is missing/future")
        verify_png(data, row["width"], row["height"])
        path = self.directory / f"capture-{sequence:09}.png"
        with path.open("xb") as handle:
            handle.write(data)
        self.publisher.publish({
            "frame_id": f"passive-{self.capture_session}-{sequence}",
            "frame_id_scope": "passive-capture-session-sequence", "native_frame_id": None,
            "session_id": row["session_id"], "actor": row["actor"],
            "capture_host": {"clock": "unix-realtime-nanoseconds-as-decimal-string",
                             "sample_pts_ns": str(pts_us * 1000), "received_ns": str(received),
                             "timestamp_source": "ffmpeg-x11grab-wallclock-pts-not-native-readback"},
            "capture_origin": {"kind": "passive-renderer-x11", "capture_session_id": self.capture_session,
                               "sequence": sequence, "receipt_sha256": self.attachment.sha256,
                               "display": row["display"], "native_frame_or_ui_fence": False},
            "image": {"path": str(path), "mime_type": "image/png", "width": row["width"],
                      "height": row["height"], "byte_length": len(data),
                      "sha256": hashlib.sha256(data).hexdigest()}})
        if 0 <= received - pts_us * 1000 <= self.stale_seconds * 1e9:
            self.ready.set()

    def _fail(self, error: Exception) -> None:
        message = f"{type(error).__name__}: {error}"
        self.error = f"{self.error}; {message}" if self.error else message
        self.publisher.run.update(status="error", error=self.error)

    def _capture(self) -> None:
        pending, stderr_pending, times = b"", b"", deque()
        lines = deque(maxlen=40)
        count = 0
        row = self.attachment.receipt
        command = [shutil.which("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "info",
                   "-copyts", "-f", "x11grab", "-probesize", "32", "-analyzeduration", "0",
                   "-use_wallclock_as_timestamps", "1",
                   "-framerate", str(self.fps), "-draw_mouse", "0",
                   "-video_size", f'{row["width"]}x{row["height"]}',
                   "-i", row["display"], "-vf", "settb=1/1000000,showinfo",
                   "-an", "-c:v", "png", "-pix_fmt", "rgb24", "-threads", "1", "-compression_level", "3",
                   "-fps_mode", "passthrough", "-f", "image2pipe", "pipe:1"]
        try:
            self.publisher.run["capture_encoder_argv"] = command
            atomic_json(self.publisher.root / "run.json", self.publisher.run)
            self.guard()
            self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.publisher.run["capture_encoder_identity"] = process_identity(self.process.pid)
            atomic_json(self.publisher.root / "run.json", self.publisher.run)
            while not self.stopping.is_set():
                self.guard()
                if self.process.poll() is not None:
                    raise RuntimeError(f"passive capture encoder exited {self.process.returncode}")
                streams, _, _ = select.select([self.process.stdout, self.process.stderr], [], [], 0.2)
                for stream in streams:
                    block = os.read(stream.fileno(), 65536)
                    if not block:
                        raise RuntimeError("passive capture encoder pipe ended")
                    if stream is self.process.stderr:
                        stderr_pending += block
                        while b"\n" in stderr_pending:
                            line, stderr_pending = stderr_pending.split(b"\n", 1)
                            lines.append(line.decode(errors="replace")[:2048])
                            match = re.search(rb"Parsed_showinfo.*\bn:\s*(\d+)\s+pts:\s*(-?\d+)", line)
                            if match:
                                times.append(tuple(map(int, match.groups())))
                    else:
                        pending += block
                if len(times) > 64 or len(stderr_pending) > 65536 or len(pending) > MAX_PNG + 65536:
                    raise RuntimeError("passive encoder pipe/timestamp bound exceeded")
                while times:
                    data, rest = pop_png(pending)
                    if data is None:
                        break
                    index, pts = times.popleft()
                    if index != count:
                        raise ValueError("passive encoder image/PTS sequence mismatch")
                    self._publish(data, count, pts)
                    pending, count = rest, count + 1
        except Ended:
            pass
        except Exception as error:
            self._fail(error)
        finally:
            if self.process is not None:
                try:
                    cleanup, tail = stop_encoder(self.process)
                except Exception as error:
                    self.publisher.run["capture_encoder_cleanup"] = {
                        "pid": self.process.pid, "exit_code": self.process.returncode,
                        "stopped": self.process.poll() is not None,
                        "pipes_closed": self.process.stdout.closed and self.process.stderr.closed,
                        "error": f"{type(error).__name__}: {error}"}
                    self._fail(error)
                else:
                    self.publisher.run["capture_encoder_cleanup"] = cleanup
                    lines.extend(line[:2048] for line in tail.splitlines())
                    if cleanup["forced_termination"]:
                        self._fail(RuntimeError("passive capture encoder required forced termination"))
                    if not cleanup["stdout_eof"] or not cleanup["stderr_eof"]:
                        self._fail(RuntimeError("passive capture encoder pipes did not reach EOF"))
            try:
                (self.archive / "capture.log").write_text("\n".join(lines) + "\n")
            except Exception as error:
                self._fail(error)
            try:
                self.publisher.close()
            except Exception as error:
                self._fail(error)
            finally:
                self.ready.set()

    def close(self) -> None:
        self.stopping.set()
        self.thread.join()
        self.closed = True
