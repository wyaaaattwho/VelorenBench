"""Additive resident-information navigation and pickup-triggered world changes."""
from __future__ import annotations

import asyncio
import json
import math
import re
import time
from uuid import uuid4

from . import crafting_recovery, navigation, scene, setup as native
from .oracles import pickup_actions

TASK_IDS = frozenset({"T01", "T02"})
SUPPLY = "common.items.food.apple"
FACILITY = {"town": "Liron", "root_xy": [26659, 4921],
            "bench": [26650, 4918, 144], "goal": [26650.5, 4920.5, 144]}
ARRIVAL = {"horizontal_m": 1, "vertical_m": .5, "speed_m_s": .1, "pair_seconds": .25}


def content_key(content):
    return (content or {}).get("Localized", {}).get("key")


def same_actor(opening, observation):
    return (observation.get("session_id") == opening["session_id"]
            and observation.get("self_identity") == opening["self_identity"])


def arrival_sample(opening, goal, row):
    return (same_actor(opening, row) and row.get("on_ground") is True
            and crafting_recovery.living(row)
            and math.dist(row["position"][:2], goal[:2]) <= ARRIVAL["horizontal_m"]
            and abs(row["position"][2] - goal[2]) <= ARRIVAL["vertical_m"]
            and math.sqrt(sum(v * v for v in row["velocity"])) <= ARRIVAL["speed_m_s"])


def settled_pair(opening, goal, samples):
    pair = samples[-2:]
    return bool(len(pair) == 2 and pair[1]["tick"] > pair[0]["tick"]
                and pair[1]["_elapsed_s"] - pair[0]["_elapsed_s"] >= ARRIVAL["pair_seconds"]
                and all(arrival_sample(opening, goal, row) for row in pair))


async def ask_workshop(actor, resident, *, origin="setup"):
    await native.action(actor, "context_interact", origin=origin,
                        operation="talk", target=resident["control_target"])
    current = await native.wait_observation(
        actor, lambda row: row.get("dialogue") is not None, "actual resident dialogue")
    prompts, receipts = [], []
    wanted = ["dialogue-question-directions", "dialogue-direction-workshop"]
    while wanted:
        prompt = current["dialogue"]
        prompts.append(prompt)
        action = {"sender_uid": prompt["sender_uid"], "dialogue_id": prompt["dialogue_id"],
                  "tag": prompt["tag"]}
        if prompt["kind"] == "Question":
            offered = next((row for row in prompt["responses"]
                            if content_key(row["content"]) == wanted[0]), None)
            if offered is None:
                raise RuntimeError("native resident does not offer the required directions branch")
            action.update(operation="respond", response_id=offered["response_id"])
            wanted.pop(0)
        else:
            since = current["survival"]["buff_clock"]["now"]
            await native.wait_observation(
                actor, lambda row: row["survival"]["buff_clock"]["now"] - since >= 1.5,
                "native statement acknowledgement guard interval")
            action["operation"] = "ack"
        receipts.append(await native.action(actor, "dialogue_action", origin=origin, **action))
        current = await native.wait_observation(
            actor, lambda row: row.get("dialogue") is not None
            and (row["dialogue"]["dialogue_id"], row["dialogue"]["tag"])
            != (prompt["dialogue_id"], prompt["tag"]), "native dialogue advancement")
    markers = [row for row in current["dialogue_markers"]
               if content_key(row["marker"].get("label")) == "hud-map-workshop"
               and row["dialogue_id"] == current["dialogue"]["dialogue_id"]]
    if len(markers) != 1:
        raise RuntimeError("native workshop branch lacks its actual received marker")
    prompt = current["dialogue"]
    since = current["survival"]["buff_clock"]["now"]
    await native.wait_observation(
        actor, lambda row: row["survival"]["buff_clock"]["now"] - since >= 1.5,
        "native statement acknowledgement guard interval")
    await native.action(actor, "dialogue_action", origin=origin, operation="ack",
                        sender_uid=prompt["sender_uid"], dialogue_id=prompt["dialogue_id"],
                        tag=prompt["tag"])
    follow = await native.wait_observation(
        actor, lambda row: row.get("dialogue") is None and any(
            event.get("event") == "dialogue" and event.get("data", {}).get("kind") == "End"
            and event["data"].get("dialogue_id") == prompt["dialogue_id"]
            for event in actor.native_events), "actual native resident dialogue End")
    if markers[0] not in follow["dialogue_markers"]:
        raise RuntimeError("native workshop marker did not persist past follow-on dialogue")
    return {"prompts": prompts, "receipts": receipts, "marker": markers[0], "follow_on": follow}


