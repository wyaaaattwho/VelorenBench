"""Owned tiny X11 engineering display only. NO game/model/native frame requests."""
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
from uuid import uuid4

import pytest

from scripts.rpg_benchmark import frame_feed as feed
from scripts.rpg_benchmark import passive_capture as capture
from scripts.rpg_benchmark import visualization as viewer
from scripts.rpg_benchmark import visualization_media as media
from .test_visualization import png, request, wait_for


# An X11 client painting its own window, not Voxygen or an agent/controller.
PAINTER = r"""
import ctypes as c, os, sys, random
x = c.CDLL("libX11.so.6")
x.XOpenDisplay.argtypes=[c.c_char_p]; x.XOpenDisplay.restype=c.c_void_p
x.XDefaultRootWindow.argtypes=[c.c_void_p]; x.XDefaultRootWindow.restype=c.c_ulong
x.XCreateSimpleWindow.argtypes=[c.c_void_p,c.c_ulong,c.c_int,c.c_int,c.c_uint,c.c_uint,c.c_uint,c.c_ulong,c.c_ulong]
x.XCreateSimpleWindow.restype=c.c_ulong
for name in ("XMapWindow","XClearWindow"):
    getattr(x,name).argtypes=[c.c_void_p,c.c_ulong]
x.XSetWindowBackground.argtypes=[c.c_void_p,c.c_ulong,c.c_ulong]
x.XSync.argtypes=[c.c_void_p,c.c_int]
x.XCloseDisplay.argtypes=[c.c_void_p]
d=x.XOpenDisplay(None)
if not d: raise RuntimeError("owned fixture cannot open display")
width=int(os.environ.get("FIXTURE_WIDTH","96")); height=int(os.environ.get("FIXTURE_HEIGHT","64"))
w=x.XCreateSimpleWindow(d,x.XDefaultRootWindow(d),0,0,width,height,0,0,0xcc2233)
x.XMapWindow(d,w); x.XSync(d,0)
if os.environ.get("FIXTURE_PATTERN"):
    x.XCreateGC.argtypes=[c.c_void_p,c.c_ulong,c.c_ulong,c.c_void_p]; x.XCreateGC.restype=c.c_void_p
    x.XSetForeground.argtypes=[c.c_void_p,c.c_void_p,c.c_ulong]
    x.XFillRectangle.argtypes=[c.c_void_p,c.c_ulong,c.c_void_p,c.c_int,c.c_int,c.c_uint,c.c_uint]
    gc=x.XCreateGC(d,w,0,None); rng=random.Random(42)
    for y in range(0,height,8):
        for xx in range(0,width,8):
            x.XSetForeground(d,gc,rng.randrange(0x1000000))
            x.XFillRectangle(d,w,gc,xx,y,8,8)
    x.XSync(d,0)
print("fixture-ready",flush=True)
try:
    for line in sys.stdin:
        x.XSetWindowBackground(d,w,int(line.strip(),16)); x.XClearWindow(d,w); x.XSync(d,0)
        print("painted",flush=True)
finally:
    x.XCloseDisplay(d)
"""


def line_ready(stream):
    assert select.select([stream], [], [], 10)[0], "owned display/client readiness timeout"
    return stream.readline().strip()


def output_root(tmp_path):
    root = Path(os.environ.get("VELORENBENCH_MEDIA_EVIDENCE", str(tmp_path / "exports")))
    return root / ("owned-passive-x11-" + uuid4().hex[:10])


