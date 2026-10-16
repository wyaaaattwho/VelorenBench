"""All new evidence here is mocked engineering evidence, not native/model results."""
import asyncio
import copy
import importlib
import json
from pathlib import Path
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts import rpg_sdk_agent as common
from scripts.rpg_benchmark import capabilities, evidence, external, results, run
from scripts.rpg_benchmark.native import exception_detail
from scripts.rpg_benchmark.timing import AttemptTiming
from .test_boundary_shutdown import PipeFixture, step
from .test_external_harness import facts, selected, mock_selection

HARNESS = Path(__file__).resolve().parents[1]
ROOT = HARNESS.parent
OPENING = {"session_id": "native", "self_identity": {"uid": 42, "lifecycle": "life"},
           "control_actor": {"uid": 42, "lifecycle": "life"}}


def judge(data, receipts, *, success=True, reason="native_goal_success", unknown=()):
    return external.adjudicate(
        data, receipts, OPENING, {"success": success, "dead": reason == "player_death"},
        native_classification="capability_success" if success else "agent_failure",
        stop_reason=reason, stop_signal={}, terminal_monotonic=20, unknown_effects=unknown)


@pytest.mark.parametrize("reason", ["native_goal_success", "player_death", "evaluation_horizon"])
@pytest.mark.parametrize("offset,expected", [(-.01, "invalid"), (.0068, "valid"), (.0301, "valid"),
                                            (None, "unknown")])
def test_native_terminal_is_not_rewritten_by_reset_and_ordering_controls_validity(reason, offset, expected):
    data, receipt = facts()
    data["provider_errors"] = [{"error": {"type": "ClientConnectionResetError", "message": "reset"},
                                **({"wall_monotonic": 20 + offset} if offset is not None else {})}]
    classification, proof = judge(data, [receipt], success=reason == "native_goal_success", reason=reason)
    assert classification == ("capability_success" if reason == "native_goal_success" else "agent_failure")
    assert proof["evaluation_validity"]["status"] == expected
    assert proof["accepted"] is (reason == "native_goal_success" and expected == "valid")
    assert proof["error_observations"][0]["error"]["type"] == "ClientConnectionResetError"


def test_provider_reported_policy_rejection_remains_observation_not_adapter_cause():
    data, receipt = facts()
    data["provider_errors"] = [{
        "kind": "provider_rejected", "wall_monotonic": 19,
        "adapter_label": "adapter_failure", "error": {
            "code": "PI_AI_ERROR", "message": "provider-reported policy rejection"}}]
    classification, proof = judge(data, [receipt], success=False, reason="normal_entry_exited")
    assert classification == "provider_error" and not proof["accepted"]
    assert proof["error_observations"][0]["adapter_label"] == "adapter_failure"
    assert proof["error_observations"][0]["error"]["code"] == "PI_AI_ERROR"


@pytest.mark.parametrize("row,expected", [
    ({"kind": "provider_rejected", "error": "ValueError", "wall_monotonic": 19}, "invalid"),
    ({"kind": "provider_error", "error_type": "ClientConnectionResetError",
      "message": "withheld", "wall_monotonic": 20.01}, "valid"),
    ({"kind": "provider_response", "status": 400, "wall_monotonic": 19}, "invalid"),
    ({"kind": "provider_error", "error_type": "RuntimeError"}, "unknown"),
])
def test_actual_dsh_error_envelope_shapes_remain_raw_and_never_crash_collector(row, expected):
    data, receipt = facts()
    data["provider_errors"] = [row]
    _, proof = judge(data, [receipt])
    assert proof["evaluation_validity"]["status"] == expected
    observed = proof["error_observations"][0]
    assert all(observed[key] == value for key, value in row.items())
    assert proof["accepted"] is (expected == "valid")


@pytest.mark.parametrize("stamp,expected", [(19, "invalid"), (21, "valid"), (None, "unknown")])
def test_entry_cancellation_requires_observed_post_signal_ordering(stamp, expected):
    data, receipt = facts()
    error = {"type": "CancelledError", "message": ""}
    data["entry"].update(status="interrupted", error=error)
    data["error_observations"] = [{"source": "adapter", "error": error, "wall_monotonic": stamp}]
    _, proof = external.adjudicate(
        data, [receipt], OPENING, {"success": True}, native_classification="capability_success",
        stop_reason="native_goal_success", terminal_monotonic=20,
        stop_signal={"sent": True, "signal": "SIGINT", "requested_monotonic": 20.5})
    assert proof["evaluation_validity"]["status"] == expected
    assert proof["accepted"] is (expected == "valid")
    assert len(proof["error_observations"]) == 2


