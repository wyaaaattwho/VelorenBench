"""Additive native merchant, Message and Slay tasks."""
from __future__ import annotations

import asyncio
from collections import Counter
import math
import re
import time

from . import contract_evidence as private, scene, setup as native
from .crafting_recovery import died, equipped, living, own_actions

TASK_IDS = frozenset({"T10", "T11", "T12"})
COINS = "common.items.utility.coins"
SURPLUS = "common.items.crafting_ing.twigs"
CHEST = "common.items.armor.hide.rawhide.chest"
BOW = "common.items.weapons.bow.starter"


def key(content):
    return content.get("Localized", {}).get("key")


def plain(value):
    if isinstance(value, dict):
        if "Plain" in value:
            return value["Plain"]
        for child in value.values():
            text = plain(child)
            if text is not None:
                return text
    return None


def rows(fixture):
    return private.read_journal(fixture["oracle_path"], fixture["run_id"])


async def observed(actor, predicate, reason, timeout=30):
    return await native.wait_observation(actor, predicate, reason, timeout=timeout)


async def pending(actor):
    observation = await observed(actor, lambda row: row["social_and_trade"]["pending_trade"] is not None,
                                 "native merchant pending trade")
    return observation["social_and_trade"]["pending_trade"]


async def trade_action(actor, operation, *, origin="setup", **extra):
    trade = await pending(actor)
    return await native.action(
        actor, "trade_action", origin=origin, operation=operation, trade_id=trade["trade_id"],
        counterparty_uid=trade["parties"][1 - trade["self_party_index"]], phase=trade["phase"], **extra)


async def invite(actor, npc):
    observation = await actor.request("observe")
    current = next((row for row in observation["nearby_entities"] if row["uid"] == npc["uid"]), None)
    if current is None:
        raise RuntimeError("merchant is no longer ordinarily observed")
    if current["distance"] > 3:
        await native.action(actor, "move_to", position=current["position"], tolerance=1, timeout_ticks=1800)
        observation = await actor.request("observe")
        current = next(row for row in observation["nearby_entities"] if row["uid"] == npc["uid"])
    await native.action(actor, "social_action", operation="invite_trade", target=current["control_target"])
    return await pending(actor)


async def dialogue_action(actor, dialogue, response=None, *, origin="setup"):
    arguments = {"operation": "ack", "sender_uid": dialogue["sender_uid"],
                 "dialogue_id": dialogue["dialogue_id"], "tag": dialogue["tag"]}
    if dialogue["kind"] == "Question":
        if response is None:
            raise ValueError("dialogue question needs an exact offered response")
        arguments.update(operation="respond", response_id=response["response_id"])
    else:
        await asyncio.sleep(1.7)
    return await native.action(actor, "dialogue_action", origin=origin, **arguments)


async def prompt(actor, previous=None):
    return (await observed(
        actor, lambda row: row.get("dialogue") is not None
        and (previous is None or (row["dialogue"]["dialogue_id"], row["dialogue"]["tag"]) != previous),
        "fresh native dialogue prompt"))["dialogue"]


async def menu(actor, npc):
    while True:
        current = await actor.request("observe")
        speaker = next((row for row in current["nearby_entities"] if row["uid"] == npc["uid"]), None)
        if speaker is None:
            raise RuntimeError("native dialogue source is not ordinarily observed")
        if speaker["distance"] > 2:
            await native.action(actor, "move_to", position=speaker["position"], tolerance=1, timeout_ticks=1800,
                                basis=current)
            continue
        dialogue = current.get("dialogue")
        if dialogue is None or dialogue["sender_uid"] != npc["uid"]:
            await native.action(actor, "context_interact", operation="talk", target=speaker["control_target"],
                                basis=current)
            dialogue = await prompt(actor)
        if dialogue["kind"] == "Question":
            return dialogue
        await dialogue_action(actor, dialogue)
        await asyncio.sleep(.3)


async def native_npc(admin, actor, fixture, identity):
    binding = next((entry for entry in private.bindings(rows(fixture)) if entry["actor"] == identity), None)
    if binding is None:
        raise RuntimeError("authored real RTSim participant is not loaded")
    arguments = ("rtsim_tp", binding["rtsim_uid"])
    await native.command(admin, *(arguments if actor is admin else ("sudo", actor.name, *arguments)))
    return next(row for row in (await observed(
        actor, lambda row: any(entry["uid"] == binding["uid"] and entry["distance"] < 4
                               for entry in row["nearby_entities"]),
        "real fixture NPC in ordinary view"))["nearby_entities"] if row["uid"] == binding["uid"])


