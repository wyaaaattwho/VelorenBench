"""Offline engineering fixtures only. Never launches native/game/model processes."""
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4
import zlib

import pytest

from scripts.rpg_benchmark import frame_feed as feed
from scripts.rpg_benchmark import visualization as viewer
from scripts.rpg_benchmark import visualization_media as media
from scripts.rpg_benchmark.renderer import RendererBoundary


def png(rgb=(220, 20, 40), width=16, height=16):
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))
    pixels = (b"\0" + bytes(rgb) * width) * height
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


def native_frame(directory, number=1, at=None, rgb=(220, 20, 40)):
    data = png(rgb)
    path = directory / f"native-{number}.png"
    path.write_bytes(data)
    at = at if at is not None else time.time_ns()
    return {"frame_schema_version": "1.0", "frame_id": f"fixture-{number}", "session_id": "session-fixture",
            "actor": {"uid": 7, "lifecycle": "fixture-life"}, "native_tick": None,
            "client_update_counter": number,
            "capture_host": {"clock": "unix-realtime-nanoseconds-as-decimal-string",
                             "begin_ns": str(at), "readback_complete_ns": str(at + 1000)},
            "image": {"path": str(path), "mime_type": "image/png",
                      "sha256": hashlib.sha256(data).hexdigest(), "byte_length": len(data),
                      "width": 16, "height": 16, "model_attachment_delivered": False},
            "private": "MUST_NOT_PUBLISH"}


@pytest.fixture
def source(tmp_path):
    directory = tmp_path / "frames"
    directory.mkdir()
    publisher = feed.Publisher(directory, "fixture-run", "fixture-task", "fixture-terrain")
    publisher.publish(native_frame(directory))
    return feed.Source(directory), publisher


def evidence_out(tmp_path, name):
    root = Path(os.environ.get("VELORENBENCH_MEDIA_EVIDENCE", str(tmp_path / "exports")))
    return root / (name + "-" + uuid4().hex[:8])


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("fixture worker timed out")


def test_public_feed_exact_provenance_no_private_or_original_absolute_paths(source):
    source, publisher = source
    row = source.latest()
    assert row["run_id"] == "fixture-run" and row["frame_id"] == "fixture-1"
    assert row["task_id"] == "fixture-task" and row["session_id"] == "session-fixture"
    text = json.dumps(row)
    assert "MUST_NOT_PUBLISH" not in text and str(source.directory) not in text
    assert source.receipt(feed.frame_key(row)) == row
    assert source.catalog() == [row]
    assert source.pixels(row) == png()
    with pytest.raises(FileExistsError):
        publisher.publish(native_frame(source.directory))


def test_source_selection_empty_stale_ended_and_clock_skew(source, tmp_path, monkeypatch):
    source, publisher = source
    live = feed.Source(source.directory, live=True)
    at = int(live.latest()["capture_host"]["begin_ns"]) / 1e9
    monkeypatch.setattr(feed.time, "time", lambda: at + 1)
    assert live.state()["feed_state"] == "live"
    monkeypatch.setattr(feed.time, "time", lambda: at + 4)
    assert live.state()["feed_state"] == "stale"
    monkeypatch.setattr(feed.time, "time", lambda: at - 10)
    assert live.state()["feed_state"] == "stale"
    publisher.close()
    assert live.state()["feed_state"] == "ended"
    empty = tmp_path / "empty"
    empty.mkdir()
    feed.Publisher(empty, "other-run", "other-task", "t")
    other = feed.Source(empty)
    assert other.latest() is None and other.state()["feed_state"] == "empty"
    with pytest.raises(ValueError, match="provenance"):
        other.validate(source.latest())
    with pytest.raises(FileNotFoundError):
        feed.Source(tmp_path / "unselected")
    publisher.run["run_id"] = "replacement"
    feed.atomic_json(publisher.root / "run.json", publisher.run)
    with pytest.raises(ValueError, match="identity changed"):
        live.run()


@pytest.mark.parametrize("field,value", [
    ("file", "../escape.png"), ("file", "/etc/passwd.png"), ("file", r"..\escape.png"),
    ("mime_type", "image/jpeg"), ("sha256", "invalid"), ("width", 100000),
    ("byte_length", 0),
])
def test_malformed_png_descriptors_refused(source, field, value):
    source, _ = source
    row = source.latest()
    row["image"][field] = value
    with pytest.raises(ValueError):
        source.pixels(row)


