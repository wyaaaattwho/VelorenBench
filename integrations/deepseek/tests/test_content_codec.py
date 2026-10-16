"""Lossless text and typed integer wire projection; no runtime/world required."""
import asyncio
import copy
import json
import math
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from dsh_external_v1.dsh_schema import native_arguments, provider_schema
from dsh_external_v1.adapter import collect_evidence
from dsh_external_v1.dsh_tools import PlayerTools, validate_public_text
from scripts import rpg_sdk_agent as common
from scripts.rpg_benchmark.player_types import Result

BIG = 5340282195752818211


class ContentCodecTests(unittest.TestCase):
    def test_collector_verifies_and_preserves_exact_integer_translation(self):
        schema = common.tool_schema("step")
        wire = {"action": {"type": "jump", "action_id": "fixed"},
                "control_fence": {"expected_session_id": "s", "generation": str(BIG),
                                  "actor": {"uid": str(BIG), "lifecycle": "l"}}}
        arguments = native_arguments(schema, wire)
        call = {"kind": "dsh_jsonrpc", "envelope": {"method": "session.event", "params": {
            "event": {"type": "tool/call", "data": {
                "callId": "call", "name": "game_step", "arguments": json.dumps(wire)}}}}}
        mapping = {"kind": "dsh_common_tool_mapping", "dsh_call_id": "call",
                   "tool_name": "game_step", "wire_arguments": wire,
                   "native_arguments": arguments, "argument_schema": schema}
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            (out / "entry.json").write_text(json.dumps({
                "status": "idle", "cleanup_errors": [],
                "runtime_cleanup": {"owned_processes": [], "stopped": True}}))
            context = {"output": directory, "adapter_id": "deepseek-harness",
                       "player_interface": common.INTERFACE_VERSION}
            (out / "events.ndjson").write_text(
                "\n".join(json.dumps(row) for row in (call, mapping)) + "\n")
            observed = collect_evidence(context)["tool_calls"][0]
            self.assertEqual(observed["arguments"], arguments)
            self.assertEqual(observed["wire_arguments"], wire)
            for field, value in (("wire_arguments", {}), ("native_arguments", {}),
                                 ("tool_name", "game_observe")):
                with self.subTest(field=field):
                    changed = {**mapping, field: value}
                    (out / "events.ndjson").write_text(
                        "\n".join(json.dumps(row) for row in (call, changed)) + "\n")
                    with self.assertRaises(ValueError):
                        collect_evidence(context)

    def test_text_is_validated_not_normalized_or_parsed_into_output(self):
        text = '{"world_xy":[1.0,-0.0],"instance":5340282195752818211}'
        validate_public_text(text)
        evidence = SimpleNamespace(write=lambda *a, **k: None)
        native = SimpleNamespace(evidence=evidence, ready={
            "session_id": "s", "renderer": {"actor": {"uid": 1, "lifecycle": "l"},
                                         "control": {"generation": 0}}})
        tools = PlayerTools(native, {
            "observation_mode": "structured", "action_profile": "ordinary-renderer-native-v1",
        }, frames=Path("/unused"), episode="synthetic")

        async def result(_):
            return Result(text)
        tools.handlers["game_observe"] = result
        row = asyncio.run(tools.execute({
            "id": "transport", "name": "game_observe", "arguments": {},
            "context": {"harness": "official-deepseek-harness", "session_id": "s",
                        "call_id": "actual-synthetic-call"},
        }))
        self.assertEqual(row["content"], [{"type": "text", "text": text}])
        self.assertNotIn("result", row, "No numeric shadow payload may cross the JS boundary")

    def test_malformed_and_nonfinite_json_remain_explicit_errors(self):
        for text in ('{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}', '{"x":1e999}',
                     '{"x":', 'undefined'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                validate_public_text(text)
        for text in ('{"x":-0.0}', '{"x":18446744073709551615}', '[null,true,1e-99]'):
            validate_public_text(text)

    def test_exact_integer_schema_projection_and_native_types(self):
        schema = common.tool_schema("step")
        original = copy.deepcopy(schema)
        projected, changes = provider_schema(schema)
        self.assertEqual(schema, original)
        self.assertTrue(changes)
        uid = projected["properties"]["control_fence"]["properties"]["actor"]["properties"]["uid"]
        self.assertEqual(uid["oneOf"], [{"type": "integer"}, {"type": "string"}])
        for value in (BIG, 2**64 - 1):
            args = {"action": {"type": "jump", "action_id": "fixed"},
                    "control_fence": {"expected_session_id": "s",
                                      "actor": {"uid": str(value), "lifecycle": "l"},
                                      "generation": str(value)}}
            result = native_arguments(schema, args)
            self.assertIs(type(result["control_fence"]["actor"]["uid"]), int)
            self.assertEqual(result["control_fence"]["actor"]["uid"], value)
            self.assertEqual(args["control_fence"]["actor"]["uid"], str(value))
            common.validate_request("step", result)

    def test_invalid_integer_tokens_and_unsafe_numbers_never_become_native_integers(self):
        schema = common.ENTITY_REF
        for value in (BIG, float(BIG), True, "-0", "+1", "01", "1.0", "1e3", " 1", "１",
                      str(2**64), "-1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                native_arguments(schema, {"uid": value, "lifecycle": "l"})
        self.assertEqual(native_arguments(schema, {"uid": 42, "lifecycle": "l"})["uid"], 42)
        with self.assertRaises(ValueError):
            native_arguments(common.tool_schema("step"), {
                "action": {"type": "jump"}, "duration_ticks": "1"})

    def test_typed_action_union_arrays_nulls_and_native_fences_are_unchanged(self):
        schema = common.tool_schema("step")
        for args in (
            {"action": {"type": "jump", "unknown": "1"}},
            {"action": {"type": "not_a_native_action"}},
            {"action": {"type": "jump"}, "control_fence": {"actor": {"uid": "42"}}},
            {"action": {"type": "jump"}, "duration_ticks": True},
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                native_arguments(schema, args)
        schema = {"type": "array", "minItems": 1, "maxItems": 2,
                  "items": {"anyOf": [{"type": "integer", "minimum": 0,
                                      "maximum": 2**64 - 1}, {"type": "null"}]}}
        self.assertEqual(native_arguments(schema, [str(BIG), None]), [BIG, None])
        number = {"type": "number", "minimum": -1, "maximum": 1}
        self.assertTrue(math.copysign(1, native_arguments(number, -0.0)) < 0)
        with self.assertRaises(ValueError):
            native_arguments(number, "0")


if __name__ == "__main__":
    unittest.main()
