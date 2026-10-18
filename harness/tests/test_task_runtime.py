import asyncio
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest

from scripts import rpg_sdk_agent as common
from scripts.rpg_benchmark import capabilities, observations, providers, renderer_support
from scripts.rpg_benchmark.player_types import Image, Invocation, Result, Tool
from scripts.rpg_benchmark.renderer_tools import RendererTools
from scripts.rpg_benchmark.responses import ResponsesProvider, play
from scripts.rpg_benchmark.responses_adapter import Adapter
from .test_multimodal import Evidence, png


def declaration():
    value = copy.deepcopy(providers.LUNA)
    value.update(id="inspecting-local-alternate", default_model="alternate-model", default_effort="high",
                 model_profiles=[{"model": "alternate-model", "reasoning_efforts": ["low", "high"]}])
    return value


def configuration(mode="hybrid"):
    return capabilities.negotiate(providers.manifest(declaration(), mode), {})[0]


@pytest.mark.parametrize("mode", ["structured", "image", "hybrid"])
def test_declared_alternate_profile_survives_adapter_entry_arguments_and_transport(mode, tmp_path):
    args = SimpleNamespace(adapter_config=json.dumps({"mock_provider": "http://127.0.0.1:1234",
                                                      "provider": declaration()}),
                           observation_mode=mode, model="alternate-model", reasoning_effort="low",
                           action_profile=None, recordings=tmp_path / "frames")
    adapter = Adapter(args)
    plan = adapter.launch(task="macro goal", socket=tmp_path / "s", output=tmp_path / "entry",
                          output_root=tmp_path, python="/python", common_interface="/sdk")
    command = list(plan.command)
    selected = json.loads(command[command.index("--configuration") + 1])
    provider = ResponsesProvider(Evidence(), mock_url="http://127.0.0.1:1234",
                                 declaration=declaration(), configuration=selected)
    body = provider.body([])
    assert body["model"] == "alternate-model" and body["reasoning"] == {"effort": "low"}
    assert selected["observation_mode"] == mode
    with pytest.raises(ValueError):
        asyncio.run(provider.send({**body, "model": "unnegotiated"}))


@pytest.mark.parametrize("mutation", [
    {"model": "undeclared"}, {"reasoning_effort": "none"}, {"action_profile": "raw-native-v2"}])
def test_unsupported_profile_is_rejected_not_replaced_with_luna(mutation):
    with pytest.raises(ValueError):
        capabilities.negotiate(providers.manifest(declaration(), "image"), mutation)


@pytest.mark.parametrize("payload", [
    {"id": "r", "status": "completed", "output": []},
    {"id": "r", "status": "incomplete", "output": []},
    {"id": "r", "status": "completed", "output": [{"type": "reasoning"}]},
    {"id": "r", "status": "completed", "error": {"code": "failed"}, "output": []},
    {"id": "r", "status": "completed", "output": [{"type": "function_call", "name": "private_observe",
                                                   "call_id": "c", "arguments": "{}", "status": "completed"}]},
])
def test_http_200_is_not_accepted_provider_delivery(payload):
    observed = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            observed.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever)
    thread.start()
    evidence = Evidence()
    try:
        provider = ResponsesProvider(evidence, declaration=declaration(), configuration=configuration(),
                                     mock_url=f"http://127.0.0.1:{server.server_port}")
        with pytest.raises(RuntimeError):
            asyncio.run(provider.send(provider.body([{"role": "user", "content": "goal"}])))
        assert observed[0]["model"] == "alternate-model"
        assert not any(row["kind"] == "provider_response" for row in evidence.rows)
        assert any(row["kind"] == "provider_error" for row in evidence.rows)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("conflicting", [False, True])
def test_duplicate_provider_tool_identity_replays_receipt_without_mutation(conflicting):
    evidence, mutations = Evidence(), []
    async def contract(invocation):
        return Result("{}")
    async def motor(invocation):
        mutations.append(invocation.arguments)
        return Result('{"state":"unknown","retry_policy":"never resubmit"}')
    tools = SimpleNamespace(tools=[
        Tool("game_contract", "contract", common.object_schema(""), contract),
        Tool("game_motor", "motor", common.object_schema(""), motor)])
    provider = ResponsesProvider(evidence, declaration=declaration(), configuration=configuration("structured"),
                                 mock_url="http://127.0.0.1:1234")
    outputs = iter([
        {"type": "function_call", "name": "game_motor", "call_id": "same", "arguments": "{}", "status": "completed"},
        {"type": "function_call", "name": "game_motor", "call_id": "same",
         "arguments": '{"changed":true}' if conflicting else "{}", "status": "completed"},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}],
         "status": "completed"}])
    def post(body):
        return {"id": f"response-{len(evidence.rows)}", "status": "completed", "output": [next(outputs)]}
    provider._post = post
    if conflicting:
        with pytest.raises(ValueError, match="different intent"):
            asyncio.run(play(provider, tools, "macro goal", evidence, session_id="s"))
    else:
        asyncio.run(play(provider, tools, "macro goal", evidence, session_id="s"))
    assert mutations == [{}]
    assert any(row["kind"] == "model_duplicate_call" for row in evidence.rows) is not conflicting


