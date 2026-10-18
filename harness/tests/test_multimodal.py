import asyncio
import base64
import hashlib
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
import zlib

from scripts.rpg_benchmark import capabilities, observations, providers
from scripts.rpg_benchmark.player_types import Image, Invocation, Result
from scripts.rpg_benchmark.renderer import RendererBoundary, temporal_artifact
from scripts.rpg_benchmark.renderer_tools import RendererTools
from scripts.rpg_benchmark.responses import content, image_identities, ResponsesProvider, play
from scripts.rpg_benchmark.responses_adapter import Adapter
from scripts.rpg_benchmark.timing import AttemptTiming, stamp
from scripts.rpg_benchmark.native import SocketEndpoint


def png():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0\xff\0\0\0\xff\0")) + chunk(b"IEND", b""))


class Evidence:
    def __init__(self):
        self.rows = []

    def write(self, kind, **row):
        self.rows.append({"kind": kind, **row})


class VisibilityTests(unittest.TestCase):
    def test_all_receipts_errors_events_drop_private_state(self):
        for field in ("result", "data"):
            for late in (True, False):
                value = {"type": "event" if field == "data" else "response",
                         field: {"status": "completed", "observation": {"secret": "LEAK"},
                                 "detail": "LEAK", "frame_schema_version": "1.0"},
                         "late": late, "private": "LEAK",
                         "error": {"code": "known_code", "message": "LEAK", "detail": "LEAK"}}
                projected = observations.project_image(value)
                self.assertNotIn("LEAK", json.dumps(projected))
                self.assertEqual(projected[field], {"status": "completed"})
        self.assertIsNone(observations.project_image({"result": None})["result"])

    def test_image_denies_hidden_resolution_and_private_queries(self):
        for command in ("observe", "events", "health", "goal_recipes", "inspect_map",
                        "inspect_terrain", "private_observe", "capabilities"):
            self.assertFalse(observations.image_allowed({"cmd": command, "args": {"private": True}}))
        for name in ("craft", "pickup", "equip", "use_item", "move_to", "attack"):
            self.assertFalse(observations.image_allowed({"cmd": "step", "args": {"action": {"type": name}}}))
        for key in ("target", "destination", "movement_intent"):
            self.assertFalse(observations.image_allowed({"cmd": "set_control_state", "args": {key: "hidden"}}))
        self.assertTrue(observations.image_allowed({"cmd": "step", "args": {"action": {"type": "sit"}}}))

    def test_generic_negotiation_requires_real_transport_and_profile(self):
        args = SimpleNamespace(adapter_config=json.dumps({"mock_provider": "http://127.0.0.1:1234", "provider": providers.LUNA}),
                               observation_mode="image", action_profile=None, model=None,
                               reasoning_effort=None, recordings=Path("/tmp/outputs/recordings/test"))
        adapter = Adapter(args)
        self.assertEqual(adapter.configuration["action_profile"], "visual-ui-v1")
        manifest = json.loads(json.dumps(adapter.manifest))
        del manifest["capabilities"]["image_transport"]
        with self.assertRaises(ValueError):
            capabilities.negotiate(manifest, {})
        with self.assertRaises(ValueError):
            capabilities.negotiate(adapter.manifest, {"action_profile": "ordinary-renderer-native-v1"})

    def test_png_hash_dimensions_crc_and_truncation(self):
        data = png()
        observations.verify_png(data, 2, 1)
        for bad in (data[:-1], data + b"suffix", data[:32] + b"X" + data[33:]):
            with self.assertRaises(ValueError):
                observations.verify_png(bad, 2, 1)
        with self.assertRaises(ValueError):
            observations.verify_png(data, 1, 1)

    def test_private_artifact_never_calls_client_counter_server_tick(self):
        original = {"opening_validation": {"native_tick": 42}, "frame": {"native_tick": None}}
        actual = temporal_artifact(original)
        self.assertEqual(actual["opening_validation"], {"native_tick": None, "client_update_counter": 42})
        self.assertEqual(actual["frame"], {"native_tick": None})
        self.assertEqual(original["opening_validation"]["native_tick"], 42)

    def test_actual_image_block_not_text_or_filename(self):
        data = png()
        image = Image(data, "image/png", hashlib.sha256(data).hexdigest(), 2, 1)
        items = content(Result('{"frame":"f"}', images=(image,)), "call-1")
        self.assertEqual(items[0]["output"], '{"frame":"f"}')
        block = items[1]["content"][1]
        self.assertEqual(block["type"], "input_image")
        self.assertEqual(base64.b64decode(block["image_url"].split(",")[1]), data)
        self.assertEqual(image_identities(items)[0]["sha256"], image.sha256)

    def test_adapter_entry_uses_shared_valid_lifecycle_stamp(self):
        args = SimpleNamespace(adapter_config=json.dumps({"mock_provider": "http://127.0.0.1:1234", "provider": providers.LUNA}),
                               observation_mode="image", action_profile=None, model=None,
                               reasoning_effort=None, recordings=Path("/tmp/outputs/recordings/test"))
        adapter = Adapter(args)
        with tempfile.TemporaryDirectory() as temp:
            adapter.output = Path(temp)
            timing = AttemptTiming()
            (adapter.output / "harness-entry.json").write_text(json.dumps(stamp()))
            adapter.observe_entry(timing)
            self.assertIn("harness_entry", timing.points)


class ToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.evidence = Evidence()
        self.native = SimpleNamespace(ready={
            "session_id": "session", "uid": 1, "capabilities": {"LEAK": "hidden"},
            "renderer": {"actor": {"uid": 1, "lifecycle": "life"}, "control": {"generation": 0},
                         "unexpected_private": "LEAK"}},
            submissions=0)
        self.tools = RendererTools(self.native, self.evidence, "image", Path("/unused"), "ep")

    async def test_initial_contract_and_tools_contain_no_hidden_ready(self):
        value = await self.tools.handler("contract")(Invocation("s", "c", "game_contract", {}))
        self.assertNotIn("LEAK", value.text_result_for_llm)
        names = {tool.name for tool in self.tools.tools}
        self.assertNotIn("game_observe", names)
        self.assertNotIn("game_health", names)
        with self.assertRaises(ValueError):
            self.tools.validate_request("observe", {"private": True})

    async def test_control_help_identifies_each_modes_public_token_source(self):
        for mode in ("structured", "hybrid", "image"):
            with self.subTest(mode=mode):
                tools = RendererTools(self.native, self.evidence, mode, Path("/unused"), "ep")
                offered = {tool.name: tool for tool in tools.tools}
                fence = offered["game_step"].parameters["properties"]["control_fence"]
                for help_text in (fence["description"],
                                  offered["game_set_control_state"].description):
                    self.assertIn("structured/hybrid", help_text)
                    self.assertIn("image-only frame", help_text)
                self.assertNotIn("observe/map remain available", offered["game_step"].description)
                self.assertIn(
                    "expected_protection_generation to the observed protection_generation",
                    offered["game_set_control_state"].description)
                self.assertEqual("game_observe" in offered, mode != "image")

    async def test_late_async_exception_is_not_model_state_or_retry(self):
        async def failure():
            raise RuntimeError("LEAK position health inventory target answer")
        task = asyncio.create_task(failure())
        await asyncio.sleep(0)
        request = {"id": "original", "args": {"action": {"action_id": "original-action"}}}
        self.tools.pending["original"] = (request, task)
        for _ in range(2):
            value = await self.tools.handler("result")(
                Invocation("s", "c", "game_result", {"request_id": "original", "wait": True}))
            self.assertEqual(value.result_type, "failure")
            self.assertNotIn("LEAK", value.text_result_for_llm)
            self.assertNotIn("LEAK", value.error)
        self.assertEqual(self.native.submissions, 0)

    async def test_frame_identity_original_transform_and_capture_intervals(self):
        data = png()
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            path = directory / "frame.png"
            path.write_bytes(data)
            frame = {
                "frame_schema_version": "1.0", "frame_id": "one", "session_id": "session",
                "actor": self.native.ready["renderer"]["actor"], "native_tick": None,
                "capture_host": {"begin_ns": "200", "readback_complete_ns": "240"},
                "correlation": {"action_effect_ordering": "unconfirmed"},
                "image": {"path": str(path), "mime_type": "image/png",
                          "sha256": hashlib.sha256(data).hexdigest(), "byte_length": len(data),
                          "width": 2, "height": 1, "model_attachment_delivered": False}}
            envelope, image = observations.read_frame(
                frame, directory, "session", frame["actor"], "ep", 2, 100)
            self.assertEqual(envelope["timing"]["capture_begin_interval_ns"], "100")
            self.assertEqual(envelope["model_image"]["original_sha256"], image.sha256)
            self.assertEqual(envelope["model_image"]["transformed_sha256"], image.sha256)
            self.assertNotIn(str(path), json.dumps(envelope))
            frame["image"]["sha256"] = "0" * 64
            with self.assertRaises(ValueError):
                observations.read_frame(frame, directory, "session", frame["actor"], "ep", 3, 100)


class BoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_socket_denies_private_queries_and_projects_late_errors(self):
        calls = []
        ready = asyncio.get_running_loop().create_future()
        ready.set_result({"type": "event", "event": "ready", "session_id": "s", "uid": 1,
                          "protocol_version": "0.2", "capabilities": {"LEAK": "hidden"}})
        renderer_ready = asyncio.get_running_loop().create_future()
        actor = {"uid": 1, "lifecycle": "life"}
        renderer_ready.set_result({"data": {"actor": actor, "actuator": {
            "control": {"owner": "", "generation": 0, "sequence": 0, "protection_generation": 0,
                        "LEAK": "hidden"}}, "ui_panels": ["world", "inventory", "equipment", "map"],
                        "LEAK": "hidden"}})
        class Journal:
            def write(self, **row):
                pass
        async def envelope(request, **kwargs):
            calls.append(request)
            await asyncio.sleep(.02)
            return {"type": "response", "id": request["id"], "protocol_version": "0.2",
                    "session_id": "s", "ok": True,
                    "result": {"status": "completed", "observation": {"LEAK": "hidden"},
                               "frame_schema_version": "1.0", "detail": "LEAK"}}
        bridge = SimpleNamespace(ready=ready, renderer_ready=renderer_ready, envelope=envelope,
                                 journal=Journal())
        with tempfile.TemporaryDirectory() as temp:
            endpoint = SocketEndpoint(Path(temp))
            boundary = RendererBoundary(bridge, endpoint.path, "image")
            await boundary.start()
            reader, writer = await asyncio.open_unix_connection(str(endpoint.path))
            try:
                initial = json.loads(await reader.readline())
                self.assertNotIn("LEAK", json.dumps(initial))
                commands = ["observe", "private_observe", "events", "health", "inspect_map", "goal_recipes", "ping"]
                for command in commands:
                    writer.write((json.dumps({"id": command, "cmd": command, "protocol_version": "0.2",
                                              "args": {}}) + "\n").encode())
                    await writer.drain()
                    response = json.loads(await reader.readline())
                    self.assertNotIn("LEAK", json.dumps(response))
                    self.assertEqual(response["ok"], command == "ping")
                self.assertEqual([row["cmd"] for row in calls], ["ping"])
                error = boundary.public_response({
                    "ok": False, "result": {"LEAK": "hidden"},
                    "error": {"code": "stale_session", "message": "LEAK", "nested": {"LEAK": "hidden"}}},
                    {"cmd": "step"})
                self.assertNotIn("LEAK", json.dumps(error))
            finally:
                writer.close()
                await writer.wait_closed()
                await boundary.close()
                endpoint.close()


if __name__ == "__main__":
    unittest.main()