def test_hash_crc_time_symlink_and_nonregular_refusals(source, tmp_path):
    source, _ = source
    row = source.latest()
    path = source.directory / row["image"]["file"]
    path.write_bytes(b"malformed")
    with pytest.raises(ValueError, match="hash"):
        source.pixels(row)
    row["image"].update(byte_length=9, sha256=hashlib.sha256(b"malformed").hexdigest())
    with pytest.raises(ValueError):
        source.pixels(row)
    path.unlink()
    other = tmp_path / "other.png"
    other.write_bytes(png())
    path.symlink_to(other)
    with pytest.raises(ValueError, match="symlink"):
        source.pixels(source.latest())
    ancestor = tmp_path / "link"
    ancestor.symlink_to(source.directory, target_is_directory=True)
    with pytest.raises(ValueError):
        feed.Source(ancestor)
    with pytest.raises(ValueError):
        feed.read_bytes(source.directory, 100)
    row = source.latest()
    row["capture_host"]["readback_complete_ns"] = "0"
    with pytest.raises(ValueError, match="timestamps"):
        source.validate(row)


def test_screenshot_original_pixels_metadata_collision_and_read_only(source, tmp_path):
    source, publisher = source
    publisher.close()
    before = {str(p.relative_to(source.directory)): p.read_bytes()
              for p in source.directory.rglob("*") if p.is_file()}
    out = evidence_out(tmp_path, "pytest-original-png")
    record = media.screenshot(source, out)
    assert record["status"] == "saved" and record["source"]["feed_state"] == "ended"
    assert (out / "frame.png").read_bytes() == png()
    # Actual PNG content check without optional Pillow: decode filter-0 RGB pixels.
    encoded = (out / "frame.png").read_bytes()
    payload = encoded[41:-16]  # IDAT payload for this bounded fixture.
    decoded = zlib.decompress(payload)
    assert decoded == (b"\0" + bytes((220, 20, 40)) * 16) * 16
    assert json.loads((out / "metadata.json").read_text())["frames"][0] == source.latest()
    with pytest.raises(FileExistsError):
        media.screenshot(source, out)
    assert (out / "frame.png").read_bytes() == encoded
    assert before == {str(p.relative_to(source.directory)): p.read_bytes()
                      for p in source.directory.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="overlap"):
        media.screenshot(source, source.directory / "destructive")


def test_empty_exports_missing_tools_codec_and_encoder_failure(source, tmp_path, monkeypatch):
    source, publisher = source
    monkeypatch.setattr(media.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="ffmpeg and ffprobe"):
        media.preflight()
    monkeypatch.setattr(media.shutil, "which", lambda name: "/fixture/" + name)
    monkeypatch.setattr(media.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=" V..... ffv1"))
    with pytest.raises(RuntimeError, match="libx264"):
        media.preflight()
    monkeypatch.setattr(media, "preflight", lambda: {"ffmpeg": "/fixture/ffmpeg", "ffprobe": "/fixture/ffprobe"})
    empty = tmp_path / "empty"
    empty.mkdir()
    feed.Publisher(empty, "empty-run", "empty-task", "t")
    with pytest.raises(ValueError, match="empty"):
        media.screenshot(feed.Source(empty), tmp_path / "empty-shot")
    with pytest.raises(ValueError, match="empty"):
        media.movie(feed.Source(empty), tmp_path / "empty-movie")
    monkeypatch.setattr(media, "owned_command", Mock(side_effect=RuntimeError("fixture encoder failure")))
    out = tmp_path / "failed-encode"
    with pytest.raises(RuntimeError, match="encoder failure"):
        media.movie(source, out)
    assert json.loads((out / "metadata.json").read_text())["status"] == "error"
    assert not (out / "video.mp4").exists()
    assert not list(out.glob(".encode-*"))