@pytest.fixture
def registered_display(tmp_path, request):
    for name in ("Xvfb", "xdpyinfo", "ffmpeg"):
        if not shutil.which(name):
            pytest.skip("optional passive Linux display tool unavailable: " + name)
    directory = tmp_path / "structured-run-recordings"
    directory.mkdir()
    publisher = feed.Publisher(directory, "OWNED-X11-engineering", "structured-no-frame-requests", "fixture")
    read_fd, write_fd = os.pipe()
    xvfb = renderer = None
    width, height, pattern = getattr(request, "param", (96, 64, False))
    try:
        xvfb = subprocess.Popen(
            ["Xvfb", "-displayfd", str(write_fd), "-screen", "0", f"{width}x{height}x24", "-nolisten", "tcp"],
            pass_fds=(write_fd,), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        os.close(write_fd)
        write_fd = None
        assert select.select([read_fd], [], [], 10)[0]
        display = ":" + os.read(read_fd, 64).decode().strip()
        renderer = subprocess.Popen([sys.executable, "-u", "-c", PAINTER],
                                    env={**os.environ, "DISPLAY": display,
                                         "FIXTURE_WIDTH": str(width), "FIXTURE_HEIGHT": str(height),
                                         **({"FIXTURE_PATTERN": "1"} if pattern else {})},
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True)
        assert line_ready(renderer.stdout) == "fixture-ready"
        receipt = capture.register(
            publisher, display=display, xvfb_pid=xvfb.pid,
            xvfb_start=capture.process_identity(xvfb.pid)["start_ticks"],
            renderer_pid=renderer.pid, renderer_start=capture.process_identity(renderer.pid)["start_ticks"],
            session_id="OWNED-X11-session", actor={"uid": 42, "lifecycle": "fixture-painter-only"})
        yield directory, publisher, receipt, xvfb, renderer
    finally:
        publisher.close()
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)
        for child in (renderer, xvfb):
            if child is not None:
                if child.poll() is None:
                    child.send_signal(signal.SIGCONT)
                    child.terminate()
                    try:
                        child.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=3)
                for stream in (child.stdin, child.stdout, child.stderr):
                    if stream:
                        stream.close()


def start_capture(fixture, root):
    directory, _, _, _, _ = fixture
    source = capture.PassiveSource(directory, root / "capture", fps=5)
    try:
        source.wait_ready()
    except BaseException:
        source.close()
        raise
    return source


def paint(renderer, color):
    renderer.stdin.write(color + "\n")
    renderer.stdin.flush()
    assert line_ready(renderer.stdout) == "painted"


def test_owned_changing_pixels_ui_mp4_png_without_any_agent_frames(registered_display, tmp_path):
    directory, publisher, receipt, xvfb, renderer = registered_display
    root = output_root(tmp_path)
    source = start_capture(registered_display, root)
    app = server = thread = None
    try:
        assert feed.Source(directory).latest() is None
        assert feed.Source(directory).catalog() == []
        first = source.latest()
        app = viewer.App(source, root / "exports")
        server = viewer.Server(("127.0.0.1", 0), app)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        wait_for(lambda: app.preview.row)
        state = json.loads(request(server, "/api/state")[1])
        assert state["capture_mode"] == "passive-renderer-x11" and state["feed_state"] == "live"
        assert state["frame"]["native_frame_id"] is None
        origin = f"http://127.0.0.1:{server.server_port}"
        headers = {"Origin": origin, "X-Observer-Token": app.token, "Content-Type": "application/json"}
        assert request(server, "/api/record/start", "POST", "{}", **headers)[0] == 202
        paint(renderer, "22cc44")
        green = wait_for(lambda: (row if (row := source.latest())["image"]["sha256"]
                                  != first["image"]["sha256"] else None))
        screenshot = media.screenshot(source, root / "screenshot", green)
        assert screenshot["frames"][0]["native_frame_id"] is None
        decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", str(root / "screenshot/frame.png"),
                                  "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                                 capture_output=True, timeout=10, check=True).stdout
        center = (32 * 96 + 48) * 3
        assert decoded[center:center + 3] == bytes((0x22, 0xcc, 0x44))
        paint(renderer, "3344cc")
        wait_for(lambda: source.latest()["image"]["sha256"] != green["image"]["sha256"])
        assert request(server, "/api/record/stop", "POST", "{}", **headers)[0] == 202
        wait_for(lambda: app.recording.snapshot()["state"] not in {"recording", "encoding"})
        assert app.recording.snapshot()["state"] == "saved"
        state = json.loads(request(server, "/api/state")[1])
        status, movie, headers = request(server, state["recording"]["download"])
        assert status == 200 and movie[4:8] == b"ftyp" and headers["Content-Type"] == "video/mp4"
        metadata = json.loads(request(server, state["recording"]["metadata"])[1])
        rows = metadata["frames"]
        assert len({row["image"]["sha256"] for row in rows}) >= 3
        assert all(row["native_frame_id"] is None for row in rows)
        assert all(row["frame_id_scope"] == "passive-capture-session-sequence" for row in rows)
        assert all("begin_ns" not in row["capture_host"] for row in rows)
        assert all(row["capture_origin"]["receipt_sha256"] == source.attachment.sha256 for row in rows)
        assert all(row["session_id"] == receipt["session_id"] and row["actor"] == receipt["actor"]
                   for row in rows)
        assert feed.Source(directory).latest() is None and feed.Source(directory).catalog() == []
        assert not list(directory.glob("*.png"))
        # Evidence can be audited independently of the pytest assertion/log.
        (root / "engineering-evidence.json").write_text(json.dumps({
            "kind": "owned-X11-fixture-not-gameplay", "no_agent_native_frame_requests": True,
            "agent_frame_receipts": len(feed.Source(directory).catalog()),
            "receipt": receipt, "capture_source": str(source.directory),
            "screenshot": str(root / "screenshot/frame.png"),
            "mp4": str(Path(app.recording.snapshot()["path"]) / "video.mp4"),
            "probe": metadata["probe"], "sample_count": len(rows),
            "distinct_pngs": len({row["image"]["sha256"] for row in rows})}, indent=2) + "\n")
    finally:
        if server:
            server.shutdown()
            server.server_close()
        if app:
            app.close()
        if thread:
            thread.join()
        source.close()
    assert source.process.poll() is not None
    assert xvfb.poll() is None and renderer.poll() is None


