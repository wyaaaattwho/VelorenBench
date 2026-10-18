import asyncio
import copy
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts.rpg_benchmark import capabilities, external, providers, run
from scripts.rpg_benchmark.responses_adapter import Adapter
from scripts.rpg_benchmark.timing import AttemptTiming


@pytest.mark.parametrize("mode,profile", [
    ("structured", "ordinary-renderer-native-v1"),
    ("image", "visual-motor-v1"),
    ("hybrid", "ordinary-renderer-native-v1"),
])
def test_mode_mapping_applies_to_explicit_pairs_and_defaults(mode, profile):
    manifest = providers.manifest(providers.LUNA, mode)
    assert capabilities.negotiate(manifest, {})[0]["action_profile"] == capabilities.default_profile("renderer", mode)
    legacy = copy.deepcopy(manifest)
    legacy["default_configuration"]["action_profile"] = profile
    assert capabilities.negotiate(legacy, {})[0]["action_profile"] == profile
    assert capabilities.negotiate(manifest, {"action_profile": profile})[0]["action_profile"] == profile
    for wrong in {"ordinary-renderer-native-v1", "visual-motor-v1", "raw-native-v2"} - {profile}:
        with pytest.raises(ValueError):
            capabilities.negotiate(manifest, {"action_profile": wrong})
        invalid = copy.deepcopy(manifest)
        invalid["default_configuration"]["action_profile"] = wrong
        with pytest.raises(ValueError):
            capabilities.negotiate(invalid, {})


@pytest.mark.parametrize("mode", ["structured", "image", "hybrid"])
@pytest.mark.parametrize("mapping", [None, {}, {"structured": "ordinary-renderer-native-v1"}])
def test_multimodal_mapping_is_required_and_well_formed(mode, mapping):
    manifest = providers.manifest(providers.LUNA, mode)
    if mapping is None:
        del manifest["capabilities"]["action_profiles_by_observation_mode"]
    else:
        manifest["capabilities"]["action_profiles_by_observation_mode"] = mapping
    with pytest.raises(ValueError):
        capabilities.negotiate(manifest, {})


@pytest.mark.parametrize("legacy", [False, True])
def test_legitimate_structured_only_without_mapping_remains_compatible(legacy):
    if legacy:
        manifest = {"schema": "rpg-external-adapter/1", "model": "old", "reasoning_effort": "none"}
    else:
        manifest = providers.manifest(providers.LUNA, "structured")
        manifest["capabilities"].pop("action_profiles_by_observation_mode")
        manifest["capabilities"]["observation_modes"] = ["structured"]
    assert capabilities.negotiate(manifest, {})[0]["observation_mode"] == "structured"
    for mode in ("image", "hybrid"):
        with pytest.raises(ValueError):
            capabilities.negotiate(manifest, {"observation_mode": mode})


def adapter_at(path):
    args = SimpleNamespace(
        adapter_config=json.dumps({"mock_provider": "http://127.0.0.1:1234", "provider": providers.LUNA}),
        observation_mode="structured", model=None, reasoning_effort=None, action_profile=None,
        recordings=path / "frames")
    adapter = Adapter(args)
    adapter.output = path
    return adapter


@pytest.mark.parametrize("entry", [None, "{", "[]", '{"status":"starting","cleanup_errors":[]}',
                                  '{"status":"cancelled","cleanup_errors":[]}',
                                  '{"status":"idle","cleanup_errors":"bad"}',
                                  '{"status":"cancelled","cleanup_errors":[],"terminal":[]}'])
@pytest.mark.parametrize("tail", [b'{"kind":', b'\xff'])
def test_partial_collection_keeps_complete_provider_and_intent_records(entry, tail, tmp_path):
    adapter = adapter_at(tmp_path)
    if entry is not None:
        (tmp_path / "entry.json").write_text(entry)
    rows = [
        {"kind": "entry_ready"},
        {"kind": "provider_request", "model": "gpt-6-luna", "effort": "none", "synthetic": True},
        {"kind": "model_tool_call", "id": "call", "name": "game_step",
         "arguments": rows_args()},
        {"kind": "tool_native_submission", "tool_call_id": "call",
         "request": {"id": "native", "cmd": "step", "args": rows_args()}},
    ]
    (tmp_path / "events.ndjson").write_bytes(
        "".join(json.dumps(row) + "\n" for row in rows).encode() + tail)
    before = (tmp_path / "events.ndjson").read_bytes()
    facts = adapter.collect()
    assert facts["entry"]["status"] == "incomplete" and not facts["cleanup_complete"]
    assert len(facts["model_requests"]) == len(facts["tool_calls"]) == len(facts["submissions"]) == 1
    assert len(facts["collection_errors"]) == 2
    assert (tmp_path / "events.ndjson").read_bytes() == before
    _, proof = external.adjudicate(
        facts, [], {"session_id": "s", "self_identity": {"uid": 1}}, {"success": False},
        native_classification="operator_interrupted", stop_reason="operator_interrupted", stop_signal={})
    assert not proof["accepted"] and proof["evaluation_validity"]["status"] != "valid"