def test_all_seventeen_old_definitions_and_scoring_are_byte_preserved():
    from .historical import historical_fixture
    package = Path(run.__file__).parent
    fixtures = Path(__file__).parent / "fixtures"
    baseline = json.loads(historical_fixture("accepted-contracts.json").read_text())
    for name in ("scene.py", "suite.py", "setup.py", "crafting.py",
                 "combat.py", "navigation.py", "mixed.py", "oracles.py"):
        assert run.lifecycle.sha(package / name) == baseline["files"][name]
    definitions = json.loads((package / "scenes.json").read_text())
    legacy = json.loads((fixtures / "legacy-scenes.json").read_text())
    selected_definitions = {row["id"]: row for row in definitions["scenes"]}
    assert len(legacy["scenes"]) == 17
    assert sum(len(row["terrains"]) for row in legacy["scenes"]) == 25
    assert all(selected_definitions[row["id"]] == row for row in legacy["scenes"])
    for name in ("schema", "seeds"):
        assert definitions[name] == legacy[name]


@pytest.mark.parametrize("case", ["missing-entry", "missing-model", "entry-error", "cleanup-error", "unknown"])
def test_no_silent_acceptance_of_entry_cleanup_or_unknown_effects(case):
    data, receipt = facts()
    unknown = []
    if case == "missing-entry":
        data["entry_receipt_present"] = False
    elif case == "missing-model":
        data["model_requests"] = []
    elif case == "entry-error":
        data["entry"].update(status="error", error={"type": "ImportError"})
    elif case == "cleanup-error":
        data["entry"]["cleanup_errors"] = [{"type": "RuntimeError", "message": "cleanup failed"}]
    else:
        unknown = [{"request_id": "still-in-flight"}]
    classification, proof = judge(data, [receipt], unknown=unknown)
    assert classification == "capability_success"
    assert proof["evaluation_validity"]["status"] in {"invalid", "unknown"}
    assert not proof["accepted"]


def test_native_outcome_full_exception_group_and_task_clock_survive_cleanup(tmp_path):
    clock = AttemptTiming()
    clock.mark("agent_launch_start")
    clock.mark("agent_launched")
    clock.mark("harness_entry")
    record = {"mode": "capability", "classification": "agent_failure", "stop_reason": "player_death",
              "score": {"success": False, "dead": True}, "cleanup_complete": False}
    results.terminal(record, clock)
    before = copy.deepcopy(record["task_terminal"])
    failure = ExceptionGroup("outer", [ExceptionGroup("inner", [ConnectionResetError("late")]),
                                       RuntimeError("cleanup")])
    results.error(record, failure, source="runtime", phase="cleanup")
    record.update(classification="infrastructure_error", error=exception_detail(failure))
    results.finalize(record, clock)
    record["timing"] = clock.finish("player_death")
    (tmp_path / "result.json").write_text(json.dumps(record))
    persisted = json.loads((tmp_path / "result.json").read_text())
    assert persisted["task_terminal"] == before
    assert persisted["classification"] == "agent_failure"
    assert persisted["evaluation_validity"]["status"] == "invalid"
    assert persisted["error"]["exceptions"][0]["exceptions"][0]["type"] == "ConnectionResetError"
    assert persisted["timing"]["points"]["terminal"] == before["observed_at"]
    assert persisted["timing"]["task_elapsed_seconds"] < persisted["timing"]["attempt_elapsed_seconds"]
    assert common.error_record(failure)["exceptions"][0]["exceptions"][0]["message"] == "late"


def test_cyclic_exception_context_is_retained_as_reference_not_infinite_recursion():
    outer, inner = RuntimeError("outer"), ValueError("inner")
    outer.__context__, inner.__context__ = inner, outer
    for serialize in (exception_detail, common.error_record):
        detail = serialize(outer)
        assert detail["context"]["type"] == "ValueError"
        assert detail["context"]["context"]["recursive_reference"]


