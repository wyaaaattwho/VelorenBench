"""Synthetic peers only: actual official Cordis/stdio/pi-ai/model-input path."""
import asyncio
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import struct
import unittest
from uuid import uuid4
import zlib

from aiohttp import web

from dsh_external_v1.dsh_agent import (
    EFFORT, MODEL, PROVIDER, OfficialRuntime, identity_gone, prepare_profile,
)
from dsh_external_v1.dsh_native_protocol import Evidence, NativeError
from dsh_external_v1.dsh_tools import PlayerTools, ToolBroker
from dsh_external_v1.adapter import collect_evidence
from dsh_external_v1 import dsh_tools
from scripts.rpg_benchmark import external

BIG = 5340282195752818211
OUTPUT = Path(os.environ["DSH_OBSERVATION_TEST_OUTPUT"])
RUNTIME = Path(os.environ["DSH_RUNTIME"])


def png():
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data)))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0\xff\0\0\0\xff\0")) + chunk(b"IEND", b""))


class Native:
    """Recorded public geometry plus synthetic IDs; no world process or evaluator."""
    def __init__(self, evidence, frames, *, signed_zero=True, refusal=False):
        self.evidence, self.frames = evidence, frames
        self.refusal = refusal
        self.ready = {
            "session_id": "synthetic-native", "uid": BIG,
            "renderer": {"actor": {"uid": BIG, "lifecycle": "life"},
                         "control": {"generation": BIG}},
            "capabilities": {"private": "PRIVATE_SENTINEL"},
        }
        self.observation = {
            "self_identity": copy.deepcopy(self.ready["renderer"]["actor"]),
            "continuous_control": {"generation": BIG},
            "local_geometry": {"geometry": {"directions": [
                {"direction": "right", "world_xy": [1.0, -0.0 if signed_zero else 0.0]},
                {"direction": "back", "world_xy": [-0.0 if signed_zero else 0.0, -1.0]},
            ]}},
            "combat_perception": {"self": {"last_health_change": {"instance": BIG}}},
            "public_extra": {"all_fields": [True, None, "public"]},
        }
        self.requests = []
        self.tasks = []
        self.release = asyncio.Event()

    def submit(self, cmd, args):
        self.request_validator(cmd, args)
        request = {"id": uuid4().hex, "cmd": cmd, "args": copy.deepcopy(args)}
        self.requests.append(request)
        self.evidence.write("synthetic_native_request", request=request)

        async def respond():
            if cmd == "step":
                await self.release.wait()
            response = {"id": request["id"], "session_id": self.ready["session_id"],
                        "ok": True, "result": copy.deepcopy(self.observation)}
            if cmd == "step":
                response["result"]["status"] = "completed"
                response["result"]["action_id"] = args["action"]["action_id"]
                expected = {"generation": BIG, "expected_session_id": "synthetic-native",
                            "actor": {"uid": BIG, "lifecycle": "life"}}
                if self.refusal or args.get("control_fence") != expected:
                    response["ok"] = False
                    response["error"] = {"code": "stale_control_fence",
                                         "message": "PRIVATE_SENTINEL"}
                    self.evidence.write("synthetic_native_response", response=response)
                    raise NativeError(response)
            self.evidence.write("synthetic_native_response", response=response)
            return response
        task = asyncio.create_task(respond())
        self.tasks.append(task)
        return request, task

    async def request(self, cmd, args):
        if cmd != "frame":
            return await self.submit(cmd, args)[1]
        self.request_validator(cmd, args)
        data = png()
        path = self.frames / "frame.png"
        path.write_bytes(data)
        return {"result": {
            "frame_schema_version": "1.0", "frame_id": "synthetic-frame",
            "session_id": self.ready["session_id"], "actor": self.ready["renderer"]["actor"],
            "native_tick": None,
            "capture_host": {"begin_ns": "200", "readback_complete_ns": "240"},
            "correlation": {"action_effect_ordering": "unconfirmed"},
            "image": {"path": str(path), "mime_type": "image/png",
                      "sha256": hashlib.sha256(data).hexdigest(), "byte_length": len(data),
                      "width": 2, "height": 1, "model_attachment_delivered": False},
        }}