async def setup_facility(admin, actor, definition):
    await native.command(admin, "site", FACILITY["town"])
    town = await native.ready_observation(admin)
    before = len(admin.native_events)
    await native.command(admin, "rtsim_npc", "civilised,humanoid", 1)

    def search_reply(_):
        return next((row["data"].get("text") for row in admin.native_events[before:]
                     if row.get("event") == "chat" and row["data"].get("kind") == "command_info"
                     and "-- NPCs matching" in (row["data"].get("text") or "")), None)

    await native.wait_observation(admin, search_reply, "native nearest existing resident search")
    identifiers = re.findall(r"\((\d+)\),", search_reply(None))
    if len(identifiers) != 1:
        raise RuntimeError("native owner search did not identify one existing humanoid resident")
    rtsim_uid = int(identifiers[0])
    await native.command(admin, "rtsim_info", rtsim_uid)
    await native.command(admin, "rtsim_tp", rtsim_uid)

    def legal_resident(row):
        bindings = [entry for entry in private_dialogues({
            "private_oracle": str(admin.out / "private-native-oracle.ndjson")})
                    if entry["event"] == "loaded_actor_bindings"]
        native_uids = {entry["uid"] for entry in bindings[-1]["data"]["bindings"]
                       if entry.get("rtsim_uid") == rtsim_uid} if bindings else set()
        return next((entity for entity in row["nearby_entities"]
                     if entity["uid"] in native_uids and entity.get("body") == "Humanoid"
                     and entity.get("alias") is None
                     and entity.get("health", {}).get("current", 0) > 0), None)

    residents = await native.wait_observation(admin, legal_resident, "actual existing native resident binding")
    resident = legal_resident(residents)
    if math.dist(resident["position"][:2], town["position"][:2]) > 200:
        raise RuntimeError("nearest existing native resident is outside the selected settlement fixture")
    await native.goto(admin, admin, resident["position"], horizontal_tolerance=2)
    reference = await ask_workshop(admin, resident)
    marker = reference["marker"]["marker"]["wpos"]
    if [marker["x"], marker["y"]] != FACILITY["root_xy"]:
        raise RuntimeError("native resident selected a different actual workshop plot; fixture unqualified")
    await native.goto(admin, admin, FACILITY["goal"])
    facility = await native.ready_observation(admin)
    benches = [row for row in facility["nearby_interactable_sprites"]
               if row["sprite"] == "CraftingBench" and row["position"] == FACILITY["bench"]]
    if len(benches) != 1:
        raise RuntimeError("real native workshop bench differs from the qualified facility fixture")
    await native.goto(admin, admin, town["position"])
    reachability = []
    # The general long-distance qualifier drops probes onto roofs. This real
    # covered facility needs its actual street/floor level, not roof waypoints.
    for point in ([26624.5, 4918.5, 144], [26638.5, 4918.5, 144],
                  [26645.5, 4920.5, 144], FACILITY["goal"]):
        receipt = await native.action(admin, "move_to", position=point, tolerance=.6,
                                      speed=.5, timeout_ticks=1800)
        achieved = await native.ready_observation(admin)
        reached = (arrival_sample(achieved, point, achieved) if point == FACILITY["goal"]
                   else navigation.at_route_endpoint(achieved, point))
        if not reached:
            raise RuntimeError("native facility floor-level approach did not reach its endpoint")
        reachability.append({"goal": point, "receipt": receipt, "achieved": achieved,
                             "terrain": await native.qualify(admin, achieved["position"])})
    await native.command(admin, "rtsim_tp", rtsim_uid)
    current = await native.wait_observation(admin, legal_resident, "current existing native resident binding")
    resident = legal_resident(current)
    if actor.process is None:
        await actor.start()
        current = await native.wait_observation(admin, legal_resident, "current existing native resident binding")
        resident = legal_resident(current)
    await native.goto(admin, actor, resident["position"], horizontal_tolerance=2)
    opening = await native.ready_observation(actor)
    native.require_uninjured_start(opening)
    if opening["dialogue_markers"] or opening.get("dialogue") is not None:
        raise RuntimeError("T01 actor already received missing facility information before measurement")
    return {"case": "T01", "definition": definition, "task": definition["task"],
            "opening": opening, "start": opening["position"], "goal": FACILITY["goal"],
            "supplies": [], "resident": resident, "facility": FACILITY,
            "native_information_reference": reference, "native_facility_observation": facility,
            "native_resident_discovery": {"rtsim_uid": rtsim_uid, "owner_events": admin.native_events[before:]},
            "reachability": reachability, "private_oracle": str(admin.out / "private-native-oracle.ndjson")}


