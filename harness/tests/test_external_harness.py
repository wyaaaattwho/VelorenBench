import argparse
import ast
import asyncio
import copy
import json
import os
from pathlib import Path
import sys

import pytest

sys.dont_write_bytecode = True
HARNESS = Path(__file__).resolve().parents[1]
CANDIDATE = HARNESS.parent
sys.path.insert(0, str(HARNESS))
from scripts.rpg_benchmark import external, pins, run
from scripts.rpg_benchmark.timing import AttemptTiming


def selected():
    root = Path(__file__).parent / "fixtures/adapters"
    return {"external_adapters": {
        identifier: {"path": str(root / folder / "adapter.json"),
                     "sha256": pins.lifecycle.sha(root / folder / "adapter.json")}
        for identifier, folder in (("deepseek-harness", "dsh-adapter"),
                                   ("lightweight-mock", "mock-adapter"))}}


def mock_selection(root):
    """Explicit fake native inputs for orchestration tests, never native evidence."""
    from scripts.rpg_benchmark import runtime
    package = Path(runtime.__file__).parent
    return {"output_root": str(root), "protected_paths": [], "seeds": [130626853],
            "dependencies": {
                "scene_manifest": {"path": str(package / "scenes.json")},
                "sdk": {"path": str(package.parent / "rpg_sdk_agent.py")},
                "python": {"path": sys.executable},
                **{name: {"path": str(root / name), "sha256": "0" * 64}
                   for name in ("server", "bridge", "build_receipt")}},
            "engine_root": str(root), "assets": {"root": str(root / "assets")},
            "native_backend_identity": {"backend": "headless", "synthetic": True},
            "native": {}, "content_sha256": "0" * 64, "sources": {},
            "renderer": None, "native_interactions": None, "renderer_fixture_helper": None}


def facts():
    request = {"id": "req", "cmd": "step", "args": {
        "action": {"type": "craft", "recipe": "mock-recipe", "action_id": "intent"}}}
    evidence = {
        "entry": {"status": "idle", "cleanup_errors": []}, "entry_receipt_present": True,
        "model_requests": [{"model": "gpt-6-luna", "effort": "none", "synthetic": False}],
        "provider_errors": [], "tool_calls": [{"id": "call", "arguments": request["args"]}],
        "submissions": [{"call_id": "call", "request": request}],
    }
    receipt = {"request": request, "actor": 42,
               "response": {"id": "req", "ok": True, "session_id": "native",
                            "result": {"status": "completed", "action_id": "intent"}}}
    return evidence, receipt


@pytest.mark.parametrize("case", ["success", "synthetic", "provider", "no-model", "entry-error"])
def test_external_success_uses_native_goal_and_real_model_join_not_claim(case):
    evidence, receipt = facts()
    evidence["entry"]["success"] = True  # Deliberately irrelevant self-claim.
    if case == "synthetic":
        evidence["model_requests"][0]["synthetic"] = True
    elif case == "provider":
        evidence["provider_errors"] = [{"status": 402}]
    elif case == "no-model":
        evidence["model_requests"] = []
    elif case == "entry-error":
        evidence["entry"]["status"] = "error"
    classification, proof = external.adjudicate(
        evidence, [receipt], {"session_id": "native", "self_identity": {"uid": 42}},
        {"success": True}, native_classification="capability_success",
        stop_reason="native_goal_success", stop_signal={})
    assert classification == "capability_success"  # Native outcome is independent of acceptance.
    assert proof["accepted"] is (case == "success")
    assert proof["evaluation_validity"]["status"] != "valid" or case in {"success", "synthetic"}