async def stock_merchant(admin, actor, npc, fixture):
    npc = await native_npc(admin, actor, fixture, fixture["merchant_actor"])
    trade = await invite(actor, npc)
    fixture["merchant_initial_stock"] = trade
    await trade_action(actor, "decline")
    await observed(actor, lambda row: row["social_and_trade"]["pending_trade"] is None, "trade declined")
    stock = trade["inventories"][1 - trade["self_party_index"]]
    existing = next((row for row in stock if scene.item_id(row) == CHEST), None)
    if existing is None:
        await native.command(admin, "tp", actor.name)
        await native.gift(admin, admin, CHEST, 1)
        vendor = await native_npc(admin, admin, fixture, fixture["merchant_actor"])
        seed_trade = await invite(admin, vendor)
        own = seed_trade["self_party_index"]
        item = next(row for row in seed_trade["inventories"][own] if scene.item_id(row) == CHEST)
        coin = next(row for row in seed_trade["inventories"][1 - own] if scene.item_id(row) == COINS)
        if not item.get("unit_site_value") or not coin.get("unit_site_value"):
            raise RuntimeError("native merchant cannot lawfully value the authored equipment stock")
        payment = math.floor(item["unit_site_value"] / coin["unit_site_value"])
        if payment < 1 or coin["amount"] < payment:
            raise RuntimeError("native merchant lacks actual funding for ordinary setup stock purchase")
        await trade_action(admin, "add_item", slot=item["slot"], quantity=1, ours=True)
        await trade_action(admin, "add_item", slot=coin["slot"], quantity=payment, ours=False)
        await trade_action(admin, "accept")
        await observed(admin, lambda row: (row["social_and_trade"]["pending_trade"] or {}).get(
            "phase") == "Review", "stock trade native review")
        await trade_action(admin, "accept")
        completed = await observed(admin, lambda row: (row.get("last_trade_result") or {}).get(
            "committed") is True, "ordinary native fixture stock transaction")
        fixture["ordinary_stock_provision"] = {
            "source": "finite authored administrator item sold by ordinary native exchange",
            "trade": completed["last_trade_result"], "native_price_payment": payment}
    npc = await native_npc(admin, actor, fixture, fixture["merchant_actor"])
    trade = await invite(actor, npc)
    own = trade["self_party_index"]
    stock = trade["inventories"][1 - own]
    item = next((row for row in stock if scene.item_id(row) == CHEST), None)
    coins = next((row for row in trade["inventories"][own] if scene.item_id(row) == COINS), None)
    surplus = next((row for row in trade["inventories"][own] if scene.item_id(row) == SURPLUS), None)
    if (item is None or coins is None or surplus is None or trade["site_prices"] is None
            or not item.get("unit_site_value") or not coins.get("unit_site_value")
            or not surplus.get("unit_site_value")
            or coins["amount"] - math.ceil(item["unit_site_value"] / coins["unit_site_value"]) < 20):
        raise RuntimeError("merchant stock/valuation/funding does not qualify the public purchase and reserves")
    fixture["qualified_stock"] = trade
    fixture.update(merchant=npc, equipment=CHEST, equip_slot="chest",
                   reserves={COINS: 20, SURPLUS: 2})
    fixture["task"] += (
        f"\n\nMerchant: {npc['name_text']}. Buy {CHEST} and wear it in your chest slot. "
        f"Sell at least one surplus twig to this merchant. Keep at least 20 coins ({COINS}) "
        f"and two twigs ({SURPLUS}) in your bag. Native stock and prices are visible during trade.")


async def prepare_offer(admin, actor, npc, fixture, recipient):
    desired = "npc-response-quest-messenger-ask" if fixture["case"] == "T11" else "npc-response-quest-slay-ask"
    capture_safe = fixture.get("definition", {}).get("fixture_version") == "t12-native-slay-v6"
    while True:
        if capture_safe:
            npc = await living_giver(admin, actor, fixture)
        elif fixture.get("definition", {}).get("fixture_version") == "t12-native-slay-v5":
            await separate_opening_threats(admin, actor, fixture)
        if fixture["case"] == "T11":
            current = await actor.request("observe")
            participant = next((entry for entry in private.bindings(rows(fixture))
                                if entry["actor"] == fixture["prepared_recipient_actor"]), None)
            if participant is None:
                raise RuntimeError("Message preoffer participant left the loaded fixture region")
            point = [current["position"][0] + 3, *current["position"][1:]]
            receipt = await native.command(admin, "sudo", f"uid@{participant['uid']}", "goto", *point)
            fixture.setdefault("preoffer_recipient_placements", []).append({
                "actor": participant["actor"], "position": point, "receipt": receipt,
                "scope": "preacceptance only; preserves local real NPC offer conditions, no quest acceptance"})
        question = await menu(actor, npc)
        request = next((row for row in question["responses"]
                        if key(row["content"]) == "dialogue-question-quest_req"), None)
        if request is None:
            raise RuntimeError("native resident has no fresh ordinary quest-request option")
        if capture_safe:
            await place_capture_target(admin, actor, fixture)
            npc = await living_giver(admin, actor, fixture)
            before = capture_snapshot(fixture)
        await dialogue_action(actor, question, request)
        offer = await prompt(actor, (question["dialogue_id"], question["tag"]))
        while offer["kind"] == "Statement":
            if fixture["case"] == "T12":
                current = await actor.request("observe")
                speaker = next((row for row in current["nearby_entities"] if row["uid"] == npc["uid"]), None)
                if speaker is None:
                    raise RuntimeError("Slay offer source left ordinary identity view")
                if speaker["distance"] > 2:
                    await native.action(actor, "move_to", position=speaker["position"], tolerance=1.2)
            await dialogue_action(actor, offer)
            offer = await prompt(actor, (offer["dialogue_id"], offer["tag"]))
        expected = key(offer["content"]) == desired
        if capture_safe and expected:
            after = capture_snapshot(fixture)
            consistent = capture_consistent(before, after)
            fixture.setdefault("offer_captures", []).append({
                "offer": offer, "before": before, "after": after, "consistent": consistent,
                "scope": "native quest-request capture interval; no post-offer target relocation"})
            expected &= consistent
        if fixture["case"] == "T11":
            name = plain(offer["content"].get("Localized", {}).get("args", {}).get("name"))
            observation = await actor.request("observe")
            candidate = next((row for row in observation["nearby_entities"]
                              if row["uid"] != npc["uid"] and row.get("alias") is None
                              and (row.get("name_text") or "").rsplit(" (", 1)[0] == name), None)
            expected &= candidate is not None
            if expected:
                recipient = candidate
        fixture.setdefault("offer_qualification", []).append({"offer": offer, "selected": expected})
        if expected:
            fixture.update(offer=offer, giver=npc, recipient=recipient)
            if fixture["case"] == "T11":
                fixture["recipient_actor"] = next(
                    entry["actor"] for entry in private.bindings(rows(fixture))
                    if entry["uid"] == recipient["uid"])
            return
        reject = next(row for row in offer["responses"] if key(row["content"]) == "common-no")
        await dialogue_action(actor, offer, reject)
        await asyncio.sleep(.5)


