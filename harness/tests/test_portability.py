import asyncio
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from scripts.rpg_benchmark import capabilities, external, lifecycle, native, pins, run, runtime, suite
from scripts import rpg_sdk_agent as common


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")
    return lifecycle.sha(path)


def fixture_config(root):
    root.mkdir(parents=True, exist_ok=True)
    for name in ("engine", "native-source", "assets"):
        (root / name).mkdir()
    for definition in suite.load(Path(suite.__file__).with_name("scenes.json")).values():
        if "asset" in definition:
            asset = root / "assets" / (definition["asset"].replace(".", "/") + ".ron")
            asset.parent.mkdir(parents=True, exist_ok=True)
            asset.write_text("// mocked existence only; never native qualification\n")
    for name in ("server", "bridge"):
        file = root / name
        file.write_text("synthetic input; never executable native evidence\n")
        file.chmod(0o700)
    write(root / "receipt.json", {"bridge_sha256": lifecycle.sha(root / "bridge")})
    content = {
        "schema": runtime.CONTENT, "package_files": runtime.package_files(),
        "seeds": [130626853], "sources": {}, "external_adapters": {},
        "native": {"server": lifecycle.sha(root / "server"), "bridge": lifecycle.sha(root / "bridge"),
                   "build_receipt": lifecycle.sha(root / "receipt.json"),
                   "assets_files": pins.inventory(root / "assets"),
                   "server_source_head_current": "mock-head", "native_source_head_current": "mock-head"},
    }
    config = {
        "schema": runtime.SCHEMA,
        "content": {"path": "content.json", "sha256": write(root / "content.json", content)},
        "output_root": "runs", "sources": {}, "external_adapters": {}, "protected_processes": [],
        "native": {"server": "server", "bridge": "bridge", "build_receipt": "receipt.json",
                   "assets": "assets", "engine_root": "engine", "native_source": "native-source"},
    }
    write(root / "runtime.json", config)
    return config, content


def load_fixture(root):
    with patch.object(runtime.subprocess, "check_output", return_value="mock-head\n"):
        return pins.load(root / "runtime.json", lifecycle.sha(root / "runtime.json"))


def save_fixture(root, config, content):
    config["content"]["sha256"] = write(root / "content.json", content)
    write(root / "runtime.json", config)


def test_location_changes_do_not_change_content_identity(tmp_path):
    first, second = tmp_path / "first", tmp_path / "relocated"
    fixture_config(first)
    before = load_fixture(first)
    shutil.copytree(first, second)
    after = load_fixture(second)
    assert after["content_sha256"] == before["content_sha256"]
    assert after["runtime_sha256"] == before["runtime_sha256"]
    assert after["assets"]["root"] != before["assets"]["root"]
    assert after["dependencies"]["python"]["path"] == sys.executable
    assert not after["sources"]


@pytest.mark.parametrize("input", ["server", "bridge", "receipt.json", "assets", "engine", "native-source"])
def test_missing_or_tampered_required_inputs_never_pass(tmp_path, input):
    fixture_config(tmp_path)
    target = tmp_path / input
    if target.is_file():
        target.write_text("changed")
    elif input == "assets":
        target.rename(tmp_path / "unbound-assets")
    else:
        target.rmdir()
    with pytest.raises((ValueError, FileNotFoundError)):
        load_fixture(tmp_path)


@pytest.mark.parametrize("path", ["../escape", "runs/../escape"])
def test_relative_path_escape_is_rejected(tmp_path, path):
    config, content = fixture_config(tmp_path)
    config["native"]["assets"] = path
    save_fixture(tmp_path, config, content)
    with pytest.raises(ValueError, match="binding path"):
        load_fixture(tmp_path)


def test_config_content_package_and_asset_integrity_are_independent(tmp_path):
    config, content = fixture_config(tmp_path)
    original_digest = lifecycle.sha(tmp_path / "runtime.json")
    (tmp_path / "runtime.json").write_text((tmp_path / "runtime.json").read_text() + "\n")
    with pytest.raises(ValueError, match="SHA-256"):
        pins.load(tmp_path / "runtime.json", original_digest)
    (tmp_path / "assets/extra").write_text("not pinned")
    with pytest.raises(ValueError, match="inventory changed"):
        load_fixture(tmp_path)
    (tmp_path / "assets/extra").unlink()
    content["package_files"]["rpg_benchmark/native.py"] = "0" * 64
    save_fixture(tmp_path, config, content)
    with pytest.raises(ValueError, match="package content changed"):
        load_fixture(tmp_path)