@pytest.mark.parametrize("mode", ["structured", "image", "hybrid"])
def test_normal_initial_perception_matches_mode_before_first_model_call(mode):
    import hashlib
    from scripts.rpg_benchmark.responses import image_identities
    evidence, observed, bodies = Evidence(), [], []
    async def contract(invocation):
        return Result('{"session_id":"public"}')
    async def observe(invocation):
        observed.append(mode)
        return Result('{"structured_state":"only-structured-and-hybrid"}')
    async def frame(invocation):
        data = png()
        return Result('{"frame_id":"public-frame"}', images=(
            Image(data, "image/png", hashlib.sha256(data).hexdigest(), 2, 1),))
    tools = SimpleNamespace(tools=[
        Tool("game_contract", "", common.object_schema(""), contract),
        Tool("game_observe", "", common.object_schema(""), observe),
        Tool("game_frame", "", common.object_schema(""), frame)])
    provider = ResponsesProvider(evidence, declaration=declaration(), configuration=configuration(mode),
                                 mock_url="http://127.0.0.1:1234")
    def post(body):
        bodies.append(body)
        return {"id": "r", "status": "completed", "output": [{
            "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": "stop"}]}]}
    provider._post = post
    asyncio.run(play(provider, tools, "macro goal", evidence, session_id="s"))
    assert bool(observed) is (mode != "image")
    assert bool(image_identities(bodies[0]["input"])) is (mode != "structured")
    assert ("only-structured-and-hybrid" in json.dumps(bodies[0])) is (mode != "image")


def test_terminal_cancellation_evidence_keeps_source_and_does_not_break_adjudication(tmp_path):
    from scripts.rpg_benchmark import results
    args = SimpleNamespace(adapter_config=json.dumps({"mock_provider": "http://127.0.0.1:1234",
                                                      "provider": declaration()}),
                           observation_mode="hybrid", model=None, reasoning_effort=None,
                           action_profile=None, recordings=tmp_path / "frames")
    adapter = Adapter(args)
    adapter.output = tmp_path
    error = {"type": "CancelledError", "message": ""}
    # A late recording timestamp alone is no longer sufficient cancellation
    # evidence. This positive case records the actually observed stop identity.
    causal = {"kind": "evaluator_stop", "stop_id": "observed-stop", "signal": "SIGINT"}
    (tmp_path / "entry.json").write_text(json.dumps({
        "status": "cancelled", "error": error, "caused_by": causal,
        "terminal": {"monotonic": 12}, "cleanup_errors": []}))
    (tmp_path / "events.ndjson").write_text("".join(json.dumps(row) + "\n" for row in (
        {"kind": "entry_ready", "wall_monotonic": 8},
        {"kind": "provider_request", "model": "alternate-model", "effort": "high", "synthetic": True},
        {"kind": "entry_error", "wall_monotonic": 12, "error": error, "caused_by": causal})))
    facts = adapter.collect()
    normalized = results.observations(
        facts, 10, {"signal": "SIGINT", "sent": True, "requested_monotonic": 11, "stop_id": "observed-stop"})
    assert all(row["source"] == "entry" for row in normalized)
    assert results.validity(facts, normalized, native_terminal=True)["status"] == "valid"


