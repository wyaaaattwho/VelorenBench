"""Additive encounter/build/support tasks; legacy combat remains unchanged."""
from __future__ import annotations

LEGACY_TEMPLATES = {"T06": "weapon_condition", "T07": "skill_training",
                    "T08": "ranged_encounter", "T09": "group_support"}
TEMPLATES = {
    "weapon_condition": {"legacy_case": "T06", "requires": ["movement", "combat", "equipment"]},
    "skill_training": {"legacy_case": "T07", "requires": ["movement", "combat", "skills"]},
    "ranged_encounter": {"legacy_case": "T08", "requires": ["movement", "combat", "inventory"]},
    "group_support": {"legacy_case": "T09", "requires": ["movement", "combat", "skills"]},
}

import asyncio
import copy
import time

from . import crafting_recovery as shared, native_outcomes as oracle, scene, setup as native, visual_ui
from .native import NativeBridge

TASK_IDS = frozenset({"T06", "T07", "T08", "T09"})
WEAPONS = {
    "sword": "common.items.weapons.sword.starter",
    "bow": "common.items.weapons.bow.starter",
    "staff": "common.items.weapons.staff.starter_staff",
    "sceptre": "common.items.weapons.sceptre.starter_sceptre",
}
ACTIVE_SKILLS = dict(enumerate((
    "CrescentSlash", "FellStrike", "Skewer", "Cascade", "CrossCut", "Finisher",
    "HeavySweep", "HeavyPommelStrike", "AgileQuickDraw", "AgileFeint",
    "DefensiveRiposte", "DefensiveDisengage", "CripplingGouge", "CripplingHamstring",
    "CleavingWhirlwindSlice", "CleavingEarthSplitter", "HeavyFortitude", "HeavyPillarThrust",
    "AgileDancingEdge", "AgileFlurry", "DefensiveStalwartSword", "DefensiveDeflect",
    "CripplingEviscerate", "CripplingBloodyGash", "CleavingBladeFever", "CleavingSkySplitter",
)))


def rows(fixture):
    return oracle.read(fixture["private_journal"], fixture["native_run_id"])


def cover_contact(entry, cover, owner_uid, cutoff):
    if (entry["sequence"] <= cutoff or entry["event"] != "projectile_solid_contact"
            or entry["data"]["owner_uid"] != owner_uid):
        return False
    point = entry["data"]["position"]
    if not isinstance(point, dict) or set(point) != {"x", "y", "z"}:
        raise ValueError("native projectile contact requires exact Vec3 object")
    return any(all(block["voxel"][i] - .0001 <= point[key] <= block["voxel"][i] + 1.0001
                   for i, key in enumerate(("x", "y", "z"))) for block in cover)


async def native_binding(actor, uid):
    result = None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        journal = oracle.read(actor.out / "private-native-oracle.ndjson", actor.out.name)
        result = oracle.binding(journal, uid)
        if result is not None:
            return result
        await asyncio.sleep(.1)
    raise RuntimeError(f"private native UID/entity binding unavailable: {uid}")


async def equip(admin, actor, item):
    current = await actor.request("observe")
    if shared.equipped(current, "active_mainhand") != item:
        current = await native.gift(admin, actor, item, 1)
        entry = next(row for row in current["inventory"]["items"] if scene.item_id(row["item"]) == item)
        await native.action(actor, "equip", equip_slot="active_mainhand", **native.slot(entry))
    await native.wait_observation(actor, lambda row: shared.equipped(row, "active_mainhand") == item,
                                  "declared native mainhand")


