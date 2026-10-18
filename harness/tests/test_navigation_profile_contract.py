"""Profile requirements describe the original family mechanics, not its name."""
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.rpg_benchmark import navigation, suite, support, task_templates
from scripts.rpg_benchmark.renderer_tools import RendererTools


def test_natural_long_pickup_goal_is_not_admitted_to_the_legacy_motor_profile():
    definition = support.catalog()["N5"]
    cell = support.resolve(definition, "natural", "renderer", "image", "visual-motor-v1")
    assert not cell["implemented"]
    assert cell["missing_features"] == ["pickup"]
    assert definition["requires"] == ["coordinate_goal", "movement", "pickup"]


@pytest.mark.parametrize("task_id", ["N2", "extension-rendezvous"])
def test_rendezvous_without_supplies_does_not_require_a_pickup_interface(task_id):
    definition = copy.deepcopy(support.catalog()["N2"])
    definition["id"] = task_id
    definition.pop("requires", None)
    configured = task_templates.configure(navigation, definition)
    assert configured["template"] == "supply_rendezvous"  # Public template name retained.
    assert configured["requires"] == ["coordinate_goal", "movement"]
    cell = support.admit(configured, "flat", "renderer", "image", "visual-motor-v1",
                         "task-engineering", unqualified_engineering=True)
    assert cell["implemented"] and not cell["missing_features"]
    assert not cell["scored_evaluation"] and not cell["accepted"]
    assert cell["qualification_key"]["task_id"] == task_id
    with pytest.raises(ValueError, match="requirements"):
        task_templates.configure(navigation, {**configured, "requires": [
            "movement", "coordinate_goal", "pickup"]})


def test_legacy_motor_profile_does_not_gain_unrelated_semantic_operations():
    definition = support.catalog()["N7"]
    cell = support.resolve(definition, "natural", "renderer", "image", "visual-motor-v1")
    assert not cell["implemented"] and cell["missing_features"] == ["map"]
    assert "pickup" not in cell["available_features"]


@pytest.mark.parametrize("task_id", ["N5", "author-apple-route"])
@pytest.mark.parametrize("profile", ["visual-motor-v1", "visual-ui-v1"])
def test_pickup_template_preserves_authored_dependencies_for_any_id(task_id, profile):
    definition = copy.deepcopy(support.catalog()["N5"])
    definition["id"] = task_id
    definition.pop("requires", None)
    configured = task_templates.configure(navigation, definition)
    assert configured["template"] == "pickup_rendezvous"
    assert configured["requires"] == ["coordinate_goal", "movement", "pickup"]
    cell = support.resolve(configured, "natural", "renderer", "image", profile)
    assert cell["implemented"] == (profile == "visual-ui-v1")
    assert cell["missing_features"] == ([] if cell["implemented"] else ["pickup"])
    if cell["implemented"]:
        support.admit(configured, "natural", "renderer", "image", profile,
                      "task-engineering", unqualified_engineering=True)
    else:
        with pytest.raises(ValueError, match="pickup"):
            support.admit(configured, "natural", "renderer", "image", profile,
                          "task-engineering", unqualified_engineering=True)
    assert not cell["scored_evaluation"] and not cell["accepted"]
    assert cell["qualification_key"]["task_id"] == task_id
    for mode in ("structured", "hybrid"):
        assert support.resolve(configured, "natural", "renderer", mode)["implemented"]
    with pytest.raises(ValueError, match="requirements"):
        task_templates.configure(navigation, {**configured, "requires": ["movement", "coordinate_goal"]})


@pytest.mark.parametrize("source_id", ["N1", "N2", "N3", "N4", "N6"])
@pytest.mark.parametrize("profile", ["visual-motor-v1", "visual-ui-v1"])
def test_supply_free_layouts_remain_supported_for_both_image_profiles(source_id, profile):
    definition = copy.deepcopy(support.catalog()[source_id])
    definition["id"] = "author-" + source_id
    cell = support.admit(definition, "flat", "renderer", "image", profile,
                         "task-engineering", unqualified_engineering=True)
    assert cell["implemented"] and not cell["missing_features"]
    assert cell["required_features"] == ["coordinate_goal", "movement"]
    assert not cell["scored_evaluation"]


@pytest.mark.parametrize("task_id", ["N5", "author-apple-route"])
@pytest.mark.parametrize("template", ["wall_rendezvous", "supply_rendezvous", "terrain_rendezvous", "named_site"])
def test_explicit_template_cannot_hide_a_pickup_layout_dependency(task_id, template):
    # Isolate the semantic mismatch using fields accepted even by the narrower
    # legacy wall/supply templates; their separate unknown-field guard must not
    # mask a missing layout dependency.
    definition = {"id": task_id, "kind": "navigation", "template": template,
                  "layout": "natural_long", "terrains": ["natural"],
                  "gameplay_mode": "work-state"}
    with pytest.raises(ValueError, match="requirements.*layout"):
        task_templates.configure(navigation, definition)
    with pytest.raises(ValueError, match="requirements.*layout"):
        suite.runtime_options(definition, renderer=True, terrain="natural")