def test_outputs_cannot_overlap_inputs_in_either_direction(tmp_path):
    config, content = fixture_config(tmp_path)
    for output in ("assets/run", "assets", ".", "native-source/world"):
        config["output_root"] = output
        save_fixture(tmp_path, config, content)
        with pytest.raises(ValueError, match="overlaps protected input"):
            load_fixture(tmp_path)


def test_symlink_bindings_and_nonexecutables_are_rejected(tmp_path):
    config, content = fixture_config(tmp_path)
    (tmp_path / "link").symlink_to("assets")
    config["native"]["assets"] = "link"
    save_fixture(tmp_path, config, content)
    with pytest.raises(ValueError, match="symlink"):
        load_fixture(tmp_path)
    config["native"]["assets"] = "assets"
    save_fixture(tmp_path, config, content)
    (tmp_path / "bridge").chmod(0o600)
    with pytest.raises(ValueError, match="not executable"):
        load_fixture(tmp_path)


def test_protection_is_host_scoped_and_signals_always_refuse_protected_pid():
    # Deployment host identities are not portable source configuration.
    # Exercise the identical hostname/PID/start/port behavior on synthetic data.
    with patch.object(Path, "read_text", return_value=json.dumps({
            "hostname": "fixture-host",
            "processes": [{"pid": 424242, "start_ticks": 4242, "port": 18081}]})):
        _check_synthetic_host_protection()


def _check_synthetic_host_protection():
    lifecycle.configure_guards([])
    with patch.object(lifecycle.platform, "node", return_value="unrelated-installation"), patch.object(
            lifecycle, "process_start_ticks", side_effect=AssertionError("must not probe old PID")):
        assert lifecycle.protection_snapshot() == []
        assert not lifecycle.protected_ports()
    with patch.object(lifecycle.platform, "node", return_value="fixture-host"):
        guard = lifecycle.guards()[0]
        assert guard == {"pid": 424242, "start_ticks": 4242, "port": 18081}
        with patch.object(lifecycle, "process_start_ticks", side_effect=AssertionError("no probe before refusal")):
            with pytest.raises(RuntimeError, match="protected PID"):
                lifecycle.assert_owned(424242, 4242)
        process = Mock(pid=424242, returncode=None)
        with pytest.raises(RuntimeError, match="protected PID"):
            asyncio.run(native.stop_owned(process, 4242, {}))
        process.send_signal.assert_not_called()
        process.kill.assert_not_called()


def test_additional_configured_guard_is_validated_and_not_optional_when_selected():
    try:
        for invalid in ([{"pid": True, "start_ticks": 2, "port": 3}],
                        [{"pid": 1, "start_ticks": 2, "port": 65536}]):
            with pytest.raises(ValueError):
                lifecycle.configure_guards(invalid)
        lifecycle.configure_guards([{"pid": 999999999, "start_ticks": 2, "port": 14005}])
        with patch.object(lifecycle.platform, "node", return_value="elsewhere"), patch.object(
                lifecycle, "process_start_ticks", side_effect=FileNotFoundError):
            with pytest.raises(FileNotFoundError):
                lifecycle.protection_snapshot()
    finally:
        lifecycle.configure_guards([])


def test_generic_imports_and_all_cli_help_without_provider_or_repository_cwd(tmp_path):
    command = (
        f"import sys; sys.path.insert(0, {str(Path(runtime.__file__).parents[2])!r}); "
        "sys.modules['copilot']=None; "
        "from scripts.rpg_benchmark import run, native, suite, pins; "
        "from scripts.rpg_sdk_agent import GameTools; "
        "assert not any(k.startswith('copilot.') for k in sys.modules); "
        "assert 'headless_action_validation' not in str(sys.modules)"
    )
    subprocess.run([sys.executable, "-I", "-B", "-c", command], cwd=tmp_path, check=True)
    for command in (["--help"], ["identity"], ["tasks"], ["run", "--help"]):
        entry = (f"import sys; sys.path.insert(0, {str(Path(runtime.__file__).parents[2])!r}); "
                 "from scripts.rpg_benchmark.cli import main; main()")
        result = subprocess.run([sys.executable, "-I", "-B", "-c", entry, *command],
                                cwd=tmp_path, text=True, capture_output=True, check=True)
        assert result.stdout
    tasks = subprocess.check_output(
        [sys.executable, "-I", "-B", "-c", entry, "tasks"], cwd=tmp_path, text=True)
    assert json.loads(tasks)["task_count"] == 29
    assert set(json.loads(tasks)["tasks"]) >= {"T03", "T04", "T05"}


