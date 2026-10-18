"""Semantic dependency binding; all positive evidence is synthetic TEST ONLY."""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts.rpg_benchmark import (
    crafting, lifecycle, mixed, qualification, renderer_support, suite, support, task_templates,
)
from .test_admission_closeout import args_for, install_records, record_for, selected_native
from .test_backend_admission import load_renderer
from .test_portability import save_fixture


def catalog_data():
    return json.loads(Path(suite.__file__).with_name("scenes.json").read_text())


def bind_catalog(tmp_path, value, *, formatted=False):
    """Actually validate a NEW synthetic runtime/content binding; no native launch."""
    path = tmp_path / "author-catalog.json"
    path.write_text(json.dumps(value, indent=4 if formatted else None, sort_keys=formatted))
    config = json.loads((tmp_path / "runtime.json").read_text())
    content = json.loads((tmp_path / "content.json").read_text())
    config["task_manifest"] = path.name
    content["task_manifest"] = lifecycle.sha(path)
    save_fixture(tmp_path, config, content)
    return load_renderer(tmp_path)


def capability_args(tmp_path, task, terrain):
    args = args_for(tmp_path, scene=task, terrain=terrain)
    args.mode, args.unqualified_engineering = "capability", False
    return args


@pytest.mark.parametrize("task,terrain", [("B1", "flat"), ("X1", "natural"), ("X2", "natural")])
@pytest.mark.parametrize("change", ["unrelated-addition", "format-only", "unrelated-content"])
def test_existing_record_survives_unrelated_addition_formatting_and_content(
        tmp_path, monkeypatch, task, terrain, change):
    selected_native(tmp_path)
    value = catalog_data()
    selected = bind_catalog(tmp_path, value)
    path = Path(selected["dependencies"]["scene_manifest"]["path"])
    record = record_for(suite.load(path)[task], selected, terrain=terrain)
    install_records(tmp_path, monkeypatch, [record])
    args = capability_args(tmp_path, task, terrain)
    before = renderer_support.admit_selection(args, selected)
    if change == "unrelated-addition":
        example = json.loads(Path(suite.__file__).with_name("examples").joinpath("combat-variant.json").read_text())
        value["scenes"].append(example)
    elif change == "unrelated-content":
        next(row for row in value["scenes"] if row["id"] == "T01")["task"] += " TEST ONLY unrelated change."
    selected = bind_catalog(tmp_path, value, formatted=change == "format-only")
    after = renderer_support.admit_selection(args, selected)
    assert after["qualification_key"] == before["qualification_key"] == record["key"]
    assert after["task_manifest_sha256"] != before["task_manifest_sha256"]
    assert "task_manifest_sha256" not in record["key"]
    assert after["scored_evaluation"] and not after["accepted"]
    assert after["model_success"] == "not-inferred"
    assert support.matrix(selected=selected)["renderer"][f"{task}/{terrain}"]["image"]["scored_evaluation"]
    assert not args.out.exists() and not args.recordings.exists()


@pytest.mark.parametrize("task", ["X1", "X2"])
@pytest.mark.parametrize("dependency,role", [("C2", "craft"), ("B1", "encounter")])
def test_changed_actual_dependency_content_invalidates_only_composite_key(
        tmp_path, monkeypatch, task, dependency, role):
    selected_native(tmp_path)
    value = catalog_data()
    selected = bind_catalog(tmp_path, value)
    definition = suite.load(Path(selected["dependencies"]["scene_manifest"]["path"]))[task]
    record = record_for(definition, selected, terrain="natural")
    install_records(tmp_path, monkeypatch, [record])
    args = capability_args(tmp_path, task, "natural")
    assert renderer_support.admit_selection(args, selected)["scored_evaluation"]
    row = next(row for row in value["scenes"] if row["id"] == dependency)
    if dependency == "B1":
        other = next(row for row in value["scenes"] if row["id"] == "B2")
        row.update(asset=other["asset"], body=copy.deepcopy(other["body"]))
    else:
        row["task"] += " TEST ONLY changed referenced crafting content."
    selected = bind_catalog(tmp_path, value)
    after = support.matrix(selected=selected)["renderer"][f"{task}/natural"]["image"]
    assert after["qualification_key"]["task_content_sha256"] == record["key"]["task_content_sha256"]
    assert (after["qualification_key"]["setup_dependencies"][role]
            != record["key"]["setup_dependencies"][role])
    assert not after["scored_evaluation"] and after["qualification_record"] is None
    with pytest.raises(ValueError, match="unqualified"):
        renderer_support.admit_selection(args, selected)
    assert not args.out.exists() and not args.recordings.exists()