def rtsim_position(events):
    replies = [plain(row.get("data", {}).get("content")) for row in events
               if row.get("data", {}).get("kind") == "command_info"]
    matches = [re.search(r"^Pos: Vec3 \{ x: ([^,]+), y: ([^,]+), z: ([^}]+) \}$", text, re.MULTILINE)
               for text in replies if text and "\nRole: Monster\n" in text]
    matches = [match for match in matches if match is not None]
    if len(matches) != 1:
        raise RuntimeError("native RTSim Monster information lacks one exact position reply")
    point = [float(value) for value in matches[0].groups()]
    if not all(math.isfinite(value) for value in point):
        raise RuntimeError("native RTSim Monster position is non-finite")
    return point


async def separate_opening_threats(admin, actor, fixture):
    current = await actor.request("observe")
    capture_safe = fixture.get("definition", {}).get("fixture_version") == "t12-native-slay-v6"
    protected = set()
    enemies = set()
    if capture_safe:
        identities = [fixture.get(name) for name in (
            "target_actor", "giver_actor", "prepared_recipient_actor", "recipient_actor")]
        protected.update(row["uid"] for row in private.bindings(rows(fixture))
                         if row["actor"] in identities)
        protected.add(current["self_identity"]["uid"])
        enemies = {row["target"]["uid"] for row in current.get("combat_perception", {}).get("nearby_combat", [])
                   if row.get("alignment") == "Enemy"}
    threats = [row for row in current["nearby_entities"] if row.get("alias") is None
               and row["uid"] not in protected
               and (row["uid"] in enemies or any(name in str(row.get("name_text")).casefold()
                                                for name in ("legoom", "wolf", "tusk ram")))
               and not (row.get("health") or {}).get("dead")]
    for threat in threats:
        placements = fixture.setdefault("preacceptance_ambient_placements", [])
        if any(row["uid"] == threat["uid"] for row in placements):
            raise RuntimeError("authored opening threat returned after its initial placement")
        point = await native.settled_point(admin, [
            current["position"][0] - 96, current["position"][1] + 4 * len(placements),
            current["position"][2]])
        receipt = await native.command(admin, "sudo", f"uid@{threat['uid']}", "goto", *point)
        actual = await observed(admin, lambda row: any(
            entity["uid"] == threat["uid"] and entity["distance"] < 8
            and (entity.get("health") or {}).get("current", 0) > 0
            for entity in row["nearby_entities"]), "same ambient creature alive at authored natural separation")
        readback = next(row for row in actual["nearby_entities"] if row["uid"] == threat["uid"])
        placements.append({"uid": threat["uid"], "before": threat, "after": readback,
                           "destination": point, "receipt": receipt,
                           "scope": "finite preacceptance ambient separation; no kill, damage, target replacement or later positioning"})
        await native.command(admin, "tp", actor.name)


def capture_snapshot(fixture):
    journal = rows(fixture)
    bindings = private.bindings(journal)
    selected = {}
    combat = private.latest(journal, "combat_entity_bindings", {}).get("bindings", [])
    for name in ("target_actor", "giver_actor"):
        binding = next((row for row in bindings if row["actor"] == fixture[name]), None)
        health = next((row for row in combat if binding and row["uid"] == binding["uid"]
                       and row["server_entity"] == binding["server_entity"]), None)
        if binding is None or health is None or health["dead"] or health["health"] <= 0:
            raise RuntimeError(f"Slay capture requires original living loaded {name}")
        if name == "target_actor" and not 0 < health["health"] < health["maximum_health"]:
            raise RuntimeError("Slay capture target lost its finite native injury")
        if any(row["event"] == "rtsim_death_hook" and row["data"].get("actor") == fixture[name]
               for row in journal):
            raise RuntimeError(f"original Slay {name} died during setup")
        selected[name] = {**binding, "health_readback": health}
    if any(fixture["target_actor"] in [row["actor"] for row in quest["actors"]]
           for quest in private.latest(journal, "quest_states", [])):
        raise RuntimeError("designated Slay target is already bound to a native quest")
    return {"sequence": journal[-1]["sequence"], **selected}


def capture_consistent(before, after):
    for name in ("target_actor", "giver_actor"):
        left, right = before[name], after[name]
        if any(left[key] != right[key] for key in ("actor", "uid", "server_entity", "rtsim_uid")):
            return False
        for source in ("server_position", "rtsim_position"):
            points = [[row[source][axis] for axis in ("x", "y", "z")] for row in (left, right)]
            if not all(math.isfinite(value) for point in points for value in point):
                raise RuntimeError("non-finite native Slay capture position")
            if math.dist(*points) >= 8:
                return False
    target, giver = after["target_actor"], after["giver_actor"]
    return 32 < math.dist(
        [target["rtsim_position"][axis] for axis in ("x", "y")],
        [giver["rtsim_position"][axis] for axis in ("x", "y")]) <= 96


