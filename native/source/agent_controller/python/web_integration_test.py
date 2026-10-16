import tempfile
import time
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from veloren_controller import VelorenController
from veloren_controller.web import ControllerRuntime, create_app


username = f"web_agent_{uuid.uuid4().hex[:8]}"


def controller_factory() -> VelorenController:
    return VelorenController(username=username, character="agent")


with tempfile.TemporaryDirectory() as directory:
    runtime = ControllerRuntime(
        controller_factory,
        sample_hz=10,
        recording_directory=directory,
    )
    app = create_app(runtime, control_token="integration-secret")

    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200, health.text
        assert health.json()["status"] == "running", health.json()

        deadline = time.monotonic() + 5
        while True:
            state = client.get("/api/state")
            assert state.status_code == 200, state.text
            if state.json()["latest_frame"] is not None:
                break
            if time.monotonic() >= deadline:
                raise AssertionError("telemetry did not produce an initial frame")
            time.sleep(0.05)

        action = client.post(
            "/api/action",
            json={
                "action": {"type": "move_forward", "speed": 0.5},
                "duration_ticks": 10,
            },
            headers={"X-Agent-Token": "integration-secret"},
        )
        assert action.status_code == 200, action.text
        assert action.json()["status"] == "completed", action.json()

        recording = client.post(
            "/api/recording/start",
            json={"filename": "integration.jsonl"},
            headers={"X-Agent-Token": "integration-secret"},
        )
        assert recording.status_code == 200, recording.text

        with client.websocket_connect("/ws") as websocket:
            snapshot = websocket.receive_json()
            frame = websocket.receive_json()
            assert snapshot["type"] == "telemetry_snapshot", snapshot
            assert frame["type"] == "telemetry_frame", frame

        stopped = client.post(
            "/api/recording/stop",
            headers={"X-Agent-Token": "integration-secret"},
        )
        assert stopped.status_code == 200, stopped.text

    recording_path = Path(directory) / "integration.jsonl"
    assert recording_path.exists()
    assert recording_path.stat().st_size > 0

    print(
        {
            "session_id": health.json()["session_id"],
            "action_status": action.json()["status"],
            "telemetry_sequence": frame["sequence"],
            "recording_bytes": recording_path.stat().st_size,
        }
    )