def test_native_goal_without_correlated_receipt_rejected():
    evidence, receipt = facts()
    receipt["actor"] = 99
    classification, proof = external.adjudicate(
        evidence, [receipt], {"session_id": "native", "self_identity": {"uid": 42}},
        {"success": True}, native_classification="capability_success",
        stop_reason="native_goal_success", stop_signal={})
    assert classification == "capability_success" and not proof["accepted"]
    assert proof["evaluation_validity"]["status"] == "invalid"
    evidence["tool_calls"][0]["arguments"] = {}
    with pytest.raises(ValueError, match="intent"):
        external.adjudicate(
            evidence, [receipt], {"session_id": "native", "self_identity": {"uid": 42}},
            {"success": True}, native_classification="capability_success",
            stop_reason="native_goal_success", stop_signal={})


@pytest.mark.parametrize("reason,entered", [
    ("native_goal_success", True), ("player_death", True), ("evaluation_horizon", True),
    ("provider_error", True), ("adapter_error", True), ("operator_interrupted", True),
    ("prelaunch_failure", False),
])
def test_all_success_failure_prelaunch_duration_shapes(reason, entered):
    clock = AttemptTiming()
    if entered:
        clock.mark("setup_qualified")
        clock.mark("agent_launch_start")
        clock.mark("agent_launched")
        clock.mark("harness_entry")
    clock.mark("terminal")
    clock.mark("agent_final_stop")
    clock.mark("cleanup_end")
    value = clock.finish(reason)
    assert value["attempt_elapsed_seconds"] > 0 and value["setup_elapsed_seconds"] > 0
    assert value["cleanup_elapsed_seconds"] >= 0
    if entered:
        assert value["task_elapsed_seconds"] > 0
    else:
        assert value["task_elapsed_seconds"] is None and value["task_duration_null_reason"] == reason


def test_invalid_external_registration_and_missing_live_admission():
    value = selected()
    with pytest.raises(ValueError, match="not registered"):
        external.Adapter(value, "unregistered", {}, engineering=True)
    with pytest.raises(ValueError, match="A0"):
        external.Adapter(value, "lightweight-mock", {})


def test_second_adapter_launches_through_same_generic_contract(tmp_path):
    adapter = external.Adapter(selected(), "lightweight-mock", {}, engineering=True)
    plan = adapter.launch(
        task="Only the player macro goal", socket=Path("/private.sock"),
        output=tmp_path / "external", output_root=tmp_path,
        python=str(pins.PYTHON), common_interface=str(HARNESS / "scripts/rpg_sdk_agent.py"))
    assert isinstance(plan, external.Launch)
    assert "Only the player macro goal" == adapter.context["task"]
    assert set(adapter.context).isdisjoint({"fixture", "score", "admin", "definition", "server_command"})
    failed = external.Adapter(selected(), "lightweight-mock", {"failure": "prelaunch"}, engineering=True)
    with pytest.raises(RuntimeError, match="prelaunch"):
        failed.launch(task="goal", socket=Path("/private.sock"), output=tmp_path / "external",
                      output_root=tmp_path, python=str(pins.PYTHON),
                      common_interface=str(HARNESS / "scripts/rpg_sdk_agent.py"))


