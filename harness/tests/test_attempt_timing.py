import asyncio
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import AsyncMock

import pytest

from scripts.rpg_benchmark import run, timing


def point(seconds):
    wall = datetime(2026, 10, 15, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    return {"monotonic": seconds, "wall": wall.isoformat()}


def clock(monkeypatch, *, launch=True, launched=True, entry=True, evaluator=True):
    monkeypatch.setattr(timing, "stamp", lambda: point(0))
    value = timing.AttemptTiming()
    value.mark("setup_qualified", point(10))
    if launch:
        value.mark("agent_launch_start", point(11))
    if launched:
        value.mark("agent_launched", point(12))
    if entry:
        value.mark("harness_entry", point(15))
    if evaluator:
        value.mark("evaluator_start", point(13))
    value.mark("terminal", point(20))
    if launched:
        value.mark("agent_final_stop", point(21))
    value.mark("cleanup_end", point(22))
    return value


@pytest.mark.parametrize("reason,launched,entry", [
    ("native_goal_success", True, True),
    ("provider_error", True, True),
    ("supervisor_import_failure", True, False),
    ("profile_bootstrap_failure", True, False),
    ("create_subprocess_failure", False, False),
])
def test_attempted_launch_exact_duration_includes_startup(monkeypatch, reason, launched, entry):
    value = clock(monkeypatch, launched=launched, entry=entry, evaluator=launched)
    result = value.finish(reason)
    assert result["task_elapsed_seconds"] == 9
    assert result["setup_elapsed_seconds"] == 10
    assert result["attempt_elapsed_seconds"] == 22
    assert result["cleanup_elapsed_seconds"] == 2
    assert result["task_duration_null_reason"] is None
    assert result["harness_entry_elapsed_seconds"] == (4 if entry else None)
    assert result["evaluator_start_offset_seconds"] == (2 if launched else None)
    assert ("harness_entry" in result["points"]) is entry


def test_true_setup_failure_has_null_task_and_actual_durations(monkeypatch):
    value = clock(monkeypatch, launch=False, launched=False, entry=False, evaluator=False)
    value.points.pop("setup_qualified")
    result = value.finish("fixture_failure")
    assert result["task_elapsed_seconds"] is None
    assert result["task_duration_null_reason"] == "fixture_failure"
    assert result["setup_elapsed_seconds"] == 20
    assert result["attempt_elapsed_seconds"] == 22
    assert result["cleanup_elapsed_seconds"] == 2


@pytest.mark.parametrize("name,seconds", [
    ("setup_qualified", -1), ("agent_launch_start", 9), ("agent_launched", 10),
    ("harness_entry", 11), ("terminal", 14), ("agent_final_stop", 19),
    ("cleanup_end", 19), ("evaluator_start", 11), ("evaluator_start", 21),
])
def test_rejects_out_of_order_points_without_negative_duration(monkeypatch, name, seconds):
    value = clock(monkeypatch)
    value.points[name] = point(seconds)
    with pytest.raises(ValueError, match="out of order"):
        value.finish("invalid")


@pytest.mark.parametrize("missing", ["agent_launch_start", "agent_launched"])
def test_rejects_entry_without_preceding_launch(monkeypatch, missing):
    value = clock(monkeypatch)
    value.points.pop(missing)
    with pytest.raises(ValueError, match="preceding launch"):
        value.finish("invalid")


@pytest.mark.parametrize("harness", ["gameagent", "copilot-sdk", "external"])
@pytest.mark.parametrize("case", ["success", "provider", "bootstrap", "spawn", "prelaunch"])
def test_central_persistence_uses_launch_clock_for_every_harness(tmp_path, monkeypatch, harness, case):
    monkeypatch.setattr(timing, "stamp", lambda: point(0))

    async def simulated_episode(args, value, out):
        value.mark("setup_qualified", point(10))
        if case != "prelaunch":
            value.mark("agent_launch_start", point(11))
        if case not in {"prelaunch", "spawn"}:
            value.mark("agent_launched", point(12))
        if case in {"success", "provider"}:
            value.mark("harness_entry", point(15))
        value.mark("terminal", point(20))
        value.mark("cleanup_end", point(22))
        if case == "spawn":
            spawn = AsyncMock(side_effect=FileNotFoundError("deterministic spawn failure"))
            await spawn()
        if case in {"bootstrap", "prelaunch"}:
            raise ImportError("deterministic setup/bootstrap failure")
        return {"success": case == "success", "classification": case,
                "stop_reason": "native_goal_success" if case == "success" else "provider_error"}

    monkeypatch.setattr(run, "_episode", simulated_episode)
    def selected_pins(path, digest, *, backend):
        assert backend == "headless"
        return {"output_root": str(tmp_path), "protected_paths": []}

    monkeypatch.setattr(run.pins, "load", selected_pins)
    monkeypatch.setattr(run.lifecycle, "protection_snapshot", lambda: [])
    args = run.parser().parse_args([
        "--pins", str(tmp_path / "unused"), "--pins-sha256", "0" * 64,
        "--out", str(tmp_path / "attempt"), "--output-root", str(tmp_path),
        "--mode", "capability", "--harness", harness])
    result = asyncio.run(run.episode(args))
    assert json.loads((args.out / "result.json").read_text()) == result
    duration = result["timing"]
    assert duration["task_elapsed_seconds"] == (None if case == "prelaunch" else 9)
    assert duration["setup_elapsed_seconds"] == 10
    assert duration["attempt_elapsed_seconds"] == 22
    assert duration["cleanup_elapsed_seconds"] == 2
    assert (duration["task_duration_null_reason"] is not None) is (case == "prelaunch")
    if case in {"bootstrap", "spawn"}:
        assert result["pre_entry_failure"] is True
        assert "harness_entry" not in duration["points"]