def route_layout():
    origin = [18700, 10800, 500]
    blocks = {(x, y, 0) for x in range(18) for y in range(11)}
    blocks.update((x, y, z) for x in range(9, 18) for y in range(11) for z in (1, 2))
    for corridor in (range(0, 3), range(5, 8)):
        blocks.update((7, y, 1) for y in corridor)
        blocks.update((8, y, z) for y in corridor for z in (1, 2))
    blocks.update((9, y, z) for y in (*range(3, 5), *range(8, 11)) for z in range(3, 7))
    event = {(9, y, z) for y in range(5, 8) for z in range(3, 7)}

    def absolute(point):
        return [origin[i] + point[i] for i in range(3)]

    return {"origin": origin, "voxels": [absolute(p) for p in sorted(blocks)],
            "event_voxels": [absolute(p) for p in sorted(event)],
            "start": absolute([1.5, 6.5, 1]), "supply": absolute([2.5, 6.5, 1]),
            "goal": absolute([15.5, 6.5, 3]),
            "alternative": [absolute(p) for p in (
                [5.5, 1.5, 1], [7.5, 1.5, 2], [8.5, 1.5, 3],
                [11.5, 1.5, 3], [15.5, 6.5, 3])],
            "design": "two-metre raised destination; central stair passage and north-side stairs; four-metre occluder"}


async def build_permission(admin, voxels):
    low = [min(p[i] for p in voxels) for i in range(3)]
    high = [max(p[i] for p in voxels) + 1 for i in range(3)]
    area = "navigation_" + uuid4().hex[:12]
    await native.command(admin, "area_add", area, "build",
                         low[0], high[0], low[1], high[1], low[2], high[2])
    await native.command(admin, "permit_build", area)
    observed = await admin.request("observe")
    if not (observed["character_controls"].get("can_build") or {}).get("enabled"):
        await native.command(admin, "build")
    await native.wait_observation(
        admin, lambda row: (row["character_controls"].get("can_build") or {}).get("enabled") is True,
        "private owner-only terrain permission")


async def build_batch(admin, voxels):
    receipts = []
    for point in voxels:
        receipts.append(await native.action(
            admin, "build_block", operation="place", position=point, expected_kind="Air",
            new_kind="Rock", color_rgb=[160, 160, 160]))
        events = await admin.request("events")
        admin.journal.write(origin="private_terrain_authoring", drained_native_events=events)
    return receipts


async def verify_blocks(admin, voxels):
    expected, observed = {tuple(p) for p in voxels}, set()
    evidence = []
    while expected - observed:
        point = min(expected - observed)
        await native.command(admin, "goto", point[0] + .5, point[1] + .5, point[2] + 1)
        row = await admin.request("observe")
        matched = {tuple(block["position"]) for block in row["nearby_blocks"]
                   if block.get("kind") == "Rock" and block.get("solid") is True} & expected
        if tuple(point) not in matched:
            raise RuntimeError(f"submitted terrain change has no exact native collision-block readback: {point}")
        observed |= matched
        evidence.append({"position": row["position"], "tick": row["tick"],
                         "matched_voxels": [list(p) for p in sorted(matched)]})
    return {"verified_voxels": [list(p) for p in sorted(observed)], "observations": evidence}


