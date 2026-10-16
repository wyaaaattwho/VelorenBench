import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import rpg_sdk_agent as common
from scripts.rpg_benchmark import capabilities, observations, providers, renderer_support, visual_ui
from scripts.rpg_benchmark.renderer_tools import RendererTools
from scripts.rpg_benchmark.player_types import Invocation


class Evidence:
    def write(self, *args, **kwargs):
        pass


def action():
    return {"type": "visual_input", "action_id": "a", "frame_id": "frame",
            "ui_state_id": "frame", "expected_session_id": "s", "gesture": "right_click",
            "x": 10, "y": 20, "actuator": {
                "actor": {"uid": 1, "lifecycle": "life"}, "active_action_id": None,
                "control": {"owner": "", "generation": 0, "sequence": 0, "retired": False,
                            "protection_generation": 0}}}


def native():
    return SimpleNamespace(ready={
        "session_id": "s", "renderer": {
            "actor": action()["actuator"]["actor"], "control": {},
            "visual_ui": {"schema": visual_ui.SCHEMA}}})


def test_visual_profile_is_explicit_and_mode_mapped():
    manifest = providers.manifest(providers.LUNA, "image")
    assert capabilities.negotiate(manifest, {})[0]["action_profile"] == "visual-motor-v1"
    assert capabilities.negotiate(manifest, {"action_profile": visual_ui.PROFILE})[0]["action_profile"] == visual_ui.PROFILE
    for mode in ("structured", "hybrid"):
        with pytest.raises(ValueError, match="mapping"):
            capabilities.negotiate(providers.manifest(providers.LUNA, mode),
                                   {"action_profile": visual_ui.PROFILE})


def test_headless_and_legacy_profiles_do_not_advertise_visual_input():
    with pytest.raises(ValueError):
        common.validate_request("step", {"action": action()})
    for mode, profile in (("image", "visual-motor-v1"), ("image", visual_ui.PROFILE)):
        tools = RendererTools(native(), Evidence(), mode, Path("/unused"), "ep", profile)
        kinds = [entry["properties"]["type"]["enum"][0]
                 for entry in tools.schemas["step"]["properties"]["action"]["oneOf"]]
        assert ("visual_input" in kinds) == (profile == visual_ui.PROFILE)
        assert not {"use_item", "equip", "dialogue_action", "context_interact"} & set(kinds)
    wrong = native()
    del wrong.ready["renderer"]["visual_ui"]
    with pytest.raises(ValueError, match="capability"):
        RendererTools(wrong, Evidence(), "image", Path("/unused"), "ep", visual_ui.PROFILE)


@pytest.mark.parametrize("field,value", [
    ("item_id", "hidden"), ("uid", 7), ("slot", 0), ("response_id", 3),
    ("x", -1), ("gesture", "trade"), ("actuator", {"generation": 0}),
])
def test_visual_schema_rejects_hidden_or_unsupported_selectors(field, value):
    value_action = action()
    value_action[field] = value
    with pytest.raises(ValueError):
        common.validate_request("step", {"action": value_action}, renderer=True)


def test_interact_has_no_pointer_or_hidden_target():
    selected = action()
    selected["gesture"] = "interact"
    with pytest.raises(ValueError):
        common.validate_request("step", {"action": selected}, renderer=True)
    selected.pop("x")
    selected.pop("y")
    common.validate_request("step", {"action": selected}, renderer=True)


def receipt():
    request = {"cmd": "step", "id": "r", "args": {"action": action()}}
    canonical = {"type": "use_item", "action_id": "a", "slot": 0}
    response = {"id": "r", "ok": True, "session_id": "s", "result": {
        "action_id": "a", "action_type": "use_item", "status": "completed",
        "detail": {"item_definition_id": "PRIVATE", "consumable": True},
        "visual_resolution": {"source": "native-hud-hit-test-v1",
                              "original_args": request["args"], "action": canonical}}}
    return request, response


def test_private_normalization_retains_exact_visual_model_intent_without_oracle_changes():
    request, response = receipt()
    row = visual_ui.private_receipt(request, response, 1, 123)
    assert row["public_request"] == request
    assert row["request"]["args"]["action"] == response["result"]["visual_resolution"]["action"]
    assert row["response"] == response
    assert request["args"]["action"]["type"] == "visual_input"
    for path in ("source", "original_args", "action"):
        bad = copy.deepcopy(response)
        bad["result"]["visual_resolution"][path] = {}
        with pytest.raises(ValueError):
            visual_ui.private_receipt(request, bad, 1, 123)