def rows_args():
    return {"action": {"type": "jump", "action_id": "a"}}


def test_invalid_provider_identity_is_reported_not_accepted_as_partial_success(tmp_path):
    adapter = adapter_at(tmp_path)
    (tmp_path / "entry.json").write_text('{"status":"idle","cleanup_errors":[],"terminal":{"monotonic":1}}')
    (tmp_path / "events.ndjson").write_text(json.dumps({
        "kind": "provider_request", "model": "wrong", "effort": "none", "synthetic": True}) + "\n")
    facts = adapter.collect()
    assert not facts["model_requests"] and facts["collection_errors"]


def test_late_entry_timestamp_is_retained_without_reordering_terminal(tmp_path):
    adapter = adapter_at(tmp_path)
    timing = AttemptTiming()
    timing.mark("terminal")
    terminal = timing.points["terminal"]
    (tmp_path / "harness-entry.json").write_text(json.dumps({
        "monotonic": terminal["monotonic"] + .01,
        "wall": datetime.now(timezone.utc).isoformat()}))
    adapter.observe_entry(timing)
    assert "harness_entry" not in timing.points and "late_harness_entry" in timing.points
    assert timing.finish("operator_interrupted")["points"]["terminal"] == terminal


def test_explicit_null_mapping_is_not_legacy_compatibility():
    manifest = providers.manifest(providers.LUNA, "structured")
    manifest["capabilities"]["observation_modes"] = ["structured"]
    manifest["capabilities"]["action_profiles_by_observation_mode"] = None
    with pytest.raises(ValueError):
        capabilities.negotiate(manifest, {})


def test_impossible_entry_timestamp_is_not_inserted_in_lifecycle(tmp_path):
    adapter = adapter_at(tmp_path)
    timing = AttemptTiming()
    timing.mark("agent_launched")
    (tmp_path / "harness-entry.json").write_text(json.dumps({
        **timing.points["agent_launched"],
        "monotonic": timing.points["agent_launched"]["monotonic"] - 1}))
    with pytest.raises(ValueError, match="precedes"):
        adapter.observe_entry(timing)
    assert "harness_entry" not in timing.points


def test_invalid_clock_does_not_mask_primary_interruption(tmp_path, monkeypatch):
    async def simulated(args, timing, out):
        record = {"phase": "measurement", "mode": "capability", "cleanup_complete": True}
        timing.record = record
        timing.mark("agent_launch_start")
        timing.mark("agent_launched")
        timing.mark("harness_entry", {
            **timing.points["agent_launch_start"],
            "monotonic": timing.points["agent_launch_start"]["monotonic"] - 1})
        run.record_failure(record, timing, asyncio.CancelledError("primary"))
        return record
    monkeypatch.setattr(run, "_episode", simulated)
    monkeypatch.setattr(run.pins, "load", lambda *args, **kwargs: {
        "output_root": str(tmp_path), "protected_paths": []})
    monkeypatch.setattr(run.lifecycle, "protection_snapshot", lambda: [])
    args = run.parser().parse_args([
        "--config", str(tmp_path / "unused"), "--config-sha256", "0" * 64,
        "--out", str(tmp_path / "attempt"), "--mode", "capability"])
    result = asyncio.run(run.episode(args))
    assert result["error"]["type"] == "CancelledError"
    assert result["task_terminal"]["classification"] == "operator_interrupted"
    assert result["timing"]["terminal_reason"] == "operator_interrupted"
    assert "timing_error" in result["timing"]
    assert not result["evaluation_validity"]["accepted"]


