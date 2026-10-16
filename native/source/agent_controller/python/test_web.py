import queue
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from veloren_controller import ActionResult, BridgeTimeoutError
from veloren_controller.web import create_app


class FakeController:
    def __init__(self) -> None:
        self.is_running = True
        self.session_id = "web-test-session"
        self.actions: list[tuple[dict, dict]] = []

    def step(self, action: dict, **options: object) -> ActionResult:
        self.actions.append((action, options))
        return ActionResult(
            action_id=str(options.get("action_id") or "generated"),
            action_type=str(action["type"]),
            status="completed",
            started_tick=10,
            finished_tick=11,
            observation={"tick": 11},
        )


class FakeTelemetry:
    def __init__(self, controller: FakeController) -> None:
        self.is_running = True
        self.controller = controller
        self.messages: queue.Queue[dict] = queue.Queue()
        self.recording_path: Path | None = None

    def snapshot(self) -> dict:
        return {
            "telemetry_schema_version": "1.0",
            "session_id": self.controller.session_id,
            "latest_frame": {"sequence": 1},
            "trajectory": [],
            "event_history": [],
            "stats": {},
        }

    def get_message(self, timeout: float | None = None) -> dict:
        try:
            return self.messages.get(timeout=timeout)
        except queue.Empty as error:
            raise BridgeTimeoutError("no telemetry") from error

    def start_recording(self, path: Path, *, append: bool = False) -> None:
        if self.recording_path is not None:
            raise RuntimeError("already recording")
        self.recording_path = path

    def stop_recording(self) -> None:
        self.recording_path = None


class FakeRuntime:
    def __init__(self, directory: Path) -> None:
        self.controller = FakeController()
        self.telemetry = FakeTelemetry(self.controller)
        self.recording_directory = directory
        self.is_running = True

    def require_controller(self) -> FakeController:
        if not self.is_running:
            raise RuntimeError("stopped")
        return self.controller

    def require_telemetry(self) -> FakeTelemetry:
        if not self.is_running:
            raise RuntimeError("stopped")
        return self.telemetry

    def health(self) -> dict:
        return {
            "web_api_version": "0.1",
            "status": "running" if self.is_running else "stopped",
            "controller_running": self.is_running,
            "telemetry_running": self.is_running,
            "session_id": self.controller.session_id,
            "started_at": 1.0,
        }

    def recording_path(self, filename: str | None) -> Path:
        return self.recording_directory / (filename or "generated.jsonl")


class WebApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.runtime = FakeRuntime(Path(self.directory.name))
        self.app = create_app(
            self.runtime,
            auto_start=False,
            control_token="secret",
        )
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def test_health_and_state(self) -> None:
        health = self.client.get("/api/health")
        state = self.client.get("/api/state")

        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["status"], "running")
        self.assertEqual(state.status_code, 200)
        self.assertEqual(state.json()["latest_frame"]["sequence"], 1)

    def test_dashboard_and_static_assets_are_served(self) -> None:
        dashboard = self.client.get("/")
        script = self.client.get("/static/app.js")
        stylesheet = self.client.get("/static/styles.css")

        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("Veloren Agent Console", dashboard.text)
        self.assertIn("怎么选目标", dashboard.text)
        self.assertIn("perception-target", dashboard.text)
        self.assertIn('data-action="attack"', dashboard.text)
        self.assertIn('data-action="pickup"', dashboard.text)
        self.assertIn('data-action="mine"', dashboard.text)
        self.assertIn('data-inv-action="use_item"', dashboard.text)
        self.assertEqual(script.status_code, 200)
        self.assertIn("connectWebSocket", script.text)
        self.assertIn("buildActionPayload", script.text)
        self.assertEqual(stylesheet.status_code, 200)
        self.assertIn("action-grid", stylesheet.text)
        self.assertIn("Content-Security-Policy", dashboard.headers)
        self.assertEqual(
            dashboard.headers["X-Content-Type-Options"],
            "nosniff",
        )

    def test_action_requires_token_and_returns_result(self) -> None:
        payload = {
            "action": {"type": "move_forward", "speed": 0.5},
            "action_id": "web-action",
            "duration_ticks": 30,
        }
        unauthorized = self.client.post("/api/action", json=payload)
        accepted = self.client.post(
            "/api/action",
            json=payload,
            headers={"X-Agent-Token": "secret"},
        )

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["action_id"], "web-action")
        self.assertEqual(self.runtime.controller.actions[0][0]["type"], "move_forward")

    def test_invalid_action_is_rejected(self) -> None:
        response = self.client.post(
            "/api/action",
            json={"action": {}},
            headers={"X-Agent-Token": "secret"},
        )
        self.assertEqual(response.status_code, 422)

    def test_recording_is_confined_to_recording_directory(self) -> None:
        invalid = self.client.post(
            "/api/recording/start",
            json={"filename": "../outside.jsonl"},
            headers={"X-Agent-Token": "secret"},
        )
        started = self.client.post(
            "/api/recording/start",
            json={"filename": "session.jsonl"},
            headers={"X-Agent-Token": "secret"},
        )
        stopped = self.client.post(
            "/api/recording/stop",
            headers={"X-Agent-Token": "secret"},
        )

        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(started.status_code, 200)
        self.assertEqual(Path(started.json()["path"]).name, "session.jsonl")
        self.assertEqual(stopped.status_code, 200)

    def test_websocket_receives_snapshot_and_live_frame(self) -> None:
        with self.client.websocket_connect("/ws") as websocket:
            snapshot = websocket.receive_json()
            self.runtime.telemetry.messages.put(
                {"type": "telemetry_frame", "sequence": 2}
            )
            frame = websocket.receive_json()

        self.assertEqual(snapshot["type"], "telemetry_snapshot")
        self.assertEqual(frame["sequence"], 2)


if __name__ == "__main__":
    unittest.main()