async def living_giver(admin, actor, fixture):
    await separate_opening_threats(admin, actor, fixture)
    observation = await actor.request("observe")
    native.require_living(observation)
    binding = next((row for row in private.bindings(rows(fixture))
                    if row["actor"] == fixture["giver_actor"]), None)
    if binding is None:
        raise RuntimeError("original Slay giver is no longer loaded")
    giver = next((row for row in observation["nearby_entities"] if row["uid"] == binding["uid"]), None)
    if giver is None or (giver.get("health") or {}).get("dead") or (giver.get("health") or {}).get("current", 0) <= 0:
        raise RuntimeError("original Slay giver is not living in ordinary view")
    if giver["distance"] > 3:
        await native_npc(admin, actor, fixture, fixture["giver_actor"])
        await separate_opening_threats(admin, actor, fixture)
        observation = await actor.request("observe")
        native.require_living(observation)
        giver = next((row for row in observation["nearby_entities"] if row["uid"] == binding["uid"]), None)
        if giver is None or giver["distance"] > 3 or (giver.get("health") or {}).get("current", 0) <= 0:
            raise RuntimeError("original living Slay giver drifted after setup realignment")
    fixture.setdefault("giver_reobservations", []).append(giver)
    return giver


async def place_capture_target(admin, actor, fixture):
    opening = await actor.request("observe")
    native.require_living(opening)
    monster = fixture["designated_monster"]
    point = await native.settled_point(admin, [opening["position"][0] + 48, *opening["position"][1:]])
    ground = await native.qualify(admin, point)
    await native.command(admin, "rtsim_tp", monster["rtsim_uid"])
    await observed(admin, lambda _: any(row["actor"] == monster["actor"]
                                       for row in private.bindings(rows(fixture))),
                   "original injured Monster loaded before native offer capture")
    binding = next(row for row in private.bindings(rows(fixture)) if row["actor"] == monster["actor"])
    receipt = await native.command(admin, "sudo", f"uid@{binding['uid']}", "goto", *point)
    await native.command(admin, "tp", actor.name)
    await observed(admin, lambda _: any(
        row["actor"] == monster["actor"] and row["rtsim_uid"] == monster["rtsim_uid"]
        and all(math.dist([row[source][axis] for axis in ("x", "y", "z")], point) < 8
                for source in ("server_position", "rtsim_position"))
        for row in private.bindings(rows(fixture))), "preoffer server and RTSim target placement")
    start = len(admin.native_events)
    info_receipt = await native.command(admin, "rtsim_info", monster["rtsim_uid"])
    information = admin.native_events[start:]
    readback = rtsim_position(information)
    if math.dist(readback, point) >= 8:
        raise RuntimeError("Slay target drifted before native offer capture")
    placement = {"actor": monster["actor"], "before_binding": binding, "destination": point,
                 "receipt": receipt, "qualified_ground": ground,
                 "scope": "pre-offer capture placement only; never an accepted quest"}
    fixture.setdefault("preoffer_target_placements", []).append(placement)
    fixture["slay_site"] = {
        "construction_revision": "town-approach-006", "target_placement": placement,
        "target_readback": {"position": readback, "rtsim_uid": monster["rtsim_uid"],
                            "info_receipt": info_receipt, "events": information},
        "safezone_created": False}


async def slay_site(admin, actor, fixture, residents):
    monster = next(row for row in residents if row.get("role") == "Monster")
    fixture["target_actor"] = monster["actor"]
    start = len(admin.native_events)
    info_receipt = await native.command(admin, "rtsim_info", monster["rtsim_uid"])
    information = admin.native_events[start:]
    target = rtsim_position(information)
    point = await native.settled_point(admin, [target[0] - 56, target[1], target[2]])
    qualified = await native.qualify(admin, point)
    await observed(admin, lambda _: any(row["actor"] == monster["actor"]
                                       for row in private.bindings(rows(fixture))),
                   "actual RTSim Slay target loaded without close setup contact")
    binding = next(row for row in private.bindings(rows(fixture)) if row["actor"] == monster["actor"])
    placements = []
    for resident in residents:
        if resident.get("profession") != "Farmer":
            continue
        current = next(row for row in private.bindings(rows(fixture))
                       if row["actor"] == resident["actor"])
        destination = (point if not placements else
                       await native.settled_point(admin, [point[0], point[1] - 4, point[2]]))
        receipt = await native.command(admin, "sudo", f"uid@{current['uid']}", "goto", *destination)
        placements.append({"actor": resident["actor"], "position": destination, "receipt": receipt})
    await native.goto(admin, actor, point)
    fixture["slay_site"] = {
        "scope": "preacceptance natural giver placement; real Monster never repositioned, damaged or accepted",
        "target_readback": {"position": target, "info_receipt": info_receipt, "events": information},
        "target_binding": binding,
        "giver_placements": placements, "qualified_ground": qualified,
        "safezone_created": False}

def slay_distance(fixture, opening):
    target = next((row for row in private.bindings(rows(fixture))
                   if row["actor"] == fixture["target_actor"]), None)
    if target is None:
        raise RuntimeError("Slay opening target is not currently loaded")
    point = target["server_position"]
    return math.dist(opening["position"][:2], [point["x"], point["y"]])


