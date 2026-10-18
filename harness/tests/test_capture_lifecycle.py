"""Owned subprocess/real-encoder lifecycle regressions; no game/controller."""
from functools import partial
import json
from pathlib import Path
import signal
import subprocess
import sys
import threading
from unittest.mock import patch

import pytest

from scripts.rpg_benchmark import frame_feed as feed
from scripts.rpg_benchmark import passive_capture as capture
from scripts.rpg_benchmark import visualization_media as media
from .test_passive_capture import (line_ready, output_root, registered_display,
                                   start_capture)
from .test_visualization import source, wait_for


def retire(child):
    """Fallback for failed assertions, only this test's unreaped Popen child."""
    if child.poll() is None:
        child.kill()
    child.wait(timeout=3)
    for stream in (child.stdout, child.stderr):
        stream.close()


def test_pipe_eof_is_not_process_exit(tmp_path):
    marker = tmp_path / "graceful-completion"
    script = """
import os, signal, sys, time
from pathlib import Path
def finish(*args):
    os.close(1)
    os.close(2)
    time.sleep(0.15)
    Path(sys.argv[1]).write_text("finished")
    raise SystemExit(0)
signal.signal(signal.SIGTERM, finish)
print("ready", flush=True)
while True: time.sleep(1)
"""
    child = subprocess.Popen([sys.executable, "-u", "-c", script, str(marker)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert line_ready(child.stdout) == b"ready"
        cleanup, _ = capture.stop_encoder(child, timeout=1)
        assert cleanup["exit_code"] == 0
        assert marker.read_text() == "finished"
        assert cleanup["stdout_eof"] and cleanup["stderr_eof"]
        assert cleanup["pipes_closed"] and not cleanup["forced_termination"]
    finally:
        retire(child)


def test_drain_error_still_reaps_child_and_preserves_error():
    child = subprocess.Popen(
        [sys.executable, "-u", "-c",
         "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
         "print('ready',flush=True);time.sleep(60)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert line_ready(child.stdout) == b"ready"
        with patch.object(capture.select, "select", side_effect=OSError("injected drain failure")):
            with pytest.raises(OSError, match="injected drain failure"):
                capture.stop_encoder(child, timeout=0.2)
        assert child.poll() == -signal.SIGKILL
        assert child.stdout.closed and child.stderr.closed
        assert not (Path("/proc") / str(child.pid)).exists()
    finally:
        retire(child)


def test_exit_winning_kill_race_is_not_reported_as_forced(monkeypatch):
    child = subprocess.Popen(
        [sys.executable, "-u", "-c",
         "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
         "signal.signal(signal.SIGUSR1,lambda *args:exit(255));"
         "print('ready',flush=True);time.sleep(60)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    kill = child.kill

    def exit_just_before_kill():
        # A deterministic interleaving of a real child's exit with Popen.kill:
        # Popen's own send_signal re-polls and does not signal a reaped PID.
        child.send_signal(signal.SIGUSR1)
        child.wait(timeout=3)
        kill()

    try:
        assert line_ready(child.stdout) == b"ready"
        monkeypatch.setattr(child, "kill", exit_just_before_kill)
        cleanup, _ = capture.stop_encoder(child, timeout=0.2)
        assert cleanup["exit_code"] == 255
        assert not cleanup["forced_termination"]
        assert cleanup["kill_attempted"]
        assert cleanup["stdout_eof"] and cleanup["stderr_eof"] and cleanup["pipes_closed"]
    finally:
        retire(child)


@pytest.mark.parametrize("prior_failure", [False, True])
def test_cleanup_error_finishes_archive_and_wakes_waiters(
        registered_display, tmp_path, monkeypatch, prior_failure):
    root = output_root(tmp_path)
    source = start_capture(registered_display, root)
    original = capture.stop_encoder

    def broken_drain(child):
        with patch.object(capture.select, "select", side_effect=OSError("injected drain failure")):
            return original(child, timeout=0.2)

    monkeypatch.setattr(capture, "stop_encoder", broken_drain)
    source.ready.clear()
    try:
        if prior_failure:
            with (registered_display[1].root / "capture.json").open("ab") as handle:
                handle.write(b" ")
            wait_for(lambda: not source.thread.is_alive())
        source.close()
        assert source.ready.is_set()
        assert source.error and "injected drain failure" in source.error
        saved = feed.Source(source.directory)
        assert saved.run()["status"] == "error"
        assert "injected drain failure" in saved.run()["error"]
        if prior_failure:
            assert "receipt identity changed" in saved.run()["error"]
        cleanup = saved.run()["capture_encoder_cleanup"]
        assert cleanup["stopped"] and cleanup["pipes_closed"]
        assert not source.thread.is_alive() and source.process.poll() is not None
    finally:
        retire(source.process)


def test_forced_stop_marks_capture_error(registered_display, tmp_path, monkeypatch):
    directory, _, _, xvfb, renderer = registered_display
    marker = tmp_path / "encoder-ready"
    popen = subprocess.Popen
    script = ("import signal,time;from pathlib import Path;"
              "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
              f"Path({str(marker)!r}).write_text('ready');time.sleep(60)")

    def stubborn_encoder(command, **kwargs):
        if Path(command[0]).name == "ffmpeg":
            return popen([sys.executable, "-u", "-c", script], **kwargs)
        return popen(command, **kwargs)

    monkeypatch.setattr(capture.subprocess, "Popen", stubborn_encoder)
    monkeypatch.setattr(capture, "stop_encoder", partial(capture.stop_encoder, timeout=0.2))
    source = capture.PassiveSource(directory, output_root(tmp_path) / "capture")
    try:
        wait_for(marker.exists)
    finally:
        source.close()
    run = feed.Source(source.directory).run()
    assert run["capture_encoder_cleanup"]["forced_termination"]
    assert run["capture_encoder_cleanup"]["exit_code"] == -signal.SIGKILL
    assert run["status"] == "error" and "forced termination" in run["error"]
    assert source.state()["feed_state"] == "error"
    with pytest.raises(RuntimeError, match="forced termination"):
        source.wait_ready()
    assert xvfb.poll() is None and renderer.poll() is None


def test_real_media_encoder_cancel_reaps_and_never_saves(source, tmp_path, monkeypatch):
    saved, publisher = source
    publisher.close()
    cancel = threading.Event()
    popen = subprocess.Popen
    children = []
    logs = []

    def cancel_started_encoder(command, **kwargs):
        child = popen(command, **kwargs)
        if Path(command[0]).name == "ffmpeg" and "concat" in command:
            children.append(child)
            logs.append(kwargs["stderr"])
            cancel.set()
        return child

    monkeypatch.setattr(media.subprocess, "Popen", cancel_started_encoder)
    out = output_root(tmp_path) / "cancelled-mp4"
    with pytest.raises(RuntimeError, match="cancelled"):
        media.movie(saved, out, cancel=cancel)
    assert len(children) == 1
    child = children[0]
    assert child.poll() is not None
    assert not (Path("/proc") / str(child.pid)).exists()
    assert all(log.closed for log in logs)
    assert not (out / "video.mp4").exists()
    assert not list(out.glob(".encode-*"))
    record = json.loads((out / "metadata.json").read_text())
    assert record["status"] == "error" and "cancelled" in record["error"]


@pytest.mark.parametrize("ending", ["concurrent-close", "source-ended", "stale-recording"])
@pytest.mark.parametrize("registered_display", [(1920, 1080, True)], indirect=True)
def test_large_frame_end_ownership_and_saved_media(registered_display, tmp_path, monkeypatch, ending):
    """Cancellation/end/stale paths retain complete archived pixels, not partial PNGs."""
    import fcntl  # This fixture is explicitly Linux X11/procfs only.

    _, publisher, _, xvfb, renderer = registered_display
    root = output_root(tmp_path)
    release = threading.Event()
    paused = threading.Event()
    original_publish = capture.PassiveSource._publish
    original_stop = capture.stop_encoder
    stop_owners = []
    closers = []
    recording = None

    def pause_consumption(self, data, sequence, pts):
        original_publish(self, data, sequence, pts)
        if sequence == 0:
            paused.set()
            if not release.wait(10):
                raise RuntimeError("test did not release paused capture reader")

    def owned_stop(child):
        stop_owners.append(threading.current_thread().ident)
        return original_stop(child)

    monkeypatch.setattr(capture.PassiveSource, "_publish", pause_consumption)
    monkeypatch.setattr(capture, "stop_encoder", owned_stop)
    source = start_capture(registered_display, root)
    identity = capture.process_identity(source.process.pid)
    try:
        assert paused.wait(3)
        pipe_capacity = fcntl.fcntl(source.process.stdout.fileno(), fcntl.F_GETPIPE_SZ)
        waits = wait_for(lambda: [
            {"tid": path.parent.name, "wchan": path.read_text().strip()}
            for path in (Path("/proc") / str(source.process.pid) / "task").glob("*/wchan")
            if "pipe_write" in path.read_text()])
        before = source.latest()
        assert before["image"]["byte_length"] > max(65536, pipe_capacity)
        if ending == "concurrent-close":
            barrier = threading.Barrier(3)

            def close_together():
                barrier.wait(timeout=3)
                source.close()

            closers = [threading.Thread(target=close_together) for _ in range(2)]
            for thread in closers:
                thread.start()
            barrier.wait(timeout=3)
            wait_for(source.stopping.is_set)
        elif ending == "source-ended":
            publisher.close()
        else:
            recording = media.Recording(source, root / "viewer-exports")
            recording.start()
            source.stale_seconds = 0.001
            wait_for(lambda: source.state()["feed_state"] == "stale")
            with pytest.raises(RuntimeError, match="fresh running"):
                recording.stop()
            assert recording.snapshot()["state"] == "error"
            assert not (recording.out / "video.mp4").exists()
            source.stopping.set()
        release.set()
        wait_for(lambda: not source.thread.is_alive())
    finally:
        release.set()
        source.close()
        for thread in closers:
            thread.join(timeout=5)
        if recording:
            recording.close()
    assert all(not thread.is_alive() for thread in closers)
    assert stop_owners == [source.thread.ident]
    assert source.process.poll() == 255
    assert not (Path("/proc") / str(identity["pid"])).exists()
    assert source.error is None and source.ready.is_set()
    assert xvfb.poll() is None and renderer.poll() is None
    cleanup = source.publisher.run["capture_encoder_cleanup"]
    assert cleanup["stopped"] and not cleanup["forced_termination"]
    assert cleanup["stdout_eof"] and cleanup["stderr_eof"] and cleanup["pipes_closed"]
    saved = feed.Source(source.directory)
    assert saved.run()["status"] == "ended"
    assert saved.catalog() == [before]  # Shutdown drain must not publish new samples.
    assert not list(source.directory.glob("*.partial*"))
    assert len(list(source.directory.glob("capture-*.png"))) == 1
    assert feed.Source(registered_display[0]).catalog() == []
    screenshot = media.screenshot(saved, root / "saved-png")
    movie = media.movie(saved, root / "saved-mp4", fps=5)
    assert screenshot["status"] == movie["status"] == "saved"
    stream = movie["probe"]["streams"][0]
    assert (stream["width"], stream["height"]) == (1920, 1080)
    assert int(stream["nb_read_frames"]) == 1
    subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i",
                    str(root / "saved-png/frame.png"), "-f", "null", "-"],
                   capture_output=True, check=True, timeout=15)
    (root / "lifecycle-evidence.json").write_text(json.dumps({
        "kind": "owned-X11-fixture-not-gameplay", "ending": ending,
        "encoder_identity": identity, "pipe_capacity_bytes": pipe_capacity,
        "observed_wait_channels": waits, "archived_frame_bytes": before["image"]["byte_length"],
        "stop_owner_is_capture_worker": stop_owners == [source.thread.ident],
        "cleanup": cleanup, "source_catalog_unchanged": saved.catalog() == [before],
        "png": str(root / "saved-png/frame.png"),
        "mp4": str(root / "saved-mp4/video.mp4"),
        "probe": movie["probe"],
        "registered_source_children_alive": xvfb.poll() is None and renderer.poll() is None,
    }, indent=2) + "\n")
