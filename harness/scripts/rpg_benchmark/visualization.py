"""Optional read-only browser viewer and media exports for one renderer run.

Extracted/adapted from veloren_observer_dashboard.py (Preview condition/latest
fanout, loopback Host/Origin/token checks, registered downloads and owned media
lifecycle). Does not import that workspace, Camera, recorder or projection.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import stat
import subprocess
import threading
import time
from urllib.parse import urlsplit

from .frame_feed import Source, frame_key, open_read, overlaps_source, safe_path
from . import visualization_media as media


class Preview:
    """One latest validated PNG/condition; slow viewers never queue source frames."""
    def __init__(self, source: Source):
        self.source = source
        self.changed = threading.Condition()
        self.stopping = threading.Event()
        self.sequence = 0
        self.row = self.data = None
        self.error = None
        self.thread = threading.Thread(target=self._run, name="benchmark-preview")

    def poll(self) -> None:
        try:
            row = self.source.latest()
            with self.changed:
                previous = self.row
            if row is not None and row != previous:
                if previous and frame_key(row) == frame_key(previous):
                    raise ValueError("immutable latest frame metadata changed")
                data = self.source.pixels(row)
                with self.changed:
                    self.row, self.data = row, data
                    self.sequence += 1
                    self.error = None
                    self.changed.notify_all()
            # Check run identity even when no new image appears.
            self.source.run()
        except Exception as error:
            with self.changed:
                self.error = f"{type(error).__name__}: {error}"
                self.changed.notify_all()

    def _run(self) -> None:
        while not self.stopping.is_set():
            self.poll()
            self.stopping.wait(0.2)

    def snapshot(self) -> dict:
        with self.changed:
            state = self.source.state(self.row)
            if self.error:
                state.update(feed_state="error", error=self.error)
            state["sequence"] = self.sequence
            state["frame_key"] = frame_key(self.row) if self.row else None
            age = state["age_seconds"]
            state["fresh_for_seconds"] = (max(0, self.source.stale_seconds - age)
                                           if age is not None and age >= 0
                                           and state["feed_state"] == "live" else 0)
            return state

    def wait_frame(self, after: int, timeout: float = 1):
        with self.changed:
            self.changed.wait_for(lambda: self.sequence > after or self.error
                                  or self.stopping.is_set(), timeout)
            if (self.error or self.stopping.is_set() or self.sequence <= after
                    or self.row is None):
                return None
            if self.source.live and self.source.state(self.row)["feed_state"] != "live":
                return None
            return self.sequence, self.row, self.data

    def close(self) -> None:
        self.stopping.set()
        with self.changed:
            self.changed.notify_all()
        if self.thread.ident:
            self.thread.join()


class App:
    def __init__(self, source: Source, output_root: Path):
        self.source = source
        self.output_root = safe_path(output_root)
        if overlaps_source(source, output_root):
            raise ValueError("output root must not overlap the read-only source")
        self.token = secrets.token_urlsafe(32)
        self.preview = Preview(source)
        self.recording = media.Recording(source, output_root)
        self.downloads = {}
        self.lock = threading.Lock()
        self.preview.thread.start()

    def register(self, path: Path, mime: str) -> str:
        path = safe_path(path)
        if not path.is_relative_to(self.output_root):
            raise ValueError("download outside this viewer's export root")
        with self.lock:
            for key, row in self.downloads.items():
                if row["path"] == path:
                    return "/download/" + key
        info = path.stat()
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        with self.lock:
            key = secrets.token_hex(16)
            self.downloads[key] = {"path": path, "mime": mime, "sha256": digest,
                                   "identity": (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)}
        return "/download/" + key

    def download(self, key: str):
        with self.lock:
            entry = self.downloads.get(key)
        if not re.fullmatch(r"[0-9a-f]{32}", key) or entry is None:
            raise FileNotFoundError("unregistered download")
        path = safe_path(entry["path"])
        handle = open_read(path)
        try:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode)
                    or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != entry["identity"]
                    or hashlib.file_digest(handle, "sha256").hexdigest() != entry["sha256"]):
                raise ValueError("registered download identity changed")
            handle.seek(0)
            return handle, entry
        except BaseException:
            handle.close()
            raise

    def snapshot(self) -> dict:
        result = self.preview.snapshot()
        recording = self.recording.snapshot()
        if recording["state"] == "saved":
            recording["download"] = self.register(Path(recording["path"]) / "video.mp4", "video/mp4")
            recording["metadata"] = self.register(Path(recording["path"]) / "metadata.json", "application/json")
        result["recording"] = recording
        return result

    def close(self) -> None:
        self.preview.close()
        self.recording.close()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, app):
        if address[0] != "127.0.0.1":
            raise ValueError("only loopback 127.0.0.1 is supported; use SSH forwarding")
        self.app = app
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(address, Handler)
        port = self.server_address[1]
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "VelorenBenchViewer"

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *args):
        pass

    def allowed(self, post=False):
        hosts = self.headers.get_all("Host", [])
        origins = self.headers.get_all("Origin", [])
        if len(hosts) != 1 or hosts[0] not in self.server.hosts:
            return False
        if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
            return False
        if len(origins) > 1 or origins and origins[0] != "http://" + hosts[0]:
            return False
        if post:
            tokens = self.headers.get_all("X-Observer-Token", [])
            if (origins != ["http://" + hosts[0]] or len(tokens) != 1
                    or not re.fullmatch(r"[A-Za-z0-9_-]{43}", tokens[0])
                    or not hmac.compare_digest(tokens[0], self.server.app.token)):
                return False
        return True

    def headers_for(self, code, mime, length=None):
        self.send_response(code)
        self.send_header("Content-Type", mime)
        if length is not None:
            self.send_header("Content-Length", str(length))
        for name, value in {
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "Cross-Origin-Resource-Policy": "same-origin",
            "Content-Security-Policy": "default-src 'none'; script-src 'self'; "
            "style-src 'unsafe-inline'; img-src 'self'; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
            "Connection": "close",
        }.items():
            self.send_header(name, value)
        self.close_connection = True

    def send_data(self, code, data, mime="application/json"):
        if not isinstance(data, bytes):
            data = json.dumps(data, allow_nan=False).encode()
        self.headers_for(code, mime, len(data))
        self.end_headers()
        self.wfile.write(data)

    def stream(self):
        preview = self.server.app.preview
        frame = preview.wait_frame(0, 0)
        if frame is None:
            self.send_data(503, {"error": "no fresh preview"})
            return
        self.connection.settimeout(1)
        self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
        self.headers_for(200, "multipart/x-mixed-replace; boundary=benchmark-frame")
        self.end_headers()
        while not preview.stopping.is_set():
            sequence, row, pixels = frame
            header = (f"--benchmark-frame\r\nContent-Type: image/png\r\nContent-Length: {len(pixels)}\r\n"
                      f"X-Frame-Sequence: {sequence}\r\n\r\n").encode()
            self.wfile.write(header + pixels + b"\r\n")
            self.wfile.flush()
            frame = preview.wait_frame(sequence)
            while frame is None:
                state = preview.snapshot()
                if (preview.stopping.is_set() or state.get("error")
                        or self.server.app.source.live and state["feed_state"] != "live"):
                    return
                frame = preview.wait_frame(sequence)

    def do_GET(self):
        if not self.allowed():
            self.send_data(403, {"error": "Host/Origin refused"})
            return
        app = self.server.app
        try:
            url = urlsplit(self.path)
            if url.query:
                raise ValueError("query parameters are not supported")
            if url.path in {"/", "/app.js"}:
                name, mime = (("visualization.html", "text/html; charset=utf-8") if url.path == "/"
                              else ("visualization.js", "text/javascript; charset=utf-8"))
                self.send_data(200, files(__package__).joinpath(name).read_bytes(), mime)
            elif url.path == "/api/bootstrap":
                self.send_data(200, {"token": app.token})
            elif url.path == "/api/state":
                self.send_data(200, app.snapshot())
            elif url.path == "/preview.stream":
                self.stream()
            elif url.path == "/preview.png":
                frame = app.preview.wait_frame(0, 0)
                self.send_data(200 if frame else 503, frame[2] if frame else b"no fresh preview",
                               "image/png" if frame else "text/plain")
            elif url.path.startswith("/preview/") and url.path.endswith(".png"):
                key = url.path[len("/preview/"):-len(".png")]
                frame = app.preview.wait_frame(0, 0)
                if frame is None or key != frame_key(frame[1]):
                    self.send_data(409, {"error": "preview advanced/stale; refresh state"})
                else:
                    self.send_data(200, frame[2], "image/png")
            elif url.path.startswith("/api/frames/"):
                offset = url.path.removeprefix("/api/frames/")
                if not re.fullmatch(r"0|[1-9][0-9]{0,8}", offset):
                    raise ValueError("invalid saved frame offset")
                rows = app.source.catalog()
                page = rows[int(offset):int(offset) + 100]
                self.send_data(200, {"total": len(rows), "offset": int(offset),
                                    "frames": [{"key": frame_key(row), "frame": row} for row in page]})
            elif url.path.startswith("/frame/") and url.path.endswith(".png"):
                key = url.path[len("/frame/"):-len(".png")]
                row = app.source.receipt(key)
                self.send_data(200, app.source.pixels(row), "image/png")
            elif url.path.startswith("/download/"):
                handle, entry = app.download(url.path[len("/download/"):])
                with handle:
                    self.headers_for(200, entry["mime"], os.fstat(handle.fileno()).st_size)
                    self.send_header("Content-Disposition", f'attachment; filename="{entry["path"].name}"')
                    self.end_headers()
                    while block := handle.read(128 * 1024):
                        self.wfile.write(block)
            else:
                self.send_data(404, {"error": "not found"})
        except FileNotFoundError:
            self.send_data(404, {"error": "registered media not found"})
        except (ValueError, KeyError, TypeError, OSError, RuntimeError) as error:
            if isinstance(error, (BrokenPipeError, ConnectionResetError, TimeoutError)):
                return
            self.send_data(400, {"error": f"{type(error).__name__}: {error}"})

    def do_POST(self):
        if not self.allowed(post=True):
            self.send_data(403, {"error": "same-origin and session token required"})
            return
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if (len(lengths) != 1 or not lengths[0].isdigit() or not 1 <= int(lengths[0]) <= 4096
                    or self.headers.get("Transfer-Encoding")
                    or self.headers.get("Content-Type") != "application/json"):
                raise ValueError("bounded application/json body required")
            body = json.loads(self.rfile.read(int(lengths[0])))
            if not isinstance(body, dict):
                raise ValueError("object required")
            app = self.server.app
            if self.path == "/api/record/start" and not body:
                result = app.recording.start()
            elif self.path == "/api/record/stop" and not body:
                result = app.recording.stop()
            elif self.path == "/api/screenshot" and set(body) == {"frame"}:
                row = app.source.receipt(body["frame"])
                out = app.output_root / ("shot-" + secrets.token_hex(8))
                result = media.screenshot(app.source, out, row)
                result["download"] = app.register(out / "frame.png", "image/png")
                result["metadata"] = app.register(out / "metadata.json", "application/json")
            else:
                raise ValueError("unknown endpoint/body")
            self.send_data(202, result)
        except (ValueError, KeyError, TypeError, OSError, RuntimeError,
                subprocess.SubprocessError) as error:
            self.send_data(409, {"error": f"{type(error).__name__}: {error}"})


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="action", required=True)
    for action in ("serve", "screenshot", "export", "record"):
        item = sub.add_parser(action)
        item.add_argument("--source", type=Path, required=True, help="Explicit renderer --recordings directory.")
        if action == "serve":
            item.add_argument("--output-root", type=Path, required=True, help="Dedicated exports directory.")
            item.add_argument("--port", type=int, default=0, help="Loopback port; 0 chooses a free port.")
            item.add_argument("--live", action="store_true", help="Require freshness for live preview/recording.")
        else:
            item.add_argument("--out", type=Path, required=True, help="NEW export directory; never overwritten.")
        if action == "screenshot":
            item.add_argument("--frame", help="Public frame receipt key (default: latest saved frame).")
        if action == "export":
            item.add_argument("--fps", type=int, default=30)
        if action in {"serve", "record"}:
            item.add_argument("--stale-seconds", type=float, default=3)
        if action in {"serve", "record", "screenshot"}:
            item.add_argument("--passive", action="store_true",
                              help="Optional Linux X11 capture of the registered same-run renderer; no frame requests.")
            item.add_argument("--capture-out", type=Path,
                              help="NEW passive PNG archive directory; required with --passive.")
            item.add_argument("--capture-fps", type=int, default=10)
        if action == "record":
            item.add_argument("--seconds", type=float, required=True, help="Recorder window only, not episode control.")
    return result


def serve(args, source):
    if not 0 <= args.port <= 65535:
        raise ValueError("port must be 0..65535")
    app = App(source, args.output_root)
    server = None
    stopping = threading.Event()
    previous = {}
    try:
        server = Server(("127.0.0.1", args.port), app)
        server.timeout = 0.2
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda *_: stopping.set())
        print(json.dumps({"url": f"http://127.0.0.1:{server.server_address[1]}",
                          "source": str(source.directory), "read_only": True}), flush=True)
        while not stopping.is_set():
            server.handle_request()
    finally:
        if server is not None:
            server.server_close()
        app.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main(argv=None):
    command = parser()
    args = command.parse_args(argv)
    source = None
    previous = {}
    if threading.current_thread() is threading.main_thread():
        def interrupted(*_):
            raise KeyboardInterrupt
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, interrupted)
    try:
        if getattr(args, "passive", False):
            if args.capture_out is None:
                raise ValueError("--passive requires a NEW --capture-out archive directory")
            from .passive_capture import PassiveSource
            source = PassiveSource(args.source, args.capture_out, fps=args.capture_fps,
                                   stale_seconds=getattr(args, "stale_seconds", 3))
            source.wait_ready()
        else:
            if getattr(args, "capture_out", None) is not None:
                raise ValueError("--capture-out requires explicit --passive selection")
            source = Source(args.source, live=getattr(args, "live", False) or args.action == "record",
                            stale_seconds=getattr(args, "stale_seconds", 3))
        if args.action == "serve":
            serve(args, source)
            return
        if args.action == "screenshot":
            row = source.receipt(args.frame) if args.frame else None
            result = media.screenshot(source, args.out, row)
        elif args.action == "export":
            result = media.movie(source, args.out, fps=args.fps)
        else:
            if not math.isfinite(args.seconds) or args.seconds <= 0:
                raise ValueError("recording seconds must be finite and positive")
            recorder = media.Recording(source, args.out.parent)
            recorder.start(args.out)
            try:
                time.sleep(args.seconds)
                recorder.stop()
                recorder.thread.join()
                result = recorder.snapshot()
                if result["state"] != "saved":
                    raise RuntimeError(result.get("error", "recording failed"))
            finally:
                recorder.close()
        print(json.dumps(result, indent=2, allow_nan=False))
    except (ValueError, OSError, KeyError, TypeError, RuntimeError,
            subprocess.SubprocessError) as error:
        command.exit(2, f"visualize: {type(error).__name__}: {error}\n")
    except KeyboardInterrupt:
        command.exit(130, "visualize: interrupted; owned encoder retired; no success claim\n")
    finally:
        if source is not None and hasattr(source, "close"):
            source.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
