import argparse
import ast
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import subprocess

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
    with pytest.raises(ValueError, match="caller live declaration"):
        external.Adapter(value, "lightweight-mock", {})


def test_public_live_declaration_without_team_identity_and_legacy_compatibility(tmp_path):
    selected_pins = selected()
    probe = external.Adapter(selected_pins, "lightweight-mock", {}, engineering=True)
    value = {
        "schema": external.LIVE, "live": True, "provider": "caller-owned-provider",
        "pins_sha256": "1" * 64, "adapter_id": probe.id,
        "adapter_sha256": probe.declaration_sha, "configuration": probe.configuration,
        "adapter_config": {}, "model": probe.configuration["model"],
        "reasoning_effort": probe.configuration["reasoning_effort"], "model_calls_admitted": True,
    }
    path = tmp_path / "live.json"
    path.write_text(json.dumps(value))
    adapter = external.Adapter(selected_pins, probe.id, {}, admission=path, pins_sha="1" * 64)
    assert not adapter.engineering
    # Construction performs no native allocation or provider/model request.
    assert adapter.context is None
    for field, bad in (("live", False), ("provider", ""), ("adapter_config", {"changed": True}),
                       ("configuration", {}), ("pins_sha256", "2" * 64),
                       ("model_calls_admitted", False), ("adapter_sha256", "2" * 64)):
        path.write_text(json.dumps({**value, field: bad}))
        with pytest.raises(ValueError, match="declaration|configuration"):
            external.Adapter(selected_pins, probe.id, {}, admission=path, pins_sha="1" * 64)
    legacy = {**value, "schema": "rpg-external-admission/1", "approved_by": "A0"}
    path.write_text(json.dumps(legacy))
    assert not external.Adapter(selected_pins, probe.id, {}, admission=path,
                                pins_sha="1" * 64).engineering


def test_public_process_live_preflight_and_entry_without_model_calls(tmp_path):
    """Actual isolated constructors/process, not live inference or native evidence."""
    from scripts.rpg_benchmark import capabilities, providers, runtime
    implementation = tmp_path / "adapter.py"
    implementation.write_text(
        "from pathlib import Path\nfrom scripts.rpg_benchmark.external import Launch,EVIDENCE,PLAYER\n"
        "def validate_config(context):\n"
        "    assert context['config'] == {'provider': 'caller-test'}\n"
        "def build_launch(context):\n"
        "    return Launch((context['python'],'-I','-c','print(\"no-model-entry\")'),"
        "Path(context['output_root']),{})\n"
        "def collect_evidence(context):\n"
        "    return dict(schema=EVIDENCE,adapter_id=context['adapter_id'],player_interface=PLAYER,"
        "entry={'status':'idle','cleanup_errors':[]},entry_receipt_present=True,"
        "model_requests=[],tool_calls=[],submissions=[],provider_errors=[],owned_processes=[])\n")
    freeze = tmp_path / "freeze.json"
    freeze.write_text(json.dumps({"root": str(tmp_path),
                                 "files": {"adapter.py": pins.lifecycle.sha(implementation)}}))
    manifest = providers.manifest(providers.LUNA, "structured")
    manifest["capabilities"]["backends"] = ["headless", "renderer"]
    manifest["default_configuration"]["action_profile"] = "raw-native-v2"
    manifest["capabilities"]["action_profiles"].append("raw-native-v2")
    manifest["capabilities"]["action_profiles_by_observation_mode"]["structured"].append("raw-native-v2")
    preflight = (
        "import asyncio,json,sys;from scripts.rpg_benchmark import startup,runtime;"
        "value=asyncio.run(startup.probe_player(json.load(sys.stdin)));"
        "value['player_package_files']=runtime.package_files();print(json.dumps(value))")
    manifest.update(
        schema=capabilities.PROCESS_SCHEMA, id="public-process", player_interface=external.PLAYER,
        implementation=str(implementation), implementation_sha256=pins.lifecycle.sha(implementation),
        freeze={"path": str(freeze), "sha256": pins.lifecycle.sha(freeze)},
        runtime={"python": sys.executable, "preflight": ["-c", preflight]})
    path = tmp_path / "adapter.json"
    path.write_text(json.dumps(manifest))
    declaration_sha = pins.lifecycle.sha(path)
    config = {"provider": "caller-test"}
    admission = tmp_path / "live.json"
    admission.write_text(json.dumps({
        "schema": external.LIVE, "live": True, "provider": "caller-test",
        "pins_sha256": "1" * 64, "adapter_id": "public-process", "adapter_sha256": declaration_sha,
        "configuration": capabilities.negotiate(manifest, {})[0], "adapter_config": config,
        "model": "gpt-6-luna", "reasoning_effort": "none", "model_calls_admitted": True}))
    selected_pins = {"external_adapters": {"public-process": {
        "path": str(path), "sha256": declaration_sha}}}
    adapter = external.Adapter(selected_pins, "public-process", config, admission=admission,
                               pins_sha="1" * 64)
    installed = json.loads(subprocess.check_output(
        [sys.executable, "-I", "-c",
         "import json;from scripts.rpg_benchmark import runtime;print(json.dumps(runtime.package_files()))"],
        text=True))
    operation = adapter.preflight(backend="headless", detail_storage=tmp_path / "details")
    if installed != runtime.package_files():
        # Source tests intentionally differ from installed bytes: reject them.
        with pytest.raises(ValueError, match="exact evaluator player package"):
            asyncio.run(operation)
        return
    receipt = asyncio.run(operation)
    assert receipt["exit_code"] == 0 and receipt["receipt"]["construction_checked"]
    assert (tmp_path / "details/construction.ndjson").is_file()
    plan = adapter.launch(task="public goal", socket=tmp_path / "not-connected.sock",
                          output=tmp_path / "entry", output_root=tmp_path,
                          python=sys.executable, common_interface=str(HARNESS / "scripts/rpg_sdk_agent.py"))
    result = subprocess.run(plan.command, cwd=plan.cwd, env=external.clean_environment(),
                            capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "no-model-entry"
    assert not adapter.engineering and adapter.collect()["model_requests"] == []


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


def test_accepted_commands_and_evaluator_policies_preserve_precise_historical_contracts():
    from .historical import historical_fixture
    from .historical_contracts import check_core, check_module
    baseline = json.loads(historical_fixture("accepted-contracts.json").read_text())
    package = Path(run.__file__).parent
    check_module(baseline, package, "run.py", (
        "agent_command", "sdk_command", "measurement_stop", "measurement_validity",
        "log_supplies", "stop_agent", "bridge_cleanup"))
    check_core(baseline, package)
    check_module(baseline, package, "bridge_client.py")
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
    check_module(baseline, package, "native.py", (
        "NativeRefusal", "NativeActionFailure", "NativeConfig", "private_server",
        "Journal", "stop_owned", "allowed", "NativeBridge"))
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