async def spawn(admin, actor, spec, position):
    await native.goto(admin, admin, position)
    before = await actor.request("observe")
    existing = {row["uid"] for row in before["nearby_entities"]}
    receipt = await native.command(admin, "make_npc", spec["asset"], 1)
    after = await native.wait_observation(
        actor, lambda row: len([entity for entity in row["nearby_entities"]
                                if entity["uid"] not in existing and entity.get("health")]) == 1,
        "unique new native encounter UID")
    target = next(row for row in after["nearby_entities"]
                  if row["uid"] not in existing and row.get("health"))
    if target["health"].get("dead") or target["health"]["current"] <= 0:
        raise RuntimeError("encounter target already dead at setup")
    binding = await native_binding(actor, target["uid"])
    if binding["alignment"] != "Enemy":
        raise RuntimeError("encounter target lacks actual native Enemy alignment")
    if "initial_hp" in spec:
        await native.command(admin, "sudo", f"uid@{target['uid']}", "health", spec["initial_hp"])
        after = await native.wait_observation(actor, lambda row: any(
            entity["uid"] == target["uid"] and entity["health"]["current"] == spec["initial_hp"]
            for entity in row["nearby_entities"]), "declared finite initial enemy injury")
        target = next(row for row in after["nearby_entities"] if row["uid"] == target["uid"])
        binding = await native_binding(actor, target["uid"])
    return {"role": spec["role"], "asset": spec["asset"], "public": target, "binding": binding,
            "spawn_receipt": receipt, "stock_loadout": spec["stock_loadout"],
            "native_loadout_observed": True}


def validate_support_opening(fixture, journal):
    source, ally = fixture["actor_binding"], fixture["ally"]
    public = next((row for row in fixture["opening"]["nearby_entities"]
                   if row["uid"] == ally["binding"]["uid"]), None)
    current = oracle.binding(journal, ally["binding"]["uid"])
    combo = [row for row in journal if row["event"] == "combo_effect_applied"
             and row["data"]["uid"] == source["uid"]
             and row["data"]["entity"] == source["server_entity"]]
    if (public is None or public.get("same_native_group") is not True
            or public.get("control_target") != ally["public"].get("control_target")
            or (public.get("health") or {}).get("current") != ally["initial_hp"]
            or current is None or current["server_entity"] != ally["binding"]["server_entity"]
            or source.get("group") is None or current.get("group") != source["group"]
            or current["health"] != ally["initial_hp"] or current["dead"] is not False
            or not combo or combo[-1]["data"]["after"] != 4):
        raise RuntimeError("support opening lacks declared live Group ally injury or exact four finite combo")


