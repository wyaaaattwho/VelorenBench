import copy
import asyncio
import json
from pathlib import Path

import pytest

from scripts.rpg_benchmark import combat_support, crafting, crafting_recovery as family, scene, setup, suite
from scripts.rpg_benchmark.renderer_support import admit_selection, support_matrix
from tests.test_rpg_scenes import craft_receipt


DEFINITIONS = suite.load(Path(suite.__file__).with_name("scenes.json"))


def observed(items, *, hp=100, chest=None, known=False, tick=10):
    return {"session_id": "s", "self_identity": {"uid": 42}, "tick": tick,
            "survival": {"health": {"current": hp, "maximum": 100}, "buffs": [],
                         "equipment": {"chest": {"id": chest} if chest else None}},
            "nearby_entities": [],
            "inventory": {"items": [{"item": {"id": name, "amount": amount}}
                                    for name, amount in items.items() if amount]},
            "known_recipes": {"items": [{"recipe": scene.RECIPE, "character_known": True}]
                              if known else []}}


def action_receipt(kind, detail, *, action_id="u", **args):
    row = craft_receipt("", "", {}, action_id)
    row["request"]["args"]["action"] = {"type": kind, "action_id": action_id, **args}
    row["response"]["result"].update(action_type=kind, detail=detail)
    return row


def fixture(case):
    definition = DEFINITIONS[case]
    opening = observed(definition["initial_items"], hp=35 if case == "T05" else 100,
                       chest=definition.get("old_shirt"))
    result = {"case": case, "definition": definition, "opening": opening, "supplies": []}
    if case == "T03":
        result["supplies"] = [{"uid": 8, "item": family.MUSHROOM, "amount": 2, "required": True}]
    return result


def positive(case):
    fix = fixture(case)
    if case == "T03":
        final = observed({**DEFINITIONS[case]["reserves"], scene.OUTPUT: 1}, known=True, tick=20)
        receipts = [
            action_receipt("use_item", {"item_definition_id": scene.GROUP, "definition_count_delta": -1}),
            action_receipt("pickup", {
                "uid": 8, "operational_success": True, "pickup_feedback": {
                    "server_event": "Collected", "operational_collection_justified": True,
                    "candidate_request_count": 1, "collected_item": {"id": family.MUSHROOM, "amount": 2}}},
                action_id="p", uid=8),
            craft_receipt(scene.RECIPE, scene.OUTPUT,
                          {scene.OUTPUT: 1, **{name: -amount for name, amount in scene.INPUTS.items()}}, "c")]
    elif case == "T04":
        final = observed({DEFINITIONS[case]["old_shirt"]: 1}, chest=crafting.CHEST, tick=20)
        receipts = [
            craft_receipt("simple_leather", crafting.LEATHER, {crafting.HIDE: -5, crafting.LEATHER: 5}, "l"),
            craft_receipt("rawhide_chest", crafting.CHEST, {crafting.LEATHER: -5, crafting.CHEST: 1}, "c"),
            action_receipt("equip", {"equip_slot": "Armor(Chest)", "item_id": crafting.CHEST},
                           action_id="e", equip_slot="chest", slot=0)]
    else:
        item = DEFINITIONS[case]["recovery"]["consumable"]
        final = observed({item: 2, "common.items.crafting_ing.empty_vial": 1}, hp=85, tick=20)
        receipts = [action_receipt("use_item", {"item_definition_id": item, "consumable": True,
                                              "definition_count_delta": -1})]
    return fix, final, receipts


def evaluate(fix, final, receipts, samples=()):
    return family.score(fix, final, receipts, [], list(samples))


def test_exact_additions_and_preserved_catalog_semantics():
    old = json.loads(Path(__file__).with_name("fixtures").joinpath("legacy-scenes.json").read_text())
    actual = json.loads(Path(suite.__file__).with_name("scenes.json").read_text())
    entries = {row["id"]: row for row in actual["scenes"]}
    assert len(old["scenes"]) == 17 and len(entries) == 29
    assert set(entries) - {row["id"] for row in old["scenes"]} == {f"T{i:02}" for i in range(1, 13)}
    for entry in old["scenes"]:
        assert entries[entry["id"]] == entry
    for key in ("placements", "player_equipment", "seeds", "schema"):
        assert actual[key] == old[key]