def test_requirements_and_configured_shared_setup_inputs_cannot_inherit(tmp_path, monkeypatch):
    selected = selected_native(tmp_path)
    definition = support.catalog()["B1"]
    record = record_for(definition, selected)
    install_records(tmp_path, monkeypatch, [record])
    definition["requires"] = [*definition["requires"], "pickup"]
    with pytest.raises(ValueError, match="requirements"):
        support.resolve(definition, "flat", "renderer", "image",
                        native_identity=qualification.selected_native(selected),
                        native_features=support.RENDERER_FEATURES)
    for field in ("placements", "player_equipment"):
        definition = copy.deepcopy(support.catalog()["B1"])
        definition[field] = {**definition[field], "test-only-different-setup-input": True}
        cell = support.resolve(definition, "flat", "renderer", "image",
                               native_identity=qualification.selected_native(selected),
                               native_features=support.RENDERER_FEATURES)
        assert not cell["scored_evaluation"] and cell["qualification_record"] is None


@pytest.mark.parametrize("problem", ["missing", "ambiguous", "wrong-family"])
def test_invalid_setup_reference_fails_both_qualification_and_setup_before_actions(monkeypatch, problem):
    definitions = copy.deepcopy(support.catalog())
    definition = definitions["X1"]
    if problem == "missing":
        del definitions["C2"]
    elif problem == "ambiguous":
        definitions["another-key-for-C2"] = copy.deepcopy(definitions["C2"])
    else:
        definitions["C2"] = {**definitions["B1"], "id": "C2"}
    async def no_native(*args, **kwargs):
        pytest.fail("invalid setup reference reached native fixture actions")
    monkeypatch.setattr(crafting, "setup", no_native)
    with pytest.raises(ValueError, match="missing or ambiguous setup dependency"):
        support.resolve(definition, "natural", "renderer", "image", definitions=definitions)
    with pytest.raises(ValueError, match="missing or ambiguous setup dependency"):
        asyncio.run(suite.setup(None, None, definition, "natural", definitions))


@pytest.mark.parametrize("problem", ["missing", "duplicate"])
def test_selected_catalog_refuses_missing_or_duplicate_referenced_tasks(tmp_path, problem):
    value = catalog_data()
    if problem == "missing":
        value["scenes"] = [row for row in value["scenes"] if row["id"] != "C2"]
    else:
        value["scenes"].append(copy.deepcopy(next(row for row in value["scenes"] if row["id"] == "C2")))
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="missing or ambiguous setup dependency|duplicate concrete scene"):
        suite.load(path)


@pytest.mark.parametrize("changed_declaration", [False, True])
def test_actual_composite_setup_and_key_share_the_family_reference_source(monkeypatch, changed_declaration):
    definitions = support.catalog()
    definition = definitions["X1"]
    original = support.resolve(definition, "natural", "renderer", "image",
                               definitions=definitions)["qualification_key"]
    # Change ONLY the family declaration. No separate setup/qualification ID
    # list is edited. Stop real setup at the mocked spawn, never run native.
    if changed_declaration:
        monkeypatch.setattr(mixed, "SETUP_REFERENCES",
                            {"craft": ("C3", "crafting"), "encounter": ("B2", "combat")})
    key = support.resolve(definition, "natural", "renderer", "image",
                          definitions=definitions)["qualification_key"]
    assert key["setup_dependencies"] == {
        role: {"task_id": name, "template": definitions[name]["template"],
               "task_content_sha256": task_templates.identity(definitions[name])}
        for role, (name, kind) in mixed.SETUP_REFERENCES.items()}
    assert (key != original) is changed_declaration
    seen = {}
    async def craft(admin, actor, selected, terrain):
        seen["craft"] = selected
        return {"placement": {"start": [0, 0, 0]}}
    async def spawn(admin, command, asset, amount):
        seen["encounter_asset"] = asset
        raise RuntimeError("TEST ONLY stop at mocked spawn")
    monkeypatch.setattr(crafting, "setup", craft)
    monkeypatch.setattr(mixed, "resource_adequacy", lambda fixture: {})
    monkeypatch.setattr(mixed.native, "settled_point", AsyncMock(side_effect=lambda admin, point: point))
    monkeypatch.setattr(mixed.native, "drop_supply", AsyncMock(return_value={"entity": {"position": [1, 2, 3]}}))
    monkeypatch.setattr(mixed.native, "goto", AsyncMock())
    monkeypatch.setattr(mixed.navigation, "physical_route", AsyncMock())
    monkeypatch.setattr(mixed.native, "command", spawn)
    actor = SimpleNamespace(request=AsyncMock(return_value={"nearby_entities": []}))
    with pytest.raises(RuntimeError, match="TEST ONLY stop"):
        asyncio.run(suite.setup(None, actor, definition, "natural", definitions))
    assert seen["craft"] == definitions[mixed.SETUP_REFERENCES["craft"][0]]
    assert seen["encounter_asset"] == definitions[mixed.SETUP_REFERENCES["encounter"][0]]["asset"]


def test_malformed_dependency_record_fails_closed(tmp_path, monkeypatch):
    selected = selected_native(tmp_path)
    record = record_for(support.catalog()["X1"], selected, terrain="natural")
    del record["key"]["setup_dependencies"]["craft"]["task_content_sha256"]
    install_records(tmp_path, monkeypatch, [record])
    with pytest.raises(ValueError, match="setup dependency identity"):
        support.resolve(support.catalog()["X1"], "natural", "renderer", "image")