def test_accepted_commands_and_evaluator_policies_are_byte_equivalent_ast():
    from .historical import historical_fixture
    baseline = json.loads(historical_fixture("accepted-contracts.json").read_text())
    after = ast.parse(Path(run.__file__).read_text())
    for name in ("agent_command", "sdk_command", "measurement_stop", "measurement_validity",
                 "log_supplies", "stop_agent", "bridge_cleanup"):
        right = next(item for item in after.body if getattr(item, "name", None) == name)
        assert baseline["ast"]["run.py"][name] == ast.dump(right)
    package = Path(run.__file__).parent
    for name in ("scene.py", "suite.py", "setup.py", "crafting.py", "combat.py",
                 "navigation.py", "mixed.py", "oracles.py", "bridge_client.py"):
        assert baseline["files"][name] == pins.lifecycle.sha(package / name)
    original_catalog = Path(__file__).parent / "fixtures/release041-scenes.json"
    assert baseline["files"]["scenes.json"] == pins.lifecycle.sha(original_catalog)
    original, current = json.loads(original_catalog.read_text()), json.loads((package / "scenes.json").read_text())
    for before, after in zip(original["scenes"], current["scenes"], strict=True):
        if before["id"] == "T12":
            assert before["fixture_version"] == "t12-native-slay-v2"
            assert after["fixture_version"] == "t12-native-slay-v6"
            before, after = before.copy(), after.copy()
            for key in ("fixture_version", "qualification"):
                before.pop(key)
                after.pop(key)
        assert before == after
    assert {key: value for key, value in original.items() if key != "scenes"} == {
        key: value for key, value in current.items() if key != "scenes"}
    after_native = ast.parse((package / "native.py").read_text())
    for name in ("NativeRefusal", "NativeActionFailure", "NativeConfig", "private_server",
                 "Journal", "stop_owned", "allowed"):
        right = next(item for item in after_native.body if getattr(item, "name", None) == name)
        assert baseline["ast"]["native.py"][name] == ast.dump(right)
    right_bridge = next(item for item in after_native.body if getattr(item, "name", None) == "NativeBridge")
    assert baseline["ast"]["native.py"]["NativeBridge"] == ast.dump(right_bridge)
    after_sdk = ast.parse((package.parent / "rpg_sdk_agent.py").read_text())
    for name in ("validate_request", "validate_controls", "validate_schema", "tool_schema"):
        right = next(item for item in after_sdk.body if getattr(item, "name", None) == name)
        assert baseline["ast"]["../rpg_sdk_agent.py"][name] == ast.dump(right)


def test_invalid_pins_are_rejected_before_output_allocation(tmp_path):
    args = run.parser().parse_args([
        "--pins", str(tmp_path / "missing"), "--pins-sha256", "0" * 64,
        "--out", str(tmp_path / "attempt"), "--output-root", str(tmp_path), "--mode", "setup"])
    with pytest.raises(FileNotFoundError):
        asyncio.run(run.episode(args))
    assert not args.out.exists()


@pytest.mark.parametrize("outcome", ["success", "failure", "prelaunch"])
def test_central_episode_persists_timing_for_each_return_or_exception(tmp_path, monkeypatch, outcome):
    async def simulated_episode(args, timing, out):
        timing.mark("setup_qualified")
        if outcome == "prelaunch":
            raise RuntimeError("Synthetic prelaunch failure")
        timing.mark("agent_launch_start")
        timing.mark("agent_launched")
        timing.mark("harness_entry")
        timing.mark("terminal")
        timing.mark("agent_final_stop")
        return {"success": outcome == "success", "capability_claim": outcome == "success",
                "classification": "capability_success" if outcome == "success" else "provider_error",
                "stop_reason": "native_goal_success" if outcome == "success" else "normal_entry_exited"}
    monkeypatch.setattr(run, "_episode", simulated_episode)
    monkeypatch.setattr(run.pins, "load", lambda *args, **kwargs: mock_selection(tmp_path))
    args = run.parser().parse_args([
        "--pins", str(tmp_path / "unused"), "--pins-sha256", "0" * 64,
        "--out", str(tmp_path / "attempt"), "--output-root", str(tmp_path),
        "--mode", "capability", "--harness", "external", "--adapter", "fixture"])
    result = asyncio.run(run.episode(args))
    persisted = json.loads((args.out / "result.json").read_text())
    assert persisted == result
    assert result["timing"]["attempt_elapsed_seconds"] > 0
    assert result["success"] is (outcome == "success")
    assert (result["timing"]["task_elapsed_seconds"] is None) is (outcome == "prelaunch")


def test_failed_native_receipt_does_not_prove_model_goal_effect():
    evidence, receipt = facts()
    receipt["response"]["result"]["status"] = "failed"
    classification, proof = external.adjudicate(
        evidence, [receipt], {"session_id": "native", "self_identity": {"uid": 42}},
        {"success": True}, native_classification="capability_success",
        stop_reason="native_goal_success", stop_signal={})
    assert classification == "capability_success" and not proof["accepted"]
    assert proof["evaluation_validity"]["status"] == "invalid"