@pytest.mark.parametrize("change", ["ticks", "socket", "geometry", "run", "boot"])
def test_registration_identity_refusals(registered_display, change):
    directory, publisher, receipt, _, _ = registered_display
    if change == "ticks":
        receipt["renderer"]["start_ticks"] += 1
    elif change == "socket":
        receipt["display_socket"]["inode"] += 1
    elif change == "geometry":
        receipt["width"] += 1
    elif change == "run":
        receipt["run_id"] = "another-run"
    else:
        receipt["boot_id"] = "another-boot"
    (publisher.root / "capture.json").write_text(json.dumps(receipt))
    with pytest.raises((ValueError, RuntimeError)):
        capture.Attachment(directory)


def test_source_change_refuses_new_pixels_exports_and_retires_only_capture(registered_display, tmp_path):
    _, publisher, _, xvfb, renderer = registered_display
    root = output_root(tmp_path)
    source = start_capture(registered_display, root)
    row = source.latest()
    try:
        # Even a semantically similar rewritten receipt is not the pinned registration.
        with (publisher.root / "capture.json").open("ab") as handle:
            handle.write(b" ")
        with pytest.raises((ValueError, RuntimeError), match="receipt identity changed"):
            source.pixels(row)
        with pytest.raises((ValueError, RuntimeError), match="receipt identity changed"):
            media.screenshot(source, root / "refused")
        wait_for(lambda: source.error)
        wait_for(lambda: source.process.poll() is not None)
        assert source.state()["feed_state"] == "error"
        assert not (root / "refused").exists()
    finally:
        source.close()
    assert xvfb.poll() is None and renderer.poll() is None


def test_source_ended_saved_capture_pixels_need_no_ffmpeg(registered_display, tmp_path, monkeypatch):
    _, publisher, _, xvfb, renderer = registered_display
    root = output_root(tmp_path)
    source = start_capture(registered_display, root)
    publisher.close()
    wait_for(lambda: not source.thread.is_alive())
    assert source.state()["feed_state"] == "ended"
    with pytest.raises(capture.Ended):
        source.pixels(source.latest())
    source.close()
    monkeypatch.setattr(capture.shutil, "which", lambda _: None)
    saved = feed.Source(source.directory)
    assert saved.latest()["native_frame_id"] is None
    assert media.screenshot(saved, root / "saved-no-ffmpeg")["status"] == "saved"
    assert xvfb.poll() is None and renderer.poll() is None