@pytest.mark.parametrize("layout", [*navigation.layouts.CASES, "recovery", "named_site_long", "unknown"])
def test_pickup_template_cannot_invent_supplies_for_a_different_layout(layout):
    definition = {**support.catalog()["N5"], "id": "author-route", "layout": layout}
    definition.pop("requires", None)
    with pytest.raises(ValueError, match="layout"):
        task_templates.configure(navigation, definition)


@pytest.mark.parametrize("profile", ["visual-motor-v1", "visual-ui-v1"])
def test_full_public_schema_distinguishes_motor_from_fenced_hud_pickup(profile):
    native = SimpleNamespace(ready={
        "session_id": "session", "capabilities": {"rpg_interactions": {}},
        "renderer": {"actor": {"uid": 1, "lifecycle": "life"}, "control": {},
                     "benchmark_features": sorted(support.RENDERER_FEATURES),
                     "visual_ui": {"schema": "native-visual-ui/1"}}})
    tools = RendererTools(native, None, "image", Path("/unused"), "episode", profile)
    names = set(tools.schemas)
    assert names == {"step", "set_control_state", "release_control_state", "ping",
                     "frame", "ui", "contract", "result"}
    actions = {entry["properties"]["type"]["enum"][0]
               for entry in tools.schemas["step"]["properties"]["action"]["oneOf"]}
    motor = {"move_forward", "move_backward", "move_left", "move_right", "move_vector",
             "jump", "sit", "stop", "toggle_wield"}
    assert actions == motor | ({"visual_input"} if profile == "visual-ui-v1" else set())
    assert tools.contract()["visual_addressing"]["pickup"] == (profile == "visual-ui-v1")
    # Neither direct semantic pickup nor hidden aliases/extra control fields
    # can smuggle an interaction through the complete image schema.
    for action in ({"type": "pickup"}, {"type": "context_interact"},
                   {"type": "move_forward", "pickup": True}):
        with pytest.raises(ValueError):
            tools.validate_request("step", {"action": action})
    controls = tools.schemas["set_control_state"]["properties"]
    assert "pickup" not in controls and "interact" not in controls
    assert controls["target"] == {"type": "null"}
    assert "destination" not in controls and "movement_intent" not in controls
    visual = {"type": "visual_input", "action_id": "action", "gesture": "interact", "frame_id": "frame",
              "ui_state_id": "ui", "expected_session_id": "session",
              "actuator": {"actor": {"uid": 1, "lifecycle": "life"},
                           "control": {"owner": "", "generation": 0, "sequence": 0,
                                       "retired": False, "protection_generation": 0},
                           "active_action_id": None}}
    if profile == "visual-ui-v1":
        tools.validate_request("step", {"action": visual})
    else:
        with pytest.raises(ValueError):
            tools.validate_request("step", {"action": visual})


@pytest.mark.parametrize("template", ["pickup_rendezvous", "terrain_rendezvous"])
def test_author_manifest_uses_the_same_layout_contract(tmp_path, template):
    manifest = json.loads(Path(suite.__file__).with_name("scenes.json").read_text())
    definition = copy.deepcopy(next(d for d in manifest["scenes"] if d["id"] == "N5"))
    definition.update(id="arbitrary-authored-apple-route", template=template)
    manifest["scenes"] = [definition]
    path = tmp_path / "authored.json"
    path.write_text(json.dumps(manifest))
    if template == "pickup_rendezvous":
        configured = suite.load(path)[definition["id"]]
        assert configured["requires"] == ["coordinate_goal", "movement", "pickup"]
        assert not support.resolve(configured, "natural", "renderer", "image",
                                   "visual-motor-v1")["implemented"]
    else:
        with pytest.raises(ValueError, match="requirements.*layout"):
            suite.load(path)


def test_generated_catalog_exposes_pickup_semantics_without_removing_ui_support():
    tool = Path(__file__).resolve().parents[2] / "tools/generate_task_docs.py"
    if not tool.exists():  # Installed test copy has tools beside tests.
        tool = Path(__file__).resolve().parents[1] / "tools/generate_task_docs.py"
    spec = importlib.util.spec_from_file_location("navigation_generated_docs", tool)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    generated = module.sections()
    assert "| N5 | navigation | pickup_rendezvous | natural | coordinate_goal, movement, pickup |" in generated["TASKS.md"]
    assert "| N2 | navigation | supply_rendezvous | flat | coordinate_goal, movement |" in generated["TASKS.md"]
    assert "| N5/natural | declared | declared | declared |" in generated["SUPPORT.md"]
    assert "| N5/natural | declared | declared | declared | missing native/profile features: pickup |" in generated["SUPPORT.md"]
    matrix = support.matrix(mode="image", profile="visual-motor-v1")
    assert not matrix["renderer"]["N5/natural"]["image"]["implemented"]
    assert matrix["renderer"]["N2/flat"]["image"]["implemented"]