@pytest.mark.parametrize("kind,expected", [("NativeError", "valid"), ("TimeoutError", "invalid")])
def test_native_refusal_is_normal_mechanics_but_unknown_rpc_failure_is_not(kind, expected):
    data, receipt = facts()
    rows = [{
        "kind": "request_error", "wall_monotonic": 19, "request_id": "earlier",
        "error": {"type": kind, "message": "mock native refusal or RPC timeout"},
    }]
    collected = evidence.sdk_facts(rows, data["entry"])
    data["error_observations"] = collected["error_observations"]
    _, proof = judge(data, [receipt])
    assert proof["evaluation_validity"]["status"] == expected
    assert len(proof["error_observations"]) == 1


@pytest.mark.parametrize("case,expected", [("normal", "valid"), ("cancelled", "valid"),
                                         ("early-cancel", "unknown"), ("provider", "unknown"),
                                         ("entry-error", "unknown"), ("no-model", "invalid")])
def test_gameagent_negative_measurements_have_independent_validity(case, expected):
    data = {
        "entry_receipt_present": True, "record_counts": {"provider_model_request": 1},
        "entry": {"status": "idle", "primary_error": None, "cleanup_errors": []},
        "provider_errors_excluding_cancellation": [], "provider_cancellation_events": [],
    }
    if case in {"cancelled", "early-cancel"}:
        data["entry"].update(status="interrupted", primary_error={"type": "CancelledError"})
        data["provider_cancellation_events"] = [{
            "host_recorded_at": "2026-10-15T00:00:02+00:00" if case == "cancelled"
            else "2026-10-15T00:00:00+00:00", "error": {"type": "CancelledError"}}]
    elif case == "provider":
        data["provider_errors_excluding_cancellation"] = [{"type": "ProviderHTTPError"}]
    elif case == "entry-error":
        data["entry"].update(status="failed", primary_error={"type": "ImportError"})
    elif case == "no-model":
        data["record_counts"] = {}
    proof = evidence.gameagent_evaluation(
        data, stop_reason="player_death", stop_signal={"sent": True},
        stop_requested_at="2026-10-15T00:00:01+00:00")
    assert proof["evaluation_validity"]["status"] == expected
    assert not proof["accepted"]  # A valid death measurement is not a successful task.


def test_invalid_timing_cannot_accept_an_otherwise_joined_native_success(tmp_path, monkeypatch):
    async def simulated(args, timing, out):
        record = {"mode": "capability", "classification": "capability_success",
                  "stop_reason": "native_goal_success", "score": {"success": True, "dead": False},
                  "cleanup_complete": True, "model_success_evidence": {
                      "accepted": True, "evaluation_validity": {"status": "valid", "reasons": []}}}
        timing.record = record
        timing.mark("agent_launch_start")
        timing.mark("agent_launched")
        results.terminal(record, timing)
        timing.points["terminal"]["monotonic"] = timing.points["agent_launch_start"]["monotonic"] - 1
        return record

    monkeypatch.setattr(run, "_episode", simulated)
    monkeypatch.setattr(run.pins, "load", lambda *args, **kwargs: mock_selection(tmp_path))
    args = run.parser().parse_args([
        "--pins", str(tmp_path / "mock"), "--pins-sha256", "0" * 64,
        "--out", str(tmp_path / "attempt"), "--output-root", str(tmp_path),
        "--mode", "capability", "--harness", "external"])
    result = asyncio.run(run.episode(args))
    assert result["task_terminal"]["native_success"]
    assert result["classification"] == "capability_success"
    assert result["timing"]["task_duration_null_reason"] == "invalid_lifecycle_chronology"
    assert result["evaluation_validity"]["status"] == "invalid"
    assert not result["evaluation_validity"]["accepted"]