async def setup(admin, actor, definition, terrain):
    initialization = await native.isolate_initial_spawn(admin, actor)
    placement = await native.site(admin, actor, terrain, definition["placements"], (32, 28))
    cleanup = await native.clean_inventory(admin, actor)
    start = list((await native.ready_observation(actor))["position"])
    variant = definition.get("selected_variant", definition["default_variant"])
    weapon = WEAPONS[variant]
    await equip(admin, actor, weapon)
    if variant != "sword":
        cleanup["replaced_weapon_inventory"] = await native.clean_inventory(admin, actor)
    for item, count in definition["initial_items"].items():
        await native.gift(admin, actor, item, count)
    fixture = {"case": definition["id"], "definition": copy.deepcopy(definition),
               "variant": variant, "weapon": weapon, "task": definition["task"],
               "initialization": initialization, "placement": placement, "cleanup_supplies": cleanup,
               "private_journal": str(actor.out / "private-native-oracle.ndjson"),
               "native_run_id": actor.out.name, "targets": [], "native_player_privilege": "not-admin",
               "autonomous_enemy_ai": True, "fixture_version": definition["fixture_version"]}
    if definition["id"] == "T07":
        await native.command(admin, "sudo", actor.name, "skill_point", "general", 1)
        await native.action(actor, "unlock_skill", skill={"UnlockGroup": {"Weapon": "Sword"}})
        await native.command(admin, "sudo", actor.name, "skill_point", "sword", 4)
        await native.wait_observation(actor, lambda row: any(
            group["group"] == {"Weapon": "Sword"} and group["available_points"] == 4
            for group in row["skill_tree"]["groups"]), "finite Sword points")
    if definition["id"] == "T09":
        ally = NativeBridge(actor.out, actor.name + "ally", actor.port, actor.userdata,
                            actor.journal, actor.config)
        actor.family_companions.append(ally)
        await ally.start()
        await native.goto(admin, ally, [start[0] - 2, start[1], start[2]])
        ally_observation = await ally.request("observe")
        ally_uid = ally_observation["self_identity"]["uid"]
        await native.action(actor, "social_action", operation="invite_group",
                            target_uid=ally_uid, target_alias=ally.name)
        await native.wait_observation(ally, lambda row: row["social_and_trade"]["incoming_invite"],
                                      "actual Group invitation")
        await native.action(ally, "social_action", operation="respond_accept",
                            inviter_uid=(await actor.request("observe"))["self_identity"]["uid"],
                            invite_kind="Group")
        await native.wait_observation(actor, lambda row: any(
            entity["uid"] == ally_uid and entity.get("same_native_group") is True
            for entity in row["nearby_entities"]), "real native Group member")
        await native.command(admin, "sudo", ally.name, "health", definition["ally_initial_hp"])
        await native.wait_observation(ally, lambda row: row["survival"]["health"]["current"]
                                      == definition["ally_initial_hp"], "finite initial ally injury")
        await native.command(admin, "sudo", actor.name, "buff", "combo_generation", 4, 1)
        await native.wait_observation(actor, lambda row: any(
            entry["event"] == "combo_effect_applied"
            and entry["data"]["uid"] == row["self_identity"]["uid"]
            and entry["data"]["after"] >= 4 for entry in rows(fixture)),
            "finite healing combo setup")
        await native.wait_observation(actor, lambda row: not row["survival"]["buffs"],
                                      "expired setup combo buff before measurement", timeout=10)
        fixture["ally"] = {"public": next(row for row in (await actor.request("observe"))["nearby_entities"]
                                         if row["uid"] == ally_uid),
                          "binding": await native_binding(actor, ally_uid),
                          "initial_hp": definition["ally_initial_hp"],
                          "threshold_hp": definition["ally_threshold_hp"],
                          "initialization": "ordinary non-admin native character; finite private injury; no potion/protection",
                          "combo_fixture": "four native combo from expired finite setup buff; setup health effects excluded"}
    if definition["id"] == "T08":
        voxels = [[int(start[0]) + dx, int(start[1]) + dy, int(start[2]) + dz]
                  for dx, dy, dz in ((5, -1, 0), (5, 0, 0), (5, 1, 0), (5, 0, 1), (4, 2, 0))]
        fixture["cover"] = await native.author_voxels(admin, voxels)
        await equip(admin, admin, WEAPONS["bow"])
        await native.goto(admin, admin, start)
        wall = fixture["cover"][1]["voxel"]
        await native.action(admin, "toggle_wield", wield=True)
        # look_at uses feet as origin; native bow projectiles spawn above them.
        await native.action(admin, "look_at", position=[wall[0] + .5, wall[1] + .5, wall[2] - 1.2])
        cutoff = rows(fixture)[-1]["sequence"]
        await native.action(admin, "attack_primary", duration_ticks=60)
        admin_uid = (await admin.request("observe"))["self_identity"]["uid"]
        def contact():
            return next((entry for entry in rows(fixture)
                         if cover_contact(entry, fixture["cover"], admin_uid, cutoff)), None)
        await native.wait_observation(admin, lambda unused: contact() is not None,
                                      "real native projectile contact on authored cover", timeout=10)
        fixture["cover_collision_qualification"] = {
            "event": contact(), "scope": "setup administrator ordinary bow projectile; not player tactics or enemy damage"}
        staging = await native.settled_point(admin, [start[0] - 6, start[1] + 12, start[2] + 30])
        await native.goto(admin, actor, staging)
        fixture["setup_staging"] = {
            "position": staging, "scope": "player waits offstage during hostile creation, then returns for opening"}
    for spec in definition["enemies"]:
        dx, dy = spec["offset"]
        point = await native.settled_point(admin, [start[0] + dx, start[1] + dy, start[2] + 10])
        fixture["targets"].append(await spawn(admin, actor, spec, point))
    await native.goto(admin, actor, start)
    opening = await actor.request("observe")
    native.require_uninjured_start(opening)
    if shared.equipped(opening, "active_mainhand") != weapon:
        raise RuntimeError("family mainhand differs from declared variant")
    fixture["opening"] = opening
    fixture["actor_binding"] = await native_binding(actor, opening["self_identity"]["uid"])
    if definition["id"] == "T07":
        sword = next(group for group in opening["skill_tree"]["groups"]
                     if group["group"] == {"Weapon": "Sword"})
        if (sword["available_points"] != 4 or len(sword["unlockable_now"]) < 2
                or any((skill["learned_level"] or 0) > 0 for skill in sword["skills"])):
            raise RuntimeError("finite active-build opening lacks unlearned meaningful native alternatives")
    for target in fixture["targets"]:
        current = next((row for row in opening["nearby_entities"] if row["uid"] == target["binding"]["uid"]
                        and row.get("control_target") == target["public"].get("control_target")), None)
        if current is None or current["health"]["current"] <= 0 or current["health"].get("dead"):
            raise RuntimeError("registered encounter target unavailable or dead before opening")
        target["opening_public"] = current
    if definition["id"] == "T09":
        validate_support_opening(fixture, rows(fixture))
    fixture["private_opening_sequence"] = rows(fixture)[-1]["sequence"]
    fixture["native_nonce"] = rows(fixture)[0]["data"]["nonce"]
    fixture["task"] += (
        f"\nEncounter configuration: {variant} weapon {weapon}; current armor, health, skills, "
        "energy and finite supplies are disclosed in ordinary observations. "
        "Targets are " + ", ".join(f"{row['role']} {row['public'].get('name_text', row['asset'])} "
                                  f"(UID {row['public']['uid']})" for row in fixture["targets"]) + ". "
        "These weapon variants are not assumed equal in power or difficulty.")
    fixture["task"] += "\nAuthored enemy/loadout disclosure: " + "; ".join(
        f"{row['public']['uid']}: {row['asset']}, {row['stock_loadout']}, "
        f"opening health {row['opening_public']['health']['current']}/"
        f"{row['opening_public']['health']['maximum']}"
        for row in fixture["targets"]) + "."
    return fixture


