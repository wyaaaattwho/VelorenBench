from __future__ import annotations

from scripts.rpg_benchmark import scene
from scripts.rpg_benchmark import setup as native
from scripts.rpg_benchmark.oracles import pickup_actions

HIDE = "common.items.crafting_ing.hide.animal_hide"
LEATHER = "common.items.crafting_ing.leather.simple_leather"
CHEST = "common.items.armor.hide.rawhide.chest"
DISTRACTORS = {"common.items.food.apple": 1, "common.items.crafting_ing.hide.tough_hide": 1}
DEFAULT_RECIPE = {"key": scene.RECIPE, "output": scene.OUTPUT, "inputs": scene.INPUTS,
                  "missing_item": "common.items.food.mushroom"}


def discovery_evidence(opening: dict, query: dict, spec: dict) -> dict:
    matches = [row for row in query.get("items", []) if row.get("recipe") == spec["key"]]
    if len(matches) != 1 or matches[0].get("character_known") is not False:
        raise RuntimeError("unlock condition requires a real native character-unknown query row")
    if any(row.get("recipe") == spec["key"] for row in opening["known_recipes"]["items"]):
        raise RuntimeError("unlock condition already exposes the learned recipe")
    if matches[0].get("satisfied") is True:
        raise RuntimeError("unlearned recipe must not be presented as executable")
    return {"query": query, "recipe_row": matches[0],
            "condition": "native unlearned recipe; public read-only future-option query plus supplied recipe-group item",
            "model_ignorance_claim": False, "unlock_is_required_before_crafting": True}


async def setup(admin, actor, definition: dict, terrain: str) -> dict:
    case = definition["id"]
    spec = definition.get("recipe_spec", DEFAULT_RECIPE)
    if case == "C1" and terrain == "natural":
        fixture = await scene.setup(admin, actor)
        fixture.update(task=scene.TASK, case=case, placement={
            "start": fixture["opening"]["position"], "constructed": False,
            "condition": "unchanged E228 native spawn/ingredients/food-group fixture"})
        return fixture
    initialization = await native.isolate_initial_spawn(admin, actor)
    cleanup = None if case == "C1" else await native.clean_inventory(admin, actor)
    placement = await native.site(admin, actor, terrain, definition["placements"])
    if case == "C1":
        fixture = await scene.setup(admin, actor)
        fixture.update(placement=placement, task=scene.TASK, case=case, initialization=initialization)
        return fixture
    before = await actor.request("observe")
    if initialization is not None:
        native.require_uninjured_start(before)
    if any(scene.counts(before).get(item, 0) for item in (scene.OUTPUT, LEATHER, CHEST)):
        raise RuntimeError("craft fixture contains pre-created product/intermediate")
    supplies, stations = [], []
    group_use = None
    if case == "C4":
        await native.gift(admin, actor, HIDE, 5)
        for dx, sprite in ((5, "TanningRack"), (13, "CraftingBench")):
            point = [placement["start"][0] + dx, placement["start"][1], placement["start"][2]]
            stations.append(await native.station(admin, point, sprite, terrain))
    else:
        if case == "C3U":
            await native.gift(admin, actor, scene.GROUP, 1)
        else:
            group_use = await native.learn(admin, actor, scene.GROUP)
        materials = dict(spec["inputs"])
        if case == "C2":
            materials[spec["missing_item"]] -= 1
        for name, amount in materials.items():
            if amount == 0:
                continue
            await native.gift(admin, actor, name, amount)
        if case == "C2":
            for dx, dy, name in (
                (*definition.get("supply_offset", [14, 5]), spec["missing_item"]),
                (10, -4, "common.items.food.apple"),
                (16, -5, "common.items.crafting_ing.hide.tough_hide"),
            ):
                if name == spec["missing_item"] and (dx, dy) == (10, -4):
                    continue
                point = [placement["start"][0] + dx, placement["start"][1] + dy, placement["start"][2]]
                supplies.append(await native.drop_supply(
                    admin, point, name, 1, terrain, required=name == spec["missing_item"]))
    await native.goto(admin, actor, placement["start"])
    opening = await actor.request("observe")
    if initialization is not None:
        native.require_uninjured_start(opening)
    target = CHEST if case == "C4" else spec["output"]
    recipes = await actor.request("goal_recipes", {"output_item_id": target, "offset": 0, "page_size": 8})
    if target not in str(recipes):
        raise RuntimeError(f"goal recipe unavailable for {target}: {recipes}")
    discovery = discovery_evidence(opening, recipes, spec) if case == "C3U" else None
    return {"case": case, "before": before, "opening": opening, "placement": placement,
            "initialization": initialization,
            "recipe_spec": spec, "discovery": discovery,
            "cleanup_supplies": cleanup, "supplies": supplies, "stations": stations,
            "recipe_group_use": group_use, "goal_recipe_setup_evidence": recipes,
            "native_player_privilege": "not-admin", "task": definition["task"]}


def transactions(fixture: dict, receipts: list[dict]) -> list[dict]:
    opening = fixture["opening"]
    selected = []
    for row in receipts:
        action = row["request"].get("args", {}).get("action", {})
        response = row["response"]
        receipt = response.get("result") or {}
        detail = receipt.get("detail") or {}
        if (action.get("type") == "craft" and response.get("ok") is True
                and receipt.get("status") == "completed"
                and receipt.get("action_id") == action.get("action_id")
                and receipt.get("action_type") == "craft"
                and response.get("session_id") == opening["session_id"]
                and row["actor"] == opening["self_identity"]["uid"]
                and detail.get("craft_update_observed") is True):
            selected.append({"recipe": action.get("recipe"), "action_id": action["action_id"],
                             "detail": detail})
    return selected