def test_unconfigured_native_cannot_launch(tmp_path):
    bridge = native.NativeBridge(tmp_path, "mock", 12345, tmp_path, Mock())
    with patch.object(asyncio, "create_subprocess_exec", new=AsyncMock()) as launch:
        with pytest.raises(ValueError, match="unconfigured"):
            asyncio.run(bridge.start())
        launch.assert_not_called()


def test_portable_adapter_declaration_and_freeze_relocate_and_remain_strict(tmp_path):
    root = tmp_path / "adapter"
    root.mkdir()
    implementation = root / "adapter.py"
    implementation.write_text(
        "def build_launch(context): raise RuntimeError('not launched')\n"
        "def collect_evidence(context): return {}\n")
    freeze_sha = write(root / "freeze.json", {
        "root": ".", "files": {"adapter.py": lifecycle.sha(implementation)}})
    declaration = {
        "schema": capabilities.SCHEMA, "id": "portable", "player_interface": external.PLAYER,
        "implementation": "adapter.py", "implementation_sha256": lifecycle.sha(implementation),
        "freeze": {"path": "freeze.json", "sha256": freeze_sha}, "tool_track": "mock",
        "capabilities": {"observation_modes": ["structured"], "action_profiles": ["raw-native-v2"],
                         "native_protocol": "0.2", "integer_transport": "strict",
                         "asynchronous_receipts": True,
                         "model_profiles": [{"model": "mock", "reasoning_efforts": ["none"]}]},
        "default_configuration": {"model": "mock", "reasoning_effort": "none",
                                  "observation_mode": "structured", "action_profile": "raw-native-v2"}}
    digest = write(root / "adapter.json", declaration)
    moved = tmp_path / "moved"
    shutil.copytree(root, moved)
    adapter = external.Adapter({"external_adapters": {
        "portable": {"path": str(moved / "adapter.json"), "sha256": digest}}},
        "portable", {}, engineering=True)
    assert adapter.configuration["model"] == "mock"
    (moved / "adapter.py").write_text("changed")
    with pytest.raises(ValueError, match="implementation changed"):
        external.Adapter({"external_adapters": {
            "portable": {"path": str(moved / "adapter.json"), "sha256": digest}}},
            "portable", {}, engineering=True)


def test_legacy_pins_require_explicit_migration_not_silent_fallback(tmp_path):
    legacy = tmp_path / "pins.json"
    digest = write(legacy, {"schema": 2})
    with pytest.raises(ValueError, match="migrate legacy pins explicitly"):
        pins.load(legacy, digest)


OPENING = {"session_id": "native", "self_identity": {"uid": 42, "lifecycle": "life"},
           "control_actor": {"uid": 42, "lifecycle": "life"}}


def control_facts():
    args = {"owner": "chosen", "generation": 4, "sequence": 1, "expected_session_id": "native",
            "actor": OPENING["control_actor"], "movement": [.2, 0], "target": None,
            "primary": False, "block": False, "expected_protection_generation": 3}
    request = {"id": "control", "cmd": "set_control_state", "args": args}
    data = {"entry": {"status": "idle", "cleanup_errors": []}, "entry_receipt_present": True,
            "model_requests": [{"model": "mock", "effort": "none", "synthetic": False}],
            "provider_errors": [], "owned_processes": [], "cleanup_complete": True,
            "tool_calls": [{"id": "call", "name": "game_set_control_state", "arguments": args}],
            "submissions": [{"call_id": "call", "request": request}]}
    receipt = {"request": request, "actor": 42, "response": {
        "id": "control", "session_id": "native", "ok": True, "result": {
            "status": "applied", "owner": "chosen", "generation": 4, "sequence": 1,
            "actor": OPENING["control_actor"], "session_id": "native",
            "accepted_tick": 9, "applied_tick": 9, "gameplay_success": None}}}
    return data, receipt


@pytest.mark.parametrize("key,value", [
    ("owner", "other"), ("generation", 5), ("generation", True), ("sequence", 2),
    ("sequence", 1.0), ("session_id", "other"), ("actor", {"uid": 42, "lifecycle": "stale"}),
    ("applied_tick", True), ("status", "submitted"),
])
def test_control_receipt_identity_lifecycle_generation_and_integer_fences(key, value):
    data, receipt = control_facts()
    receipt["response"]["result"][key] = value
    _, proof = external.adjudicate(
        data, [receipt], OPENING, {"success": True}, native_classification="capability_success",
        stop_reason="native_goal_success", stop_signal={}, terminal_monotonic=20)
    assert not proof["accepted"]