@pytest.mark.parametrize("stage", ["pre_entry", "measurement", "post_terminal", "normal", "late_goal"])
@pytest.mark.parametrize("collection_error", [False, True, "partial"])
@pytest.mark.parametrize("unknown", [False, True])
@pytest.mark.parametrize("world_event", [False, True])
def test_wired_interruption_collects_once_after_closure_and_keeps_terminal(
        stage, collection_error, unknown, world_event, tmp_path, monkeypatch):
    order, instances, records = [], [], []
    player = SimpleNamespace(
        pid=999999999,
        returncode=None if stage in {"pre_entry", "measurement", "late_goal"} else 0)
    opening = {"tick": 1, "session_id": "s", "self_identity": {"uid": 1},
               "survival": {"buffs": [{"kind": "Invulnerability"}]}}
    fixture = {"opening": opening, "task": "test goal"}
    if world_event:
        fixture["event_state"] = {"status": "armed"}
    definition = {"id": "C1", "kind": "crafting", "terrains": ["natural"], "gameplay_mode": "economy"}
    selection = {
        "dependencies": {"scene_manifest": {"path": "unused"}, "server": {"path": "/server"},
                         "bridge": {"path": "/bridge"}, "python": {"path": "/python"},
                         "sdk": {"path": "/sdk"}},
        "assets": {"root": "/assets"}, "engine_root": "/", "seeds": [130626853],
        "native_backend_identity": {}, "content_sha256": "0" * 64,
        "output_root": str(tmp_path), "protected_paths": []}

    class Bridge:
        def __init__(self, *args):
            self.process = SimpleNamespace(returncode=0)
            self.observations = 0
            self.cleanup, self.native_events, self.error_observations = {}, [], []
            self.measured_receipts = []
            self.effect_observations = {"late": {"effect_status": "unknown"}}
            instances.append(self)

        async def start(self):
            pass

        async def request(self, *args, **kwargs):
            if stage == "pre_entry":
                raise asyncio.CancelledError("before entry")
            self.observations += 1
            if stage == "late_goal":
                player.returncode = 0
            return {**opening, "tick": self.observations + 1}

        async def close(self):
            self.cleanup["stopped"] = True
            if self is instances[-1]:
                order.append("actor_closed")
                self.measured_receipts.append({"late": "durable-receipt"})
                self.native_events.append({"late": "durable-event"})
                if world_event:
                    fixture["event_state"]["late_effect"] = "observed during actor closure"
                if not unknown:
                    self.effect_observations["late"]["effect_status"] = "known"

    class Boundary:
        def __init__(self, actor, path):
            self.path, self.errors, self.error_details, self.peer = path, [], [], {}

        async def start(self):
            pass

        def begin_close(self):
            pass

        async def close(self):
            order.append("boundary_closed")

    @asynccontextmanager
    async def server(out, record, config):
        records.append(record)
        try:
            yield 12345, tmp_path
        finally:
            record["server_stopped"] = True
            order.append("server_closed")

    class FakeAdapter:
        async def preflight(self, **kwargs):
            return {"synthetic": True}

        id, engineering, legacy = "mock", True, False
        manifest = {"tool_track": "test"}
        configuration = {"model": "test", "reasoning_effort": "none"}
        capabilities = {}

        def __init__(self, *args, **kwargs):
            self.context = {}

        def launch(self, **kwargs):
            return SimpleNamespace(command=["/test"], env={}, cwd=tmp_path)

        def observe_entry(self, timing):
            if stage != "pre_entry":
                timing.mark("harness_entry", timing.points["agent_launched"])

        def collect(self):
            assert order.count("player_closed") == order.count("actor_closed") == 1
            assert "boundary_closed" in order and "server_closed" in order
            order.append("collect")
            legacy = {"entry": {"status": "cancelled", "cleanup_errors": []},
                      "entry_receipt_present": stage != "pre_entry",
                      "record_counts": {"provider_model_request": 1},
                      "provider_errors_excluding_cancellation": []}
            run.evidence.gameagent_evaluation(
                legacy, stop_reason=records[0]["stop_reason"],
                stop_signal=records[0]["cleanup"]["agent"].get("stop_signal", {}),
                stop_requested_at=records[0]["evaluator_stop_requested_at"])
            if collection_error is True:
                raise ValueError("malformed external evidence")
            return {
                "entry": {"status": "cancelled" if stage != "normal" else "idle", "cleanup_errors": []},
                "entry_receipt_present": stage != "pre_entry",
                "model_requests": [{"synthetic": True}] if stage != "pre_entry" else [],
                "tool_calls": [], "submissions": [], "provider_errors": [],
                "owned_processes": [], "cleanup_complete": True, "runtime_model_metadata": "unknown",
                "collection_errors": [{"error": "partial event"}] if collection_error == "partial" else []}

    async def stop_player(player, start, record):
        if stage == "post_terminal":
            asyncio.current_task().get_loop().call_soon(parent_task[0].cancel)
            await asyncio.sleep(0)
        order.append("player_closed")
        player.returncode = -2
        record.update(stopped=True, stop_signal={"signal": "SIGINT", "sent": True,
                                                "requested_monotonic": run.time.monotonic()})

    parent_task = []
    real_sleep = asyncio.sleep
    async def sleep(delay):
        if stage == "measurement" and delay == .25:
            raise asyncio.CancelledError("during measurement")
        await real_sleep(delay)

    async def execute(args):
        parent_task.append(asyncio.current_task())
        return await run.episode(args)

    monkeypatch.setattr(run.pins, "load", lambda *args, **kwargs: selection)
    monkeypatch.setattr(run.suite, "load", lambda *args: {"C1": definition})
    monkeypatch.setattr(run, "NativeBridge", Bridge)
    monkeypatch.setattr(run, "EpisodeBoundary", Boundary)
    monkeypatch.setattr(run, "private_server", server)
    monkeypatch.setattr(run.external, "Adapter", FakeAdapter)
    monkeypatch.setattr(run.setup, "command", AsyncMock())
    monkeypatch.setattr(run.setup, "wait_observation", AsyncMock(return_value=opening))
    monkeypatch.setattr(run.setup, "validate_opening", AsyncMock())
    monkeypatch.setattr(run.suite, "setup", AsyncMock(return_value=fixture))
    monkeypatch.setattr(run.suite, "score", lambda *args: {
        "success": stage == "post_terminal" or (stage == "late_goal" and args[2]["tick"] > 2),
        "dead": False})
    monkeypatch.setattr(run, "measurement_validity", lambda *args: {"valid": True})
    monkeypatch.setattr(run, "log_supplies", lambda *args: None)
    monkeypatch.setattr(run, "stop_agent", stop_player)
    monkeypatch.setattr(run.asyncio, "sleep", sleep)
    monkeypatch.setattr(run.asyncio, "create_subprocess_exec",
                        AsyncMock(return_value=player))
    monkeypatch.setattr(run.lifecycle, "process_start_ticks", lambda pid: 1)
    monkeypatch.setattr(run.lifecycle, "protection_snapshot", lambda: [])
    def adjudicate(*args, **kwargs):
        order.append("adjudicate")
        assert args[1] == [{"late": "durable-receipt"}]
        return "capability_success", {
            "accepted": False, "joins": [], "synthetic": True,
            "evaluation_validity": {"status": "unknown" if unknown else "valid", "reasons": []},
            "error_observations": []}
    monkeypatch.setattr(run.external, "adjudicate", adjudicate)
    args = run.parser().parse_args([
        "--config", str(tmp_path / "unused"), "--config-sha256", "0" * 64,
        "--out", str(tmp_path / "attempt"), "--mode", "capability", "--harness", "external",
        "--adapter", "mock", "--external-engineering"])
    result = asyncio.run(execute(args))
    assert json.loads((args.out / "result.json").read_text()) == result
    assert order.count("collect") == 1
    assert order.count("adjudicate") == int(collection_error is not True)
    assert result["cleanup_complete"] is (collection_error is not True)
    assert len(result["unknown_effects"]) == int(unknown)
    assert json.loads((args.out / "native-receipts.json").read_text()) == [{"late": "durable-receipt"}]
    assert json.loads((args.out / "native-events.json").read_text()) == [{"late": "durable-event"}]
    if world_event:
        assert json.loads((args.out / "private-world-event.json").read_text()) == result["world_event"]
        assert result["world_event"]["late_effect"] == "observed during actor closure"
    if stage in {"pre_entry", "measurement"}:
        assert result["classification"] == result["task_terminal"]["classification"] == "operator_interrupted"
        assert result["interruption"]["stage"] == stage
        assert result["timing"]["terminal_reason"] == "operator_interrupted"
        assert result["error"]["type"] == "CancelledError"
    elif stage == "post_terminal":
        assert result["classification"] == "capability_success"
        assert result["task_terminal"]["native_success"]
        assert result["interruption"]["stage"] == "post_terminal"
        assert result["timing"]["terminal_reason"] == "native_goal_success"
    elif stage == "late_goal":
        assert instances[-1].observations == 2
        assert result["stop_reason"] == "native_goal_success"
        assert result["task_terminal"]["native_success"]
    elif stage == "normal":
        assert result["final_state_observed"] is True
        assert result["task_terminal"]["outcome"] == "stopped_without_goal"
        assert result["task_terminal"]["final_state_observed"] is True
        assert result["task_terminal"]["classification"] == "entry_exited_without_goal"
        if not collection_error:
            assert result["evaluation_validity"]["status"] == ("unknown" if unknown else "valid")
    if collection_error:
        assert result["entry_evidence_collection"]["status"] == (
            "failed" if collection_error is True else "partial")
        assert any(row["source"] == "evidence" for row in result["error_observations"])
        if stage not in {"normal", "late_goal"}:
            assert result["error"]["type"] == "CancelledError"
    assert not result["evaluation_validity"]["accepted"] and not result["capability_claim"]