async def setup_route(admin, actor, definition):
    layout = route_layout()
    initialization = await native.isolate_initial_spawn(admin, actor)
    await native.author_voxels(admin, [layout["voxels"][0]])
    await build_permission(admin, layout["voxels"] + layout["event_voxels"])
    construction = await build_batch(admin, layout["voxels"][1:])
    readback = await verify_blocks(admin, layout["voxels"])
    await native.goto(admin, actor, layout["start"])
    cleanup = await native.clean_inventory(admin, actor)
    route = []
    await native.goto(admin, admin, layout["start"])
    for point in layout["alternative"]:
        receipt = await native.action(admin, "move_to", position=point, tolerance=.6, timeout_ticks=1800)
        achieved = await native.ready_observation(admin)
        if math.dist(achieved["position"], point) > 1:
            raise RuntimeError("native alternative route did not reach its qualified endpoint")
        route.append({"goal": point, "receipt": receipt, "achieved": achieved})
    supply = await native.drop_supply(admin, layout["supply"], SUPPLY, 1, "flat")
    await native.goto(admin, actor, layout["start"])
    opening = await native.ready_observation(actor)
    native.require_uninjured_start(opening)
    if scene.counts(opening).get(SUPPLY, 0):
        raise RuntimeError("route-change actor already holds the designated supply at opening")
    goal = layout["goal"]
    task = (definition["task"] + f" Public destination: native x={goal[0]}, y={goal[1]}, "
            f"z={goal[2]} (height).")
    return {"case": "T02", "definition": definition, "task": task, "opening": opening,
            "start": layout["start"], "goal": goal, "supplies": [supply],
            "layout": layout, "initialization": initialization, "cleanup_supplies": cleanup,
            "construction": construction, "native_geometry_readback": readback,
            "alternative_reachability": route,
            "event_state": {"status": "armed", "trigger_action_ids": [], "applications": 0,
                            "applied_monotonic": None, "time_visible": None,
                            "exposure": "not-demonstrated", "public_witnesses": []}}


async def setup(admin, actor, definition, terrain):
    if definition["id"] == "T01" and terrain == "natural":
        return await setup_facility(admin, actor, definition)
    if definition["id"] == "T02" and terrain == "flat":
        return await setup_route(admin, actor, definition)
    raise ValueError("unqualified additive navigation fixture selection")


async def update_event(admin, actor, fixture, observation):
    if fixture["case"] != "T02":
        return
    state, supply = fixture["event_state"], fixture["supplies"][0]
    if state["status"] != "armed":
        return
    ids = pickup_actions(fixture["opening"], actor.measured_receipts,
                         supply["uid"], supply["item"], supply["amount"])
    if not ids:
        return
    state.update(status="applying", trigger_action_ids=ids, trigger_monotonic=time.monotonic(),
                 actor_observation=observation, applications=state["applications"] + 1)
    admin.journal.write(origin="private_world_event", event="route_change_triggered", **state)
    current = await actor.request("observe", origin="judge")
    blocks = fixture["layout"]["event_voxels"]
    if any(math.dist(current["position"][:2], [p[0] + .5, p[1] + .5]) < 2 for p in blocks):
        raise RuntimeError("runtime route event cannot safely apply: actor intersects the closure region")
    state["submissions"] = await build_batch(admin, blocks)
    state["native_readback"] = await verify_blocks(admin, blocks)
    # The unchanged alternative was physically walked before opening. Recheck its
    # supports after mutation; its entire corridor is disjoint from the closure.
    supports = []
    for point in fixture["layout"]["alternative"]:
        await native.goto(admin, admin, point)
        supports.append(await native.qualify(admin, point))
    state.update(status="applied", applied_monotonic=time.monotonic(),
                 alternative_supports_after=supports)
    admin.journal.write(origin="private_world_event", event="route_change_applied", **state)