def test_actual_ffmpeg_mp4_probe_decode_and_timing(source, tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("optional system ffmpeg/ffprobe unavailable")
    source, publisher = source
    begin = int(source.latest()["capture_host"]["begin_ns"])
    publisher.publish(native_frame(source.directory, 2, begin + 200_000_000, (20, 200, 60)))
    publisher.publish(native_frame(source.directory, 3, begin + 550_000_000, (30, 40, 220)))
    publisher.close()
    out = evidence_out(tmp_path, "pytest-genuine-mp4")
    result = media.movie(source, out, fps=20)
    assert result["status"] == "saved"
    assert result["source_duration_seconds"] == pytest.approx(0.6)
    assert int(result["probe"]["streams"][0]["nb_read_frames"]) == 12
    assert "mp4" in result["probe"]["format"]["format_name"]
    data = (out / "video.mp4").read_bytes()
    assert data[4:8] == b"ftyp" and not data.startswith(b"\x1aE\xdf\xa3")
    decoded = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(out / "video.mp4"),
                              "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                             capture_output=True, check=True, timeout=15).stdout
    assert len(decoded) == 12 * 16 * 16 * 3
    first, last = decoded[:3], decoded[-3:]
    assert first[0] > 180 and first[1] < 60 and first[2] < 80
    assert last[2] > 180 and last[0] < 80
    assert [row["frame_id"] for row in result["frames"]] == ["fixture-1", "fixture-2", "fixture-3"]
    with pytest.raises(FileExistsError):
        media.movie(source, out)


def test_movie_identity_and_nonincreasing_time_failures(source, tmp_path, monkeypatch):
    source, _ = source
    monkeypatch.setattr(media, "preflight", lambda: {})
    first = source.latest()
    with pytest.raises(ValueError, match="strictly increasing"):
        media.movie(source, tmp_path / "same-time", [first, copy.deepcopy(first)])
    second = copy.deepcopy(first)
    second["session_id"] = "other-session"
    with pytest.raises(ValueError, match="cannot mix"):
        media.movie(source, tmp_path / "mixed", [first, second])


def test_owned_encoder_failure_and_cancel_cleanup(tmp_path):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(RuntimeError, match="cancelled"):
        media.owned_command([sys.executable, "-c", "import time;time.sleep(30)"],
                            tmp_path / "cancel.log", cancel)
    with pytest.raises(RuntimeError, match="exit 7"):
        media.owned_command([sys.executable, "-c", "raise SystemExit(7)"],
                            tmp_path / "failed.log", threading.Event())


def test_probe_failure_is_not_saved_and_spool_is_cleaned(source, tmp_path, monkeypatch):
    source, _ = source
    monkeypatch.setattr(media, "preflight", lambda: {"ffmpeg": "fixture", "ffprobe": "fixture"})
    monkeypatch.setattr(media, "owned_command", lambda command, *a, **k:
                        Path(command[-1]).write_bytes(b"not an mp4"))
    monkeypatch.setattr(media.subprocess, "run", lambda *a, **k:
                        SimpleNamespace(stdout=json.dumps({"format": {"format_name": "webm", "duration": "1"},
                                                           "streams": [{"codec_name": "vp9", "nb_read_frames": "1"}]})))
    out = tmp_path / "bad-probe"
    with pytest.raises(RuntimeError, match="MP4/frame probe"):
        media.movie(source, out)
    assert not (out / "video.mp4").exists()
    assert json.loads((out / "metadata.json").read_text())["status"] == "error"
    assert not list(out.glob(".encode-*"))


def test_preview_single_latest_fanout_skips_frames_and_staleness(source, monkeypatch):
    source, publisher = source
    source.live = True
    preview = viewer.Preview(source)
    preview.poll()
    first = preview.wait_frame(0, 0)
    assert first[1]["frame_id"] == "fixture-1" and first[2] == png()
    begin = time.time_ns()
    for n in range(2, 6):
        publisher.publish(native_frame(source.directory, n, begin + n))
    preview.poll()
    assert preview.sequence == 2
    assert preview.wait_frame(1, 0)[1]["frame_id"] == "fixture-5"
    assert not hasattr(preview, "queue")
    assert preview.wait_frame(2, 0) is None
    monkeypatch.setattr(feed.time, "time", lambda: begin / 1e9 + 10)
    assert preview.snapshot()["feed_state"] == "stale"
    assert preview.wait_frame(0, 0) is None
    preview.close()
    assert preview.wait_frame(0, 0) is None


def test_recording_window_includes_persisted_intermediate_frames_and_close(source, tmp_path, monkeypatch):
    source, publisher = source
    source.live = True
    monkeypatch.setattr(media, "preflight", lambda: {})
    received = []
    def movie(*args, **kwargs):
        received.extend(args[2])
        return {"file": "video.mp4"}
    monkeypatch.setattr(media, "movie", movie)
    recording = media.Recording(source, tmp_path / "recordings")
    assert recording.start()["state"] == "recording"
    with pytest.raises(RuntimeError, match="active"):
        recording.start()
    begin = time.time_ns()
    publisher.publish(native_frame(source.directory, 2, begin))
    publisher.publish(native_frame(source.directory, 3, begin + 1))
    recording.stop()
    wait_for(lambda: recording.snapshot()["state"] == "saved")
    assert [row["frame_id"] for row in received] == ["fixture-1", "fixture-2", "fixture-3"]
    recording.close()
    assert not recording.thread.is_alive()
    interrupted = media.Recording(source, tmp_path / "recordings")
    interrupted.start()
    interrupted.close()
    assert interrupted.snapshot()["state"] == "error"


def test_recording_stale_ended_saved_and_collision_failures(source, tmp_path, monkeypatch):
    source, publisher = source
    monkeypatch.setattr(media, "preflight", lambda: {})
    recording = media.Recording(source, tmp_path / "recordings")
    with pytest.raises(RuntimeError, match="fresh running"):
        recording.start()
    source.live = True
    recording.start()
    publisher.close()
    with pytest.raises(RuntimeError, match="fresh running"):
        recording.stop()
    assert recording.snapshot()["state"] == "error"
    assert not (recording.out / "video.mp4").exists()
    recording.close()


def test_encoder_disappearing_after_start_persists_error_and_frame_provenance(source, tmp_path, monkeypatch):
    source, _ = source
    source.live = True
    monkeypatch.setattr(media, "preflight", lambda: {})
    recording = media.Recording(source, tmp_path / "recordings")
    recording.start()
    monkeypatch.setattr(media, "preflight", Mock(side_effect=RuntimeError("ffmpeg disappeared")))
    recording.stop()
    wait_for(lambda: recording.snapshot()["state"] == "error")
    recording.thread.join()
    metadata = json.loads((recording.out / "metadata.json").read_text())
    assert metadata["status"] == "error" and "disappeared" in metadata["error"]
    assert metadata["source_frame"]["frame_id"] == "fixture-1"
    assert metadata["source_frame"]["session_id"] == "session-fixture"
    assert not (recording.out / "video.mp4").exists()
    recording.close()


def test_boundary_publishes_only_successful_public_frame_without_mutation(source):
    source, publisher = source
    bridge = SimpleNamespace(public_media=publisher, journal=Mock())
    boundary = object.__new__(RendererBoundary)
    boundary.bridge, boundary.mode = bridge, "image"
    boundary.public_observer = Mock()
    response = {"ok": True, "result": native_frame(source.directory, 2),
                "session_id": "session-fixture", "type": "response"}
    saved = copy.deepcopy(response)
    result = boundary.public_response(response, {"cmd": "frame"})
    assert response == saved and result["result"]["frame_id"] == "fixture-2"
    assert source.latest()["frame_id"] == "fixture-2"
    result = boundary.public_response({"ok": True, "result": {"private": "NEVER"}},
                                      {"cmd": "observe"})
    assert "NEVER" not in json.dumps(result)
    assert len(source.catalog()) == 2
    publisher.publish = Mock(side_effect=OSError("private contents"))
    # Optional publication errors preserve the original public native success.
    assert boundary.public_response(response, {"cmd": "frame"})["ok"] is True
    assert source.run()["status"] == "error"
    assert "private contents" not in json.dumps(source.run())


@pytest.fixture
def http_server(source, tmp_path):
    source, _ = source
    app = viewer.App(source, Path(os.environ.get("VELORENBENCH_MEDIA_EVIDENCE",
                                               str(tmp_path / "exports"))) / uuid4().hex)
    server = viewer.Server(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    wait_for(lambda: app.preview.row)
    yield server, app
    server.shutdown()
    server.server_close()
    app.close()
    thread.join()
    assert not app.preview.thread.is_alive()


def request(server, path, method="GET", body=None, **headers):
    host = f"127.0.0.1:{server.server_port}"
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    connection.request(method, path, body=body, headers={"Host": host, **headers})
    response = connection.getresponse()
    result = response.status, response.read(), dict(response.getheaders())
    connection.close()
    return result


def test_host_origin_csrf_path_and_asset_protections(http_server):
    server, app = http_server
    assert request(server, "/", Host="evil.example")[0] == 403
    assert request(server, "/", Origin="http://evil.example")[0] == 403
    assert request(server, "/", **{"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert request(server, "/")[0] == 200
    assert b"Export displayed PNG" in request(server, "/")[1]
    assert b"X-Observer-Token" in request(server, "/app.js")[1]
    assert request(server, "/api/record/start", "POST", "{}")[0] == 403
    origin = f"http://127.0.0.1:{server.server_port}"
    headers = {"Origin": origin, "X-Observer-Token": app.token, "Content-Type": "application/json"}
    assert request(server, "/api/record/start", "POST", "{}", **headers)[0] == 409  # saved mode
    for path in ("/download/../../etc/passwd", "/frame/../escape.png",
                 "/api/state?source=/etc/passwd", "/api/frames/-1"):
        assert request(server, path)[0] in {400, 404}
    with pytest.raises(ValueError, match="loopback"):
        viewer.Server(("0.0.0.0", 0), app)


def test_http_preview_screenshot_provenance_registered_download_and_replacement(http_server):
    server, app = http_server
    state = json.loads(request(server, "/api/state")[1])
    key = state["frame_key"]
    assert request(server, "/preview.png")[1] == png()
    assert request(server, f"/frame/{key}.png")[1] == png()
    origin = f"http://127.0.0.1:{server.server_port}"
    headers = {"Origin": origin, "X-Observer-Token": app.token, "Content-Type": "application/json"}
    status, data, _ = request(server, "/api/screenshot", "POST", json.dumps({"frame": key}), **headers)
    assert status == 202
    shot = json.loads(data)
    status, pixels, response_headers = request(server, shot["download"])
    assert status == 200 and pixels == png() and response_headers["Content-Type"] == "image/png"
    metadata = json.loads(request(server, shot["metadata"])[1])
    assert metadata["frames"][0]["frame_id"] == "fixture-1"
    registered = app.downloads[shot["download"].split("/")[-1]]
    try:
        registered["path"].write_bytes(b"replacement")
        assert request(server, shot["download"])[0] == 400
    finally:
        registered["path"].write_bytes(png())


def test_actual_ui_start_stop_download_mp4(http_server, source):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("optional system ffmpeg/ffprobe unavailable")
    server, app = http_server
    source, publisher = source
    source.live = True
    origin = f"http://127.0.0.1:{server.server_port}"
    headers = {"Origin": origin, "X-Observer-Token": app.token, "Content-Type": "application/json"}
    assert request(server, "/api/record/start", "POST", "{}", **headers)[0] == 202
    publisher.publish(native_frame(source.directory, 2, rgb=(20, 200, 60)))
    assert request(server, "/api/record/stop", "POST", "{}", **headers)[0] == 202
    wait_for(lambda: app.recording.snapshot()["state"] not in {"recording", "encoding"})
    assert app.recording.snapshot()["state"] == "saved"
    state = json.loads(request(server, "/api/state")[1])
    status, data, response_headers = request(server, state["recording"]["download"])
    assert status == 200 and data[4:8] == b"ftyp"
    assert response_headers["Content-Type"] == "video/mp4"
    metadata = json.loads(request(server, state["recording"]["metadata"])[1])
    assert [row["frame_id"] for row in metadata["frames"]] == ["fixture-1", "fixture-2"]


def test_duplicate_host_and_origin_rejected(http_server):
    server, _ = http_server
    for duplicate in ("Host", "Origin"):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        connection.putrequest("GET", "/")
        connection.putheader(duplicate, f"127.0.0.1:{server.server_port}" if duplicate == "Host" else "http://evil")
        if duplicate == "Origin":
            connection.putheader("Origin", f"http://127.0.0.1:{server.server_port}")
        connection.endheaders()
        assert connection.getresponse().status == 403
        connection.close()