def test_stopped_registered_renderer_refused(registered_display, tmp_path):
    _, _, _, _, renderer = registered_display
    root = output_root(tmp_path)
    source = start_capture(registered_display, root)
    try:
        renderer.send_signal(signal.SIGSTOP)
        wait_for(lambda: source.error)
        assert "stopped" in source.error
        assert source.state()["feed_state"] == "error"
    finally:
        renderer.send_signal(signal.SIGCONT)
        source.close()


def test_png_pipe_framing_and_missing_optional_encoder(tmp_path, monkeypatch):
    data = png()
    assert capture.pop_png(data + data) == (data, data)
    assert capture.pop_png(data[:20]) == (None, data[:20])
    with pytest.raises(ValueError, match="non-PNG"):
        capture.pop_png(b"not a PNG pipe")
    monkeypatch.setattr(capture.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="requires ffmpeg"):
        capture.PassiveSource(tmp_path / "unused", tmp_path / "out")


@pytest.mark.parametrize("registered_display", [(1920, 1080, True)], indirect=True)
def test_large_frame_stop_drains_encoder_and_reaps_only_owned_child(registered_display, tmp_path, monkeypatch):
    """Real X11/PNG encoder: pause consumption until the owned writer blocks.

    This proves the shared shutdown defect, not the cause of historical -9s.
    A normal 1080p patterned HUD/frame is larger than the kernel pipe capacity.
    """
    _, _, _, xvfb, renderer = registered_display
    root = output_root(tmp_path)
    original = capture.PassiveSource._publish

    def pause_after_frame(self, data, sequence, pts):
        original(self, data, sequence, pts)
        self.stopping.wait()

    monkeypatch.setattr(capture.PassiveSource, "_publish", pause_after_frame)
    source = start_capture(registered_display, root)
    identity = capture.process_identity(source.process.pid)
    try:
        wait_for(lambda: any("pipe_write" in path.read_text()
                            for path in (Path("/proc") / str(source.process.pid) / "task").glob("*/wchan")))
        saved = feed.Source(source.directory)
        frame = saved.latest()
        assert frame["image"]["width"] == 1920 and frame["image"]["height"] == 1080
        assert len(saved.pixels(frame)) > 65536
        assert capture.process_identity(source.process.pid) == identity
    finally:
        source.close()
    assert not source.thread.is_alive() and source.closed
    assert source.process.poll() == 255  # ffmpeg handles SIGTERM after completing/draining output.
    cleanup = source.publisher.run["capture_encoder_cleanup"]
    assert cleanup["stopped"] and not cleanup["forced_termination"]
    assert cleanup["pipes_closed"] and cleanup["stdout_eof"] and cleanup["stderr_eof"]
    assert source.process.stdout.closed and source.process.stderr.closed
    assert not (Path("/proc") / str(identity["pid"])).exists()
    assert xvfb.poll() is None and renderer.poll() is None
    source.close()  # Idempotent, no attempt to signal a stale PID.
    exported = media.screenshot(feed.Source(source.directory), root / "saved")
    assert exported["status"] == "saved"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(root / "saved/frame.png"),
                    "-f", "null", "-"], capture_output=True, check=True, timeout=15)


def test_owned_unresponsive_encoder_forced_fallback_is_truthful():
    process = subprocess.Popen(
        [sys.executable, "-u", "-c",
         "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
         "print('ready',flush=True);time.sleep(60)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert line_ready(process.stdout) == b"ready"
        identity = capture.process_identity(process.pid)
        cleanup, _ = capture.stop_encoder(process, timeout=0.2)
        assert cleanup["forced_termination"] and cleanup["exit_code"] == -signal.SIGKILL
        assert cleanup["stopped"] and cleanup["pipes_closed"]
        assert cleanup["stdout_eof"] and cleanup["stderr_eof"]
        assert not (Path("/proc") / str(identity["pid"])).exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
        process.stderr.close()
