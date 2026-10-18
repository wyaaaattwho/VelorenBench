"""Additive food/reserve, armor/replacement and injured recovery tasks."""
from __future__ import annotations

LEGACY_TEMPLATES = {"T03": "food_reserve", "T04": "craft_equip", "T05": "recover_health"}
TEMPLATES = {
    "food_reserve": {"legacy_case": "T03", "requires": ["movement", "inventory", "pickup", "crafting"]},
    "craft_equip": {"legacy_case": "T04", "requires": ["movement", "inventory", "crafting", "equipment"]},
    "recover_health": {"legacy_case": "T05", "requires": ["inventory"],
                       "run_modes_by_observation_mode": {"image": ["task-engineering"]}},
}

import asyncio
import math
import time

from . import crafting, scene, setup as native
from .oracles import pickup_actions

TASK_IDS = frozenset({"T03", "T04", "T05"})
TWIG = "common.items.crafting_ing.twigs"
MUSHROOM = "common.items.food.mushroom"
APPLE = "common.items.food.apple"


def recipe_known(observation: dict, recipe: str) -> bool:
    return any(row.get("recipe") == recipe and row.get("character_known", True) is True
               for row in observation.get("known_recipes", {}).get("items", []))


def equipped(observation: dict, slot: str) -> str | None:
    item = observation.get("survival", {}).get("equipment", {}).get(slot)
    return scene.item_id(item) if isinstance(item, dict) else None


def health(observation: dict) -> dict:
    return observation.get("survival", {}).get("health") or {}


def living(observation: dict) -> bool:
    hp = health(observation).get("current")
    return (type(hp) in (int, float) and math.isfinite(hp) and hp > 0
            and observation.get("survival", {}).get("dead") is not True
            and health(observation).get("dead") is not True)


def died(observation: dict) -> bool:
    hp = health(observation).get("current")
    return (observation.get("survival", {}).get("dead") is True
            or health(observation).get("dead") is True
            or type(hp) in (int, float) and math.isfinite(hp) and hp <= 0)


def require_injured_start(observation: dict, contract: dict) -> None:
    native.require_living(observation)
    hp = health(observation)
    if (hp.get("current") != contract["initial_hp"]
            or hp.get("maximum") != contract["maximum_hp"]
            or observation["survival"]["buffs"]):
        raise RuntimeError("recovery opening requires its exact injured HP/max and no residual buffs")
    if any((row.get("health") or {}).get("current", 0) > 0
           and row.get("alias") is None for row in observation["nearby_entities"]):
        raise RuntimeError("recovery opening retains an uncontrolled living NPC")


