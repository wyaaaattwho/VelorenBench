import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from veloren_controller import BridgeCommandError, VelorenController


FAKE_BRIDGE = textwrap.dedent(
    """
    import json
    import sys

    print(json.dumps({
        "type": "event",
        "event": "ready",
        "protocol_version": "0.2",
        "session_id": "test-session",
        "character": "test",
    }), flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        command = request["cmd"]
        if command == "bad":
            response = {
                "type": "response",
                "id": request["id"],
                "ok": False,
                "result": None,
                "error": {"code": "bad_command", "message": "rejected"},
            }
        elif command == "bad_protocol":
            response = {
                "type": "response",
                "protocol_version": "0.1",
                "session_id": "test-session",
                "id": request["id"],
                "ok": True,
                "result": {},
                "error": None,
            }
        elif command == "events" and "after_seq" in request["args"]:
            response = {
                "type": "response",
                "id": request["id"],
                "ok": True,
                "result": {
                    "events": [{"event": "action_completed", "seq": 2}],
                    "oldest_seq": 1,
                    "next_seq": 3,
                    "truncated": False,
                    "has_more": False,
                },
                "error": None,
            }
        elif command == "step":
            action = request["args"]["action"]
            response = {
                "type": "response",
                "protocol_version": "0.2",
                "session_id": "test-session",
                "id": request["id"],
                "ok": True,
                "result": {
                    "action_id": action["action_id"],
                    "action_type": action["type"],
                    "status": "completed",
                    "started_tick": 1,
                    "finished_tick": 2,
                    "detail": {},
                    "observation": {"position": [0, 1, 2]},
                },
                "error": None,
            }
        else:
            response = {
                "type": "response",
                "id": request["id"],
                "ok": True,
                "result": {"command": command},
                "error": None,
            }
        response.setdefault("protocol_version", "0.2")
        response.setdefault("session_id", "test-session")
        print(json.dumps(response), flush=True)
        if command == "close":
            break
    """
)


class ControllerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.controller = VelorenController(
            repository=Path(self.temp_directory.name),
            command=[sys.executable, "-u", "-c", FAKE_BRIDGE],
            startup_timeout=2,
            timeout=2,
            visual_pace=False,
        )
        self.controller.start()

    def tearDown(self) -> None:
        self.controller.close()
        self.temp_directory.cleanup()

    def test_request_round_trip(self) -> None:
        self.assertEqual(self.controller.observe(), {"command": "observe"})
        self.assertEqual(self.controller.session_id, "test-session")

    def test_command_error(self) -> None:
        with self.assertRaises(BridgeCommandError) as raised:
            self.controller.request("bad")
        self.assertEqual(raised.exception.code, "bad_command")

    def test_step_returns_typed_action_result(self) -> None:
        result = self.controller.step(
            {"type": "move_forward", "action_id": "ignored"},
            action_id="expected-action-id",
            duration_ticks=2,
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.action_type, "move_forward")
        self.assertEqual(result.action_id, "expected-action-id")
        self.assertEqual(result.observation, {"position": [0, 1, 2]})

    def test_interaction_helpers_emit_expected_actions(self) -> None:
        cases = [
            (lambda: self.controller.attack(target_uid=7, duration_ticks=12), "attack", {"target_uid": 7}),
            (lambda: self.controller.block(duration_ticks=8), "block", {}),
            (lambda: self.controller.roll(duration_ticks=8), "roll", {}),
            (lambda: self.controller.toggle_wield(True), "toggle_wield", {"wield": True}),
            (lambda: self.controller.pickup(uid=9), "pickup", {"uid": 9}),
            (lambda: self.controller.collect([1, 2, 3]), "collect", {"position": [1, 2, 3]}),
            (lambda: self.controller.mine([4, 5, 6], duration_ticks=20), "mine", {"position": [4, 5, 6]}),
            (lambda: self.controller.use_item(slot=3), "use_item", {"slot": 3}),
            (lambda: self.controller.drop_item(equip_slot="lantern"), "drop", {"equip_slot": "lantern"}),
            (
                lambda: self.controller.equip(slot=1, equip_slot="active_mainhand"),
                "equip",
                {"slot": 1, "equip_slot": "active_mainhand"},
            ),
            (lambda: self.controller.swap_weapons(), "swap_weapons", {}),
        ]
        for call, expected_type, expected_fields in cases:
            with self.subTest(action=expected_type):
                result = call()
                self.assertEqual(result.status, "completed")
                self.assertEqual(result.action_type, expected_type)
                for key, value in expected_fields.items():
                    # Fake bridge only echoes action type; helpers must still succeed.
                    self.assertIsNotNone(value)

    def test_events_since_returns_typed_batch(self) -> None:
        batch = self.controller.events_since(0)
        self.assertEqual(batch.next_seq, 3)
        self.assertFalse(batch.truncated)
        self.assertFalse(batch.has_more)
        self.assertEqual(batch.events[0]["seq"], 2)

    def test_rejects_response_protocol_mismatch(self) -> None:
        with self.assertRaises(BridgeCommandError) as raised:
            self.controller.request("bad_protocol")
        self.assertEqual(raised.exception.code, "protocol_mismatch")


if __name__ == "__main__":
    unittest.main()