@pytest.mark.parametrize("action", [
    {"type": "context_interact", "operation": "talk", "target": {"uid": 2, "lifecycle": "observed"}},
    {"type": "dialogue_action", "operation": "respond", "sender_uid": 2, "dialogue_id": 1, "tag": 0, "response_id": 1},
    {"type": "social_action", "operation": "invite_trade", "target": {"uid": 2, "lifecycle": "observed"}},
    {"type": "trade_action", "operation": "add_item", "trade_id": 1, "counterparty_uid": 2,
     "phase": "Mutate", "slot": 1, "quantity": 1, "ours": False},
    {"type": "unlock_skill", "skill": {"General": "HealthIncrease"}},
    {"type": "configure_auxiliary_ability", "slot": 0, "ability": {"MainWeapon": 0}},
])
def test_public_rpg_schemas_preserve_exact_native_selection_and_remain_forbidden_in_image(action):
    common.validate_request("step", {"action": action})
    assert not observations.image_allowed({"cmd": "step", "args": {"action": action}})
    evidence = Evidence()
    native = SimpleNamespace(ready={"capabilities": {"rpg_interactions": {"version": "1.0"}}})
    structured = RendererTools(native, evidence, "structured", Path("/frames"), "e")
    common.validate_schema({"action": action}, structured.schemas["step"])
    image = RendererTools(native, evidence, "image", Path("/frames"), "e")
    with pytest.raises(ValueError):
        common.validate_schema({"action": action}, image.schemas["step"])
    projected = observations.project_image({"result": {
        "status": "completed", "skill_tree": "PRIVATE", "quest_states": "PRIVATE",
        "dialogue": "PRIVATE", "last_trade_result": "PRIVATE", "private_oracle": "PRIVATE"}})
    assert "PRIVATE" not in json.dumps(projected)


def test_strict_native_social_dialogue_trade_cross_field_semantics():
    for action in (
        {"type": "dialogue_action", "operation": "ack", "sender_uid": 1, "dialogue_id": 1,
         "tag": 0, "response_id": 1},
        {"type": "social_action", "operation": "invite_group", "target": {"uid": 2, "lifecycle": "l"}},
        {"type": "trade_action", "operation": "accept", "trade_id": 1,
         "counterparty_uid": 2, "phase": "Review", "slot": 1},
    ):
        with pytest.raises(ValueError):
            common.validate_request("step", {"action": action})


def test_visual_goal_wording_uses_only_static_geometry_not_private_state():
    from scripts.rpg_benchmark import layouts, renderer_tasks
    fixture = {"task": "Original public rendezvous.", "layout": layouts.scene("wall_side_detour"),
               "opening": {"position": "PRIVATE", "survival": "PRIVATE"}, "goal": "PRIVATE"}
    text = renderer_tasks.public_task({"id": "N1"}, fixture, "image")
    assert "PRIVATE" not in text and "2.5 metres west" in text
    assert fixture["goal"] == "PRIVATE"
    assert renderer_tasks.public_task({"id": "N1"}, fixture, "structured") == fixture["task"]
    fixture["layout"]["geometry_sha256"] = "changed"
    with pytest.raises(ValueError):
        renderer_tasks.public_task({"id": "N1"}, fixture, "image")


@pytest.mark.parametrize("corrupt_success", [False, True])
def test_native_null_refusal_is_not_a_success_join_or_an_adjudication_crash(corrupt_success):
    from scripts.rpg_benchmark import external
    actor = {"uid": 1, "lifecycle": "life"}
    facts = {"entry": {"status": "idle", "cleanup_errors": []}, "entry_receipt_present": True,
             "model_requests": [{"synthetic": False}], "provider_errors": [],
             "error_observations": [], "tool_calls": [], "submissions": []}
    receipts = []
    for n in range(2):
        args = {"owner": "ordinary-model", "generation": n + 1, "sequence": 1,
                "expected_session_id": "s", "actor": actor, "movement": [n, 1 - n],
                "target": None, "primary": False, "block": False}
        request = {"id": f"req-{n}", "cmd": "set_control_state", "args": args}
        facts["tool_calls"].append({"id": f"call-{n}", "name": "game_set_control_state",
                                    "arguments": args})
        facts["submissions"].append({"call_id": f"call-{n}", "request": request})
        response = {"id": request["id"], "session_id": "s", "ok": bool(n) or corrupt_success,
                    "result": None if n == 0 else {
                        "status": "applied", "session_id": "s", "actor": actor,
                        "owner": args["owner"], "generation": args["generation"], "sequence": 1,
                        "applied_tick": 2}}
        receipts.append({"request": request, "response": response, "actor": 1})
    kwargs = dict(native_classification="provider_or_agent_error", stop_reason="normal_entry_exited",
                  stop_signal={})
    opening = {"session_id": "s", "self_identity": actor, "control_actor": actor}
    if corrupt_success:
        with pytest.raises(ValueError, match="lacks a receipt"):
            external.adjudicate(facts, receipts, opening, {"success": False}, **kwargs)
    else:
        classification, proof = external.adjudicate(facts, receipts, opening, {"success": False}, **kwargs)
        assert classification == "model_stopped_without_goal"
        assert [join["request_id"] for join in proof["joins"]] == ["req-1"]
        assert not proof["accepted"]