async def setup(admin, actor, definition: dict, terrain: str) -> dict:
    case = definition["id"]
    initialization = await native.isolate_initial_spawn(admin, actor)
    placement = await native.site(admin, actor, terrain, definition["placements"])
    cleanup = await native.clean_inventory(admin, actor)
    opening_site = await native.ready_observation(actor)
    native.require_uninjured_start(opening_site)
    start = list(opening_site["position"])
    placement["condition"] = "private matching-source natural site; not historical-world equivalence"
    for name, amount in definition["initial_items"].items():
        await native.gift(admin, actor, name, amount)
    supplies, stations = [], []
    if case == "T03":
        supply = definition["supply"]
        dx, dy = supply["offset"]
        supplies.append(await native.drop_supply(
            admin, [start[0] + dx, start[1] + dy, start[2]], supply["item"],
            supply["amount"], terrain))
        supplies.append(await native.drop_supply(
            admin, [start[0] + 7, start[1] - 4, start[2]],
            "common.items.crafting_ing.hide.tough_hide", 1, terrain, required=False))
    elif case == "T04":
        for dx, dy, sprite in ((5, 0, "TanningRack"), (0, 10, "CraftingBench")):
            stations.append(await native.station(
                admin, [start[0] + dx, start[1] + dy, start[2]], sprite, terrain))
    await native.goto(admin, actor, start)
    opening = await actor.request("observe")
    native.require_uninjured_start(opening)
    if scene.counts(opening) != definition["initial_items"]:
        raise RuntimeError("family inventory differs from the declared partial/raw/recovery stock")
    if opening["inventory"]["free_slots"] < 3:
        raise RuntimeError("family fixture lacks craft/replacement/container inventory capacity")
    fixture = {"case": case, "opening": opening, "before": opening_site,
               "initialization": initialization, "cleanup_supplies": cleanup,
               "definition": definition, "placement": placement, "supplies": supplies,
               "stations": stations, "task": definition["task"],
               "native_player_privilege": "not-admin"}
    if case == "T03":
        query = await actor.request("goal_recipes", {
            "output_item_id": scene.OUTPUT, "offset": 0, "page_size": 8})
        fixture["discovery"] = crafting.discovery_evidence(opening, query, crafting.DEFAULT_RECIPE)
    elif case == "T04":
        if equipped(opening, "chest") != definition["old_shirt"]:
            raise RuntimeError("family armor fixture lacks the declared equipped old shirt")
        for recipe, output in (("simple_leather", crafting.LEATHER), ("rawhide_chest", crafting.CHEST)):
            query = await actor.request("goal_recipes", {
                "output_item_id": output, "offset": 0, "page_size": 8})
            if not any(row.get("recipe") == recipe and row.get("character_known") is True
                       for row in query.get("items", [])):
                raise RuntimeError(f"family armor recipe is not natively known: {recipe}")
    elif case == "T05":
        contract = definition["recovery"]
        fixture["injury_receipt"] = await native.command(
            admin, "sudo", actor.name, "health", contract["initial_hp"])
        opening = await native.wait_observation(
            actor, lambda row: (health(row).get("current") == contract["initial_hp"]
                                and row["survival"]["character_state"] in {"Idle", "Wielding"}
                                and (row["continuous_control"].get("native_protection") or {}).get(
                                    "status") == "settled"),
            "declared injured opening with completed native injury/equipping transition")
        require_injured_start(opening, contract)
        fixture.update(opening=opening, recovery_contract=contract)
    else:
        raise ValueError("unknown family task")
    return fixture


def own_actions(opening: dict, receipts: list[dict], action_type: str) -> list[dict]:
    selected = []
    for row in receipts:
        action = row["request"].get("args", {}).get("action", {})
        response = row["response"]
        receipt = response.get("result") or {}
        if (action.get("type") == action_type and response.get("ok") is True
                and row.get("actor") == opening["self_identity"]["uid"]
                and response.get("session_id") == opening["session_id"]
                and receipt.get("status") == "completed"
                and receipt.get("action_type") == action_type
                and isinstance(action.get("action_id"), str)
                and receipt.get("action_id") == action["action_id"]):
            selected.append({"action": action, "detail": receipt.get("detail") or {}})
    return selected