@pytest.mark.parametrize("compatibility,scene,horizon,expected", [
    (None, "future-scene", None, None), (None, "future-scene", 73, 73),
    ("legacy-e244", "C1", None, 180), ("legacy-e244", "C1", 73, 73),
])
def test_historical_horizons_are_explicit_compatibility_not_generic_policy(
        tmp_path, monkeypatch, compatibility, scene, horizon, expected):
    def selected_adapter(*args, **kwargs):
        raise RuntimeError("mock adapter selection; no entry launched")

    monkeypatch.setattr(run.pins, "load", lambda *args, **kwargs: {})
    monkeypatch.setattr(external, "Adapter", selected_adapter)
    args = run.parser().parse_args([
        "--pins", str(tmp_path / "mock"), "--pins-sha256", "0" * 64,
        "--out", str(tmp_path / "attempt"), "--mode", "capability", "--harness", "external",
        "--scene", scene])
    args.compatibility, args.evaluation_horizon = compatibility, horizon
    with pytest.raises(RuntimeError, match="mock adapter selection"):
        asyncio.run(run._episode(args, AttemptTiming(), tmp_path))
    assert args.evaluation_horizon == expected


def test_v1_compatibility_uses_the_declaration_not_an_implicit_backbone():
    configuration, _, legacy = capabilities.negotiate({
        "schema": "rpg-external-adapter/1", "model": "legacy-test-model", "reasoning_effort": "low",
    }, {})
    assert legacy and configuration["model"] == "legacy-test-model"
    assert configuration["reasoning_effort"] == "low"
def control_facts():
    args = {"owner": "chosen", "generation": 4, "sequence": 1, "expected_session_id": "native",
            "actor": OPENING["control_actor"], "movement": [.2, 0], "target": None,
            "primary": False, "block": False, "expected_protection_generation": 3}
    data, receipt = facts()
    request = {"id": "control", "cmd": "set_control_state", "args": args}
    data["tool_calls"] = [{"id": "call", "name": "game_set_control_state", "arguments": args}]
    data["submissions"] = [{"call_id": "call", "request": request}]
    receipt.update(request=request, response={
        "id": "control", "session_id": "native", "ok": True, "result": {
            "status": "applied", "owner": "chosen", "generation": 4, "sequence": 1,
            "actor": OPENING["control_actor"], "session_id": "native",
            "accepted_tick": 9, "applied_tick": 9, "gameplay_success": None}})
    return data, receipt


def test_continuous_control_intention_receipt_join_not_submission_or_applied_success():
    data, receipt = control_facts()
    _, proof = judge(data, [receipt])
    assert proof["accepted"] and proof["joins"][0]["command"] == "set_control_state"
    _, proof = judge(data, [receipt], success=False)
    assert not proof["accepted"]  # Applied input alone cannot pass the oracle.
    _, proof = judge(data, [])
    assert not proof["accepted"]


@pytest.mark.parametrize("key,value", [
    ("owner", "other"), ("generation", 5), ("generation", True), ("sequence", 2),
    ("sequence", 1.0), ("session_id", "other"), ("actor", {"uid": 42, "lifecycle": "stale"}),
    ("applied_tick", True), ("status", "submitted"),
])
def test_control_receipt_identity_lifecycle_generation_and_integer_fences(key, value):
    data, receipt = control_facts()
    receipt["response"]["result"][key] = value
    assert not judge(data, [receipt])[1]["accepted"]


@pytest.mark.parametrize("key,value", [("generation", True), ("sequence", 1.0),
                                      ("expected_protection_generation", "3")])
def test_strict_control_request_types_are_not_coerced(key, value):
    data, receipt = control_facts()
    data["submissions"][0]["request"]["args"][key] = value
    with pytest.raises(ValueError):
        judge(data, [receipt])


def test_generic_capabilities_support_non_luna_and_reject_unimplemented_modes_profiles():
    manifest = {
        "schema": capabilities.SCHEMA,
        "capabilities": {"native_protocol": "0.2", "integer_transport": "strict",
                         "asynchronous_receipts": True, "observation_modes": ["structured"],
                         "action_profiles": ["raw-native-v2"],
                         "model_profiles": [{"model": "independent-test-model",
                                             "reasoning_efforts": ["low", "high"]}]},
        "default_configuration": {"model": "independent-test-model", "reasoning_effort": "low",
                                  "observation_mode": "structured", "action_profile": "raw-native-v2"},
    }
    configuration, _, legacy = capabilities.negotiate(manifest, {"reasoning_effort": "high"})
    assert not legacy and configuration["model"] == "independent-test-model"
    assert configuration["reasoning_effort"] == "high"
    for request in ({"observation_mode": "image"}, {"observation_mode": "hybrid"},
                    {"action_profile": "hidden-admin"}, {"model": "undeclared"},
                    {"reasoning_effort": "none"}):
        with pytest.raises(ValueError, match="Unsupported"):
            capabilities.negotiate(manifest, request)