def public_witness(fixture, request, response):
    if (fixture["case"] != "T02" or response.get("ok") is not True
            or response.get("session_id") != fixture["opening"]["session_id"]):
        return
    state = fixture["event_state"]
    if state["status"] not in {"applying", "applied"} or request.get("cmd") not in {"observe", "inspect_terrain"}:
        return
    expected = {tuple(p) for p in fixture["layout"]["event_voxels"]}
    found = set()

    def visit(value):
        if isinstance(value, dict):
            point = value.get("position")
            if (isinstance(point, list) and len(point) == 3
                    and all(type(v) in (int, float) for v in point)
                    and tuple(point) in expected and value.get("kind") == "Rock"
                    and value.get("solid") is True):
                found.add(tuple(point))
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(response.get("result"))
    if found:
        stamp = time.monotonic()
        state["time_visible"] = state["time_visible"] or stamp
        state["exposure"] = "public-changed-voxel-observed"
        state["public_witnesses"].append({
            "request_id": request.get("id"), "command": request["cmd"], "monotonic": stamp,
            "session_id": response["session_id"], "changed_voxels": [list(p) for p in sorted(found)],
            "interpretation": "normal structured geometry delivered; not a replanning or collision claim"})


def private_dialogues(fixture):
    from pathlib import Path
    payload = Path(fixture["private_oracle"]).read_bytes()
    lines = payload.split(b"\n")
    pending = lines.pop()
    fixture["private_oracle_snapshot"] = {
        "complete_records": len(lines), "pending_tail_bytes": len(pending),
        "framing": "newline commits a native record; incomplete tail is not evidence"}
    return [json.loads(line) for line in lines]


def event_qualified(fixture, pickups):
    state = fixture["event_state"]
    expected = {tuple(p) for p in fixture["layout"]["event_voxels"]}
    observed = {tuple(p) for p in state.get("native_readback", {}).get("verified_voxels", [])}
    receipts = state.get("submissions", [])
    return (state["status"] == "applied" and state["applications"] == 1
            and bool(set(state["trigger_action_ids"]) & set(pickups)) and observed == expected
            and len(receipts) == len(expected)
            and len({row.get("action_id") for row in receipts}) == len(expected)
            and all(row.get("status") == "completed" and row.get("action_type") == "build_block"
                    and tuple(row.get("detail", {}).get("position", [])) in expected
                    for row in receipts)
            and len(state.get("alternative_supports_after", [])) == len(fixture["layout"]["alternative"]))