def score(fixture: dict, final: dict, receipts: list[dict], events: list[dict],
          samples: list[dict]) -> dict:
    opening, case, definition = fixture["opening"], fixture["case"], fixture["definition"]
    before, after = scene.counts(opening), scene.counts(final)
    same_actor = (opening["session_id"] == final.get("session_id")
                  and opening["self_identity"] == final.get("self_identity"))
    observations = [opening, *fixture.get("mechanism_recovery_samples", []), *samples, final]
    own_samples = [row for row in observations if row.get("session_id") == opening["session_id"]
                   and row.get("self_identity") == opening["self_identity"]]
    dead = any(died(row) for row in own_samples)
    result = {"same_actor": same_actor, "dead": dead, "inventory_after": after,
              "success": False, "fixture_version": definition["fixture_version"]}
    parts = {}
    rows = list({row["action_id"]: row for row in crafting.transactions(fixture, receipts)}.values())
    if case == "T03":
        unlocks = crafting.unlock_actions(opening, receipts)
        supply = next(row for row in fixture["supplies"] if row["required"])
        pickups = pickup_actions(opening, receipts, supply["uid"], supply["item"], supply["amount"])
        crafts = [row for row in rows if row["recipe"] == scene.RECIPE
                  and row["detail"].get("output_definition_id") == scene.OUTPUT
                  and row["detail"].get("output_definition_delta", 0) >= 1
                  and all(row["detail"].get("inventory_delta", {}).get(item, 0) <= -amount
                          for item, amount in scene.INPUTS.items())]
        parts = {
            "knowledge_unlocked": (not recipe_known(opening, scene.RECIPE)
                                   and recipe_known(final, scene.RECIPE) and bool(unlocks)),
            "missing_ingredients_acquired": bool(pickups),
            "crafted": bool(crafts),
            "product_retained": before.get(scene.OUTPUT, 0) == 0 and after.get(scene.OUTPUT, 0) >= 1,
            "reserves_retained": all(after.get(item, 0) >= amount
                                     for item, amount in definition["reserves"].items())}
        result.update(unlock_action_ids=unlocks, pickup_action_ids=pickups,
                      craft_action_ids=[row["action_id"] for row in crafts],
                      reserve_counts={item: after.get(item, 0) for item in definition["reserves"]})
    elif case == "T04":
        first = [row for row in rows if row["recipe"] == "simple_leather"
                 and row["detail"].get("output_definition_id") == crafting.LEATHER
                 and row["detail"].get("output_definition_delta", 0) > 0]
        last = [row for row in rows if row["recipe"] == "rawhide_chest"
                and row["detail"].get("output_definition_id") == crafting.CHEST
                and row["detail"].get("output_definition_delta", 0) >= 1]
        equips = [row for row in own_actions(opening, receipts, "equip")
                  if row["action"].get("equip_slot") == "chest"
                  and row["detail"].get("equip_slot") == "Armor(Chest)"
                  and row["detail"].get("item_id") == crafting.CHEST]
        parts = {
            "raw_opening": (before.get(crafting.HIDE) == 5 and before.get(crafting.LEATHER, 0) == 0
                            and before.get(crafting.CHEST, 0) == 0
                            and equipped(opening, "chest") == definition["old_shirt"]
                            and before.get(definition["old_shirt"], 0) == 0),
            "intermediate_crafted": (crafting.sum_delta(first, crafting.HIDE) <= -5
                                     and crafting.sum_delta(first, crafting.LEATHER) >= 5),
            "chest_crafted": (crafting.sum_delta(last, crafting.LEATHER) <= -5
                              and crafting.sum_delta(last, crafting.CHEST) >= 1),
            "target_equipped": bool(equips) and equipped(final, "chest") == crafting.CHEST,
            "old_shirt_retained": after.get(definition["old_shirt"], 0) >= 1,
            "raw_chain_consumed": after.get(crafting.HIDE, 0) == 0 and after.get(crafting.LEATHER, 0) == 0}
        result.update(intermediate_effects=first, final_effects=last,
                      equip_action_ids=[row["action"]["action_id"] for row in equips],
                      chest_equipment=equipped(final, "chest"))
    elif case == "T05":
        contract = definition["recovery"]
        item = contract["consumable"]
        uses = [row for row in own_actions(opening, receipts, "use_item")
                if row["detail"].get("item_definition_id") == item
                and row["detail"].get("consumable") is True
                and row["detail"].get("definition_count_delta", 0) <= -1]
        hp, maximum = health(final).get("current"), health(final).get("maximum")
        finite_hp = type(hp) in (int, float) and math.isfinite(hp)
        parts = {
            "injured_opening": (health(opening).get("current") == contract["initial_hp"]
                                and health(opening).get("maximum") == contract["maximum_hp"]
                                and not opening["survival"].get("buffs")),
            "consumed": bool(uses) and after.get(item, 0) < before.get(item, 0),
            "health_increased": finite_hp and hp > contract["initial_hp"],
            "health_threshold": finite_hp and hp >= contract["threshold_hp"]
                                and maximum == contract["maximum_hp"],
            "backup_retained": after.get(item, 0) >= contract["backup_count"]}
        buffs = [{"tick": row.get("tick"), "buffs": row["survival"].get("buffs", [])}
                 for row in own_samples if row["survival"].get("buffs")]
        changes = [row for row in events if row.get("session_id") == opening["session_id"]
                   and opening["tick"] <= row.get("tick", -1) <= final["tick"]
                   and (row.get("data") or {}).get("source") == "server_outcome_health_change"
                   and (row.get("data") or {}).get("target_uid") == opening["self_identity"]["uid"]]
        result.update(consume_action_ids=[row["action"]["action_id"] for row in uses],
                      health_before=health(opening), health_after=health(final),
                      health_samples=[{"tick": row.get("tick"), "health": health(row)} for row in own_samples],
                      buff_observations=buffs, native_health_changes=changes,
                      backup_count=after.get(item, 0),
                      returned_items={name: amount - before.get(name, 0) for name, amount in after.items()
                                      if name != item and amount > before.get(name, 0)},
                      attribution="actual consumption and subsequent health state; no exclusive net-HP attribution")
    else:
        raise ValueError("unknown family task")
    result.update(parts=parts, success=same_actor and not dead and living(final) and all(parts.values()),
                  scope="additive family native action/effect joins and public simultaneous goal predicates")
    return result