async def slay_offer(admin, actor, fixture, residents, farmers):
    version = fixture["definition"]["fixture_version"]
    if version == "t12-native-slay-v6":
        monster = next(row for row in residents if row.get("role") == "Monster")
        fixture.update(target_actor=monster["actor"], designated_monster=monster,
                       giver_actor=farmers[0], prepared_recipient_actor=farmers[1])
        recipient = await native_npc(admin, actor, fixture, farmers[1])
        await separate_opening_threats(admin, actor, fixture)
        await native_npc(admin, actor, fixture, farmers[0])
        npc = await living_giver(admin, actor, fixture)
        await prepare_offer(admin, actor, npc, fixture, recipient)
        return fixture["giver"], recipient
    if version in {"t12-native-slay-v3", "t12-native-slay-v4", "t12-native-slay-v5", "t12-native-slay-v6"}:
        fixture["target_actor"] = next(row["actor"] for row in residents if row.get("role") == "Monster")
        recipient = await native_npc(admin, actor, fixture, farmers[1])
        npc = await native_npc(admin, actor, fixture, farmers[0])
        await prepare_offer(admin, actor, npc, fixture, recipient)
        monster = next(row for row in residents if row["actor"] == fixture["target_actor"])
        placement = None
        if version in {"t12-native-slay-v4", "t12-native-slay-v5", "t12-native-slay-v6"}:
            opening = await actor.request("observe")
            point = await native.settled_point(admin, [
                opening["position"][0] + 48, *opening["position"][1:]])
            ground = await native.qualify(admin, point)
            await native.command(admin, "rtsim_tp", monster["rtsim_uid"])
            await observed(admin, lambda _: any(row["actor"] == monster["actor"]
                                               for row in private.bindings(rows(fixture))),
                           "original RTSim Monster loaded for preacceptance placement")
            binding = next(row for row in private.bindings(rows(fixture)) if row["actor"] == monster["actor"])
            receipt = await native.command(admin, "sudo", f"uid@{binding['uid']}", "goto", *point)
            await native.command(admin, "tp", actor.name)
            await observed(admin, lambda _: any(
                row["actor"] == monster["actor"]
                and math.dist([row["server_position"][axis] for axis in ("x", "y", "z")], point) < 8
                and math.dist([row["rtsim_position"][axis] for axis in ("x", "y", "z")], point) < 8
                for row in private.bindings(rows(fixture))),
                "same native Monster actual server and RTSim placement readback")
            placement = {"actor": monster["actor"], "before_binding": binding, "destination": point,
                         "receipt": receipt, "qualified_ground": ground,
                         "scope": "finite preacceptance placement only; same actual Monster; no damage or acceptance"}
        start = len(admin.native_events)
        receipt = await native.command(admin, "rtsim_info", monster["rtsim_uid"])
        information = admin.native_events[start:]
        fixture["slay_site"] = {
            "construction_revision": ("town-approach-005" if version == "t12-native-slay-v5" else
                                      "town-approach-004") if placement else "town-origin-003",
            "scope": "original town residents; same Monster preacceptance placement" if placement else
                     "original native town residents; no NPC relocation, target damage or quest acceptance",
            "target_readback": {"position": rtsim_position(information), "rtsim_uid": monster["rtsim_uid"],
                                "info_receipt": receipt,
                                "events": information}, "safezone_created": False}
        if placement is not None:
            fixture["slay_site"]["target_placement"] = placement
        return npc, recipient
    while True:
        await slay_site(admin, actor, fixture, residents)
        recipient = await native_npc(admin, actor, fixture, farmers[1])
        npc = await native_npc(admin, actor, fixture, farmers[0])
        await prepare_offer(admin, actor, npc, fixture, recipient)
        current = await actor.request("observe")
        distance = slay_distance(fixture, current)
        qualified = 40 < distance < 88
        fixture.setdefault("preacceptance_approaches", []).append({
            "distance_m": distance, "qualified": qualified,
            "scope": "construction margin inside unchanged32-to96m opening guard; no acceptance"})
        if qualified:
            return npc, recipient
        reject = next(row for row in fixture["offer"]["responses"] if key(row["content"]) == "common-no")
        await dialogue_action(actor, fixture["offer"], reject)
        await asyncio.sleep(.5)


async def stationary_cleanup(admin, actor):
    # Native teleportation preserves velocity. Do not capture cleanup's return
    # origin during the residual collision impulse of the T12 NPC approach.
    # Keep the old shared setup helper and its exact goto readback unchanged.
    await observed(
        actor, lambda row: row["on_ground"] and math.hypot(*row["velocity"][:2]) < .1,
        "grounded stationary T12 inventory-cleanup origin")
    return await native.clean_inventory(admin, actor)


