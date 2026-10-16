import asyncio
import inspect
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from copilot import CopilotClient
from copilot.generated.session_events import (
    PermissionRequestCustomTool, PermissionRequestRead, SessionEvent,
    SessionEventType, SessionIdleData, SessionErrorData,
)
from copilot.tools import ToolInvocation

from scripts import rpg_sdk_agent as adapter
from scripts.rpg_benchmark.native import SocketEndpoint


READY = {"event": "ready", "protocol_version": "0.2", "session_id": "unit-session",
         "uid": 42, "native_extra": {"unchanged": True}}


def response(request, *, ok=True, session_id="unit-session"):
    row = {"type": "response", "id": request["id"], "protocol_version": "0.2",
           "session_id": session_id, "ok": ok}
    row["result" if ok else "error"] = (
        {"native_command": request["cmd"], "native_extra": 73} if ok else
        {"code": "unit_refusal", "message": "native error, not a success"})
    return row


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.evidence = adapter.Evidence(self.root / "events.ndjson")
        endpoint = SocketEndpoint(self.root)
        self.addCleanup(endpoint.close)
        self.socket = endpoint.path
        self.requests = []
        self.writers = set()
        self.host_tasks = set()
        self.dispatch_tasks = set()
        self.step_received = asyncio.Event()
        self.release_step = asyncio.Event()
        self.detached = False
        self.ready = READY
        self.mutate = lambda request, row: row

        async def host(reader, writer):
            self.writers.add(writer)
            task = asyncio.current_task()
            self.host_tasks.add(task)
            try:
                writer.write((json.dumps(self.ready) + "\n").encode())
                await writer.drain()

                async def dispatch(request):
                    if request["cmd"] == "close":
                        self.detached = True
                    if request["cmd"] == "step":
                        self.step_received.set()
                        await self.release_step.wait()
                    row = (response(request) if request["cmd"] == "close" else
                           self.mutate(request, response(request)))
                    if not writer.is_closing() and (not self.detached or request["cmd"] == "close"):
                        writer.write((json.dumps(row) + "\n").encode())
                        await writer.drain()

                async with asyncio.TaskGroup() as group:
                    while line := await reader.readline():
                        request = json.loads(line)
                        self.requests.append(request)
                        child = group.create_task(dispatch(request))
                        self.dispatch_tasks.add(child)
                        child.add_done_callback(self.dispatch_tasks.discard)
            finally:
                writer.close()
                await writer.wait_closed()
                self.writers.discard(writer)
                self.host_tasks.discard(task)

        self.server = await asyncio.start_unix_server(host, path=str(self.socket))
        self.native = adapter.NativeClient(self.socket, self.evidence, rpc_timeout=1)

    async def asyncTearDown(self):
        self.release_step.set()
        await self.native.close()
        self.server.close()
        await self.server.wait_closed()
        for writer in list(self.writers):
            writer.close()
        await asyncio.gather(*self.host_tasks)
        self.evidence.close()

    async def tools(self):
        await self.native.connect()
        return adapter.GameTools(self.native, self.evidence)

    async def invoke(self, tools, command, arguments):
        return await next(tool.handler for tool in tools.tools if tool.name == f"game_{command}")(
            ToolInvocation(session_id="sdk-unit-session", tool_call_id=uuid4().hex,
                           tool_name=f"game_{command}", arguments=arguments))

    async def test_ready_and_out_of_order_envelopes_unchanged(self):
        await self.native.connect()
        self.assertEqual(self.native.ready, READY)
        action = asyncio.create_task(self.native.request("step", {"action": {"type": "stop"}}))
        await self.step_received.wait()
        observed = await self.native.request("observe", {})
        self.assertFalse(action.done())
        self.assertEqual(observed, response(self.requests[1]))
        self.release_step.set()
        acted = await action
        self.assertEqual(acted, response(self.requests[0]))
        self.assertNotEqual(acted["id"], observed["id"])
        for request in self.requests:
            self.assertEqual(set(request), {"id", "protocol_version", "cmd", "args"})

    async def test_sdk_action_handle_does_not_block_observation(self):
        tools = await self.tools()
        submitted = await self.invoke(tools, "step", {"action": {"type": "stop"}})
        payload = json.loads(submitted.text_result_for_llm)
        self.assertEqual(payload["state"], "submitted")
        self.assertNotIn("response", payload)
        request_id = payload["request_id"]
        await self.step_received.wait()
        pending = await self.invoke(tools, "result", {"request_id": request_id})
        self.assertEqual(json.loads(pending.text_result_for_llm)["state"], "pending")
        observed = await self.invoke(tools, "observe", {})
        self.assertEqual(observed.result_type, "success")
        self.assertFalse(tools.submissions[request_id].done())
        self.release_step.set()
        receipt = await self.invoke(tools, "result", {"request_id": request_id, "wait": True})
        row = json.loads(receipt.text_result_for_llm)
        self.assertEqual(row["response"], response(self.requests[0]))
        self.assertEqual(row["source"]["tool_track"], "raw-native-reference")
        self.assertEqual(row["source"]["uid"], READY["uid"])

    async def test_c4_missing_action_id_is_added_once_without_rewriting_action(self):
        tools = await self.tools()
        arguments = {"action": {"type": "craft", "recipe": "simple_leather", "amount": 5}}
        submitted = await self.invoke(tools, "step", arguments)
        payload = json.loads(submitted.text_result_for_llm)
        await self.step_received.wait()
        request = self.requests[0]
        identifier = request["args"]["action"]["action_id"]
        self.assertTrue(identifier)
        self.assertEqual(payload["action_id"], identifier)
        self.assertEqual(payload["request"], request)
        self.assertEqual(request["args"], {"action": {**arguments["action"], "action_id": identifier}})
        self.assertNotIn("action_id", arguments["action"])
        self.release_step.set()
        receipt = await self.invoke(tools, "result", {"request_id": payload["request_id"], "wait": True})
        self.assertEqual(json.loads(receipt.text_result_for_llm)["action_id"], identifier)
        self.assertEqual(len(self.requests), 1)

    async def test_explicit_action_id_preserved_and_generated_ids_unique(self):
        tools = await self.tools()
        self.release_step.set()
        for identifier in ("chosen-craft-id", "é" * 64):
            submitted = await self.invoke(tools, "step", {
                "action": {"type": "stop", "action_id": identifier}})
            payload = json.loads(submitted.text_result_for_llm)
            self.assertEqual(payload["action_id"], identifier)
            await self.invoke(tools, "result", {"request_id": payload["request_id"], "wait": True})
            self.assertEqual(self.requests[-1]["args"]["action"]["action_id"], identifier)
        generated = []
        for _ in range(2):
            submitted = await self.invoke(tools, "step", {"action": {"type": "stop"}})
            payload = json.loads(submitted.text_result_for_llm)
            generated.append(payload["action_id"])
            await self.invoke(tools, "result", {"request_id": payload["request_id"], "wait": True})
        self.assertNotEqual(*generated)

    async def test_invalid_action_ids_fail_before_submission(self):
        tools = await self.tools()
        for identifier in ("", None, 1, True, [], {}, "x" * 129, "é" * 65):
            with self.subTest(identifier=identifier):
                result = await self.invoke(tools, "step", {
                    "action": {"type": "stop", "action_id": identifier}})
                self.assertEqual(result.result_type, "failure")
                self.assertIn("action_id", result.text_result_for_llm)
        self.assertEqual(self.requests, [])

    async def test_catalog_is_truthful_and_contract_is_local_not_native(self):
        tools = await self.tools()
        declared = {
            "observe", "events", "inspect_terrain", "inspect_map", "goal_recipes", "health",
            "ping", "step", "set_control_state", "release_control_state", "contract", "result"}
        self.assertEqual(tools.names, {f"game_{name}" for name in declared})
        metadata = await self.invoke(tools, "contract", {})
        payload = json.loads(metadata.text_result_for_llm)
        self.assertEqual(payload["metadata_origin"], "adapter-provided")
        self.assertEqual(payload["declared_for_pinned_bridge_sha256"], adapter.PINNED_BRIDGE_SHA256)
        self.assertEqual(payload["source"]["interface_version"], adapter.INTERFACE_VERSION)
        self.assertEqual(set(payload["native_commands"]), declared - {"contract", "result"})
        self.assertEqual(set(payload["action_types"]), set(adapter.ACTION_FIELDS))
        self.assertEqual(self.requests, [])
        for command in ("capabilities", "action_status", "cancel", "acquire_control"):
            with self.assertRaisesRegex(ValueError, "allowlisted"):
                self.native.submit(command, {})
        prompt = adapter.session_options(tools, self.root, self.root, "unit", None)["system_message"]["content"]
        self.assertNotIn("Query capabilities", prompt)
        self.assertIn("adapter-provided", prompt)

    async def test_native_arguments_are_typed_and_do_not_guess_aliases(self):
        tools = await self.tools()
        invalid = [
            ("goal_recipes", {"goal": "leather"}),
            ("goal_recipes", {"output_item_id": "item", "page_size": 33}),
            ("goal_recipes", {"output_item_id": "item", "offset": True}),
            ("inspect_terrain", {}),
            ("inspect_terrain", {"position": [1, 2]}),
            ("inspect_terrain", {"position": [True, 2, 3]}),
            ("inspect_map", {"bounds": [2, 0, 1, 1]}),
            ("inspect_map", {"cell_stride": 0}),
            ("events", {"limit": 5}),
            ("events", {"after_seq": 0, "filter": {}}),
            ("step", {"action": {"type": "move", "direction": [0, 1]}}),
            ("step", {"action": {"type": "move_to", "target": [1, 2, 3]}}),
            ("step", {"action": {"type": "attack", "target": "Green Legoom"}}),
            ("step", {"action": {"type": "attack", "target_uid": "61"}}),
            ("step", {"action": {"type": "attack", "duration": 1}}),
            ("step", {"action": {"type": "move_vector", "direction": [0, 0]}}),
            ("step", {"action": {"type": "craft", "recipe": "simple_leather", "amount": 0}}),
            ("step", {"action": {"type": "craft", "recipe": "simple_leather", "station": [1, 2, 3.5]}}),
            ("step", {"action": {"type": "use_item"}}),
            ("step", {"action": {"type": "use_item", "slot": 1, "slot_idx": 0}}),
            ("step", {"action": {"type": "drop", "equip_slot": "chest", "split": True}}),
            ("step", {"action": {"type": "give_up"}, "timeout_ticks": 60}),
            ("step", {"action": {"type": "attack"}, "duration_ticks": 601}),
            ("step", {"action": {"type": "attack"}, "duration_ticks": 20, "timeout_ticks": 10}),
            ("result", {"request_id": []}),
        ]
        for command, arguments in invalid:
            with self.subTest(command=command, arguments=arguments):
                result = await self.invoke(tools, command, arguments)
                self.assertEqual(result.result_type, "failure")
                self.assertEqual(json.loads(result.text_result_for_llm)["error"]["type"], "ValueError")
        self.assertEqual(self.requests, [])
        for command, arguments in [
            ("goal_recipes", {"output_item_id": "item", "offset": 0, "page_size": 32}),
            ("inspect_terrain", {"position": [1, 2, 3]}),
            ("events", {"after_seq": 0, "limit": 5}),
            ("step", {"action": {"type": "move_to", "position": [1, 2, 3]}, "timeout_ticks": 120}),
            ("step", {"action": {"type": "equip", "loadout_idx": 0, "slot_idx": 1}}),
        ]:
            adapter.validate_request(command, arguments)

    async def test_pinned_native_float32_lower_bounds_are_not_rounded_down(self):
        tools = await self.tools()
        for action in (
            {"type": "move_forward", "speed": .05},
            {"type": "move_to", "position": [1, 2, 3], "tolerance": .1},
        ):
            result = await self.invoke(tools, "step", {"action": action})
            self.assertEqual(result.result_type, "failure")
            self.assertIn("must be finite in", result.text_result_for_llm)
        self.assertEqual(self.requests, [])
        for action in (
            {"type": "move_forward", "speed": 0.05000000074505806},
            {"type": "move_forward", "speed": .1},
            {"type": "move_to", "position": [1, 2, 3], "tolerance": 0.10000000149011612},
        ):
            adapter.validate_request("step", {"action": action})

    async def test_control_identity_and_fences_are_explicit_and_unchanged(self):
        tools = await self.tools()
        actor = {"uid": 42, "lifecycle": "observed-actor"}
        owner = {"owner": "model-selected", "generation": 4, "sequence": 1,
                 "expected_session_id": "unit-session", "actor": actor,
                 "expected_protection_generation": 3}
        control = {**owner, "movement": [0, 0], "target": None, "primary": False, "block": False}
        applied = await self.invoke(tools, "set_control_state", control)
        self.assertEqual(applied.result_type, "success")
        self.assertEqual(self.requests[-1]["args"], control)
        released = await self.invoke(tools, "release_control_state", {**owner, "sequence": 2})
        self.assertEqual(released.result_type, "success")
        self.assertEqual(self.requests[-1]["args"], {**owner, "sequence": 2})
        before = len(self.requests)
        for invalid in [
            {}, {**control, "movement": [1, 1]}, {**control, "sequence": 0},
            {**control, "movement_intent": "approach"},
            {**control, "movement_intent": "destination"},
            {**control, "facing": "track"},
            {**control, "look_direction": [0, 0, 0]},
            {**control, "vertical_input": "fly_up"},
            {**control, "jump": True, "fly": True},
            {**control, "melee": "primary"},
        ]:
            result = await self.invoke(tools, "set_control_state", invalid)
            self.assertEqual(result.result_type, "failure")
        self.assertEqual(len(self.requests), before)
        self.release_step.set()
        fence = {"generation": 3, "expected_session_id": "unit-session", "actor": actor}
        submitted = await self.invoke(tools, "step", {
            "action": {"type": "stop"}, "control_fence": fence})
        payload = json.loads(submitted.text_result_for_llm)
        await self.invoke(tools, "result", {"request_id": payload["request_id"], "wait": True})
        self.assertEqual(self.requests[-1]["args"]["control_fence"], fence)

    async def test_admin_slash_chat_and_bad_schema_never_reach_socket(self):
        tools = await self.tools()
        for command, arguments in [
            ("admin", {}), ("step", {"action": {"type": "server_command", "command": "sudo"}}),
            ("step", {"action": {"type": "chat", "message": " \t/sudo"}}),
            ("step", {"action": {"type": "chat", "message": 1}}),
            ("step", {"action": {"type": ""}}), ("step", {"action": []}),
            ("step", {"action": {"type": "stop", "duration": float("nan")}}),
        ]:
            with self.subTest(command=command, arguments=arguments):
                with self.assertRaises(ValueError):
                    self.native.submit(command, arguments)
        for command, args in [("observe", {"unknown": 1}), ("step", {}),
                              ("result", {"request_id": "unknown"}),
                              ("result", {"request_id": "unknown", "wait": 1})]:
            result = await self.invoke(tools, command, args)
            self.assertEqual(result.result_type, "failure")
        self.assertEqual(self.requests, [])
        adapter.validate_request("step", {"action": {"type": "chat", "message": "hello"}})

    async def test_native_refusal_is_failure_with_original_error_envelope(self):
        tools = await self.tools()
        self.mutate = lambda request, row: response(request, ok=False)
        result = await self.invoke(tools, "observe", {})
        self.assertEqual(result.result_type, "failure")
        row = json.loads(result.text_result_for_llm)
        self.assertEqual(row["response"], response(self.requests[0], ok=False))
        self.assertEqual(row["error"]["type"], "NativeError")
        self.assertFalse(self.native.failure.done())  # Native refusal is not transport corruption.

    async def test_bad_session_rejects_all_pending_requests(self):
        await self.native.connect()
        self.mutate = lambda request, row: response(request, session_id="wrong-session")
        with self.assertRaisesRegex(adapter.ProtocolError, "identity"):
            await self.native.request("observe", {})
        self.assertTrue(self.native.failure.done())
        with self.assertRaises(adapter.ProtocolError):
            self.native.submit("observe", {})

    async def test_unknown_id_is_not_misrouted(self):
        await self.native.connect()
        self.mutate = lambda request, row: {**row, "id": "not-submitted"}
        with self.assertRaisesRegex(adapter.ProtocolError, "unknown or duplicate"):
            await self.native.request("observe", {})

    async def test_duplicate_response_is_transport_error_not_second_receipt(self):
        await self.native.connect()
        observed = await self.native.request("observe", {})
        writer = next(iter(self.writers))
        writer.write((json.dumps(observed) + "\n").encode())
        await writer.drain()
        error = await asyncio.wait_for(asyncio.shield(self.native.failure), 1)
        self.assertRegex(str(error), "unknown or duplicate")

    async def test_native_step_refusals_remain_unchanged_with_identity(self):
        tools = await self.tools()
        self.release_step.set()
        for code in ("stale_control_owner", "avatar_busy", "action_id_conflict"):
            def refuse(request, row):
                row = response(request, ok=False)
                row["error"] = {"code": code, "message": "native refusal unchanged"}
                return row
            self.mutate = refuse
            submitted = await self.invoke(tools, "step", {
                "action": {"type": "stop", "action_id": f"explicit-{code}"}})
            payload = json.loads(submitted.text_result_for_llm)
            result = await self.invoke(tools, "result", {"request_id": payload["request_id"], "wait": True})
            failure = json.loads(result.text_result_for_llm)
            self.assertEqual(result.result_type, "failure")
            self.assertEqual(failure["response"]["error"], {"code": code, "message": "native refusal unchanged"})
            self.assertEqual(failure["action_id"], f"explicit-{code}")
            self.assertEqual(failure["request_id"], payload["request_id"])
            self.assertFalse(self.native.failure.done())

    async def test_protocol_version_type_must_match_ready(self):
        await self.native.connect()
        self.mutate = lambda request, row: {**row, "protocol_version": True}
        with self.assertRaisesRegex(adapter.ProtocolError, "identity"):
            await self.native.request("observe", {})

    async def test_socket_eof_fails_pending_request(self):
        await self.native.connect()
        task = asyncio.create_task(self.native.request("step", {"action": {"type": "stop"}}))
        await self.step_received.wait()
        next(iter(self.writers)).close()
        with self.assertRaisesRegex(adapter.ProtocolError, "closed"):
            await task
        self.assertTrue(self.native.failure.done())

    async def test_cancelled_request_allows_correlated_late_receipt(self):
        await self.native.connect()
        task = asyncio.create_task(self.native.request("step", {"action": {"type": "stop"}}))
        await self.step_received.wait()
        request_id = self.requests[0]["id"]
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIn(request_id, self.native.abandoned)
        self.release_step.set()
        observed = await self.native.request("observe", {})
        self.assertEqual(observed, response(self.requests[1]))
        self.assertFalse(self.native.failure.done())

    async def test_timeout_is_error_not_default_success(self):
        tools = await self.tools()
        self.native.rpc_timeout = 0.02
        result = await self.invoke(tools, "step", {"action": {"type": "stop"}})
        submitted = json.loads(result.text_result_for_llm)
        request_id = submitted["request_id"]
        result = await self.invoke(tools, "result", {"request_id": request_id, "wait": True})
        self.assertEqual(result.result_type, "failure")
        self.assertEqual(json.loads(result.text_result_for_llm)["error"]["type"], "TimeoutError")
        self.assertEqual(json.loads(result.text_result_for_llm)["action_id"], submitted["action_id"])
        repeated = await self.invoke(tools, "result", {"request_id": request_id, "wait": True})
        self.assertEqual(repeated.result_type, "failure")
        self.assertEqual(len(self.requests), 1)
        self.release_step.set()
        observed = await self.invoke(tools, "observe", {})
        self.assertEqual(observed.result_type, "success")
        self.assertEqual([r["cmd"] for r in self.requests], ["step", "observe"])
        self.assertFalse(self.native.failure.done())

    async def test_bad_ready_fails_without_sdk_start(self):
        self.ready = {**READY, "event": "not-ready"}
        with self.assertRaisesRegex(adapter.ProtocolError, "ready"):
            await self.native.connect()
        self.assertEqual(self.requests, [])
        self.assertIsNone(self.native.reader_task)

    async def test_contract_rejects_unpinned_protocol_before_sdk_start(self):
        self.ready = {**READY, "protocol_version": "0.3"}
        with self.assertRaisesRegex(adapter.ProtocolError, "requires native protocol 0.2"):
            await self.native.connect()
        self.assertEqual(self.requests, [])

    async def test_close_cancels_pending_without_native_process_ownership(self):
        tools = await self.tools()
        submitted = await self.invoke(tools, "step", {"action": {"type": "stop"}})
        await self.step_received.wait()
        await self.native.close()
        self.assertTrue(tools.submissions[json.loads(submitted.text_result_for_llm)["request_id"]].cancelled())
        self.assertTrue(self.native.reader_task.done())
        self.assertEqual(self.requests[-1]["cmd"], "close")
        self.assertEqual(self.native.pending, {})

    async def test_permission_allowlist_and_native_policy(self):
        tools = await self.tools()
        request = PermissionRequestCustomTool(tool_name="game_observe", tool_description="read", args={})
        self.assertEqual(tools.permission(request, {}).kind, "approve-once")
        for request in [
            PermissionRequestCustomTool(tool_name="bash", tool_description="shell", args={}),
            PermissionRequestCustomTool(tool_name="game_observe", tool_description="read", args={},
                                        managed_approval_required=True),
            PermissionRequestCustomTool(tool_name="game_step", tool_description="act",
                                        args={"action": {"type": "server_command"}}),
            PermissionRequestCustomTool(tool_name="game_step", tool_description="act",
                                        args={"action": {"type": "chat", "message": "/admin"}}),
            PermissionRequestRead(intention="read fixture", path="/fixture.json"),
        ]:
            self.assertEqual(tools.permission(request, {}).kind, "reject")

    async def test_actual_installed_sdk_serializes_isolated_config_without_provider(self):
        tools = await self.tools()
        client = CopilotClient(mode="empty", working_directory=str(self.root),
                               base_directory=str(self.root / "sdk"), builtin_plugin_directories=[])
        options = adapter.session_options(tools, self.root / "work", self.root / "config", "unit-model", "high")
        inspect.signature(CopilotClient.create_session).bind(client, **options)
        calls = []

        async def rpc(method, payload, **kwargs):
            calls.append((method, payload))
            if method == "session.create":
                return {"sessionId": payload["sessionId"]}
            if method == "session.options.update":
                return {"success": True}
            return {}

        client._client = AsyncMock()
        client._client.request.side_effect = rpc
        session = await client.create_session(**options)
        self.addAsyncCleanup(session.disconnect)
        payload = next(data for method, data in calls if method == "session.create")
        self.assertEqual(set(payload["availableTools"]), {f"custom:{name}" for name in tools.names})
        self.assertEqual(payload["excludedTools"], ["builtin:*", "mcp:*"])
        for flag in ["enableConfigDiscovery", "enableOnDemandInstructionDiscovery", "enableFileHooks",
                     "enableHostGitOperations", "enableSessionStore", "enableSkills",
                     "enableFileChangeTracking", "requestExtensions"]:
            self.assertIs(payload[flag], False)
        self.assertEqual(payload["memory"], {"enabled": False})
        self.assertEqual(payload["systemMessage"]["sections"]["environment_context"]["action"], "remove")
        self.assertEqual(payload["instructionDirectories"], [])
        self.assertEqual(payload["excludedBuiltinAgents"], [])
        self.assertEqual(options["custom_agents"], [])
        self.assertNotIn("customAgents", payload)
        self.assertEqual(payload["configDir"], str(self.root / "config"))
        self.assertEqual(payload["workingDirectory"], str(self.root / "work"))
        self.assertNotIn("provider", payload)
        self.assertNotIn("sessionLimits", payload)
        self.assertNotIn("gitHubToken", payload)
        self.assertFalse(any(tool.get("skipPermission") for tool in payload["tools"]))
        self.assertEqual({tool["name"] for tool in payload["tools"]}, tools.names)

    def mock_sdk(self, *, idle=True):
        client, session = AsyncMock(), AsyncMock()
        session.session_id = "unit-sdk-session"
        started = asyncio.Event()
        received = {}

        def event():
            return SessionEvent(data=SessionIdleData(), id=uuid4(), timestamp=datetime.now(timezone.utc),
                                type=SessionEventType.SESSION_IDLE)

        async def create(**options):
            received.update(options)
            self.assertEqual(list(Path(options["working_directory"]).iterdir()), [])
            self.assertEqual(list(Path(options["config_directory"]).iterdir()), [])
            received["startup_idle"] = event()
            options["on_event"](received["startup_idle"])
            return session

        async def send(task):
            started.set()
            if idle:
                received["turn_idle"] = event()
                received["on_event"](received["turn_idle"])
            return "unit-message"

        client.create_session.side_effect = create
        session.send.side_effect = send
        return client, session, started, received

    async def test_adapter_lifecycle_persists_truthful_idle_and_creation_events(self):
        client, session, _, received = self.mock_sdk()
        root = self.root / "outputs"
        args = adapter.parse_args(["--episode-socket", str(self.socket), "--task", "unit goal",
                                   "--model", "unit-model", "--out", str(root / "idle")])
        with patch.object(adapter, "OUTPUT_ROOT", root), patch("copilot.CopilotClient", return_value=client):
            result = await adapter.run(args)
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["capability_success"], "not-evaluated")
        self.assertEqual(result["idle_event"], received["turn_idle"].to_dict())
        self.assertNotEqual(result["idle_event"], received["startup_idle"].to_dict())
        session.send.assert_awaited_once_with("unit goal")
        session.abort.assert_not_awaited()
        session.disconnect.assert_awaited_once()
        client.stop.assert_awaited_once()
        rows = [json.loads(line) for line in (args.out / "events.ndjson").read_text().splitlines()]
        sdk_rows = [row["event"] for row in rows if row["kind"] == "sdk_event"]
        self.assertIn(received["startup_idle"].to_dict(), sdk_rows)
        self.assertIn(received["turn_idle"].to_dict(), sdk_rows)
        self.assertEqual(json.loads((args.out / "result.json").read_text())["status"], "idle")

    async def test_adapter_interruption_aborts_and_persists_result(self):
        client, session, started, _ = self.mock_sdk(idle=False)
        root = self.root / "outputs"
        args = adapter.parse_args(["--episode-socket", str(self.socket), "--task", "unit goal",
                                   "--model", "unit-model", "--out", str(root / "interrupted")])
        with patch.object(adapter, "OUTPUT_ROOT", root), patch("copilot.CopilotClient", return_value=client):
            task = asyncio.create_task(adapter.run(args))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        session.abort.assert_awaited_once()
        session.disconnect.assert_awaited_once()
        client.stop.assert_awaited_once()
        result = json.loads((args.out / "result.json").read_text())
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["capability_success"], "not-evaluated")
        self.assertEqual(self.requests[-1]["cmd"], "close")

    async def test_cleanup_error_is_persistent_and_not_success(self):
        client, session, _, _ = self.mock_sdk()
        client.stop.side_effect = RuntimeError("unit owned-runtime stop failure")
        root = self.root / "outputs"
        args = adapter.parse_args(["--episode-socket", str(self.socket), "--task", "unit goal",
                                   "--model", "unit-model", "--out", str(root / "cleanup-error")])
        with patch.object(adapter, "OUTPUT_ROOT", root), patch("copilot.CopilotClient", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                await adapter.run(args)
        result = json.loads((args.out / "result.json").read_text())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["cleanup_errors"][0]["operation"], "sdk_stop_owned_runtime")
        session.disconnect.assert_awaited_once()
        self.assertEqual(self.requests[-1]["cmd"], "close")


class SdkEventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.evidence = adapter.Evidence(Path(self.temp.name) / "events.ndjson")
        self.addCleanup(self.evidence.close)
        self.native = adapter.NativeClient(Path("/not-connected-unit-socket"), self.evidence)

    def event(self, data, kind):
        return SessionEvent(data=data, id=uuid4(), timestamp=datetime.now(timezone.utc), type=kind)

    async def test_idle_submits_once_and_does_not_claim_capability(self):
        session = AsyncMock()
        callbacks = []
        session.on = lambda callback: (callbacks.append(callback) or (lambda: callbacks.remove(callback)))
        idle = self.event(SessionIdleData(), SessionEventType.SESSION_IDLE)

        async def send(task):
            callbacks[0](idle)
            return "unit-message"

        session.send.side_effect = send
        metadata = {"provider_usage": [], "actual_served_model": "unknown"}
        await adapter.sdk_turn(session, "unit task", self.native, self.evidence, metadata)
        session.send.assert_awaited_once_with("unit task")
        self.assertEqual(metadata["idle_event"], idle.to_dict())
        self.assertEqual(metadata["actual_served_model"], "unknown")
        self.assertEqual(callbacks, [])

    async def test_sdk_error_propagates_and_unsubscribes(self):
        session = AsyncMock()
        callbacks = []
        session.on = lambda callback: (callbacks.append(callback) or (lambda: callbacks.remove(callback)))
        error = self.event(SessionErrorData(error_type="unit", message="provider failed"),
                           SessionEventType.SESSION_ERROR)

        async def send(task):
            callbacks[0](error)
            return "unit-message"

        session.send.side_effect = send
        with self.assertRaisesRegex(RuntimeError, "provider failed"):
            await adapter.sdk_turn(session, "unit task", self.native, self.evidence,
                                   {"provider_usage": []})
        self.assertEqual(callbacks, [])

    async def test_interruption_unsubscribes_without_reprompt(self):
        session = AsyncMock()
        callbacks = []
        started = asyncio.Event()
        session.on = lambda callback: (callbacks.append(callback) or (lambda: callbacks.remove(callback)))

        async def send(task):
            started.set()
            return "unit-message"

        session.send.side_effect = send
        turn = asyncio.create_task(adapter.sdk_turn(session, "unit task", self.native,
                                                    self.evidence, {"provider_usage": []}))
        await started.wait()
        turn.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await turn
        self.assertEqual(callbacks, [])
        session.send.assert_awaited_once()

    async def test_malformed_provider_usage_is_an_error_not_terminal_cancellation(self):
        events = adapter.SdkEvents(self.evidence, {"provider_usage": []})
        malformed = Mock()
        malformed.to_dict.return_value = {"type": "assistant.usage", "data": None}
        events.process(malformed, True)
        with self.assertRaises(TypeError):
            await events.ended

    async def test_sigterm_handler_cancels_owned_loop_and_is_removed(self):
        callbacks = {}
        loop = asyncio.get_running_loop()

        async def waiting(args):
            callbacks["cancel"]()
            await asyncio.Future()

        with patch.object(adapter, "run", side_effect=waiting), \
                patch.object(loop, "add_signal_handler", side_effect=lambda signum, callback: callbacks.update(cancel=callback)), \
                patch.object(loop, "remove_signal_handler") as remove:
            with self.assertRaises(asyncio.CancelledError):
                await adapter.run_with_signals(None)
            remove.assert_called_once_with(adapter.signal.SIGTERM)


