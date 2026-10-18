from __future__ import annotations

import asyncio
import time
from uuid import uuid4

from scripts.rpg_benchmark import setup as native
from scripts.rpg_benchmark.scene import counts, item_id

LEGACY_TEMPLATES = {name: "single_creature" for name in ("B1", "B2", "B3")}
TEMPLATES = {"single_creature": {
    "requires": ["movement", "combat", "inventory"],
    "required": ["asset", "body", "consumables", "loadout_provenance"],
}}


def validate_definition(definition):
    if (not isinstance(definition["body"], dict) or not definition["body"]
            or not isinstance(definition["asset"], str)
            or not definition["asset"].startswith("common.entity.")
            or not isinstance(definition["consumables"], dict)
            or any(type(amount) is not int or amount <= 0
                   for amount in definition["consumables"].values())):
        raise ValueError("single_creature: invalid body/asset/consumable parameters")


def mechanism_verified(score):
    return bool(score["enemy_damage_events"])


async def setup(admin, actor, definition: dict, terrain: str) -> dict:
    initialization = await native.isolate_initial_spawn(admin, actor)
    cleanup = await native.clean_inventory(admin, actor)
    placement = await native.site(admin, actor, terrain, definition["placements"], (28, 24))
    for item, amount in definition["consumables"].items():
        await native.gift(admin, actor, item, amount)
    start = placement["start"]
    spawn = [start[0] + 12, start[1], start[2]]
    if terrain == "natural":
        spawn = await native.settled_point(admin, spawn)
    await native.goto(admin, admin, spawn)
    before = await actor.request("observe")
    existing = {row["uid"] for row in before["nearby_entities"]}
    receipt = await native.command(admin, "make_npc", definition["asset"], 1)
    body = definition["body"]
    observed = await native.wait_observation(actor, lambda value: len([
        row for row in value["nearby_entities"] if row["uid"] not in existing
        and row.get("body_identity") == body]) == 1, "unique exact creature body/UID")
    target = next(row for row in observed["nearby_entities"]
                  if row["uid"] not in existing and row.get("body_identity") == body)
    if target.get("health", {}).get("dead") or target.get("health", {}).get("current", 0) <= 0:
        raise RuntimeError("constructed combat target is not living")
    opening = await actor.request("observe")
    if opening["survival"].get("buffs"):
        raise RuntimeError("combat player has residual setup buffs")
    if item_id(opening["survival"]["equipment"]["active_mainhand"]) != "common.items.weapons.sword.starter":
        raise RuntimeError("combat player weapon differs from fixed starter sword")
    if counts(opening) != definition["consumables"]:
        raise RuntimeError("combat consumable inventory differs from manifest")
    equipment = {key: item_id(item) for key, item in opening["survival"]["equipment"].items() if item}
    if equipment != definition["player_equipment"]:
        raise RuntimeError("combat equipped loadout differs from manifest")
    if any(group["available_points"] != 0 or group["unlockable_now"] for group in opening["skill_tree"]["groups"]):
        raise RuntimeError("combat fixed cleared skill set did not take effect")
    if opening["survival"]["health"]["current"] != 100:
        raise RuntimeError("combat initial health differs from fixed 100 HP")
    if any(item and item["durability"]["lost"] not in (None, 0)
           for item in opening["survival"]["equipment"].values()):
        raise RuntimeError("combat player equipment is not fully repaired")
    return {"case": definition["id"], "opening": opening, "before_spawn": before,
            "initialization": initialization,
            "placement": placement, "cleanup_supplies": cleanup, "target": target,
            "spawn_receipt": receipt, "asset": definition["asset"], "task": definition["task"],
            "autonomous_ai": True, "native_player_privilege": "not-admin",
            "equipment": opening["survival"]["equipment"], "skills": opening["skill_tree"],
            "provenance": {"method": "controlled single /make_npc, before/after unique UID and exact body",
                           "loadout": definition["loadout_provenance"],
                           "runtime_npc_inventory_observed": False,
                           "limitation": "normal native observation does not expose NPC equipment/Agent ECS; loadout is source/asset-derived, not an independent ECS read"},
            "difficulty": "provisional; mechanism effects are not actual-model tier calibration"}