async def setup(admin, actor, definition, terrain):
    if terrain != "natural":
        raise ValueError("NPC contract terrain unqualified")
    fixture = {"case": definition["id"], "definition": definition, "task": definition["task"],
               "oracle_path": str(actor.out / "private-native-oracle.ndjson"), "run_id": actor.out.name,
               "native_player_privilege": "not-admin", "supplies": []}
    if fixture["case"] != "T12":
        await native.command(admin, "safezone", 128)
    await observed(actor, lambda row: living(row) and row["on_ground"], "living grounded contract actor")
    if definition.get("fixture_version") == "t12-native-slay-v6":
        residents = private.latest(rows(fixture), "private_resident_fixture")["residents"]
        farmers = [row["actor"] for row in residents if row.get("profession") == "Farmer"]
        monster = next(row for row in residents if row.get("role") == "Monster")
        fixture.update(target_actor=monster["actor"], designated_monster=monster,
                       giver_actor=farmers[0], prepared_recipient_actor=farmers[1])
        await native_npc(admin, actor, fixture, farmers[0])
        await living_giver(admin, actor, fixture)
    fixture["cleanup_supplies"] = await (stationary_cleanup(admin, actor) if fixture["case"] == "T12"
                                       else native.clean_inventory(admin, actor))
    if fixture["case"] == "T12":
        stock = await native.gift(admin, actor, BOW, 1)
        if scene.counts(stock).get(BOW) != 1:
            raise RuntimeError("optional Slay bow lacks actual native inventory insertion")
        fixture["optional_inventory"] = {BOW: 1}
        fixture["task"] += "\nYour bag also contains a basic bow as an optional ranged choice; no particular weapon is a success condition."
    residents = private.latest(rows(fixture), "private_resident_fixture")["residents"]
    for resident in residents:
        if resident.get("profession") == "Farmer":
            await native.command(admin, "sudo", f"uid@{next(
                entry['uid'] for entry in private.bindings(rows(fixture)) if entry['actor'] == resident['actor'])}",
                                 "give_item", COINS, 3000)
    if fixture["case"] == "T10":
        identity = next(row["actor"] for row in residents if row.get("profession") == "Merchant")
        npc = await native_npc(admin, actor, fixture, identity)
        await native.gift(admin, actor, COINS, 10000)
        await native.gift(admin, actor, SURPLUS, 10)
        fixture["merchant_actor"] = identity
        await stock_merchant(admin, actor, npc, fixture)
    else:
        farmers = [row["actor"] for row in residents if row.get("profession") == "Farmer"]
        if fixture["case"] == "T12":
            npc, recipient = await slay_offer(admin, actor, fixture, residents, farmers)
        else:
            recipient = await native_npc(admin, actor, fixture, farmers[1])
            npc = await native_npc(admin, actor, fixture, farmers[0])
            fixture["prepared_recipient_actor"] = farmers[1]
            await prepare_offer(admin, actor, npc, fixture, recipient)
        fixture["giver_actor"] = farmers[0]
        if fixture["case"] == "T12":
            fixture["target_actor"] = next(row["actor"] for row in residents if row.get("role") == "Monster")
        else:
            if npc["name_text"] == recipient["name_text"] or npc["uid"] == recipient["uid"]:
                raise RuntimeError("Message fixture candidates are not distinguishable")
    fixture["opening"] = await actor.request("observe")
    await validate_opening(actor, fixture)
    return fixture


async def validate_opening(actor, fixture):
    opening = await actor.request("observe")
    native.require_living(opening)
    if opening["survival"]["health"]["current"] != 100:
        raise RuntimeError("contract opening must have full native health")
    if opening["inventory"]["free_slots"] < 3:
        raise RuntimeError("contract opening lacks actual free bag capacity")
    journal = rows(fixture)
    player = next((entry for entry in private.bindings(journal)
                   if entry["uid"] == opening["self_identity"]["uid"] and "Character" in entry["actor"]), None)
    if player is None:
        raise RuntimeError("contract player has no exact native Actor/entity generation binding")
    quests = private.latest(journal, "quest_states", [])
    if any(player["actor"] in [entry["actor"] for entry in quest["actors"]] for quest in quests):
        raise RuntimeError("fresh contract actor already participates in a quest")
    if fixture["case"] != "T10" and opening.get("dialogue") != fixture["offer"]:
        raise RuntimeError("fresh unaccepted ordinary contract offer changed before entry")
    if fixture["case"] == "T12" and any(
            row["event"] == "rtsim_death_hook" and row["data"].get("actor") == fixture["target_actor"]
            for row in journal):
        raise RuntimeError("Slay target died before the fresh task opening")
    if fixture["case"] == "T12":
        version = fixture.get("definition", {}).get("fixture_version")
        if version in {"t12-native-slay-v3", "t12-native-slay-v4", "t12-native-slay-v5", "t12-native-slay-v6"}:
            monsters = [row for row in private.latest(journal, "private_resident_fixture", {}).get("residents", [])
                        if row.get("role") == "Monster" and row["actor"] == fixture["target_actor"]]
            readback = fixture["slay_site"]["target_readback"]
            if (len(monsters) != 1 or monsters[0]["rtsim_uid"] != readback["rtsim_uid"]
                    or rtsim_position(readback["events"]) != readback["position"]):
                raise RuntimeError("town Slay lacks its exact native Monster identity/readback")
            distance = math.dist(opening["position"][:2], fixture["slay_site"]["target_readback"]["position"][:2])
            if not distance > 32:
                raise RuntimeError("native town Slay opening has no distinct ordinary target approach")
            if version in {"t12-native-slay-v4", "t12-native-slay-v5", "t12-native-slay-v6"}:
                placement = fixture["slay_site"]["target_placement"]
                binding = next((row for row in private.bindings(journal)
                                if row["actor"] == fixture["target_actor"]), None)
                if (placement["actor"] != fixture["target_actor"]
                        or placement["before_binding"]["actor"] != fixture["target_actor"]
                        or binding is None or binding["rtsim_uid"] != readback["rtsim_uid"]):
                    raise RuntimeError("authored town approach lacks the same loaded native Monster")
                distance = slay_distance(fixture, opening)
                if not 32 < distance <= 96:
                    raise RuntimeError(f"authored town approach outside32-to96m opening guard: {distance}")
            if version == "t12-native-slay-v6":
                capture = fixture["offer_captures"][-1]
                if (capture["offer"] != fixture["offer"] or not capture["consistent"]
                        or not capture_consistent(capture["before"], capture_snapshot(fixture))):
                    raise RuntimeError("Slay offer capture drifted; stale offer cannot qualify the opening")
        else:
            distance = slay_distance(fixture, opening)
            if not 32 < distance <= 96:
                raise RuntimeError(f"Slay opening lacks the qualified natural approach (32 < distance <= 96): {distance}")
        fixture["slay_site"]["opening_target_distance"] = distance
    fixture.update(opening=opening, player={**player, "client_session_id": opening["session_id"],
                                           "client_actor": opening["control_actor"]},
                   oracle_cutoff=journal[-1]["sequence"],
                   preexisting_quest_ids=[quest["quest_id"] for quest in quests],
                   opening_validation={"alive": True, "fresh_unaccepted": True,
                                       "native_actor_generation_bound": True, "free_slots": opening["inventory"]["free_slots"]})
    return opening