async def mechanism(actor, fixture: dict) -> None:
    """Private engineering driver, never registered as a model tool."""
    case = fixture["case"]
    if case == "T03":
        observed = await actor.request("observe", origin="mechanism")
        book = next(row for row in observed["inventory"]["items"] if scene.item_id(row["item"]) == scene.GROUP)
        await native.action(actor, "use_item", origin="mechanism", **native.slot(book))
        await native.wait_observation(actor, lambda row: recipe_known(row, scene.RECIPE),
                                      "native food knowledge transition")
        supply = next(row for row in fixture["supplies"] if row["required"])
        await native.action(actor, "move_to", origin="mechanism",
                            position=supply["entity"]["position"], tolerance=1, timeout_ticks=1800)
        await native.action(actor, "pickup", origin="mechanism", uid=supply["uid"])
        await native.action(actor, "craft", origin="mechanism", recipe=scene.RECIPE, amount=1)
    elif case == "T04":
        for station, recipe, amount in zip(fixture["stations"], ("simple_leather", "rawhide_chest"), (5, 1)):
            await native.action(actor, "move_to", origin="mechanism",
                                position=station["approach"], tolerance=1, timeout_ticks=1800)
            await native.action(actor, "craft", origin="mechanism", recipe=recipe, amount=amount)
        observed = await actor.request("observe", origin="mechanism")
        chest = next(row for row in observed["inventory"]["items"]
                     if scene.item_id(row["item"]) == crafting.CHEST)
        await native.action(actor, "equip", origin="mechanism", equip_slot="chest", **native.slot(chest))
    elif case == "T05":
        contract = fixture["definition"]["recovery"]
        observed = await actor.request("observe", origin="mechanism")
        potion = next(row for row in observed["inventory"]["items"]
                      if scene.item_id(row["item"]) == contract["consumable"])
        await native.action(actor, "use_item", origin="mechanism", **native.slot(potion))
        deadline = time.monotonic() + 15
        recovery_samples = fixture.setdefault("mechanism_recovery_samples", [])
        while time.monotonic() < deadline:
            observed = await actor.request("observe", origin="mechanism")
            recovery_samples.append(observed)
            if health(observed).get("current", 0) >= contract["threshold_hp"]:
                return
            await asyncio.sleep(.1)
        raise RuntimeError("consumed recovery item did not reach the public health threshold")
    else:
        raise ValueError("unknown family task")