def intent_receipts(opening, receipts):
    accepted = []
    for row in receipts:
        request, response = row["request"], row["response"]
        if (row.get("actor") != opening["self_identity"]["uid"]
                or response.get("session_id") != opening["session_id"] or response.get("ok") is not True):
            continue
        args, result = request.get("args", {}), response.get("result") or {}
        if request["cmd"] == "step":
            action = args.get("action", {})
            fence = args.get("control_fence") or {}
            # HUD canonicalization retains a genuine frame/actuator fence, not
            # a structured control_fence. Never let damaged visual provenance
            # fall through to the structured path, even if one is supplied.
            visual = ("public_request" in row or "resolution_source" in row
                      or "visual_resolution" in result or action.get("type") == "visual_input")
            fenced = (visual_ui.intent_fence_matches(row, opening) if visual else
                      fence.get("expected_session_id") == opening["session_id"]
                      and fence.get("actor") == opening["control_actor"])
            if (result.get("status") in {"completed", "cancelled"}
                    and result.get("action_type") == action.get("type")
                    and isinstance(action.get("action_id"), str)
                    and result.get("action_id") == action["action_id"]
                    and fenced):
                accepted.append({"request_id": request["id"], "action": action, "detail": result.get("detail") or {},
                                 "wall_monotonic": row.get("wall_monotonic")})
        elif request["cmd"] == "set_control_state" and result.get("status") == "applied":
            if (args.get("expected_session_id") == opening["session_id"]
                    and args.get("actor") == opening["control_actor"]
                    and result.get("session_id") == opening["session_id"]
                    and all(result.get(key) == args.get(key)
                            for key in ("actor", "owner", "generation", "sequence"))
                    and (any(args.get(key) is True for key in ("primary", "secondary"))
                         or args.get("auxiliary") is not None)):
                accepted.append({"request_id": request["id"], "action": {"type": "set_control_state", **args},
                                 "detail": result, "wall_monotonic": row.get("wall_monotonic")})
    return accepted