class Peer:
    """Loopback SSE fixture, never connects to an account or external provider."""
    def __init__(self, calls, native):
        self.calls, self.native = calls, native
        self.requests = []

    async def start(self):
        app = web.Application()
        app.router.add_post("/responses", self.respond)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        return f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    async def respond(self, request):
        body = await request.json()
        self.requests.append(body)
        n = len(self.requests)
        if n <= len(self.calls):
            name, args = self.calls[n - 1]
            if callable(args):
                args = args(body)
            if name == "game_result":
                outputs = [x for x in body["input"] if x.get("type") == "function_call_output"]
                args = {**args, "request_id": json.loads(outputs[-1]["output"])["request_id"]}
                if args.get("wait"):
                    self.native.release.set()
            item = {"type": "function_call", "id": f"fc_{n}", "call_id": f"call_{n}",
                    "name": name, "arguments": json.dumps(args), "status": "completed"}
        else:
            item = {"type": "message", "id": "done", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": "Synthetic check finished.",
                                 "annotations": []}]}
        response = {"id": f"resp_{n}", "object": "response", "created_at": 0,
                    "status": "completed", "model": MODEL, "output": [item],
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                              "input_tokens_details": {"cached_tokens": 0},
                              "output_tokens_details": {"reasoning_tokens": 0}}}
        events = [
            {"type": "response.created",
             "response": {**response, "status": "in_progress", "output": []}},
            {"type": "response.output_item.added", "output_index": 0, "item": item},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": response},
        ]
        return web.Response(text="".join(f"data: {json.dumps(x)}\n\n" for x in events),
                            content_type="text/event-stream")