@pytest.mark.parametrize("case", sorted(family.TASK_IDS))
def test_real_effect_contract_positive_and_wrong_actor_missing_unknown_receipts(case):
    fix, final, receipts = positive(case)
    assert evaluate(fix, final, receipts)["success"]
    assert not evaluate(fix, final, [])["success"]
    for index in range(len(receipts)):
        for mutation in ("actor", "session", "id", "unknown", "tool_only"):
            bad = copy.deepcopy(receipts)
            row = bad[index]
            if mutation == "actor":
                row["actor"] = 43
            elif mutation == "session":
                row["response"]["session_id"] = "other"
            elif mutation == "id":
                row["response"]["result"]["action_id"] = "other"
            elif mutation == "unknown":
                row["response"]["result"]["status"] = "unknown"
            else:
                row["response"]["result"]["detail"] = {"submitted_to_server": True}
            assert not evaluate(fix, final, bad)["success"], (case, index, mutation)
    other = copy.deepcopy(final)
    other["self_identity"]["uid"] = 43
    assert not evaluate(fix, other, receipts)["success"]
    other = copy.deepcopy(final)
    other["survival"]["health"]["current"] = 0
    assert not evaluate(fix, other, receipts)["success"]


def test_t03_requires_true_knowledge_acquisition_product_and_each_reserve():
    fix, final, receipts = positive("T03")
    bad = copy.deepcopy(fix)
    bad["opening"]["known_recipes"] = final["known_recipes"]
    assert not evaluate(bad, final, receipts)["success"]
    bad = copy.deepcopy(final)
    bad["known_recipes"]["items"] = []
    assert not evaluate(fix, bad, receipts)["success"]
    for item in (scene.OUTPUT, *DEFINITIONS["T03"]["reserves"]):
        bad = copy.deepcopy(final)
        bad["inventory"]["items"] = [row for row in bad["inventory"]["items"] if row["item"]["id"] != item]
        assert not evaluate(fix, bad, receipts)["success"]
    bad = copy.deepcopy(receipts)
    bad[1]["response"]["result"]["detail"]["pickup_feedback"]["collected_item"]["amount"] = 1
    assert not evaluate(fix, final, bad)["success"]
    bad = copy.deepcopy(receipts)
    bad[1]["request"]["args"]["action"]["uid"] = 9
    assert not evaluate(fix, final, bad)["success"]


def test_t04_counts_equipped_not_lost_and_requires_old_shirt_and_true_equip():
    fix, final, receipts = positive("T04")
    assert crafting.CHEST not in scene.counts(final)
    assert evaluate(fix, final, receipts)["success"]
    for name, value in (("chest", DEFINITIONS["T04"]["old_shirt"]), ("chest", None)):
        bad = copy.deepcopy(final)
        bad["survival"]["equipment"][name] = {"id": value} if value else None
        assert not evaluate(fix, bad, receipts)["success"]
    bad = copy.deepcopy(final)
    bad["inventory"]["items"] = []
    assert not evaluate(fix, bad, receipts)["success"]
    old_fixture = {"case": "C4", "opening": fix["opening"]}
    assert not crafting.score(old_fixture, final, receipts, [], [])["success"]


