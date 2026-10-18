from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.rpg_benchmark import renderer_support, suite


@pytest.mark.parametrize("task", [f"T{i:02}" for i in range(1, 13)])
@pytest.mark.parametrize("mode", ["structured", "hybrid", "image"])
@pytest.mark.parametrize("run_mode", ["capability", "task-engineering"])
def test_integrated_family_admission_is_explicit(tmp_path, tmp_path_factory, monkeypatch, task, mode, run_mode):
    from .test_admission_closeout import selected_native, record_for, install_records
    terrain = "flat" if task == "T02" else "natural"
    args = SimpleNamespace(
        scene=task, terrain=terrain, observation_mode=mode, mode=run_mode,
        harness="responses", action_profile="visual-motor-v1" if mode == "image" else "ordinary-renderer-native-v1",
        out=tmp_path / "private/run", recordings=tmp_path / "outputs/recordings/test")
    selected = selected_native(tmp_path_factory.mktemp("TEST-ONLY-selected"))
    expected = mode != "image"
    historical = renderer_support.historical_policy()["renderer"][f"{task}/{terrain}"][mode]
    assert bool(historical["scored_evaluation"]
                and historical.get("action_profile", args.action_profile) == args.action_profile) is expected
    # No later binary inherits that historical positive. Exact positives below
    # are synthetic TEST ONLY records, isolated from the production registry.
    with pytest.raises(ValueError, match="unsupported"):
        renderer_support.admit_selection(args, selected)
    if expected:
        definition = suite.load(Path(selected["dependencies"]["scene_manifest"]["path"]))[task]
        install_records(tmp_path_factory.mktemp("TEST-ONLY-qualified"), monkeypatch, [
            record_for(definition, selected, terrain=terrain, mode=mode, profile=args.action_profile)])
        cell = renderer_support.admit_selection(args, selected)
        assert cell["scored_evaluation"] and cell["integration_qualification"] == "native-bound-engineering"
        assert not cell["accepted"] and cell["model_success"] == "not-inferred"
    else:
        with pytest.raises(ValueError, match="unsupported"):
            renderer_support.admit_selection(args, selected)
    assert not args.out.exists() and not args.recordings.exists()


def test_integrated_catalog_dispatch_and_adjudicated054_boundary():
    entries = suite.load(Path(suite.__file__).with_name("scenes.json"))
    assert len(entries) == 29
    assert {key for key in entries if key.startswith("T")} == {f"T{i:02}" for i in range(1, 13)}
    for task in ("T01", "T02"):
        assert entries[task]["kind"] == "navigation_tasks"
    for task in ("T03", "T04", "T05"):
        assert entries[task]["kind"] == "crafting_recovery"
    for task in ("T06", "T07", "T08", "T09"):
        assert entries[task]["kind"] == "combat_support"
    for task in ("T10", "T11", "T12"):
        assert entries[task]["kind"] == "npc_contracts"
    matrix = renderer_support.historical_policy()["renderer"]
    for mode in ("structured", "hybrid", "image"):
        assert matrix["T12/natural"][mode]["scored_evaluation"] is (mode != "image")
        current = renderer_support.support_matrix()["renderer"]["T12/natural"][mode]
        assert not current["scored_evaluation"] and current["qualification_record"] is None
    assert entries["T06"]["variants"] == ["sword", "bow", "staff", "sceptre"]