def merchant_score(fixture, final, receipts, samples):
    opening = fixture["opening"]
    own = own_actions(opening, receipts, "trade_action")
    equips = own_actions(opening, receipts, "equip")
    before, after = scene.counts(opening), scene.counts(final)
    observations = [opening, *samples, final]
    receipt_samples = []
    for row in receipts:
        action_type = row["request"].get("args", {}).get("action", {}).get("type")
        if action_type not in {"trade_action", "equip"} or not own_actions(opening, [row], action_type):
            continue
        observation = row["response"]["result"].get("observation")
        if (isinstance(observation, dict)
                and observation.get("session_id") == opening["session_id"]
                and observation.get("self_identity") == opening["self_identity"]
                and observation.get("control_actor") == opening["control_actor"]
                and type(observation.get("tick")) is int
                and opening["tick"] <= observation["tick"] <= final["tick"]):
            receipt_samples.append(observation)
    if receipt_samples:
        observations = sorted([*observations, *receipt_samples], key=lambda row: row["tick"])
    completed, purchases, sales = [], 0, 0
    seen = set()
    def holdings(observation):
        counts = Counter(scene.counts(observation))
        for item in observation.get("survival", {}).get("equipment", {}).values():
            if isinstance(item, dict) and scene.item_id(item):
                counts[scene.item_id(item)] += item.get("amount", 1)
        return counts

    for position, observation in enumerate(observations):
        result = observation.get("last_trade_result") or {}
        trade = result.get("trade") or {}
        parties = trade.get("parties", [])
        identity = json_identity(result)
        if (identity in seen or result.get("committed") is not True
                or result.get("result") != "Completed" or result.get("source") != "server_finished_trade"
                or trade.get("phase") != "Review"):
            continue
        if sorted(parties) != sorted([fixture["player"]["uid"], fixture["merchant"]["uid"]]):
            continue
        references = result.get("parties") or []
        if not any(ref.get("uid") == fixture["merchant"]["uid"]
                   and ref.get("lifecycle") == fixture["merchant"]["control_target"]["lifecycle"]
                   for ref in references if ref):
            continue
        if not any(ref.get("uid") == fixture["player"]["uid"]
                   and ref.get("lifecycle") == opening["control_actor"]["lifecycle"]
                   for ref in references if ref):
            continue
        matching = [row for row in observations[:position]
                    if (row.get("social_and_trade") or {}).get("pending_trade")
                    and row["social_and_trade"]["pending_trade"]["parties"] == parties
                    and row["social_and_trade"]["pending_trade"]["offers"] == trade.get("offers")
                    and row["social_and_trade"]["pending_trade"]["phase"] == "Review"]
        if not matching:
            continue
        before_commit = matching[-1]
        snapshot = before_commit["social_and_trade"]["pending_trade"]
        accepted = [row for row in own if row["action"].get("operation") == "accept"
                    and row["action"].get("counterparty_uid") == fixture["merchant"]["uid"]
                    and row["action"].get("trade_id") == snapshot["trade_id"]
                    and row["action"].get("phase") == "Review"]
        if not accepted:
            continue
        index = snapshot["self_party_index"]
        offered = []
        for side in (index, 1 - index):
            inventory = {row["slot"]: row for row in snapshot["inventories"][side]}
            if any(row["slot"] not in inventory or type(row["quantity"]) is not int
                   or not 0 < row["quantity"] <= inventory[row["slot"]]["amount"]
                   for row in trade["offers"][side]):
                break
            values = [(scene.item_id(inventory[row["slot"]]), row["quantity"])
                      for row in trade["offers"][side]]
            offered.append(values)
        if len(offered) != 2:
            continue
        delta = Counter()
        for sign, values in ((-1, offered[0]), (1, offered[1])):
            for name, amount in values:
                delta[name] += sign * amount
        prior, arrived = holdings(before_commit), holdings(observation)
        if any(arrived[name] != prior[name] + amount for name, amount in delta.items()):
            continue
        seen.add(identity)
        purchase = sum(amount for name, amount in offered[1] if name == fixture["equipment"])
        sale = sum(amount for name, amount in offered[0] if name == SURPLUS)
        if purchase and offered[0] or sale and offered[1]:
            purchases += purchase
            sales += sale
            completed.append({"finished_trade": result, "inventory_snapshot": snapshot,
                              "actual_item_deltas": dict(delta),
                              "trade_id_limit": "native FinishedTrade has no TradeId; exact parties/lifecycles/offers and bag causality only"})
    parts = {
        "actual_merchant_commit": bool(completed),
        "purchased_equipment": purchases >= 1 and before.get(fixture["equipment"], 0) == 0,
        "surplus_sold": sales >= 1,
        "actual_payment": bool(completed) and purchases >= 1,
        "equipment_worn": equipped(final, fixture["equip_slot"]) == fixture["equipment"]
                          and any(row["action"].get("equip_slot") == fixture["equip_slot"]
                                  and row["detail"].get("item_id") == fixture["equipment"] for row in equips),
        "reserves_retained": all(after.get(item, 0) >= quantity for item, quantity in fixture["reserves"].items())}
    return {"parts": parts, "completed_exchanges": completed}