class OfficialObservationTests(unittest.IsolatedAsyncioTestCase):
    def step(self, *, uid=str(BIG)):
        return {"action": {"type": "jump", "action_id": "synthetic-action"},
                "duration_ticks": 1, "timeout_ticks": 2,
                "control_fence": {"generation": str(BIG),
                                  "expected_session_id": "synthetic-native",
                                  "actor": {"uid": uid, "lifecycle": "life"}}}

    async def run_loop(self, mode, calls, **native_options):
        out = OUTPUT / self.id().split(".")[-1]
        out.mkdir(parents=True, exist_ok=False)
        frames = out / "frames"
        frames.mkdir()
        evidence = Evidence(out / "events.ndjson")
        native = Native(evidence, frames, **native_options)
        tools = PlayerTools(native, {
            "observation_mode": mode, "action_profile": (
                "visual-motor-v1" if mode == "image" else "ordinary-renderer-native-v1"),
        }, frames=frames, episode="synthetic-episode")
        peer = Peer(calls, native)
        broker = ToolBroker(tools, Path("/tmp") / f"dsh-observation-{uuid4().hex}.sock")
        runtime = OfficialRuntime(out, evidence, RUNTIME)
        try:
            await broker.start()
            url = await peer.start()
            prepare_profile(out / "home", broker.path, url, session_id="synthetic-session")
            await runtime.start(out / "home", "loopback-only")
            initialized = await asyncio.wait_for(runtime.rpc("initialize", {
                "cwd": str(out / "work"), "provider": PROVIDER, "model": MODEL,
                "reasoningEffort": EFFORT}), 40)
            self.assertEqual(initialized["serverInfo"]["name"], "deepseek-harness-sdk-runtime")
            await runtime.rpc("session/prompt", {
                "sessionId": "synthetic-session", "contentBlocks": [
                    {"type": "text", "text": "Synthetic transport check; not a benchmark run."}]})
            await asyncio.wait_for(runtime.rpc("session/wait", {
                "sessionId": "synthetic-session"}), 40)
            self.assertEqual(runtime.finished_turn("synthetic-session")["kind"], "completed")
        finally:
            await runtime.close()
            await broker.close()
            if hasattr(peer, "runner"):
                await peer.runner.cleanup()
            native.release.set()
            await asyncio.gather(*native.tasks, return_exceptions=True)
            evidence.close()
            (out / "model-inputs.json").write_text(json.dumps(peer.requests, indent=2) + "\n")
            (out / "cleanup.json").write_text(json.dumps(runtime.cleanup, indent=2) + "\n")
            self.assertTrue(runtime.cleanup["stopped"])
            self.assertTrue(all(identity_gone(pid, ticks) for pid, ticks in runtime.owned.items()))
        events = [json.loads(x) for x in (out / "events.ndjson").read_text().splitlines()]
        results = [x["envelope"]["params"]["event"]["data"] for x in events
                   if x["kind"] == "dsh_jsonrpc"
                   and x["envelope"].get("method") == "session.event"
                   and x["envelope"]["params"]["event"]["type"] == "tool/result"]
        return out, native, peer.requests, results, events

    def assert_images(self, out, requests, events):
        admitted = [x for x in events if x["kind"] == "dsh_media_admitted"]
        self.assertEqual(len(admitted), 1)
        self.assertEqual(admitted[0]["source_images"], [
            {"sha256": hashlib.sha256(png()).hexdigest(), "byte_length": len(png())}])
        attachment = admitted[0]["attachments"][0]
        self.assertEqual(attachment["attachmentId"], "sha256:" + hashlib.sha256(png()).hexdigest())
        files = [p for p in (out / "home/attachments").rglob("*") if p.is_file()]
        self.assertTrue(any(p.read_bytes() == png() for p in files))
        encoded = base64.b64encode(png()).decode()
        image_inputs = []

        def walk(value):
            if isinstance(value, dict):
                if value.get("type") == "input_image":
                    image_inputs.append(value)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(requests)
        self.assertTrue(image_inputs, "Official pi-ai must send images, not base64 text")
        self.assertTrue(all(x["image_url"] == f"data:image/png;base64,{encoded}"
                            for x in image_inputs))
        text_inputs = []

        def text_only(value):
            if isinstance(value, dict):
                if value.get("type") in ("input_text", "text"):
                    text_inputs.append(value["text"])
                if value.get("type") == "function_call_output" and isinstance(value["output"], str):
                    text_inputs.append(value["output"])
                for child in value.values():
                    text_only(child)
            elif isinstance(value, list):
                for child in value:
                    text_only(child)
        text_only(requests)
        self.assertNotIn(encoded, json.dumps(text_inputs))
        # Official refs/previews may expose their own normalized object path;
        # the native source path must never cross the public boundary.
        self.assertNotIn(str(out / "frames/frame.png"), json.dumps(requests))

    async def test_signed_zero_and_identity_visible(self):
        _, native, requests, results, _ = await self.run_loop(
            "structured", [("game_observe", {})])
        self.assertNotIn("ToolOutputError", json.dumps(results))
        output = next(x["output"] for x in requests[-1]["input"]
                      if x.get("type") == "function_call_output")
        self.assertIn("-0.0", output)
        self.assertIn(str(BIG), output)
        self.assertEqual(json.loads(output)["response"]["result"], native.observation)

    async def test_identity_without_signed_zero(self):
        _, _, requests, results, _ = await self.run_loop(
            "structured", [("game_observe", {})], signed_zero=False)
        self.assertNotIn("ToolOutputError", json.dumps(results))
        output = next(x["output"] for x in requests[-1]["input"]
                      if x.get("type") == "function_call_output")
        self.assertIn(str(BIG), output)
        # Durable official value/history must not contain a rounded shadow payload.
        self.assertNotIn("5340282195752819000", json.dumps(results))

    async def test_hybrid_images_and_exact_integer_argument_roundtrip(self):
        def copy_observed_fence(body):
            output = next(x["output"] for x in body["input"]
                          if x.get("type") == "function_call_output" and x["call_id"] == "call_1")
            observed = json.loads(output)["response"]
            args = self.step(uid=str(observed["result"]["self_identity"]["uid"]))
            args["control_fence"]["generation"] = str(
                observed["result"]["continuous_control"]["generation"])
            args["control_fence"]["expected_session_id"] = observed["session_id"]
            args["control_fence"]["actor"]["lifecycle"] = observed["result"]["self_identity"]["lifecycle"]
            return args

        out, native, requests, results, events = await self.run_loop("hybrid", [
            ("game_observe", {}), ("game_frame", {}), ("game_step", copy_observed_fence),
            ("game_result", {"wait": True}),
        ])
        self.assertNotIn("ToolOutputError", json.dumps(results))
        self.assertTrue(all(not x["message"]["isError"] for x in results))
        submitted = [x for x in native.requests if x["cmd"] == "step"]
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0]["args"]["control_fence"], {
            "generation": BIG, "expected_session_id": "synthetic-native",
            "actor": {"uid": BIG, "lifecycle": "life"}})
        self.assertIs(type(submitted[0]["args"]["control_fence"]["actor"]["uid"]), int)
        mappings = [x for x in events if x["kind"] == "dsh_common_tool_mapping"]
        self.assertEqual(len(mappings), 4)
        self.assertEqual({x["dsh_call_id"] for x in mappings},
                         {x["message"]["toolCallId"] for x in results})
        self.assert_images(out, requests, events)
        (out / "entry.json").write_text(json.dumps({
            "status": "idle", "cleanup_errors": [],
            "runtime_cleanup": json.loads((out / "cleanup.json").read_text())}))
        facts = collect_evidence({
            "output": str(out), "adapter_id": "deepseek-harness",
            "player_interface": external.PLAYER})
        response = next(row["response"] for row in events
                        if row["kind"] == "synthetic_native_response"
                        and row["response"]["id"] == submitted[0]["id"])
        _, proof = external.adjudicate(
            facts, [{"request": submitted[0], "actor": BIG, "response": response}],
            {"session_id": "synthetic-native", "self_identity": {"uid": BIG}},
            {"success": False}, native_classification="model_stopped_without_goal",
            stop_reason="normal_entry_exited", stop_signal={})
        observed_call = next(
            row["envelope"]["params"]["event"]["data"]
            for row in events if row["kind"] == "dsh_jsonrpc"
            and row["envelope"].get("method") == "session.event"
            and row["envelope"]["params"]["event"]["type"] == "tool/call"
            and row["envelope"]["params"]["event"]["data"]["name"] == "game_step")
        self.assertEqual([join["call_id"] for join in proof["joins"]], [observed_call["callId"]])
        self.assertEqual([join["request_id"] for join in proof["joins"]], [submitted[0]["id"]])
        self.assertFalse(proof["accepted"], "Synthetic peers never prove a benchmark goal")

    async def test_image_pending_refusal_and_history_exclude_private_state(self):
        out, native, requests, results, events = await self.run_loop("image", [
            ("game_frame", {}), ("game_step", self.step()),
            ("game_result", {"wait": False}), ("game_result", {"wait": True}),
            ("game_ping", {}),
        ], refusal=True)
        self.assertNotIn("ToolOutputError", json.dumps(results))
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(requests))
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(results))
        self.assertNotIn("combat_perception", json.dumps(requests))
        self.assertNotIn("local_geometry", json.dumps(requests))
        self.assertIn('"pending"', json.dumps(requests).replace('\\"', '"'))
        self.assertEqual(sum(x["message"]["isError"] for x in results), 1)
        self.assertIn("stale_control_fence", json.dumps(results))
        self.assertEqual(len([x for x in native.requests if x["cmd"] == "step"]), 1)
        self.assert_images(out, requests, events)

    async def test_unsafe_numeric_mutation_is_not_submitted(self):
        _, native, requests, results, _ = await self.run_loop("structured", [
            ("game_step", self.step(uid=BIG)),
        ], signed_zero=False)
        self.assertFalse(native.requests)
        self.assertTrue(results[0]["message"]["isError"])
        self.assertIn("never round or clamp", json.dumps(requests))

    async def test_exact_but_stale_mutation_fence_is_not_refreshed_or_retried(self):
        _, native, _, results, _ = await self.run_loop("structured", [
            ("game_step", self.step(uid=str(BIG - 1))),
            ("game_result", {"wait": True}),
        ], signed_zero=False)
        self.assertEqual(len(native.requests), 1)
        self.assertEqual(native.requests[0]["args"]["control_fence"]["actor"]["uid"], BIG - 1)
        self.assertTrue(results[-1]["message"]["isError"])
        self.assertIn("stale_control_fence", json.dumps(results))

    async def test_official_registry_has_no_rounded_shadow_value(self):
        out = OUTPUT / self.id().split(".")[-1]
        out.mkdir(parents=True, exist_ok=False)
        evidence = Evidence(out / "events.ndjson")
        native = Native(evidence, out, signed_zero=False)
        tools = PlayerTools(native, {
            "observation_mode": "structured", "action_profile": "ordinary-renderer-native-v1",
        }, frames=out, episode="synthetic-registry")
        broker = ToolBroker(tools, Path("/tmp") / f"dsh-registry-{uuid4().hex}.sock")
        process = None
        try:
            await broker.start()
            process = await asyncio.create_subprocess_exec(
                "node", str(Path(__file__).with_name("official_registry_peer.mjs")),
                str(Path(dsh_tools.__file__).with_name("game-plugin.mjs")),
                str(broker.path), str(out / "home"),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
            (out / "registry.json").write_bytes(stdout)
            (out / "stderr.log").write_bytes(stderr)
            self.assertEqual(process.returncode, 0, stderr.decode())
            row = json.loads(stdout)
            self.assertFalse(row["result"]["isError"])
            self.assertNotIn("payload", row["result"]["value"],
                             "Rounded redundant JS payload must not exist in official value")
            self.assertIn(str(BIG), row["result"]["content"][0]["text"])
            self.assertEqual(row["result"]["value"], {"content": row["result"]["content"]})
            self.assertTrue(all(x["isError"] and x["error"]["info"]["name"] == "ToolOutputError"
                                for x in row["guards"]))
        finally:
            if process is not None and process.returncode is None:
                process.terminate()
                await process.wait()
            await broker.close()
            await asyncio.gather(*native.tasks, return_exceptions=True)
            evidence.close()


if __name__ == "__main__":
    unittest.main()
