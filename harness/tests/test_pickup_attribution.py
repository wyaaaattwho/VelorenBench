"""Shared operational pickup attribution, independent of task ID or modality."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts.rpg_benchmark import crafting, navigation, navigation_tasks, oracles, visual_ui
from tests.test_crafting_recovery import observed, positive, evaluate
from tests.test_navigation_tasks import pickup, route_fixture, sample
from tests.test_rpg_scenes import craft_receipt


def receipt(requested_uid):
    row = pickup()
    if requested_uid == "omitted":
        del row["request"]["args"]["action"]["uid"]
    else:
        row["request"]["args"]["action"]["uid"] = requested_uid
    return row


@pytest.mark.parametrize("requested_uid", ["omitted", 8])
def test_exact_same_action_native_collection_accepts_optional_requested_uid(requested_uid):
    assert oracles.pickup_actions(observed({}), [receipt(requested_uid)],
                                  8, navigation_tasks.SUPPLY) == ["pickup"]


@pytest.mark.parametrize("requested_uid", ["omitted", 8])
@pytest.mark.parametrize("path,value", [
    (("actor",), 43),
    (("request", "args", "action", "type"), "collect"),
    (("response", "session_id"), "other"),
    (("response", "ok"), False),
    (("response", "result", "status"), "unknown"),
    (("response", "result", "action_id"), "other-action"),
    (("response", "result", "action_type"), "collect"),
    (("response", "result", "detail", "uid"), 9),
    (("response", "result", "detail", "operational_success"), False),
    (("response", "result", "detail", "pickup_feedback", "server_event"), "CollectFailed"),
    (("response", "result", "detail", "pickup_feedback", "server_event"), None),
    (("response", "result", "detail", "pickup_feedback", "operational_collection_justified"), False),
    (("response", "result", "detail", "pickup_feedback", "candidate_request_count"), 0),
    (("response", "result", "detail", "pickup_feedback", "candidate_request_count"), 2),
    (("response", "result", "detail", "pickup_feedback", "collected_item", "id"), "other-item"),
    (("response", "result", "detail", "pickup_feedback", "collected_item", "amount"), 0),
    (("response", "result", "detail", "pickup_feedback"), {}),
    (("response", "result", "detail"), {"item_count_delta": 1, "target_present_at_finish": False}),
])
def test_optional_uid_does_not_bypass_existing_attribution_predicates(requested_uid, path, value):
    row = receipt(requested_uid)
    target = row
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert oracles.pickup_actions(observed({}), [row], 8, navigation_tasks.SUPPLY) == []


@pytest.mark.parametrize("requested_uid", [9, None])
def test_explicit_wrong_or_null_uid_is_not_omission(requested_uid):
    assert oracles.pickup_actions(observed({}), [receipt(requested_uid)],
                                  8, navigation_tasks.SUPPLY) == []


def test_native_item_and_required_amount_are_still_exact():
    row = receipt("omitted")
    assert not oracles.pickup_actions(observed({}), [row], 8, "other-item")
    assert not oracles.pickup_actions(observed({}), [row], 8, navigation_tasks.SUPPLY, 2)
    assert not oracles.pickup_actions(observed({}), [], 8, navigation_tasks.SUPPLY)
    row["response"]["result"]["detail"]["pickup_feedback"]["collected_item"]["id"] = {
        "Simple": navigation_tasks.SUPPLY}
    assert oracles.pickup_actions(observed({}), [row], 8, navigation_tasks.SUPPLY) == ["pickup"]


@pytest.mark.parametrize("requested_uid", ["omitted", 8])
def test_exact_visual_resolution_uses_the_same_pickup_predicate(requested_uid):
    row = receipt(requested_uid)
    row["request"].update(id="request", cmd="step")
    canonical = copy.deepcopy(row["request"]["args"]["action"])
    public = {"id": "request", "cmd": "step", "args": {"action": {
        "type": "visual_input", "action_id": "pickup", "gesture": "interact"}}}
    response = row["response"]
    response["id"] = "request"
    response["result"]["visual_resolution"] = {
        "source": "native-hud-hit-test-v1", "original_args": copy.deepcopy(public["args"]),
        "action": canonical}
    normalized = visual_ui.private_receipt(public, response, row["actor"], 1)
    assert normalized["public_request"] == public
    assert normalized["request"]["args"]["action"] == canonical
    assert oracles.pickup_actions(observed({}), [normalized], 8, navigation_tasks.SUPPLY) == ["pickup"]
    response["result"]["visual_resolution"]["original_args"]["action"]["action_id"] = "stale"
    with pytest.raises(ValueError, match="exact original request"):
        visual_ui.private_receipt(public, response, row["actor"], 1)


def test_target_validity_uses_resolved_target_not_disappearance_or_inventory():
    opening = observed({})
    final = observed({navigation_tasks.SUPPLY: 1})
    final["survival"]["buff_clock"] = {"now": 20}
    supplies = [{"uid": 8, "item": navigation_tasks.SUPPLY, "amount": 1, "required": True,
                 "expiry_game_time_estimate": 30}]
    result = oracles.target_validity({"opening": opening}, final, [receipt("omitted")], supplies)
    assert result["confirmed_available"] and not result["uncertain"]
    assert result["required_targets"][0]["status"] == "collected_by_actor"
    assert result["required_targets"][0]["pickup_action_ids"] == ["pickup"]
    result = oracles.target_validity({"opening": opening}, final, [], supplies)
    assert result["uncertain"] and not result["confirmed_available"]


def test_navigation_interaction_does_not_grant_arrival():
    opening = sample(1, 0, [0, 0, 1])
    final = sample(2, 1, [0, 0, 1], {navigation.APPLE: 1})
    fixture = {"opening": opening, "goal": [10, 0, 1], "supplies": [{"uid": 8}]}
    result = navigation.score(fixture, final, [receipt("omitted")], [], [opening, final])
    assert result["interaction"] and result["pickup_action_ids"] == ["pickup"]
    assert not result["arrival"] and not result["success"]


def test_crafting_pickup_still_requires_independent_craft_effect_and_product():
    spec = crafting.DEFAULT_RECIPE
    opening = observed({})
    final = observed({spec["output"]: 1})
    fixture = {"case": "C2", "opening": opening, "supplies": [
        {"uid": 8, "item": spec["missing_item"]}]}
    row = receipt("omitted")
    row["response"]["result"]["detail"]["pickup_feedback"]["collected_item"]["id"] = spec["missing_item"]
    craft = craft_receipt(spec["key"], spec["output"],
                          {spec["output"]: 1, **{k: -v for k, v in spec["inputs"].items()}}, "craft")
    result = crafting.score(fixture, final, [row, craft], [], [])
    assert result["success"] and result["pickup_action_ids"] == ["pickup"]
    assert not crafting.score(fixture, final, [row], [], [])["success"]
    assert not crafting.score(fixture, observed({}), [row, craft], [], [])["success"]


def test_recovery_crafting_uses_same_attribution_without_waiving_other_parts():
    fixture, final, receipts = positive("T03")
    del receipts[1]["request"]["args"]["action"]["uid"]
    assert evaluate(fixture, final, receipts)["success"]
    assert not evaluate(fixture, final, receipts[1:])["success"]
    receipts[1]["response"]["result"]["detail"]["uid"] = 9
    assert not evaluate(fixture, final, receipts)["success"]


def test_route_no_uid_collection_triggers_once_and_scores_without_granting_arrival(monkeypatch):
    fixture = route_fixture()
    fixture["event_state"].update(status="armed", applications=0)
    admin = SimpleNamespace(journal=SimpleNamespace(write=lambda **row: None))
    actor = SimpleNamespace(measured_receipts=[receipt("omitted")],
                            request=AsyncMock(return_value=fixture["opening"]))
    batch = AsyncMock(return_value=copy.deepcopy(fixture["event_state"]["submissions"]))
    monkeypatch.setattr(navigation_tasks, "build_batch", batch)
    monkeypatch.setattr(navigation_tasks, "verify_blocks", AsyncMock(
        return_value={"verified_voxels": fixture["layout"]["event_voxels"]}))
    monkeypatch.setattr(navigation_tasks.native, "goto", AsyncMock())
    monkeypatch.setattr(navigation_tasks.native, "qualify", AsyncMock(return_value={"qualified": True}))
    asyncio.run(navigation_tasks.update_event(admin, actor, fixture, fixture["opening"]))
    asyncio.run(navigation_tasks.update_event(admin, actor, fixture, fixture["opening"]))
    assert batch.await_count == 1 and fixture["event_state"]["applications"] == 1
    assert fixture["event_state"]["status"] == "applied"
    # A real event alone is not a task win, nor a public visibility witness.
    final = sample(2, 1, fixture["opening"]["position"], {navigation_tasks.SUPPLY: 1})
    result = navigation_tasks.score(fixture, final, actor.measured_receipts, [], [final])
    assert result["parts"]["picked_up"] and result["parts"]["retained"]
    assert result["parts"]["runtime_event_qualified"]
    assert not result["parts"]["alive_arrival"] and not result["success"]
    assert fixture["event_state"]["time_visible"] is None