def json_identity(value):
    import json
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def score(fixture, final, receipts, events, samples):
    opening = fixture["opening"]
    same_actor = (final.get("session_id") == opening["session_id"]
                  and final.get("self_identity") == opening["self_identity"])
    observations = [opening, *samples, final]
    own_samples = [row for row in observations if row.get("session_id") == opening["session_id"]
                   and row.get("self_identity") == opening["self_identity"]]
    if fixture["case"] == "T12":
        valid_range = (type(opening.get("tick")) is int and type(final.get("tick")) is int
                       and opening["tick"] <= final["tick"])
        same_actor = (same_actor and valid_range and private.slay_player_scope(opening, fixture["player"])
                      and private.slay_player_scope(final, fixture["player"]))
        own_samples = [row for row in own_samples if valid_range
                       and private.slay_player_scope(row, fixture["player"])
                       and type(row.get("tick")) is int and opening["tick"] <= row["tick"] <= final["tick"]]
    journal = rows(fixture)
    native_deaths = private.player_deaths(journal, fixture["player"], after=fixture["oracle_cutoff"])
    dead = any(died(row) for row in own_samples) or bool(native_deaths)
    if fixture["case"] == "T10":
        result = merchant_score(fixture, final, receipts, own_samples)
    else:
        candidates, activation_sequences = {}, {}
        for row in journal:
            if row["event"] != "quest_states" or row["sequence"] <= fixture["oracle_cutoff"]:
                continue
            for quest in row["data"]:
                if quest["quest_id"] not in fixture["preexisting_quest_ids"]:
                    candidates.setdefault(quest["quest_id"], quest)
                    activation_sequences.setdefault(quest["quest_id"], row["sequence"])
        accepted = [row for row in own_actions(opening, receipts, "dialogue_action")
                    if row["action"].get("operation") == "respond"
                    and row["action"].get("sender_uid") == fixture["offer"]["sender_uid"]
                    and row["action"].get("dialogue_id") == fixture["offer"]["dialogue_id"]
                    and row["action"].get("tag") == fixture["offer"]["tag"]
                    and row["action"].get("response_id") == next(
                        choice["response_id"] for choice in fixture["offer"]["responses"]
                        if key(choice["content"]) == "common-yes")]
        matching = []
        for quest in candidates.values():
            kind = quest["quest"]["kind"]
            if fixture["case"] == "T11":
                instance = kind.get("Courier", {}).get("instance", {})
                correct = (instance.get("kind") == "Message"
                           and instance.get("messenger") == fixture["player"]["actor"]
                           and instance.get("source_actor") == fixture["giver_actor"]
                           and instance.get("target_actor") == fixture["recipient_actor"]
                           and quest["quest"]["arbiter"] == fixture["recipient_actor"]
                           and fixture["giver_actor"] != fixture["recipient_actor"])
            else:
                instance = kind.get("Slay", {})
                correct = (instance.get("slayer") == fixture["player"]["actor"]
                           and instance.get("target") == fixture["target_actor"]
                           and quest["quest"]["arbiter"] == fixture["giver_actor"])
            if correct and quest["resolution"] is None:
                matching.append(quest)
        settlement = (private.reward(journal, matching[0], fixture["player"], after=fixture["oracle_cutoff"])
                      if len(matching) == 1 else
                      {"resolution": None, "actual_reward_inserted": False, "chains": []})
        parts = {"ordinary_acceptance": bool(accepted) and len(matching) == 1,
                 "native_resolution_true": settlement["resolution"] is True,
                 "actual_player_reward": settlement["actual_reward_inserted"]}
        result = {"parts": parts, "settlement": settlement,
                  "accepted_quest_ids": [quest["quest_id"] for quest in matching]}
        if fixture["case"] == "T12":
            activation = activation_sequences[matching[0]["quest_id"]] if len(matching) == 1 else journal[-1]["sequence"]
            combat = private.slay(journal, fixture["target_actor"], fixture["player"], own_samples,
                                  after=activation, run_id=fixture["run_id"])
            parts.update(native_target_death=combat["native_target_death"],
                         public_player_contribution=combat["player_contribution"])
            result["combat"] = combat
    result.update(same_actor=same_actor, dead=dead, success=same_actor and not dead and living(final)
                  and all(result["parts"].values()), fixture_version=fixture["definition"]["fixture_version"],
                  native_player_death_sequences=native_deaths,
                  private_journal=private.journal_tail(fixture["oracle_path"]))
    return result


async def mechanism(actor, fixture):
    raise ValueError("NPC contracts require normal-entry ordinary acceptance/exchange; no whole-task mechanism solver")