def learned_build(fixture, final, intents, effects, journal):
    learns = []
    for row in intents:
        detail, action = row["detail"], row["action"]
        if action["type"] != "unlock_skill" or detail.get("skill_unlock_confirmed") is not True:
            continue
        if (detail.get("effect") == "observed" and detail.get("skill") == action.get("skill")
                and detail.get("expected_cost", 0) > 0
                and detail.get("after_level") == detail.get("before_level", -2) + 1
                and detail.get("after_points") == detail.get("before_points", -1) - detail["expected_cost"]):
            learns.append(row)
    configs = [row for row in intents if row["action"]["type"] == "configure_auxiliary_ability"
               and row["detail"].get("ability_configuration_confirmed") is True
               and row["detail"].get("effect") == "observed"
               and row["detail"].get("ability") == row["action"].get("ability")
               and row["detail"].get("observed") == row["action"].get("ability")]
    uses = []
    for effect in effects:
        joined = oracle.attack_join(journal, effect)
        if joined["status"] != "causally_attributed":
            continue
        ability = joined["queue_event"]["data"]["ability"]
        selected = (ability.get("ability") or {}).get("ability")
        aux = selected.get("MainWeaponAux") if isinstance(selected, dict) else None
        slot = (ability.get("input") or {}).get("Ability") if isinstance(ability.get("input"), dict) else None
        if aux not in ACTIVE_SKILLS or slot is None or ability.get("tool") != "Sword":
            continue
        skill = {"Sword": ACTIVE_SKILLS[aux]}
        learned = [row for row in learns if row["action"]["skill"] == skill]
        configured = [row for row in configs if row["action"]["slot"] == slot
                      and row["action"]["ability"] == {"MainWeapon": aux}]
        invoked = [row for row in intents if row["action"]["type"] == "set_control_state"
                   and row["action"].get("auxiliary") == slot]
        if learned and configured and invoked and any(
                group.get("group") == {"Weapon": "Sword"}
                and any(entry.get("skill") == skill and (entry.get("learned_level") or 0) >= 1
                        for entry in group.get("skills", []))
                for group in final.get("skill_tree", {}).get("groups", [])):
            uses.append({"effect": effect, "original_attack": joined["queue_event"],
                         "learn_request": learned[-1]["request_id"],
                         "configure_request": configured[-1]["request_id"],
                         "use_request": invoked[-1]["request_id"]})
    return {"learned_and_spent": bool(learns), "configured": bool(configs),
            "configured_learned_effect": bool(uses)}, {"learning": learns, "configuration": configs, "uses": uses}