def test_sdk_free_common_module_and_dsh_import_without_copilot():
    command = (
        "import sys, os; sys.modules['copilot'] = None; "
        f"sys.path[:0] = [{str(Path(run.__file__).parents[2])!r}, "
        f"{str(Path(__file__).parent / 'fixtures/adapters/dsh-adapter')!r}]; "
        f"os.environ['RPG_COMMON_INTERFACE'] = {str(Path(common.__file__))!r}; "
        "from scripts.rpg_sdk_agent import GameTools, ToolInvocation; "
        "from dsh_external_v1.dsh_tools import PlayerTools; "
        "assert not any(k.startswith('copilot.') for k in sys.modules)"
    )
    subprocess.run([sys.executable, "-B", "-c", command], check=True, capture_output=True, text=True)


@pytest.mark.parametrize("adapter_id", ["deepseek-harness", "lightweight-mock"])
def test_registered_adapters_launch_same_contract_without_judge_context(adapter_id, tmp_path):
    config = {"mock_provider": "http://127.0.0.1:18000"} if adapter_id == "deepseek-harness" else {}
    adapter = external.Adapter(selected(), adapter_id, config, engineering=True)
    plan = adapter.launch(task="macro goal only", socket=Path("/private.sock"),
                          output=tmp_path / "adapter", output_root=tmp_path,
                          python=sys.executable, common_interface=str(HARNESS / "scripts/rpg_sdk_agent.py"))
    assert plan.command and adapter.context["schema"] == "rpg-external-context/2"
    assert not adapter.legacy
    assert set(adapter.context).isdisjoint({"fixture", "score", "admin", "definition"})
    with pytest.raises(ValueError, match="Unsupported observation"):
        external.Adapter(selected(), adapter_id, config, engineering=True,
                         requested={"observation_mode": "image"})


def test_parent_freeze_preserves_hash_verified_runtime_bin_symlinks(tmp_path):
    (tmp_path / "implementation").write_text("mock binary contents")
    (tmp_path / "launcher").symlink_to("implementation")
    freeze = tmp_path / "freeze.json"
    freeze.write_text(json.dumps({"root": str(tmp_path), "files": {
        "launcher": run.lifecycle.sha(tmp_path / "implementation")}}))
    external.verify_freeze(freeze, run.lifecycle.sha(freeze))
    (tmp_path / "implementation").write_text("changed")
    with pytest.raises(ValueError, match="frozen input changed"):
        external.verify_freeze(freeze, run.lifecycle.sha(freeze))


