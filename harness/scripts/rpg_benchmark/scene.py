from __future__ import annotations

import asyncio
import math
import time

from scripts.rpg_benchmark.native import NativeBridge

RECIPE = "mushroom_stick"
OUTPUT = "common.items.food.mushroom_stick"
INPUTS = {"common.items.crafting_ing.twigs": 1, "common.items.food.mushroom": 2}
GROUP = "common.items.recipes.food"
TASK = (
    "Make and keep one mushroom stick for food (common.items.food.mushroom_stick). "
    "Your character knows the recipe and has sufficient materials. Choose your own "
    "tools and approach using the game's current observations. Keep the product."
)


def item_id(item: dict) -> str | None:
    value = item.get("id")
    return value.get("Simple") if isinstance(value, dict) else value


def counts(observation: dict) -> dict[str, int]:
    inventory = observation.get("inventory")
    if not isinstance(inventory, dict) or not isinstance(inventory.get("items"), list):
        raise ValueError("native inventory unavailable")
    result: dict[str, int] = {}
    for row in inventory["items"]:
        item = row["item"]
        name, amount = item_id(item), item.get("amount")
        if not isinstance(name, str) or type(amount) is not int or amount < 0:
            raise ValueError("malformed native inventory item")
        result[name] = result.get(name, 0) + amount
    return result


def known_recipe(observation: dict) -> dict | None:
    return next((row for row in observation.get("known_recipes", {}).get("items", [])
                 if row.get("recipe") == RECIPE and row.get("satisfied") is True), None)


async def setup(admin: NativeBridge, actor: NativeBridge) -> dict:
    from scripts.rpg_benchmark import setup as native

    async def command(name: str, args: list[str]) -> None:
        await native.action(admin, "server_command", name=name, args=args)
        await asyncio.sleep(.3)
        events = await admin.request("events")
        admin.journal.write(origin="setup", command_events=events)
        if "command_error" in str(events):
            raise RuntimeError(f"native fixture command error: {name}")

    before = await actor.request("observe")
    if counts(before).get(OUTPUT, 0):
        raise RuntimeError("fresh actor already has final product")
    for name, amount in {**INPUTS, GROUP: 1}.items():
        await command("sudo", [actor.name, "give_item", name, str(amount)])
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        observation = await actor.request("observe")
        slots = [row for row in observation["inventory"]["items"] if item_id(row["item"]) == GROUP]
        if slots:
            break
        await asyncio.sleep(.1)
    else:
        raise RuntimeError("native recipe group gift not observed")
    row = slots[0]
    reference = {key: row[key] for key in ("slot", "loadout_idx", "slot_idx") if key in row}
    if "slot" in reference:
        reference = {"slot": reference["slot"]}
    receipt = await native.action(actor, "use_item", basis=observation, **reference)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        observation = await actor.request("observe")
        if known_recipe(observation) and counts(observation).get(GROUP, 0) == 0:
            if any(counts(observation).get(name, 0) < amount for name, amount in INPUTS.items()):
                raise RuntimeError("fixture lacks complete native ingredients")
            return {"before": before, "opening": observation, "recipe_group_use": receipt,
                    "known_recipe": known_recipe(observation), "native_player_privilege": "not-admin"}
        await asyncio.sleep(.1)
    raise RuntimeError("food recipe group use did not establish a fresh satisfied recipe")


def score(opening: dict, final: dict, receipts: list[dict], *,
          recipe: str = RECIPE, output: str = OUTPUT, inputs: dict | None = None) -> dict:
    inputs = INPUTS if inputs is None else inputs
    identity = opening["self_identity"]
    same_actor = final.get("self_identity") == identity and final.get("session_id") == opening["session_id"]
    before, after = counts(opening), counts(final)
    produced = after.get(output, 0) - before.get(output, 0)
    consumed = {name: before.get(name, 0) - after.get(name, 0) for name in inputs}
    attributed = []
    for row in receipts:
        action = row["request"].get("args", {}).get("action", {})
        response = row["response"]
        native_receipt = response.get("result") or {}
        detail = native_receipt.get("detail") or {}
        if (action.get("type") == "craft" and action.get("recipe") == recipe
                and response.get("ok") is True and native_receipt.get("status") == "completed"
                and native_receipt.get("action_id") == action.get("action_id")
                and native_receipt.get("action_type") == "craft"
                and response.get("session_id") == opening["session_id"]
                and row["actor"] == identity.get("uid")
                and detail.get("craft_update_observed") is True
                and detail.get("output_definition_id") == output
                and detail.get("output_definition_delta", 0) >= 1
                and all((detail.get("inventory_delta") or {}).get(name, 0) <= -amount
                        for name, amount in inputs.items())):
            attributed.append(action["action_id"])
    health = final.get("survival", {}).get("health") or final.get("health")
    hp = health.get("current") if isinstance(health, dict) else health
    dead = final.get("survival", {}).get("dead") is True or hp == 0
    alive = type(hp) in (int, float) and math.isfinite(hp) and hp > 0 and not dead
    success = (same_actor and before.get(output, 0) == 0 and produced >= 1
               and all(consumed[name] >= amount for name, amount in inputs.items())
               and bool(attributed) and alive)
    return {"success": success, "dead": dead, "same_actor": same_actor,
            "produced_retained": produced, "consumed": consumed,
            "attributed_action_ids": attributed,
            "scope": "native craft update + correlated own receipt + exact inventory retention"}