def score(fixture: dict, final: dict, receipts: list[dict], events: list[dict],
          samples: list[dict]) -> dict:
    opening, target = fixture["opening"], fixture["target"]
    actor_uid, target_uid = opening["self_identity"]["uid"], target["uid"]
    own, incoming, target_damage = [], [], []
    for row in events:
        data = row.get("data") or {}
        if (row.get("session_id") != opening["session_id"] or row.get("tick", -1) < opening["tick"]
                or row.get("tick", -1) > final["tick"]
                or data.get("source") != "server_outcome_health_change" or data.get("amount", 0) >= 0
                or not str(data.get("cause", "")).startswith("Attack(")):
            continue
        if data.get("target_uid") == target_uid:
            target_damage.append(data)
            if data.get("contributor_uid") == actor_uid:
                own.append(data)
        if data.get("target_uid") == actor_uid and data.get("contributor_uid") == target_uid:
            incoming.append(data)
    target_samples = [row for observation in [*samples, final]
                      for row in observation.get("nearby_entities", []) if row.get("uid") == target_uid
                      and row.get("control_target") == target.get("control_target")]
    defeated = any(row.get("health", {}).get("dead") is True
                   or row.get("health", {}).get("current") == 0 for row in target_samples)
    health = final.get("survival", {}).get("health") or {}
    dead = health.get("dead") is True or health.get("current") == 0
    same = final.get("self_identity") == opening["self_identity"] and final.get("session_id") == opening["session_id"]
    alive = health.get("current", 0) > 0 and not dead
    own_last = bool(target_damage) and target_damage[-1].get("contributor_uid") == actor_uid
    return {"success": same and alive and defeated and bool(own) and own_last, "dead": dead,
            "target_uid": target_uid, "target_defeat_observed": defeated,
            "target_disappearance_is_not_death": True, "own_damage_events": own,
            "own_last_observed_damaging_contributor": own_last,
            "death_attribution_limit": "last observed damaging contributor plus native observed death; native outcomes lack an explicit death transaction ID",
            "enemy_damage_events": incoming, "damage_dealt": -sum(row["amount"] for row in own),
            "damage_received": -sum(row["amount"] for row in incoming), "health_remaining": health,
            "target_samples": target_samples, "same_actor": same,
            "scope": "exact UID/lifecycle, server contributor outcomes, observed death and player survival"}


async def mechanism(actor, fixture: dict) -> None:
    # Native AI is left autonomous; this calibration does not select attacks for it.
    start = time.monotonic()
    while time.monotonic() - start < 20:
        observation = await actor.request("observe", origin="mechanism")
        own_uid = observation["self_identity"]["uid"]
        if any((row.get("data") or {}).get("target_uid") == own_uid
               and (row.get("data") or {}).get("contributor_uid") == fixture["target"]["uid"]
               and (row.get("data") or {}).get("amount", 0) < 0 for row in actor.native_events):
            return
        if time.monotonic() - start > 3:
            target = next((row for row in observation["nearby_entities"]
                           if row["uid"] == fixture["target"]["uid"]), None)
            if target:
                basis = observation
                receipt = await actor.request("step", {"timeout_ticks": 120, "control_fence": {
                    "generation": basis["continuous_control"]["generation"],
                    "expected_session_id": basis["session_id"], "actor": basis["control_actor"]},
                    "action": {"type": "move_to", "position": target["position"], "tolerance": 3,
                               "action_id": uuid4().hex}}, origin="mechanism")
                actor.journal.write(origin="mechanism", diagnostic_approach_receipt=receipt)
        await asyncio.sleep(.25)
    raise RuntimeError("native autonomous encounter did not demonstrate damage within diagnostic window")