class ConfigurationTests(unittest.TestCase):
    def test_cli_uses_installed_effort_type_and_rejects_unrestricted_config(self):
        common = ["--episode-socket", "/episode.sock", "--task", "goal", "--out", "/unused"]
        parsed = adapter.parse_args(common + ["--model", "unit-model", "--reasoning-effort", "max"])
        self.assertEqual(parsed.reasoning_effort, "max")
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.json"
            config.write_text(json.dumps({"model": "unit-model", "reasoning_effort": "high"}))
            self.assertEqual(adapter.parse_args(common + ["--config", str(config)]).model, "unit-model")
            config.write_text(json.dumps({"model": "unit-model", "available_tools": ["bash"]}))
            with self.assertRaises(SystemExit), patch("sys.stderr"):
                adapter.parse_args(common + ["--config", str(config)])
        for extra in [["--rpc-timeout", "nan"], ["--rpc-timeout", "0"],
                      ["--reasoning-effort", "none"]]:
            with self.assertRaises(SystemExit), patch("sys.stderr"):
                adapter.parse_args(common + ["--model", "unit-model", *extra])

    def test_output_is_exclusive_and_dedicated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(adapter, "OUTPUT_ROOT", root):
                self.assertEqual(adapter.reserve_output(root / "new"), root / "new")
                with self.assertRaises(FileExistsError):
                    adapter.reserve_output(root / "new")
                for path in [root, root / ".." / "escape"]:
                    with self.assertRaises(ValueError):
                        adapter.reserve_output(path)
                link = root / "link"
                link.symlink_to(root / "new", target_is_directory=True)
                with self.assertRaises(ValueError):
                    adapter.reserve_output(link / "child")

    def test_evidence_redacts_auth_and_keeps_native_semantics(self):
        row = {"headers": {"Authorization": "Bearer unit-secret"}, "github_token": "unit-secret",
               "message": "Bearer unit-secret ghp_unitsecret", "request_id": "public-id",
               "response": {"ok": False, "error": {"code": "native_error"}}}
        clean = adapter.redact(row)
        self.assertNotIn("unit-secret", json.dumps(clean))
        self.assertNotIn("ghp_unitsecret", json.dumps(clean))
        self.assertEqual(clean["request_id"], row["request_id"])
        self.assertEqual(clean["response"], row["response"])


if __name__ == "__main__":
    unittest.main()