@pytest.mark.parametrize("ok,status", [
    (True, "completed"), (True, "pending"), (False, "failed"),
    (True, "interrupted"), (False, "unknown"),
])
def test_all_visual_receipts_and_errors_project_out_resolved_private_answers(ok, status):
    request, response = receipt()
    response.update(ok=ok, error={"code": "stale_visual_frame", "message": "PRIVATE"})
    response["result"]["status"] = status
    for field in ("result", "data"):
        raw = dict(response)
        raw[field] = raw.pop("result")
        projected = observations.project_image(raw)
        assert "PRIVATE" not in json.dumps(projected)
        assert "visual_resolution" not in json.dumps(projected)


def test_pending_and_unknown_visual_action_never_resubmits():
    async def check():
        tools = RendererTools(native(), Evidence(), "image", Path("/unused"), "ep", visual_ui.PROFILE)
        request = {"id": "r", "args": {"action": action()}}
        task = asyncio.get_running_loop().create_future()
        tools.pending["r"] = request, task
        result = await tools.handler("result")(Invocation("s", "c", "game_result", {"request_id": "r"}))
        assert json.loads(result.text_result_for_llm)["state"] == "pending"
        task.set_exception(RuntimeError("PRIVATE unknown native effect"))
        for _ in range(2):
            result = await tools.handler("result")(Invocation("s", "c", "game_result", {"request_id": "r", "wait": True}))
            assert result.result_type == "failure"
            assert "PRIVATE" not in result.text_result_for_llm
            assert json.loads(result.text_result_for_llm)["retry_policy"] == "never automatically resubmit"
    asyncio.run(check())


@pytest.mark.parametrize("scene,terrain,profile,run_mode,admitted", [
    ("T05", "natural", visual_ui.PROFILE, "task-engineering", True),
    ("T05", "natural", visual_ui.PROFILE, "capability", False),
    ("T05", "natural", "visual-motor-v1", "task-engineering", False),
    ("T01", "natural", visual_ui.PROFILE, "task-engineering", False),
    ("T02", "flat", visual_ui.PROFILE, "task-engineering", False),
    ("T03", "natural", visual_ui.PROFILE, "task-engineering", False),
    ("T04", "natural", visual_ui.PROFILE, "task-engineering", False),
    ("C1", "natural", visual_ui.PROFILE, "capability", False),
    ("N1", "flat", "visual-motor-v1", "capability", True),
    ("N1", "flat", visual_ui.PROFILE, "capability", False),
])
def test_admission_only_adds_the_qualified_image_task_profile(
        tmp_path, scene, terrain, profile, run_mode, admitted):
    args = SimpleNamespace(scene=scene, terrain=terrain, observation_mode="image",
                           action_profile=profile, mode=run_mode, harness="responses",
                           recordings=tmp_path / "outputs/recordings/new-run", out=tmp_path / "run")
    selected = {"renderer": {}, "renderer_fixture_helper": {}, "protected_paths": []}
    if admitted:
        renderer_support.admit_selection(args, selected)
    else:
        with pytest.raises(ValueError, match="unsupported"):
            renderer_support.admit_selection(args, selected)


def test_support_matrix_separates_primitives_from_full_task_qualification():
    matrix = renderer_support.support_matrix()
    image = matrix["renderer"]["T05/natural"]["image"]
    assert image["scored_evaluation"] and image["action_profile"] == visual_ui.PROFILE
    assert image["allowed_run_modes"] == ["task-engineering"] and image["accepted"] is False
    for task in ("T01/natural", "T02/flat", "T03/natural", "T04/natural", "C1/natural"):
        assert matrix["renderer"][task]["image"]["scored_evaluation"] is False
    primitives = matrix["visual_primitives"]["image"]
    assert {"inventory equip selection", "visible native Talk prompt", "native dialogue choice",
            "visible statement continuation", "persistent native workshop marker"} <= set(
                primitives["qualified_final_native"])
    assert all(run in primitives["final_native_requalification"] for run in (
        "integrated034-equipment-image-002", "integrated034-dialogue-image-005",
        "integrated034-namespace-hybrid-001"))
    assert "sealed visual002/030 remains historical" in primitives["inherited_consumable_qualification"]
    assert "integrated034-t05-image-001" in image["qualification"]
    evidence = matrix["qualification_evidence"]
    assert evidence["accepted"] is False and evidence["capability_claim"] is False
    assert evidence["native_tree_sha256"] == "4c2cc4bb7cb7e6a94f963253866f09c2a0a6fde0df2d23ece9c99f78322944db"
    assert {row["task"] for row in evidence["fresh_integrated_tasks"]} == {
        "T02", "T05", "T06", "T09", "T10", "T11"}
    assert "parent0.4.1/candidate001" in evidence["historical_correspondence"]