def score(fixture, final, receipts, events, samples):
    opening, case = fixture["opening"], fixture["case"]
    own_samples = [row for row in samples if same_actor(opening, row)]
    dead = any(crafting_recovery.died(row) for row in own_samples)
    arrival = settled_pair(opening, fixture["goal"], samples)
    parts = {"alive_arrival": arrival}
    detail = {}
    if case == "T01":
        talks = [row for row in crafting_recovery.own_actions(opening, receipts, "context_interact")
                 if row["action"].get("operation") == "talk"]
        answers = crafting_recovery.own_actions(opening, receipts, "dialogue_action")
        dialogue = [row["data"] for row in events if row.get("event") == "dialogue"
                    and row.get("session_id") == opening["session_id"]]
        questions = [row for row in dialogue if row.get("kind") == "Question"
                     and content_key(row.get("content")) == "npc-question-directions"]
        public_clues, qualified = [], []
        for question in questions:
            offered = {row["response_id"] for row in question["responses"]
                       if content_key(row["content"]) == "dialogue-direction-workshop"}
            responses = [row for row in answers if row["action"].get("operation") == "respond"
                         and row["action"].get("sender_uid") == question["sender_uid"]
                         and row["action"].get("dialogue_id") == question["dialogue_id"]
                         and row["action"].get("tag") == question["tag"]
                         and row["action"].get("response_id") in offered]
            target = question.get("sender")
            if not responses or not any(row["action"].get("target") == target for row in talks):
                continue
            for marker in final.get("dialogue_markers", []):
                native_marker = marker.get("marker") or {}
                point = native_marker.get("wpos") or {}
                private = any(row.get("event") == "validated_dialogue"
                              and row["data"].get("sender_uid") == question["sender_uid"]
                              and row["data"].get("recipient_uid") == opening["self_identity"]["uid"]
                              and row["data"]["dialogue"].get("id") == question["dialogue_id"]
                              and isinstance(row["data"]["dialogue"].get("kind"), dict)
                              and row["data"]["dialogue"].get("kind", {}).get("Marker") == native_marker
                              for row in private_dialogues(fixture))
                public = (marker.get("source") == "validated_server_dialogue"
                        and marker.get("dialogue_id") == question["dialogue_id"]
                        and marker.get("sender") == target
                        and content_key(native_marker.get("label")) == "hud-map-workshop"
                        and [point.get("x"), point.get("y")] == fixture["facility"]["root_xy"])
                if public:
                    public_clues.append(marker)
                if public and private:
                    qualified.append(marker)
        parts.update(asked_resident=bool(talks), received_actual_facility_information=bool(public_clues))
        detail.update(public_clue_receipts=public_clues, native_clue_receipts=qualified,
                      oracle_validity="unknown" if public_clues and not qualified else "valid",
                      private_marker_recipient_validated=bool(qualified),
                      talked_action_ids=[row["action"]["action_id"] for row in talks],
                      clue_scope="actual native workshop selection, received persistent marker and separate private recipient validation")
    elif case == "T02":
        supply = fixture["supplies"][0]
        pickups = pickup_actions(opening, receipts, supply["uid"], SUPPLY, 1)
        state = fixture["event_state"]
        parts.update(picked_up=bool(pickups), retained=scene.counts(final).get(SUPPLY, 0) >= 1,
                     runtime_event_qualified=event_qualified(fixture, pickups))
        detail.update(pickup_action_ids=pickups, route_event={
            key: state[key] for key in ("status", "applications", "trigger_action_ids",
                                       "applied_monotonic", "time_visible", "exposure")},
            interpretation="arrival with actual supply retention; no mandated route, collision or subjective replanning")
    else:
        raise ValueError("unknown additive navigation task")
    return {"success": same_actor(opening, final) and not dead
            and detail.get("oracle_validity") != "unknown"
            and crafting_recovery.living(final) and all(parts.values()), "dead": dead,
            "arrival": arrival, "same_actor": same_actor(opening, final), "parts": parts, **detail}


async def mechanism(actor, fixture):
    raise ValueError("additive navigation has no whole-task solution tool; qualify via ordinary public actions")


def finalize_environment(record):
    if record.get("scene") == "T01" and record.get("score", {}).get("oracle_validity") == "unknown":
        evaluation = record["evaluation_validity"]
        if evaluation["status"] != "invalid":
            evaluation["status"] = "unknown"
        evaluation["reasons"] = list(dict.fromkeys([
            *evaluation["reasons"], "private_facility_marker_join_unavailable"]))
        evaluation["accepted"] = False
        record.update(success=False, capability_claim=False)
        return
    if record.get("scene") != "T02" or "world_event" not in record:
        return
    state = record["world_event"]
    evaluation = record["evaluation_validity"]
    reason = None
    if (state["status"] not in {"armed", "applied"}
            or state["status"] == "applied" and not record.get("score", {}).get(
                "parts", {}).get("runtime_event_qualified")):
        reason = "world_event_application_unqualified"
    elif (state["status"] == "applied" and state["time_visible"] is None
          and not record.get("score", {}).get("success")):
        reason = "world_event_exposure_not_demonstrated"
    if reason:
        if evaluation["status"] != "invalid":
            evaluation["status"] = "unknown"
        evaluation["reasons"] = list(dict.fromkeys([*evaluation["reasons"], reason]))
        evaluation["accepted"] = False
        record.update(success=False, capability_claim=False)
