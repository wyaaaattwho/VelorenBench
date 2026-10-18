"""Shared intent attribution at the native visual/structured fence boundary."""
import copy

import pytest

from scripts.rpg_benchmark import combat_support, visual_ui
from .test_combat_support import positive, score
from .test_visual_ui import action as visual_action


def image_receipt(row, opening):
    """Model a native HUD result, then use the production private normalizer."""
    row = copy.deepcopy(row)
    public = copy.deepcopy(row["request"])
    public["args"].pop("control_fence")
    gesture = visual_action()
    gesture.update(action_id=public["args"]["action"]["action_id"],
                   expected_session_id=opening["session_id"])
    gesture["actuator"]["actor"] = opening["control_actor"]
    public["args"]["action"] = gesture
    row["response"]["id"] = public["id"]
    row["response"]["result"]["visual_resolution"] = {
        "source": "native-hud-hit-test-v1", "original_args": copy.deepcopy(public["args"]),
        "action": copy.deepcopy(row["request"]["args"]["action"])}
    return visual_ui.private_receipt(public, row["response"], row["actor"], 123)


def image_build():
    values = positive("T07")
    for index in (1, 2):
        values[2][index] = image_receipt(values[2][index], values[0]["opening"])
    return values


def test_image_build_intents_reach_original_learning_configuration_and_effect_predicates():
    values = image_build()
    result = score(values)
    assert result["parts"]["learned_and_spent"]
    assert result["parts"]["configured"]
    assert result["parts"]["configured_learned_effect"]
    assert result["success"]
    assert len(result["native_intents"]) == 4
    assert all("control_fence" not in row["request"]["args"] for row in values[2][1:3])


@pytest.mark.parametrize("case", sorted(combat_support.TASK_IDS))
def test_all_shared_consumers_keep_structured_and_motor_results(case):
    values = positive(case)
    before = score(values)
    # All original semantic, target/attack and support evidence is unchanged.
    assert before["success"]
    assert combat_support.intent_receipts(values[0]["opening"], values[2]) == before["native_intents"]


@pytest.mark.parametrize("kind", [
    "use_item", "equip", "dialogue_action", "context_interact", "pickup", "craft",
    "trade_action", "social_action", "unlock_skill", "configure_auxiliary_ability",
    "inventory_action", "map_marker",
])
def test_visual_fence_is_shared_by_all_existing_canonical_hud_action_types(kind):
    values = positive("T07")
    opening = values[0]["opening"]
    raw = copy.deepcopy(values[2][1])
    raw["request"]["args"]["action"]["type"] = kind
    raw["response"]["result"]["action_type"] = kind
    row = image_receipt(raw, opening)
    accepted = combat_support.intent_receipts(opening, [row])
    assert len(accepted) == 1
    assert accepted[0]["action"] == raw["request"]["args"]["action"]


