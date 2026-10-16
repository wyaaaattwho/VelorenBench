"""Release process/media/lifecycle regressions; all game/provider peers are synthetic."""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts.rpg_benchmark import capabilities, external, launching, player, providers, results, run
from scripts.rpg_benchmark.player_types import Image, Result
from scripts.rpg_benchmark.responses import image_identities
from scripts.rpg_benchmark.timing import AttemptTiming
from .test_boundary_shutdown import PipeFixture, step
from .test_external_harness import mock_selection
from .test_multimodal import png


@pytest.mark.parametrize("identity", ["independent-a", "independent-b", "no-provider-name"])
def test_renderer_admission_depends_on_transport_not_identity(identity):
    manifest = providers.manifest({**providers.LUNA, "id": identity}, "image")
    configuration, declared, _ = capabilities.negotiate(manifest, {})
    capabilities.admit_backend(configuration, declared, "renderer")
    with pytest.raises(ValueError, match="backend"):
        capabilities.admit_backend(configuration, declared, "headless")
    with pytest.raises(ValueError, match="shared projected"):
        capabilities.admit_backend(configuration, {**declared, "player_tools": "filepath"}, "renderer")


def test_wire_accounting_includes_official_tool_images_and_history():
    data = png()
    block = {"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(data).decode()}
    history = [{"type": "function_call_output", "call_id": "c", "output": [block]},
               {"role": "user", "content": [block]}]
    values = image_identities(history)
    assert len(values) == 2
    assert all(value["sha256"] == hashlib.sha256(data).hexdigest() for value in values)
    assert all(value["byte_length"] == len(data) for value in values)
    # Neither a path nor base64 buried inside a text block is an image delivery.
    assert not image_identities([{"role": "user", "content": [{"type": "input_text", "text": str(block)}]}])


def test_broker_serializes_verified_bytes_not_paths_or_hidden_state():
    data = png()
    image = Image(data, "image/png", hashlib.sha256(data).hexdigest(), 2, 1)
    blocks = player.content_blocks(Result("public receipt", images=(image,)))
    assert base64.b64decode(blocks[1]["data"], validate=True) == data
    assert blocks[1]["mimeType"] == "image/png"
    with pytest.raises(ValueError, match="bytes changed"):
        player.content_blocks(Result("public", images=(Image(data, "image/png", "0" * 64, 2, 1),)))


def test_preflight_failure_cannot_allocate_native_world(tmp_path, monkeypatch):
    selected = mock_selection(tmp_path)
    monkeypatch.setattr(run.pins, "load", lambda *args, **kwargs: selected)
    adapter = SimpleNamespace(preflight=AsyncMock(side_effect=ValueError("missing declared dependency loguru")))
    monkeypatch.setattr(external, "Adapter", lambda *args, **kwargs: adapter)
    native = AsyncMock(side_effect=AssertionError("must never allocate a native world"))
    monkeypatch.setattr(run, "private_server", native)
    args = run.parser().parse_args([
        "--pins", str(tmp_path / "synthetic"), "--pins-sha256", "0" * 64,
        "--out", str(tmp_path / "attempt"), "--mode", "capability", "--harness", "external"])
    record = asyncio.run(run.episode(args))
    assert record["pre_entry_failure"] is True
    assert record["process_cleanup_complete"] is True
    assert record["cleanup_complete"] is True
    assert record["timing"]["task_elapsed_seconds"] is None
    assert "loguru" in record["error"]["message"]
    native.assert_not_called()


def test_portable_installed_bridge_relays_without_source_executable_bits(tmp_path):
    async def check():
        fixture = PipeFixture(tmp_path, delay=.001)
        await fixture.start()
        command = Path(sys.executable).parent / "rpg-episode-bridge"
        assert command.is_file(), "Install the wheel before the process contract tests"
        environment = external.clean_environment()
        environment["RPG_EPISODE_SOCKET"] = str(fixture.boundary.path)
        process = await asyncio.create_subprocess_exec(
            str(command), env=environment, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            ready = json.loads(await asyncio.wait_for(process.stdout.readline(), 5))
            assert ready["session_id"] == "synthetic"
            process.stdin.write((json.dumps(step()) + "\n").encode())
            await process.stdin.drain()
            response = json.loads(await asyncio.wait_for(process.stdout.readline(), 5))
            assert response["ok"] and response["result"]["status"] == "completed"
            assert len(fixture.bridge.measured_receipts) == 1
        finally:
            process.stdin.close()
            await fixture.shutdown()
            await asyncio.wait_for(process.wait(), 5)
        assert process.returncode == 0
    asyncio.run(check())


@pytest.mark.parametrize("observed,unknown,expected", [
    (True, False, "valid"), (False, False, "unknown"), (True, True, "unknown")])
def test_normal_model_stop_requires_observed_final_state_and_known_effects(observed, unknown, expected):
    record = {"mode": "capability", "stop_reason": "normal_entry_exited",
              "classification": "model_stopped_without_goal", "cleanup_complete": True,
              "score": {"success": False, "dead": False}, "final_state_observed": observed,
              "unknown_effects": [{"request_id": "pending"}] if unknown else [],
              "model_success_evidence": {"accepted": False,
                                        "evaluation_validity": {"status": "valid", "reasons": []}}}
    results.finalize(record, AttemptTiming())
    assert record["evaluation_validity"]["status"] == expected
    assert record["task_terminal"]["native_terminal_observed"] is False
    assert record["success"] is False


def test_standard_provider_explicit_configuration_has_no_account_fallback(monkeypatch):
    from scripts.rpg_benchmark.provider_route import StandardResponsesRoute
    monkeypatch.delenv("BENCH_TEST_KEY", raising=False)
    with pytest.raises(ValueError, match="missing"):
        StandardResponsesRoute("https://provider.example/v1/responses", "BENCH_TEST_KEY")
    monkeypatch.setenv("BENCH_TEST_KEY", "synthetic-test-credential")
    route = StandardResponsesRoute("https://provider.example/v1/responses", "BENCH_TEST_KEY")
    endpoint, headers = route()
    assert endpoint == "https://provider.example/v1/responses"
    assert headers["Authorization"] == "Bearer synthetic-test-credential"
    for endpoint in ("http://provider.example/responses", "https://user:pass@provider.example/responses",
                     "https://provider.example/responses?api_key=bad"):
        with pytest.raises(ValueError):
            StandardResponsesRoute(endpoint, "BENCH_TEST_KEY")


@pytest.mark.parametrize("metadata", [None, {"brain": "observed-brain", "jev": "jev-1.13.0"}])
def test_external_collector_preserves_observed_models_without_assuming_architecture(metadata):
    facts = {
        "schema": external.EVIDENCE, "adapter_id": "independent", "player_interface": external.PLAYER,
        "entry": {"status": "idle", "cleanup_errors": []}, "entry_receipt_present": True,
        "model_requests": [], "tool_calls": [], "submissions": [], "provider_errors": [],
        "owned_processes": [],
    }
    if metadata is not None:
        facts["runtime_model_metadata"] = metadata
    adapter = object.__new__(external.Adapter)
    adapter.id, adapter.context = "independent", {}
    adapter.module = SimpleNamespace(collect_evidence=lambda context: facts)
    result = adapter.collect()
    assert result["runtime_model_metadata"] == (
        metadata if metadata is not None else {"brain": "unknown", "jev": "unknown"})