def test_t05_no_free_healing_hidden_threshold_or_returned_container_failure():
    fix, final, receipts = positive("T05")
    result = evaluate(fix, final, receipts)
    assert result["success"] and result["returned_items"] == {"common.items.crafting_ing.empty_vial": 1}
    assert not evaluate(fix, final, [])["parts"]["consumed"]
    for hp in (35, 79.99, float("nan")):
        bad = copy.deepcopy(final)
        bad["survival"]["health"]["current"] = hp
        assert not evaluate(fix, bad, receipts)["success"]
    threshold = copy.deepcopy(final)
    threshold["survival"]["health"]["current"] = 80
    assert evaluate(fix, threshold, receipts)["success"]
    bad = copy.deepcopy(final)
    bad["inventory"]["items"][0]["item"]["amount"] = 1
    assert not evaluate(fix, bad, receipts)["success"]
    bad = copy.deepcopy(final)
    bad["inventory"]["items"][0]["item"]["amount"] = 3
    assert not evaluate(fix, bad, receipts)["success"]
    dead_sample = copy.deepcopy(final)
    dead_sample["survival"]["health"]["current"] = 0
    assert not evaluate(fix, final, receipts, [dead_sample])["success"]
    delayed = observed(DEFINITIONS["T05"]["initial_items"], hp=35, tick=12)
    assert evaluate(fix, final, receipts, [delayed])["success"]
    setup.require_uninjured_start(observed({}, hp=100))
    with pytest.raises(RuntimeError, match="100 HP"):
        setup.require_uninjured_start(fix["opening"])
    family.require_injured_start(fix["opening"], DEFINITIONS["T05"]["recovery"])
    unknown = copy.deepcopy(final)
    unknown["survival"]["health"] = {}
    result = evaluate(fix, unknown, receipts)
    assert not result["success"] and not result["dead"]


def test_t04_duplicate_receipt_cannot_multiply_partial_craft_effect():
    fix, final, receipts = positive("T04")
    first = receipts[0]
    first["response"]["result"]["detail"].update(
        inventory_delta={crafting.HIDE: -1, crafting.LEATHER: 1}, output_definition_delta=1)
    assert not evaluate(fix, final, [first] * 5 + receipts[1:])["success"]


def test_post_terminal_cancellation_during_native_close_is_deferred_and_recorded():
    from types import SimpleNamespace
    from unittest.mock import Mock
    from scripts.rpg_benchmark.responses_entry import close_native

    async def check():
        started, finish = asyncio.Event(), asyncio.Event()
        async def close():
            started.set()
            await finish.wait()
        entry, evidence = {}, Mock()
        task = asyncio.create_task(close_native(SimpleNamespace(close=close), entry, evidence))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        finish.set()
        await task
        assert entry["cleanup_interruptions"][0]["type"] == "CancelledError"
        evidence.write.assert_called_once()
    asyncio.run(check())


def test_native_close_error_is_not_swallowed_by_cancellation_shield():
    from types import SimpleNamespace
    from unittest.mock import Mock
    from scripts.rpg_benchmark.responses_entry import close_native
    async def close():
        raise RuntimeError("concrete close failure")
    with pytest.raises(RuntimeError, match="concrete close failure"):
        asyncio.run(close_native(SimpleNamespace(close=close), {}, Mock()))


@pytest.mark.parametrize("case", sorted(family.TASK_IDS))
def test_public_goals_and_explicit_modes(case):
    from scripts.rpg_benchmark.renderer_support import historical_policy
    matrix = historical_policy()["renderer"][f"{case}/natural"]
    assert matrix["structured"]["scored_evaluation"] and matrix["hybrid"]["scored_evaluation"]
    if case == "T05":
        assert matrix["image"]["scored_evaluation"]
        assert matrix["image"]["action_profile"] == "visual-ui-v1"
        assert matrix["image"]["allowed_run_modes"] == ["task-engineering"]
        assert matrix["image"]["accepted"] is False
    else:
        assert not matrix["image"]["scored_evaluation"]
    assert DEFINITIONS[case]["task"]
    current = support_matrix()["renderer"][f"{case}/natural"]
    for mode in ("structured", "hybrid", "image"):
        assert not current[mode]["scored_evaluation"] and current[mode]["qualification_record"] is None
    if case == "T05":
        assert current["image"]["allowed_run_modes"] == ["task-engineering"]
    from types import SimpleNamespace
    args = SimpleNamespace(scene=case, terrain="natural", observation_mode="image",
                           mode="mechanism", harness="responses", recordings=None)
    with pytest.raises(ValueError, match="unsupported renderer"):
        admit_selection(args, {"renderer": {}, "renderer_fixture_helper": {}})