@pytest.mark.parametrize("mutation", [
    "row_actor", "response_session", "response_ok", "request_id", "response_id",
    "action_id", "result_action_id", "result_action_type", "status",
    "public_missing", "marker_missing", "marker_forged", "resolution_missing",
    "source", "original_args", "canonical_action", "public_cmd", "public_id",
    "public_action_type", "public_action_id", "public_frame", "public_ui",
    "public_session", "public_actor", "public_control", "public_active_action",
    "missing_frame", "missing_ui", "missing_session", "missing_actuator",
    "missing_control", "missing_actor", "missing_generation", "missing_active_action",
    "coherent_wrong_actor", "coherent_wrong_lifecycle", "coherent_wrong_session",
    "coherent_frame_mismatch", "coherent_empty_frame", "coherent_bad_control",
    "coherent_busy", "coherent_hidden_selector", "coherent_bad_gesture",
])
def test_visual_origin_boundary_rejects_forged_stale_missing_or_mismatched_evidence(mutation):
    values = image_build()
    opening = values[0]["opening"]
    row = copy.deepcopy(values[2][1])
    public = row["public_request"]
    action = public["args"]["action"]
    result = row["response"]["result"]
    if mutation == "row_actor":
        row["actor"] = 99
    elif mutation == "response_session":
        row["response"]["session_id"] = "other"
    elif mutation == "response_ok":
        row["response"]["ok"] = False
    elif mutation == "request_id":
        row["request"]["id"] = "other"
    elif mutation == "response_id":
        row["response"]["id"] = "other"
    elif mutation == "action_id":
        row["request"]["args"]["action"]["action_id"] = "other"
    elif mutation == "result_action_id":
        result["action_id"] = "other"
    elif mutation == "result_action_type":
        result["action_type"] = "use_item"
    elif mutation == "status":
        result["status"] = "unknown"
    elif mutation == "public_missing":
        row.pop("public_request")
    elif mutation.startswith("marker_"):
        row.pop("resolution_source")
        if mutation == "marker_forged":
            row["resolution_source"] = "trust me"
    elif mutation == "resolution_missing":
        result.pop("visual_resolution")
    elif mutation == "source":
        result["visual_resolution"]["source"] = "forged"
    elif mutation == "original_args":
        result["visual_resolution"]["original_args"] = {}
    elif mutation == "canonical_action":
        # private_receipt intentionally shares the resolved action in memory.
        # Detach it so this tests disagreement, not a coherent change to both.
        result["visual_resolution"]["action"] = copy.deepcopy(result["visual_resolution"]["action"])
        result["visual_resolution"]["action"]["skill"] = {"Sword": "CrescentSlash"}
    elif mutation == "public_cmd":
        public["cmd"] = "observe"
    elif mutation == "public_id":
        public["id"] = "other"
    elif mutation.startswith("public_"):
        field, value = {
            "public_action_type": ("type", "unlock_skill"),
            "public_action_id": ("action_id", "other"),
            "public_frame": ("frame_id", "stale"),
            "public_ui": ("ui_state_id", "stale"),
            "public_session": ("expected_session_id", "other"),
        }.get(mutation, (None, None))
        if field:
            action[field] = value
        else:
            field = mutation.removeprefix("public_").removeprefix("active_")
            action["actuator"][field if field != "action" else "active_action_id"] = {}
    else:
        if mutation.startswith("missing_"):
            field = mutation.removeprefix("missing_")
            if field in ("control", "actor", "active_action"):
                action["actuator"].pop("active_action_id" if field == "active_action" else field)
            elif field == "generation":
                action["actuator"]["control"].pop("generation")
            else:
                action.pop({"frame": "frame_id", "ui": "ui_state_id",
                            "session": "expected_session_id"}.get(field, field))
        elif mutation in ("coherent_wrong_actor", "coherent_wrong_lifecycle"):
            action["actuator"]["actor"] = {"uid": 99, "lifecycle": "other"}
            if mutation == "coherent_wrong_lifecycle":
                action["actuator"]["actor"]["uid"] = opening["self_identity"]["uid"]
        elif mutation == "coherent_wrong_session":
            action["expected_session_id"] = "other"
        elif mutation == "coherent_frame_mismatch":
            action["ui_state_id"] = "stale"
        elif mutation == "coherent_empty_frame":
            action["frame_id"] = action["ui_state_id"] = ""
        elif mutation == "coherent_bad_control":
            action["actuator"]["control"]["generation"] = "zero"
        elif mutation == "coherent_busy":
            action["actuator"]["active_action_id"] = "running"
        elif mutation == "coherent_hidden_selector":
            action["skill"] = {"Sword": "CrescentSlash"}
        elif mutation == "coherent_bad_gesture":
            action["gesture"] = "teleport"
        else:
            raise AssertionError(mutation)
        result["visual_resolution"]["original_args"] = copy.deepcopy(public["args"])
    assert combat_support.intent_receipts(opening, [row]) == [], mutation
    # Image-origin evidence must not fall through a supplied structured fence.
    row["request"]["args"]["control_fence"] = {
        "expected_session_id": opening["session_id"], "actor": opening["control_actor"]}
    assert combat_support.intent_receipts(opening, [row]) == [], mutation


@pytest.mark.parametrize("mutation", [
    "cost", "points", "level", "learn_confirmed", "config_confirmed", "ability",
    "slot", "target", "queue", "final_skill", "invocation",
])
def test_image_intents_do_not_bypass_learning_configuration_or_native_effect_predicates(mutation):
    values = image_build()
    learn, config = values[2][1:3]
    if mutation == "cost":
        learn["response"]["result"]["detail"]["expected_cost"] = 0
    elif mutation == "points":
        learn["response"]["result"]["detail"]["after_points"] = 4
    elif mutation == "level":
        learn["response"]["result"]["detail"]["after_level"] = 0
    elif mutation == "learn_confirmed":
        learn["response"]["result"]["detail"]["skill_unlock_confirmed"] = False
    elif mutation == "config_confirmed":
        config["response"]["result"]["detail"]["ability_configuration_confirmed"] = False
    elif mutation == "ability":
        config["response"]["result"]["detail"]["observed"] = {"MainWeapon": 0}
    elif mutation == "slot":
        config["request"]["args"]["action"]["slot"] = 1
        config["response"]["result"]["visual_resolution"]["action"]["slot"] = 1
    elif mutation == "target":
        values[3][2]["data"]["target_uid"] = 999
    elif mutation == "queue":
        values[3].pop(1)
    elif mutation == "final_skill":
        values[1]["skill_tree"]["groups"].clear()
    elif mutation == "invocation":
        values[2].pop()
    result = score(values)
    assert not result["parts"]["configured_learned_effect"]
    assert not result["success"]


def test_completed_image_build_with_live_enemy_does_not_synthesize_success():
    values = image_build()
    values[3][:] = [row for row in values[3] if row["event"] != "native_entity_death"]
    result = score(values)
    assert result["parts"]["configured_learned_effect"]
    assert not result["parts"]["encounter_resolved"]
    assert not result["success"]


def test_unfenced_semantic_steps_and_resolution_marker_alone_are_not_image_authorization():
    values = positive("T07")
    row = values[2][1]
    row["request"]["args"].pop("control_fence")
    assert combat_support.intent_receipts(values[0]["opening"], [row]) == []
    row["resolution_source"] = "actual native HUD hit-test; evaluator-private only"
    assert combat_support.intent_receipts(values[0]["opening"], [row]) == []


def test_native_stale_visual_refusal_cannot_become_a_completed_intent():
    values = image_build()
    row = values[2][1]
    row["response"].update(ok=False, error={"code": "stale_visual_frame"})
    row["response"]["result"]["status"] = "failed"
    assert combat_support.intent_receipts(values[0]["opening"], [row]) == []
