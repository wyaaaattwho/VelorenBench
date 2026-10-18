from __future__ import annotations

LEGACY_TEMPLATES = {"X1": "craft_fight_travel", "X2": "calibrated_craft_fight_travel"}
# The only current cross-task setup composition. Both actual setup and
# qualification resolve these roles through suite.setup_dependencies.
SETUP_REFERENCES = {"craft": ("C2", "crafting"), "encounter": ("B1", "combat")}
TEMPLATES = {
    "craft_fight_travel": {"legacy_case": "X1",
                          "requires": ["movement", "combat", "crafting", "pickup", "coordinate_goal"]},
    "calibrated_craft_fight_travel": {"legacy_case": "X2",
                                     "requires": ["movement", "combat", "crafting", "pickup", "coordinate_goal"]},
}

from scripts.rpg_benchmark import combat, crafting, navigation
from scripts.rpg_benchmark.scene import counts
from scripts.rpg_benchmark import setup as native


async def qualify_food_recipe(admin, spec: dict) -> dict:
    await native.learn(admin, admin, "common.items.recipes.food")
    for item, amount in spec["inputs"].items():
        await native.gift(admin, admin, item, amount)
    before = await admin.request("observe")
    receipt = await native.action(admin, "craft", recipe=spec["key"], amount=1)
    after = await admin.request("observe")
    initial, final = counts(before), counts(after)
    detail = receipt.get("detail") or {}
    if not (detail.get("craft_update_observed") is True
            and detail.get("output_definition_id") == spec["output"]
            and detail.get("output_definition_delta", 0) >= 1
            and initial.get(spec["output"], 0) == 0 and final.get(spec["output"], 0) >= 1
            and all(initial.get(item, 0) - final.get(item, 0) >= amount
                    and detail.get("inventory_delta", {}).get(item, 0) <= -amount
                    for item, amount in spec["inputs"].items())):
        raise RuntimeError("held-out food setup calibration lacks actual input/output/retention effects")
    return {"receipt": receipt, "inventory_before": initial, "inventory_after": final,
            "actor": before["self_identity"], "native_session_id": before["session_id"],
            "capability_claim": False,
            "scope": "administrator-only setup component transaction, not a player decision or compound task solver"}


def resource_adequacy(craft: dict) -> dict:
    spec = craft["recipe_spec"]
    rows = [row for row in craft["goal_recipe_setup_evidence"]["items"]
            if row.get("recipe") == spec["key"]]
    if len(rows) != 1 or rows[0].get("character_known") is not True or rows[0].get("station") is not None:
        raise RuntimeError("mixed food recipe lacks known, station-free native query support")
    inputs = {row["item"]: row["amount"] for row in rows[0]["inputs"] if row["kind"] == "item"}
    if inputs != spec["inputs"]:
        raise RuntimeError("mixed pinned ingredients disagree with actual native recipe")
    available = counts(craft["opening"])
    for supply in craft["supplies"]:
        available[supply["item"]] = available.get(supply["item"], 0) + supply["amount"]
    if any(available.get(item, 0) < amount for item, amount in inputs.items()):
        raise RuntimeError("mixed task has inadequate actual inventory and offered inputs")
    return {"recipe": spec["key"], "required": inputs, "available": available,
            "scope": "native known recipe plus supplied ingredient quantities; not a combat survival guarantee"}


async def setup(admin, actor, definition: dict, terrain: str, dependencies: dict) -> dict:
    craft_definition = {**dependencies["craft"], **definition.get("craft_overrides", {})}
    craft = await crafting.setup(admin, actor, craft_definition, terrain)
    adequate = resource_adequacy(craft)
    recipe_component = await qualify_food_recipe(admin, craft["recipe_spec"]) if definition["id"] == "X2" else None
    encounter = dict(dependencies["encounter"])
    # Compose primitives without cleaning away the crafting supplies.
    placement = craft["placement"]
    from scripts.rpg_benchmark.scene import item_id
    start = placement["start"]
    offset = definition.get("rendezvous_offset", [80, 32])
    rendezvous = await native.settled_point(admin, [start[0] + offset[0], start[1] + offset[1], start[2]])
    supply = await native.drop_supply(admin, rendezvous, navigation.APPLE, 1, terrain)
    staging = await native.settled_point(admin, [start[0] - 4, start[1] + 4, start[2]])
    await native.goto(admin, admin, rendezvous)
    await native.goto(admin, actor, staging)
    route = await navigation.physical_route(admin, start, supply["entity"]["position"],
                                            isolate_ambient=False)
    await native.goto(admin, actor, start)
    await native.goto(admin, admin, await native.settled_point(
        admin, [start[0] + 24, start[1] + 8, start[2]]))
    before = await actor.request("observe")
    existing = {row["uid"] for row in before["nearby_entities"]}
    receipt = await native.command(admin, "make_npc", encounter["asset"], 1)
    observed = await native.wait_observation(actor, lambda row: any(
        entity["uid"] not in existing and entity["body_identity"] == encounter["body"]
        for entity in row["nearby_entities"]), "mixed encounter identity")
    target = next(entity for entity in observed["nearby_entities"]
                  if entity["uid"] not in existing and entity["body_identity"] == encounter["body"])
    opening = await actor.request("observe")
    craft["opening"] = opening
    fight = {"opening": opening, "target": target}
    travel = {"opening": opening, "goal": supply["entity"]["position"], "supplies": [supply]}
    return {"case": definition["id"], "opening": opening, "craft": craft, "fight": fight,
            "initialization": craft["initialization"],
            "travel": travel, "spawn_receipt": receipt, "held_out": True,
            "reachability": route, "held_out_condition": definition.get("held_out_condition"),
            "resource_adequacy": adequate,
            "recipe_component_calibration": recipe_component,
            "task": definition["task"] + " " + navigation.goal_text(travel["goal"], True),
            "equipment": {key: item_id(item) if item else None
                          for key, item in opening["survival"]["equipment"].items()},
            "sequence_policy": "no scripted ordering or strategic controller"}


def score(fixture: dict, final: dict, receipts: list[dict], events: list[dict], samples: list[dict]) -> dict:
    parts = {
        "craft": crafting.score(fixture["craft"], final, receipts, events, samples),
        "fight": combat.score(fixture["fight"], final, receipts, events, samples),
        "travel": navigation.score(fixture["travel"], final, receipts, events, samples),
    }
    return {"success": all(row["success"] for row in parts.values()),
            "dead": any(row["dead"] for row in parts.values()), "parts": parts}