@pytest.mark.parametrize("path", ["sdk", "dsh"])
def test_two_independent_adapter_collectors_exercise_same_evaluator_with_mock_control(tmp_path, monkeypatch, path):
    """Real handlers/NDJSON reader, fake pipe bridge and fabricated model envelopes."""
    async def check():
        fixture = PipeFixture(tmp_path)
        original = fixture.respond

        def respond(request, value):
            if request["cmd"] == "set_control_state":
                args = request["args"]
                value = {"status": "applied", **{key: args[key] for key in (
                    "owner", "generation", "sequence", "actor")},
                         "session_id": "synthetic", "accepted_tick": 9, "applied_tick": 9}
            original(request, value)

        fixture.respond = respond
        await fixture.start()
        log = common.Evidence(tmp_path / "events.ndjson")
        client = common.NativeClient(fixture.boundary.path, log)
        await client.connect()
        args = control_facts()[0]["tool_calls"][0]["arguments"]
        args = {**args, "expected_session_id": "synthetic"}
        log.write("mocked_engineering_only", native=False, model=False)
        try:
            if path == "sdk":
                from copilot.tools import ToolInvocation
                tools = common.GameTools(client, log)
                options = common.session_options(tools, tmp_path, tmp_path, "mock-model", None)
                tool = next(tool for tool in options["tools"] if tool.name == "game_set_control_state")
                log.write("sdk_event", event={"type": "model.call_start", "data": {}})
                response = await tool.handler(ToolInvocation(
                    session_id="fabricated-sdk", tool_call_id="call",
                    tool_name=tool.name, arguments=args))
                assert response.result_type == "success"
            else:
                monkeypatch.syspath_prepend(str(Path(__file__).parent / "fixtures/adapters/dsh-adapter"))
                monkeypatch.setenv("RPG_COMMON_INTERFACE", str(Path(common.__file__)))
                tools = importlib.import_module("dsh_external_v1.dsh_tools").PlayerTools(client)
                log.write("dsh_entry_ready", mocked=True)
                log.write("provider_request", model="gpt-6-luna", reasoning_effort="none", offline_mock=True)
                log.write("dsh_jsonrpc", envelope={"method": "session.event", "params": {"event": {
                    "type": "tool/call", "data": {"callId": "call", "name": "game_set_control_state",
                                                 "arguments": json.dumps(args)}}}})
                response = await tools.execute({
                    "id": "broker", "name": "game_set_control_state", "arguments": args,
                    "context": {"harness": "official-deepseek-harness",
                                "session_id": "fabricated-dsh", "call_id": "call"}})
                assert response["ok"]
        finally:
            await client.close()
            await fixture.shutdown()
            log.close()
        entry = {"status": "idle", "cleanup_errors": [],
                 "runtime_cleanup": {"owned_processes": [], "stopped": True}}
        if path == "sdk":
            (tmp_path / "result.json").write_text(json.dumps(entry))
            data = evidence.select_sdk(tmp_path)["common_facts"]
            # The SDK collector normally requires actual SDK events. These are fabricated.
            for request in data["model_requests"]:
                request["synthetic"] = True
        else:
            (tmp_path / "entry.json").write_text(json.dumps(entry))
            adapter = external.Adapter(selected(), "deepseek-harness", {}, engineering=True)
            data = adapter.module.collect_evidence({
                "output": str(tmp_path), "adapter_id": adapter.id, "player_interface": external.PLAYER})
        assert len(data["submissions"]) == 1
        _, proof = external.adjudicate(
            data, fixture.bridge.measured_receipts,
            {**OPENING, "session_id": "synthetic"}, {"success": True},
            native_classification="capability_success", stop_reason="native_goal_success",
            stop_signal={}, terminal_monotonic=1e12)
        assert len(proof["joins"]) == 1
        assert proof["evaluation_validity"]["status"] == "valid"
        assert proof["synthetic"] and not proof["accepted"]
    asyncio.run(check())


def test_late_receipt_resolves_unknown_and_duplicate_request_never_mutates_again(tmp_path):
    async def check():
        fixture = PipeFixture(tmp_path, delay=.03)
        try:
            with pytest.raises(TimeoutError):
                await fixture.bridge.envelope(step(), origin="agent", timeout=.001)
            assert fixture.bridge.effect_observations["step"]["effect_status"] == "unknown"
            with pytest.raises(ValueError, match="duplicate"):
                await fixture.bridge.envelope(step(), origin="agent")
            await asyncio.sleep(.04)
            assert fixture.bridge.effect_observations["step"]["effect_status"] == "known"
            assert fixture.bridge.measured_receipts[0]["response"]["result"]["status"] == "completed"
            with pytest.raises(ValueError, match="duplicate"):
                await fixture.bridge.envelope(step(), origin="agent")
            assert len([row for row in fixture.requests if row["cmd"] == "step"]) == 1
        finally:
            await fixture.start()
            await fixture.shutdown()
    asyncio.run(check())