def sum_delta(rows: list[dict], item: str) -> int:
    return sum(row["detail"].get("inventory_delta", {}).get(item, 0) for row in rows)


def unlock_actions(opening: dict, receipts: list[dict]) -> list[str]:
    result = []
    for row in receipts:
        action = row["request"].get("args", {}).get("action", {})
        response = row["response"]
        receipt = response.get("result") or {}
        detail = receipt.get("detail") or {}
        if (action.get("type") == "use_item" and receipt.get("action_type") == "use_item"
                and response.get("ok") is True and receipt.get("status") == "completed"
                and receipt.get("action_id") == action.get("action_id")
                and row["actor"] == opening["self_identity"]["uid"]
                and response.get("session_id") == opening["session_id"]
                and detail.get("item_definition_id") == scene.GROUP
                and detail.get("definition_count_delta") == -1):
            result.append(action["action_id"])
    return result


def score(fixture: dict, final: dict, receipts: list[dict], events: list[dict],
          samples: list[dict]) -> dict:
    opening, case = fixture["opening"], fixture["case"]
    spec = fixture.get("recipe_spec", DEFAULT_RECIPE)
    if case in ("C1", "C3", "C3U"):
        result = scene.score(opening, final, receipts, recipe=spec["key"],
                             output=spec["output"], inputs=spec["inputs"])
        if case == "C3U":
            unlocks = unlock_actions(opening, receipts)
            result["unlock_action_ids"] = unlocks
            result["unlock_verified"] = bool(unlocks)
            result["success"] = result["success"] and result["unlock_verified"]
        return result
    before, after = scene.counts(opening), scene.counts(final)
    base = scene.score(opening, final, [])
    rows = transactions(fixture, receipts)
    if case == "C4":
        first = [row for row in rows if row["recipe"] == "simple_leather"
                 and row["detail"].get("output_definition_id") == LEATHER]
        last = [row for row in rows if row["recipe"] == "rawhide_chest"
                and row["detail"].get("output_definition_id") == CHEST]
        effects = (sum_delta(first, HIDE) <= -5 and sum_delta(first, LEATHER) >= 5
                   and sum_delta(last, LEATHER) <= -5 and sum_delta(last, CHEST) >= 1)
        retention = (before.get(HIDE) == 5 and before.get(LEATHER, 0) == 0
                     and before.get(CHEST, 0) == 0 and after.get(CHEST, 0) >= 1
                     and after.get(HIDE, 0) == 0 and after.get(LEATHER, 0) == 0)
        pickup_ids = []
    else:
        first = []
        last = [row for row in rows if row["recipe"] == spec["key"]
                and row["detail"].get("output_definition_id") == spec["output"]]
        wanted = next(row for row in fixture["supplies"] if row["item"] == spec["missing_item"])
        pickup_ids = pickup_actions(opening, receipts, wanted["uid"], wanted["item"])
        effects = (sum_delta(last, spec["output"]) >= 1
                   and all(sum_delta(last, item) <= -amount for item, amount in spec["inputs"].items())
                   and bool(pickup_ids))
        retention = before.get(spec["output"], 0) == 0 and after.get(spec["output"], 0) >= 1
    alive = (final.get("survival", {}).get("health") or {}).get("current", 0) > 0
    same_actor = base["same_actor"]
    return {"success": same_actor and alive and not base["dead"] and effects and retention,
            "dead": base["dead"], "same_actor": same_actor, "effects": effects,
            "retention": retention, "transactions": rows, "pickup_action_ids": pickup_ids,
            "intermediate_effects": first, "final_effects": last, "inventory_after": after,
            "distractor_retained": {item: after.get(item, 0) for item in DISTRACTORS},
            "scope": "own native transactions, input/intermediate effects and retained final product"}


async def mechanism(actor, fixture: dict) -> None:
    case = fixture["case"]
    spec = fixture.get("recipe_spec", DEFAULT_RECIPE)
    if case == "C2":
        supply = next(row for row in fixture["supplies"] if row["item"] == spec["missing_item"])
        await native.action(actor, "move_to", origin="mechanism", position=supply["entity"]["position"],
                            tolerance=1, timeout_ticks=1800)
        await native.action(actor, "pickup", origin="mechanism", uid=supply["uid"])
    if case == "C3U":
        before = await actor.request("observe", origin="mechanism")
        query = await actor.request("goal_recipes", {
            "output_item_id": spec["output"], "offset": 0, "page_size": 8}, origin="mechanism")
        discovery_evidence(before, query, spec)
        after = await actor.request("observe", origin="mechanism")
        if scene.counts(before) != scene.counts(after) or before["known_recipes"] != after["known_recipes"]:
            raise RuntimeError("read-only recipe query mutated inventory/knowledge")
        book = next(row for row in after["inventory"]["items"] if scene.item_id(row["item"]) == scene.GROUP)
        await native.action(actor, "use_item", origin="mechanism", **native.slot(book))
        await native.wait_observation(actor, lambda row: scene.known_recipe(row) is not None,
                                      "native learned recipe after actual group use")
    if case == "C4":
        for station, recipe, amount in zip(fixture["stations"], ("simple_leather", "rawhide_chest"), (5, 1)):
            await native.action(actor, "move_to", origin="mechanism", position=station["approach"],
                                tolerance=1, timeout_ticks=1800)
            await native.action(actor, "craft", origin="mechanism", recipe=recipe, amount=amount)
    else:
        await native.action(actor, "craft", origin="mechanism", recipe=spec["key"], amount=1)