@pytest.mark.parametrize("key,value", [
    ("generation", True), ("sequence", 1.0), ("expected_protection_generation", "3")])
def test_strict_control_request_types_are_not_coerced(key, value):
    data, _ = control_facts()
    data["submissions"][0]["request"]["args"][key] = value
    with pytest.raises(ValueError):
        common.validate_request("set_control_state", data["submissions"][0]["request"]["args"])


def test_late_receipt_resolves_unknown_and_duplicate_request_never_mutates_again(tmp_path):
    from .test_boundary_shutdown import PipeFixture, step

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
def test_validated_runtime_wires_generic_episode_without_gameagent_or_provider(
        tmp_path, monkeypatch, outcome, cleanup_failure, unknown):
    fixture_config(tmp_path)
    monkeypatch.setattr(runtime.subprocess, "check_output", lambda *args, **kwargs: "mock-head\n")
    data, receipt = control_facts()
    data["model_requests"][0]["synthetic"] = True
    fixture = {"task": "mock goal", "opening": {
        **OPENING, "tick": 1, "survival": {"buffs": [{"kind": "Invulnerability"}]}}}
    instances = []

    class Bridge:
        def __init__(self, out, name, *args):
            self.name, self.process, self.cleanup = name, SimpleNamespace(returncode=0), {}
            self.native_events, self.error_observations = [], []
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

    class Adapter:
        def __init__(self, *args, **kwargs):
            self.manifest = {"tool_track": "mock"}
            self.id, self.engineering = "mock", True
            self.legacy = False
            self.configuration = {"model": "mock", "reasoning_effort": "none"}
            self.capabilities, self.context = {}, {"mock": True}

        def launch(self, **kwargs):
            return external.Launch((sys.executable, "-c", "raise RuntimeError('not executed')"), tmp_path, {})

        def observe_entry(self, timing):
            pass

        def collect(self):
            data["provider_errors"] = [{"wall_monotonic": time.monotonic(),
                                        "error": {"type": "ClientConnectionResetError", "message": "mock reset"}}]
            return data

    monkeypatch.setattr(run, "NativeBridge", Bridge)
    monkeypatch.setattr(run, "EpisodeBoundary", Boundary)
    monkeypatch.setattr(run, "private_server", server)
    monkeypatch.setattr(external, "Adapter", Adapter)
    monkeypatch.setattr(run.setup, "command", AsyncMock())
    monkeypatch.setattr(run.setup, "wait_observation", AsyncMock(return_value=fixture["opening"]))
    monkeypatch.setattr(run.setup, "validate_opening", AsyncMock())
    monkeypatch.setattr(run.suite, "setup", AsyncMock(return_value=fixture))
    monkeypatch.setattr(run.suite, "score", lambda *args: {
        "success": outcome == "success", "dead": outcome == "death"})
    monkeypatch.setattr(run, "measurement_validity", lambda *args: {"valid": True})
    monkeypatch.setattr(run, "log_supplies", lambda *args: None)
    monkeypatch.setattr(run.asyncio, "create_subprocess_exec", AsyncMock(return_value=Player()))
    monkeypatch.setattr(lifecycle, "process_start_ticks", lambda pid: 1)
    monkeypatch.setattr(lifecycle, "protection_snapshot", lambda: [])
    args = run.parser().parse_args([
        "--config", str(tmp_path / "runtime.json"), "--config-sha256", lifecycle.sha(tmp_path / "runtime.json"),
        "--out", str(tmp_path / "runs/attempt"),
        "--mode", "capability", "--harness", "external", "--evaluation-horizon", "0.000000001"])
    result = asyncio.run(run.episode(args))
    assert json.loads((args.out / "result.json").read_text()) == result
    assert result["source"] is None
    assert result["task_terminal"]["outcome"] == outcome
    assert result["classification"] == {
        "success": "capability_success", "death": "agent_failure",
        "horizon": "evaluation_horizon_without_success"}[outcome]
    assert result["timing"]["points"]["terminal"] == result["task_terminal"]["observed_at"]
    assert result["timing"]["cleanup_elapsed_seconds"] >= 0
    assert not result["evaluation_validity"]["accepted"]
    assert len(result["unknown_effects"]) == int(unknown)
    if cleanup_failure:
        assert result["error"]["exceptions"][0]["exceptions"][0]["type"] == "ConnectionResetError"
        assert result["evaluation_validity"]["status"] == "invalid"
    else:
        assert (args.out / "native-receipts.json").is_file()