@pytest.mark.parametrize("outcome,cleanup_failure,unknown", [
    ("success", False, False), ("death", False, False), ("horizon", False, False),
    ("success", True, False), ("death", True, False), ("success", False, True),
])
def test_wired_episode_persists_native_terminal_after_mock_teardown(
        tmp_path, monkeypatch, outcome, cleanup_failure, unknown):
    """Exercise the real episode orchestration; all process/native/model surfaces mocked."""
    data, receipt = facts()
    data["model_requests"][0]["synthetic"] = True
    data["entry"].update(runtime_cleanup={}, provider_usage=[])
    fixture = {"task": "mock goal", "opening": {
        **OPENING, "tick": 1, "survival": {"buffs": [{"kind": "Invulnerability"}]}}}
    baseline = mock_selection(tmp_path)
    instances = []

    class Bridge:
        def __init__(self, out, name, *args):
            self.name = name
            self.process = SimpleNamespace(returncode=0)
            self.cleanup = {}
            self.native_events = []
            self.error_observations = []
            self.effect_observations = ({"uncertain": {
                "effect_status": "unknown", "request": {"id": "uncertain"}}} if unknown else {})
            self.measured_receipts = [receipt]
            instances.append(self)

        async def start(self):
            pass

        async def request(self, command, **kwargs):
            return {**fixture["opening"], "tick": 2}

        async def close(self):
            self.cleanup.update(stopped=True)
            if self is instances[-1] and cleanup_failure:
                raise ExceptionGroup("mock cleanup", [
                    ExceptionGroup("nested", [ConnectionResetError("post-terminal reset")])])

    class Boundary:
        def __init__(self, actor, path):
            self.path, self.errors, self.error_details, self.peer = path, [], [], {}

        async def start(self):
            pass

        def begin_close(self):
            pass

        async def close(self):
            pass

    @asynccontextmanager
    async def server(out, record, config):
        try:
            yield 12345, tmp_path
        finally:
            record["server_stopped"] = True

    class Player:
        pid = 999999999
        returncode = None

        def send_signal(self, signal):
            pass

        async def wait(self):
            self.returncode = -2

    def select_sdk(path):
        data["provider_errors"] = [{"wall_monotonic": time.monotonic(),
                                    "error": {"type": "ClientConnectionResetError", "message": "mock reset"}}]
        return {"entry_receipt_present": True, "entry": data["entry"],
                "runtime_model_metadata": "unknown", "common_facts": data}

    monkeypatch.setattr(run.pins, "load", lambda *args, **kwargs: baseline)
    monkeypatch.setattr(run, "NativeBridge", Bridge)
    monkeypatch.setattr(run, "EpisodeBoundary", Boundary)
    monkeypatch.setattr(run, "private_server", server)
    # This lifecycle fixture mocks all processes. Dependency/installed-entry
    # preflight is exercised separately by test_process_contract, before worlds.
    monkeypatch.setattr(run.launching, "preflight_builtin", AsyncMock(return_value={"synthetic": True}))
    monkeypatch.setattr(run.setup, "command", AsyncMock())
    monkeypatch.setattr(run.setup, "wait_observation", AsyncMock(return_value=fixture["opening"]))
    monkeypatch.setattr(run.setup, "validate_opening", AsyncMock())
    monkeypatch.setattr(run.suite, "setup", AsyncMock(return_value=fixture))
    monkeypatch.setattr(run.suite, "score", lambda *args: {
        "success": outcome == "success", "dead": outcome == "death"})
    monkeypatch.setattr(run, "measurement_validity", lambda *args: {"valid": True})
    monkeypatch.setattr(run, "log_supplies", lambda *args: None)
    monkeypatch.setattr(run.asyncio, "create_subprocess_exec", AsyncMock(return_value=Player()))
    monkeypatch.setattr(run.evidence, "select_sdk", select_sdk)
    monkeypatch.setattr(run.lifecycle, "process_start_ticks", lambda pid: 4242)
    monkeypatch.setattr(run.lifecycle, "listener_inodes", lambda port: ["mock-listener"])
    args = run.parser().parse_args([
        "--pins", str(tmp_path / "mock-pins"), "--pins-sha256", "0" * 64,
        "--out", str(tmp_path / "attempt"), "--output-root", str(tmp_path),
        "--mode", "capability", "--harness", "copilot-sdk", "--evaluation-horizon", "0.000000001"])
    result = asyncio.run(run.episode(args))
    assert json.loads((args.out / "result.json").read_text()) == result
    assert result["task_terminal"]["outcome"] == outcome
    assert result["classification"] == {
        "success": "capability_success", "death": "agent_failure",
        "horizon": "evaluation_horizon_without_success"}[outcome]
    assert result["timing"]["points"]["terminal"] == result["task_terminal"]["observed_at"]
    assert result["timing"]["cleanup_elapsed_seconds"] >= 0
    assert not result["evaluation_validity"]["accepted"]  # Fabricated model events, never model evidence.
    assert len(result["unknown_effects"]) == int(unknown)
    if cleanup_failure:
        assert result["error"]["exceptions"][0]["exceptions"][0]["type"] == "ConnectionResetError"
        assert result["evaluation_validity"]["status"] == "invalid"
    else:
        assert (args.out / "native-receipts.json").is_file()