def score(fixture, final, receipts, events, samples, *, journal=None):
    journal = rows(fixture) if journal is None else journal
    if (journal[0]["data"]["nonce"] != fixture["native_nonce"]):
        raise ValueError("private native process identity changed")
    current = [row for row in journal if row["sequence"] > fixture["private_opening_sequence"]]
    opening, source = fixture["opening"], fixture["actor_binding"]
    own = [row for row in [opening, *samples, final] if row.get("session_id") == opening["session_id"]
           and row.get("self_identity") == opening["self_identity"]]
    same = (final.get("session_id"), final.get("self_identity")) == (
        opening["session_id"], opening["self_identity"])
    dead = any(row["survival"]["health"].get("dead") is True for row in own) or any(
        row["event"] == "native_entity_death" and row["data"]["entity"] == source["server_entity"]
        and row["data"]["uid"] == source["uid"] for row in current)
    intents = intent_receipts(opening, receipts)
    combat_intents = [row for row in intents if row["action"]["type"] in {
        "attack_primary", "attack_secondary", "set_control_state"}]
    targets, all_effects = [], []
    for target in fixture["targets"]:
        binding = target["binding"]
        effects = [row for row in current if oracle.sourced_health(row, source, binding)]
        deaths = [row for row in current if row["event"] == "native_entity_death"
                  and row["data"]["uid"] == binding["uid"]
                  and row["data"]["entity"] == binding["server_entity"]]
        all_effects.extend(effects)
        targets.append({"uid": binding["uid"], "entity": binding["server_entity"],
                        "native_death": bool(deaths), "death_events": deaths,
                        "player_contribution": bool(effects) and bool(combat_intents),
                        "health_applications": effects,
                        "original_attack_joins": [oracle.attack_join(journal, row) for row in effects]})
    parts = {"target_registry_valid": bool(targets) and len({row["uid"] for row in targets}) == len(targets),
             "encounter_resolved": bool(targets) and all(row["native_death"] for row in targets),
             "declared_player_contribution": all(row["player_contribution"] for row in targets),
             "player_alive": same and not dead and shared.living(final)}
    detail = {}
    if fixture["case"] == "T07":
        build, detail = learned_build(fixture, final, intents, all_effects, journal)
        parts.update(build)
    if fixture["case"] == "T09":
        ally = fixture["ally"]["binding"]
        heals = []
        for row in current:
            if not oracle.sourced_health(row, source, ally, healing=True):
                continue
            data = row["data"]
            group = data["contributor_group"]
            if (group is not None and group == data["target_group"]
                    and (data["contributor"] or {}).get("Group", {}).get("group") == group
                    and data["cause"] == {"Buff": "Regeneration"}):
                heals.append(row)
        ally_state = oracle.binding(journal, ally["uid"])
        ally_alive = (ally_state is not None and ally_state["server_entity"] == ally["server_entity"]
                      and ally_state["dead"] is False and ally_state["health"] > 0
                      and not any(row["event"] == "native_entity_death"
                                  and row["data"]["entity"] == ally["server_entity"]
                                  for row in current))
        casts = [row for row in combat_intents if row["action"].get("secondary") is True
                 or row["action"]["type"] == "attack_secondary"]
        parts.pop("declared_player_contribution")
        parts.update(spell_submitted=bool(casts), actual_group_healing=bool(heals),
                     ally_health_threshold=bool(ally_alive and ally_state["health"] >= fixture["ally"]["threshold_hp"]),
                     ally_alive=ally_alive)
        detail.update(healing_applications=heals, ally_state=ally_state, cast_intents=casts,
                      attribution="individual sourced Group health applications, not net HP or setup healing")
    unknown = []
    if (fixture["case"] == "T07" and parts["learned_and_spent"] and parts["configured"]
            and not parts["configured_learned_effect"]
            and any(row["action"].get("auxiliary") is not None for row in intents)
            and any(oracle.attack_join(journal, row)["status"] in {
                "unknown", "ambiguous", "ability_unknown"} for row in all_effects)):
        unknown.append("configured_ability_effect_provenance_unknown")
    return {"success": all(parts.values()), "dead": dead, "same_actor": same, "parts": parts,
            "targets": targets, "build_or_support": detail, "native_intents": intents,
            "private_evidence_end_sequence": journal[-1]["sequence"],
            "measurement_unknown_reasons": unknown,
            "variant": fixture["variant"], "fixture_version": fixture["fixture_version"],
            "private_evidence_scope": "server-only private1.1; original Attack joins for configured effects; missing/generated provenance stays unknown",
            "last_blow_required": False, "tactical_interpretation": "outcome does not prove cover use or a particular tactic"}


async def mechanism(actor, fixture):
    raise ValueError("combat/support qualification uses ordinary public tools through task-engineering entry")
