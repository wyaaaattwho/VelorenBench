"""Successor extensions, not native qualification or replacement frozen fixtures."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.rpg_benchmark import (
    combat, results, startup, suite, support, task_templates, temporal, visual_ui, observations,
)
from scripts import rpg_sdk_agent as common
from .test_visual_ui import action, receipt

PACKAGE = Path(suite.__file__).parent


def example_manifest(tmp_path):
    value = json.loads((PACKAGE / "scenes.json").read_text())
    example = json.loads((PACKAGE / "examples/combat-variant.json").read_text())
    value["scenes"].append(example)
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(value))
    return path, example["id"]


def test_only_approved_b3_loadout_changes_catalog_and_preserves_scorer_provenance():
    # Undo only the explicitly approved B3 fields in memory. Recovering the
    # original byte hash keeps all other 28 scenes, globals and B3 fields strict;
    # the current full-file identity is separately pinned in test_candidate054.
    original = json.loads((PACKAGE / "scenes.json").read_text())
    assert len(original["scenes"]) == 29
    b3 = next(row for row in original["scenes"] if row["id"] == "B3")
    assert b3.pop("mainhand") == "common.items.weapons.sword.frost-1"
    assert b3["consumables"] == {"common.items.consumable.potion_minor": 3}
    b3["consumables"]["common.items.consumable.potion_minor"] = 2
    provenance = (
        "exact-body derivative of common.entity.wild.aggressive.ogre; native FromBody Male Ogre "
        "selects common.items.npc_weapons.hammer.ogre_hammer before NPC construction")
    assert b3["loadout_provenance"] == (
        provenance + "; player equipped with common.items.weapons.sword.frost-1")
    b3["loadout_provenance"] = provenance
    assert hashlib.sha256((json.dumps(original, indent=2) + "\n").encode()).hexdigest() == (
        "a3ebef6fe7120633c23da32cc7610bf65a95b05a079216a4202fde415d24c8be")
    assert hashlib.sha256((PACKAGE / "npc_contracts.py").read_bytes()).hexdigest() == (
        "a991347b287e57fbd4a1ef396cd268a32ec6dba111c547fd9e1708b155654bde")


def test_arbitrary_id_is_data_only_and_does_not_borrow_qualification(tmp_path, monkeypatch):
    path, name = example_manifest(tmp_path)
    definitions = suite.load(path)
    definition = definitions[name]
    assert name not in support.catalog()
    assert definition["template"] == "single_creature"
    actual = []
    async def setup(admin, actor, selected, terrain):
        actual.append(selected)
        return {"case": selected["id"], "opening": {}, "task": selected["task"]}
    monkeypatch.setattr(combat, "setup", setup)
    fixture = asyncio.run(suite.setup(None, None, definition, "flat", definitions))
    assert actual[0]["id"] == name
    assert fixture["task_id"] == name
    assert fixture["task_content_sha256"] == task_templates.identity(definition)
    for mode in support.MODES:
        profile = "visual-ui-v1" if mode == "image" else "ordinary-renderer-native-v1"
        cell = support.admit(definition, "flat", "renderer", mode, profile,
                             "task-engineering", unqualified_engineering=True)
        assert cell["implemented"] and cell["integration_qualification"] == "unqualified"
        assert not cell["scored_evaluation"] and not cell["accepted"]
        with pytest.raises(ValueError, match="unqualified"):
            support.admit(definition, "flat", "renderer", mode, profile, "capability",
                          unqualified_engineering=True)


def test_one_new_family_registration_is_sufficient(tmp_path, monkeypatch):
    async def setup(admin, actor, definition, terrain):
        return {"opening": {"tick": 1}, "task": definition["task"], "value": definition["amount"]}
    def score(fixture, final, receipts, events, samples):
        return {"success": final["value"] == fixture["value"]}
    family = SimpleNamespace(LEGACY_TEMPLATES={}, PARAMETERS={"amount"},
        TEMPLATES={"counter": {"requires": [], "required": ["amount"]}}, setup=setup, score=score)
    monkeypatch.setitem(suite.MODULES, "unit_counter_mechanic", family)
    value = json.loads((PACKAGE / "scenes.json").read_text())
    value["scenes"] = [{"id": "arbitrary-counter", "kind": "unit_counter_mechanic",
        "template": "counter", "amount": 3, "terrains": ["flat"],
        "gameplay_mode": "work-state", "task": "Test-only mechanic, not a scored task."}]
    path = tmp_path / "counter.json"
    path.write_text(json.dumps(value))
    definition = suite.load(path)["arbitrary-counter"]
    fixture = asyncio.run(suite.setup(None, None, definition, "flat", {}))
    assert suite.score(definition, fixture, {"value": 3}, [], [], [])["success"]
    assert not suite.score(definition, fixture, {"value": 2}, [], [], [])["success"]


def test_legacy_runtime_responsibilities_remain_family_owned():
    definitions = support.catalog()
    settings = {name: suite.runtime_options(copy.deepcopy(d), renderer=True)
                for name, d in definitions.items()}
    assert {k for k,v in settings.items() if v["small_world"]} == {"C1", "T11", "T12"}
    assert {k for k,v in settings.items() if v["deferred_actor"]} == {"T01", "N7"}
    assert {k for k,v in settings.items() if v["retain_admin"]} == {"T02"}
    assert {k: v["resident_fixture"] for k,v in settings.items() if v["resident_fixture"]} == {
        "T10": "resident-smoke-v1", "T11": "outcomes-message-v1", "T12": "outcomes-slay-v1"}
    alias = {**definitions["T06"], "id": "different-weapon-task"}
    suite.runtime_options(alias, variant="bow")
    assert alias["selected_variant"] == "bow"
    with pytest.raises(ValueError, match="variant"):
        suite.runtime_options(alias, variant="unknown")


def test_all_defined_cells_use_the_same_resolver_without_claiming_qualification():
    definitions, matrix = support.catalog(), support.matrix()["renderer"]
    assert len(definitions) == 29 and len(matrix) == 37
    assert set(matrix) == {f"{d['id']}/{t}" for d in definitions.values() for t in d["terrains"]}
    for definition in definitions.values():
        for terrain in definition["terrains"]:
            for mode in support.MODES:
                profile = "visual-ui-v1" if mode == "image" else "ordinary-renderer-native-v1"
                cell = support.admit(definition, terrain, "renderer", mode, profile,
                    "task-engineering", unqualified_engineering=True)
                assert cell["implemented"]
                assert cell["model_success"] == "not-inferred"
                assert cell["qualification_key"]["task_content_sha256"] == task_templates.identity(definition)
    with pytest.raises(ValueError, match="unqualified"):
        support.admit(definitions["T12"], "natural", "headless", "structured", None, "setup")
    cell = support.resolve(definitions["T12"], "natural", "headless", "structured")
    assert cell["implemented"] and not cell["missing_features"]
    assert cell["admission_blocker"] and not cell["allowed_run_modes"]


@pytest.mark.parametrize("change", [
    {"template": "invented"}, {"unknown_option": 3}, {"requires": ["teleport"]},
    {"body": {}}, {"consumables": {"item": -1}},
])
def test_unknown_or_invalid_task_configuration_fails(change):
    definition = {**support.catalog()["B1"], **change}
    with pytest.raises(ValueError):
        task_templates.configure(combat, definition)


def test_unknown_native_feature_and_action_profile_are_explicit_failures():
    definition = support.catalog()["B1"]
    with pytest.raises(ValueError, match="unknown native features"):
        support.resolve(definition, "flat", "renderer", "image", native_features={"invented"})
    cell = support.resolve(definition, "flat", "renderer", "image", "made-up")
    assert not cell["implemented"] and "profile" in cell["reason"]


@pytest.mark.parametrize("gesture", ["drag", "scroll_up", "scroll_down", "context_trade"])
def test_new_visual_gestures_preserve_addressing_and_private_receipt_boundaries(gesture):
    selected = action()
    selected["gesture"] = gesture
    if gesture == "drag":
        selected.update(to_x=30, to_y=40)
    if gesture == "context_trade":
        selected.pop("x")
        selected.pop("y")
    common.validate_request("step", {"action": selected}, renderer=True)
    bad = {**selected, "target": {"uid": 2, "lifecycle": "hidden"}}
    with pytest.raises(ValueError):
        common.validate_request("step", {"action": bad}, renderer=True)


@pytest.mark.parametrize("kind", [
    "pickup", "craft", "equip", "trade_action", "unlock_skill",
    "configure_auxiliary_ability", "inventory_action", "map_marker", "social_action",
])
def test_actual_native_resolution_can_feed_existing_scorers_without_public_semantics(kind):
    request, response = receipt()
    resolution = response["result"]["visual_resolution"]
    resolution["action"] = {"type": kind, "action_id": "a"}
    response["result"]["action_type"] = kind
    row = visual_ui.private_receipt(request, response, 1, 123)
    assert row["public_request"] == request
    assert row["request"]["args"]["action"] == resolution["action"]
    assert "visual_resolution" not in json.dumps(observations.project_image(response))
    response["result"]["visual_resolution"]["source"] = "inferred-from-click"
    with pytest.raises(ValueError):
        visual_ui.private_receipt(request, response, 1, 123)


def observation(cursor, tick=7):
    return {"session_id": "s", "tick": tick, "event_cursor": cursor,
            "sequence_domain": {"kind": "client-update-counter", "session_id": "s"}}


def test_native_event_window_preserves_equal_update_events_and_exact_sources():
    opening, final = observation(10), observation(12)
    events = [{**observation(seq), "seq": seq, "data": {"source": "server_outcome_health_change"}}
              for seq in (9, 10, 11, 12, 13)]
    selected = temporal.event_window(opening, final, events)
    assert [row["seq"] for row in selected] == [11, 12]
    assert all(row["data"]["source"] == "server_outcome_health_change" for row in selected)
    assert temporal.event_window({"tick": 7}, {"tick": 7}, events) is events
    for bad in (dict(final, sequence_domain={"kind": "server-tick"}),
                dict(final, event_cursor=9), dict(final, tick=6)):
        with pytest.raises(ValueError, match="incomparable"):
            temporal.event_window(opening, bad, events)
    bad_events = copy.deepcopy(events)
    bad_events[2]["sequence_domain"] = {"kind": "server-journal"}
    with pytest.raises(ValueError, match="domain"):
        temporal.event_window(opening, final, bad_events)


def test_renderer_samples_share_tick_but_not_host_sample_identity():
    first, final = observation(10), observation(11)
    for sequence, row in enumerate((first, final), 1):
        row["sample_identity"] = {"kind": "host-received-observation-sequence",
                                 "session_id": "s", "sequence": sequence}
    samples = [first]
    temporal.append_sample(samples, final)
    assert samples == [first, final]
    with pytest.raises(ValueError):
        temporal.append_sample(samples, final)


def test_headless_startup_uses_no_fake_frames_and_separate_detail_storage(tmp_path):
    configuration = {"observation_mode": "structured", "action_profile": "raw-native-v2"}
    selected = startup.context(configuration, "headless", None, tmp_path / "details")
    assert selected["frames"] is None and selected["detail_storage"] == str(tmp_path / "details")
    with pytest.raises(ValueError, match="construction"):
        startup.validate_receipt({"schema": "rpg-adapter-preflight/1"}, selected)
    receipt = {"schema": startup.PREFLIGHT, "startup": selected, "construction_checked": True}
    startup.validate_receipt(receipt, selected)
    receipt["startup"] = {**selected, "frames": "fabricated"}
    with pytest.raises(ValueError):
        startup.validate_receipt(receipt, selected)
    with pytest.raises(ValueError, match="frames=None"):
        startup.context(configuration, "headless", tmp_path / "frames", tmp_path / "details")


@pytest.mark.parametrize("backend,mode,profile", [
    ("headless", "structured", "raw-native-v2"),
    ("renderer", "structured", "ordinary-renderer-native-v1"),
    ("renderer", "hybrid", "ordinary-renderer-native-v1"),
    ("renderer", "image", "visual-ui-v1"),
])
def test_actual_public_client_tool_and_detail_construction(backend, mode, profile, tmp_path):
    selected = startup.context({"observation_mode": mode, "action_profile": profile}, backend,
        tmp_path / "frames" if backend == "renderer" else None, tmp_path / "details")
    receipt = asyncio.run(startup.probe_player(selected))
    startup.validate_receipt(receipt, selected)
    assert (tmp_path / "details/construction.ndjson").is_file()
    assert not (tmp_path / "frames").exists()  # renderer still owns frame storage creation


def test_a_frames_none_constructor_failure_is_not_hidden_by_import_preflight(tmp_path, monkeypatch):
    from scripts.rpg_benchmark import player
    selected = startup.context({"observation_mode": "structured", "action_profile": "raw-native-v2"},
                               "headless", None, tmp_path / "details")
    seen = []
    def real_construction_gap(native, evidence, configuration, *, frames, episode):
        seen.append(frames)
        return frames / "details"  # Exact known consumer construction failure class.
    monkeypatch.setattr(player, "tools_for", real_construction_gap)
    with pytest.raises(TypeError):
        asyncio.run(startup.probe_player(selected))
    assert seen == [None]


def test_alternate_catalog_is_hash_bound_and_rejects_unknown_data_before_allocation(tmp_path):
    from .test_backend_admission import fixture, load_renderer
    from .test_portability import save_fixture
    from scripts.rpg_benchmark import lifecycle
    config, content = fixture(tmp_path)
    catalog, name = example_manifest(tmp_path)
    config["task_manifest"] = catalog.name
    content["task_manifest"] = lifecycle.sha(catalog)
    save_fixture(tmp_path, config, content)
    selected = load_renderer(tmp_path)
    assert selected["dependencies"]["scene_manifest"]["path"] == str(catalog)
    assert name in suite.load(catalog)
    catalog.write_text(catalog.read_text().replace(name, "changed-example-id"))
    with pytest.raises(ValueError, match="changed|SHA|mismatch"):
        load_renderer(tmp_path)


@pytest.mark.parametrize("category", ["output_validation_rejection", "provider_transport_failure"])
def test_error_occurrence_source_type_and_category_survive_normalization(category):
    row = {"source": "provider", "category": category, "error": {"type": "ValueError"},
           "occurrence": {"clock": "host-monotonic", "value": 9, "source": "provider"},
           "host_recorded_at": "2099-01-01T00:00:00Z"}
    normalized = results.observations({"provider_errors": [row]}, 10)[0]
    assert normalized["ordering"] == "pre_terminal"
    assert normalized["source"] == "provider" and normalized["category"] == category
    assert normalized["error_detail"]["type"] == "ValueError"


@pytest.mark.parametrize("clock,causal,expected", [
    ("host-monotonic", True, True), ("host-monotonic", False, False),
    ("provider-monotonic", True, False), ("unknown", False, False),
])
def test_cancellation_requires_comparable_occurrence_and_observed_stop_causality(clock, causal, expected):
    stop = {"signal": "SIGINT", "sent": True, "requested_monotonic": 11, "stop_id": "stop-1"}
    row = {"source": "provider", "error": {"type": "CancelledError"},
           "occurrence": {"clock": clock, "value": 12, "source": "provider"},
           "host_recorded_at": "2099-01-01T00:00:00Z"}
    if causal:
        row["caused_by"] = {"kind": "evaluator_stop", "stop_id": "stop-1", "signal": "SIGINT"}
    selected = results.observations({"provider_errors": [row]}, 10, stop)[0]
    assert bool(selected.get("evaluator_terminal_cancellation")) is expected
    if clock != "host-monotonic":
        assert selected["ordering"] == "unknown"


def test_adapter_cannot_self_assert_cancellation_acceptance():
    row = {"source": "adapter", "wall_monotonic": 12, "error": {"type": "CancelledError"},
           "evaluator_terminal_cancellation": True}
    observed = results.observations({"error_observations": [row]}, 10,
        {"sent": True, "signal": "SIGINT", "requested_monotonic": 11, "stop_id": "stop"})
    assert not observed[0].get("evaluator_terminal_cancellation")


def test_legacy_success_collector_cannot_bypass_generic_causal_cancellation(tmp_path):
    from .test_rpg_benchmark import TerminalEvidenceTests
    fixture = TerminalEvidenceTests()
    selected = fixture.selected(tmp_path)
    assert fixture.proof(selected, [fixture.receipt()])["accepted"]
    for row in selected["provider_cancellation_events"]:
        row.pop("occurrence")
        row.pop("caused_by")
    assert not fixture.proof(selected, [fixture.receipt()])["accepted"]


def test_new_controls_are_advertised_only_by_selected_native_implementation(tmp_path):
    from .test_visual_ui import native, Evidence
    from scripts.rpg_benchmark.renderer_tools import RendererTools
    old = native()
    tools = RendererTools(old, Evidence(), "image", tmp_path, "episode", visual_ui.PROFILE)
    assert not tools.contract()["visual_addressing"]["crafting"]
    gesture = next(action for action in tools.schemas["step"]["properties"]["action"]["oneOf"]
                   if action["properties"]["type"]["enum"] == ["visual_input"])
    assert "drag" not in gesture["properties"]["gesture"]["enum"]
    new = native()
    new.ready["renderer"].update(benchmark_features=list(support.RENDERER_FEATURES),
                                ui_panels=["world", "inventory", "equipment", "map", "crafting", "skills", "trade"])
    tools = RendererTools(new, Evidence(), "image", tmp_path, "episode", visual_ui.PROFILE)
    assert tools.contract()["visual_addressing"]["crafting"]
    assert "trade" in tools.contract()["ui_views"]


def test_bundled_subprocess_preflight_constructs_exact_selected_storage(tmp_path):
    from scripts.rpg_benchmark import providers, run
    from scripts.rpg_benchmark.responses_adapter import Adapter
    args = run.parser().parse_args([
        "--pins", str(tmp_path / "not-read"), "--pins-sha256", "0" * 64,
        "--out", str(tmp_path / "not-allocated"), "--harness", "responses", "--mode", "task-engineering",
        "--observation-mode", "image", "--action-profile", "visual-ui-v1",
        "--adapter-config", json.dumps({"mock_provider": "http://127.0.0.1:1", "provider": providers.LUNA})])
    adapter = Adapter(args)
    result = asyncio.run(adapter.preflight(backend="renderer", frames=tmp_path / "frames",
                                           detail_storage=tmp_path / "startup"))
    assert result["receipt"]["schema"] == startup.PREFLIGHT
    assert result["receipt"]["startup"]["frames"] == str(tmp_path / "frames")
    assert (tmp_path / "startup/construction.ndjson").is_file()
    assert not (tmp_path / "frames").exists()
