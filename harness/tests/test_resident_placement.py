"""Exercise facility placement with real readiness/footing/strict-goto helpers."""
import asyncio
import copy
import json
import math
from types import SimpleNamespace

import pytest

from scripts.rpg_benchmark import navigation_tasks as tasks


class ResidentWorld:
    def __init__(self, tmp_path):
        self.clock = 0
        self.stage = 0
        self.samples = 0
        self.native_uid = 54
        self.gotos = []
        self.references = []
        self.terrain_error = False
        self.unloaded = False
        self.never_grounded = False
        self.missing_support = False
        self.first_already_grounded = False
        self.walk_on_load = False
        self.walk_offset = 0
        self.player_load_walk = False
        self.player_visits = 0
        self.invalid = None
        self.admin = self.actor("admin", tmp_path, [26608, 4912, 144])
        self.player = self.actor("player", tmp_path, [18704, 10800, 381])
        self.player.process = None
        self.bind()

    def actor(self, name, out, point):
        actor = SimpleNamespace(name=name, out=out, native_events=[], process=object(),
                                point=list(point), falling=0)

        async def start():
            actor.process = object()
            # Starting the deferred client takes time: an ECS unload/reload may
            # change native UID while preserving the selected RTSim identity.
            self.native_uid = 78
            self.bind()

        async def request(command, args=None):
            if command == "observe":
                return self.observe(actor)
            assert command == "inspect_terrain"
            if self.terrain_error:
                raise RuntimeError("terrain transport failed")
            point = args["position"]
            return {"status": "complete", "support_candidates": [] if self.missing_support else [{
                "occupiable": True, "standing_position": [*point[:2], 144],
                "support": {"kind": "Earth", "solid": True}}]}

        actor.start, actor.request = start, request
        return actor

    def bind(self):
        path = self.admin.out / "private-native-oracle.ndjson"
        path.write_text(json.dumps({"event": "rtsim_population", "data": {
            "clock": "server RTSim tick/time-of-day; not a client-update counter",
            "settlement_bootstrap": {"last_maintenance_tick": 32,
                                     "allocated_deficit": 0, "eligible_pending": 0, "ready": True}}}) + "\n"
            + json.dumps({"event": "loaded_actor_bindings", "data": {"bindings": [
            {"rtsim_uid": 2203, "uid": self.native_uid, "civilised": True, "body": "Humanoid",
             "alive": True, "on_ground": True,
             "server_position": {"x": 26696 + self.walk_offset, "y": 4790.4, "z": 144}},
            {"rtsim_uid": 9999, "uid": 999}]}}) + "\n")

    def observe(self, actor):
        self.clock += .02
        self.samples += 1
        if actor.falling:
            actor.falling -= 1
            actor.point[2] = 180 if actor.falling else 144
        if self.walk_on_load and self.stage == 1 and self.samples >= 3:
            self.walk_offset = 40
        if (self.player_load_walk and actor is self.player
                and self.player_visits == 1 and self.samples >= 3):
            self.walk_offset = 40
        resident = {
            "uid": self.native_uid, "body": "Humanoid", "alias": None,
            "health": {"current": 100}, "position": [
                26696 + self.walk_offset + .01 * self.samples, 4790.4,
                (144 if self.stage == 1 and self.first_already_grounded else
                 248.545 if self.stage and self.samples < 5 else 144.636)],
            "control_target": {"uid": self.native_uid, "lifecycle": f"ecs-{self.native_uid}"}}
        if self.invalid:
            resident.update(self.invalid)
        other = {**resident, "uid": 999, "control_target": {"uid": 999, "lifecycle": "unrelated"}}
        return {
            "position": list(actor.point), "velocity": [0, 0, -8] if actor.falling else [0, 0, 0],
            "on_ground": not actor.falling and not self.never_grounded,
            "tick": self.samples, "session_id": actor.name,
            "self_identity": {"uid": 1 if actor is self.admin else 2},
            "local_geometry": {"geometry": {"cells": [
                {"occupancy": "unknown" if self.unloaded else "open"}]}},
            "survival": {"health": {"current": 100}, "buffs": []},
            "nearby_entities": [entry for entry in (other, resident)
                                if math.dist(actor.point, entry["position"]) <= 32],
            "nearby_interactable_sprites": [{
                "sprite": "CraftingBench", "position": tasks.FACILITY["bench"]}],
            "dialogue": None, "dialogue_markers": []}

    async def command(self, admin, name, *args):
        target = admin
        if name == "sudo":
            assert args[0] == "player"
            target, name, args = self.player, args[1], args[2:]
        if name == "rtsim_npc":
            admin.native_events.append({"event": "chat", "data": {
                "kind": "command_info", "text": "-- NPCs matching (2203),"}})
        elif name == "rtsim_tp":
            assert args == (2203,)
            self.stage += 1
            if target is self.player:
                self.player_visits += 1
            self.samples = 0
            target.point = [26696 + self.walk_offset, 4790, 228]
            target.falling = 3
        elif name == "goto":
            actor = target
            point = list(args)
            self.gotos.append((self.stage, actor.name, point))
            actor.point = point
            actor.falling = 3 if point[2] != 144 else 0
        else:
            assert name in {"site", "rtsim_info"}
        return {}

    async def action(self, actor, kind, **args):
        assert kind == "move_to"
        actor.point = list(args["position"])
        return {}

    async def reference(self, actor, resident, **kwargs):
        assert resident["uid"] == self.native_uid != 999
        assert kwargs["rtsim_uid"] == 2203
        assert resident["position"][2] in {144, 144.636}
        self.references.append(copy.deepcopy(resident))
        return {"marker": {"marker": {"wpos": dict(zip(("x", "y"), tasks.FACILITY["root_xy"]))}}}

    async def sleep(self, seconds):
        self.clock += seconds

    def install(self, monkeypatch):
        monkeypatch.setattr(tasks.native, "command", self.command)
        monkeypatch.setattr(tasks.native, "action", self.action)
        monkeypatch.setattr(tasks, "ask_workshop", self.reference)
        monkeypatch.setattr(tasks.native.asyncio, "sleep", self.sleep)
        monkeypatch.setattr(tasks.native.time, "monotonic", lambda: self.clock)


def test_both_facility_placements_use_human_footing_and_fresh_same_rtsim_binding(tmp_path, monkeypatch):
    world = ResidentWorld(tmp_path)
    world.install(monkeypatch)
    fixture = asyncio.run(tasks.setup_facility(world.admin, world.player, {"task": "original puzzle"}))
    assert len(world.references) == 1 and world.references[0]["uid"] == 54
    assert fixture["resident"]["uid"] == 78
    assert fixture["native_resident_discovery"]["rtsim_uid"] == 2203
    assert fixture["start"][2] == 144 != fixture["resident"]["position"][2]
    assert fixture["goal"] == tasks.FACILITY["goal"]
    for actor in ("admin", "player"):
        points = [p for _, a, p in world.gotos if a == actor and p[2] == 144]
        assert points, (actor, world.gotos)
    assert len(fixture["resident_placements"]) == 2
    for placement in fixture["resident_placements"]:
        assert placement["standing"]["on_ground"]
        assert placement["standing"]["position"][2] == 144
        assert placement["current"]["nearby_entities"][-1] == placement["resident"]


@pytest.mark.parametrize("invalid", [
    {"alias": "player"}, {"body": "BirdMedium"}, {"health": {"current": 0}},
    {"uid": 1000}, {"position": [27000, 4790, 144]},
])
def test_illegal_or_outside_settlement_resident_is_not_replaced(tmp_path, monkeypatch, invalid):
    world = ResidentWorld(tmp_path)
    world.invalid = invalid
    world.install(monkeypatch)
    with pytest.raises(RuntimeError):
        asyncio.run(tasks.setup_facility(world.admin, world.player, {"task": "unchanged"}))
    assert not world.references
    assert world.player.process is None


def test_footing_transport_error_is_not_ignored(tmp_path, monkeypatch):
    world = ResidentWorld(tmp_path)
    world.install(monkeypatch)
    world.terrain_error = True
    with pytest.raises(RuntimeError, match="terrain transport failed"):
        asyncio.run(tasks.setup_facility(world.admin, world.player, {"task": "unchanged"}))
    assert not world.references


def test_original_strict_goto_still_rejects_floating_npc_height(tmp_path, monkeypatch):
    world = ResidentWorld(tmp_path)
    world.install(monkeypatch)
    with pytest.raises(RuntimeError, match="goto"):
        asyncio.run(tasks.native.goto(world.admin, world.admin, [26696, 4790, 144.636],
                                     horizontal_tolerance=2))


def test_final_qualified_same_resident_window_is_consumed_without_redundant_future_wait(tmp_path, monkeypatch):
    world = ResidentWorld(tmp_path)
    world.install(monkeypatch)
    original_observe, original_qualify = world.observe, tasks.native.qualify
    qualified = False
    observed_after_qualification = []

    async def qualify(actor, point):
        nonlocal qualified
        result = await original_qualify(actor, point)
        qualified = bool(world.gotos)
        return result

    def observe(actor):
        if qualified:
            observed_after_qualification.append(actor.name)
            world.walk_offset = 20  # Ordinary movement after a genuine qualified in-range window.
        return original_observe(actor)

    monkeypatch.setattr(tasks.native, "qualify", qualify)
    world.observe = observe
    resident, proof = asyncio.run(tasks.place_near_resident(
        world.admin, world.admin, 2203, {"position": [26608,4912,144]}))
    assert world.gotos and world.gotos[-1][2][2] == 144  # Strict Human-standing goto still ran.
    assert proof["current"] is proof["opening"]
    assert resident["uid"] == 54
    assert math.dist(proof["current"]["position"], resident["position"]) < 5
    assert not observed_after_qualification


def test_moving_resident_does_not_require_second_exact_overlap_and_velocity_decay(tmp_path, monkeypatch):
    """Native005: second exact RTSim overlap leaves -2.272m/s until resident escapes."""
    world = ResidentWorld(tmp_path)
    world.first_already_grounded = True
    world.install(monkeypatch)
    original_observe = world.observe
    walked = 0

    def observe(actor):
        nonlocal walked
        if actor is world.player:
            walked += 1.4
            world.walk_offset = walked
        row = original_observe(actor)
        if actor is world.player and world.player_visits >= 2 and world.samples < 15:
            row["velocity"] = [0, -2.27209210395813, 0]
        return row

    world.observe = observe
    resident, proof = asyncio.run(tasks.place_near_resident(
        world.admin, world.player, 2203, {"position": [26608,4912,144]}))
    assert world.player_visits == 1  # Already loaded Human window needs no second collision.
    assert resident["uid"] == 78 and proof["opening"]["on_ground"]
    assert math.dist(proof["current"]["position"], resident["position"]) < 5
    assert proof["target"] in [p["standing_position"] for p in proof["placement_support"]["support_candidates"]]
    assert world.gotos[-1][2] == proof["target"] and proof["target"][2] == 144


@pytest.mark.parametrize("failure", ["unloaded", "no_support", "wood", "transport"])
def test_nonoverlap_target_requires_actual_loaded_human_support_before_strict_goto(tmp_path, monkeypatch, failure):
    world = ResidentWorld(tmp_path)
    world.first_already_grounded = True
    world.install(monkeypatch)
    original_request = world.admin.request
    queries = []

    async def request(command, args=None):
        result = await original_request(command, args)
        if command == "inspect_terrain" and args["position"][:2] != world.admin.point[:2]:
            queries.append(args["position"])
            if failure == "transport":
                raise RuntimeError("resident footing transport failed")
            if failure == "unloaded":
                result["status"] = "unloaded_cells"
            elif failure == "no_support":
                result["support_candidates"] = []
            else:
                result["support_candidates"][0]["support"]["kind"] = "Wood"
        return result

    world.admin.request = request
    with pytest.raises(RuntimeError, match="terrain|footing"):
        asyncio.run(tasks.place_near_resident(
            world.admin, world.admin, 2203, {"position": [26608,4912,144]}))
    assert len(queries) == (1 if failure in {"unloaded", "transport"} else 4)
    assert not world.gotos  # Read-only alternatives; never retry an unconfirmed mutation.


@pytest.mark.parametrize("failure", ["unloaded", "never_grounded", "missing_support"])
def test_placement_requires_loaded_settled_qualified_admin_before_player_start(tmp_path, monkeypatch, failure):
    world = ResidentWorld(tmp_path)
    world.install(monkeypatch)
    setattr(world, failure, True)
    with pytest.raises(RuntimeError):
        asyncio.run(tasks.place_near_resident(
            world.admin, world.admin, 2203, {"position": [26608, 4912, 144]}))
    assert world.player.process is None
    assert not world.gotos


def test_later_placement_rejects_changed_binding_instead_of_using_old_resident(tmp_path, monkeypatch):
    world = ResidentWorld(tmp_path)
    world.install(monkeypatch)

    async def start_without_matching_binding():
        world.player.process = object()
        world.native_uid = 78
        world.bind()
        world.native_uid = 54  # Only stale/unrelated client UIDs are visible.

    world.player.start = start_without_matching_binding
    with pytest.raises(RuntimeError, match="current existing native resident binding"):
        asyncio.run(tasks.place_near_resident(
            world.admin, world.player, 2203, {"position": [26608, 4912, 144]}))
    assert not [point for stage, actor, point in world.gotos if actor == "player"]


def test_later_airborne_placement_is_independently_qualified(tmp_path, monkeypatch):
    world = ResidentWorld(tmp_path)
    world.first_already_grounded = True
    world.install(monkeypatch)
    fixture = asyncio.run(tasks.setup_facility(world.admin, world.player, {"task": "original puzzle"}))
    assert fixture["resident"]["uid"] == 78 and fixture["start"][2] == 144
    assert fixture["resident_placements"][1]["standing"]["on_ground"]


def test_placement_does_not_sky_probe_onto_roof_above_resident(tmp_path, monkeypatch):
    world = ResidentWorld(tmp_path)
    world.install(monkeypatch)

    async def roof_probe(*args, **kwargs):
        raise AssertionError("A sky landing can select the roof, not the resident's street.")

    monkeypatch.setattr(tasks.native, "settled_point", roof_probe)
    resident, placement = asyncio.run(tasks.place_near_resident(
        world.admin, world.admin, 2203, {"position": [26608, 4912, 144]}))
    assert resident["uid"] == 54 and placement["standing"]["position"][2] == 144


def test_live_binding_decodes_latest_complete_snapshot_not_entire_walk_history(tmp_path, monkeypatch):
    world = ResidentWorld(tmp_path)
    path = world.admin.out / "private-native-oracle.ndjson"
    old = path.read_text()
    world.native_uid = 78
    world.bind()
    latest = path.read_text()
    history = "".join(json.dumps({"event": "movement", "data": {"tick": i}}) + "\n"
                      for i in range(1000))
    path.write_text(old + history + latest + '{"event":"loaded_actor_bindings","data":')
    real_loads, decoded = json.loads, []

    def loads(raw):
        row = real_loads(raw)
        decoded.append(row)
        return row

    monkeypatch.setattr(tasks.json, "loads", loads)
    world.admin.point = [26696, 4790, 144]
    current = world.observe(world.admin)
    selected = tasks.legal_resident(world.admin, 2203, {"position": [26608, 4912, 144]}, current)
    assert selected["uid"] == 78
    assert len(decoded) == 1 and decoded[0]["data"]["bindings"][0]["uid"] == 78
    # Full-history scoring still sees ALL complete records and no partial tail.
    fixture = {"private_oracle": str(path)}
    assert len(tasks.private_dialogues(fixture)) == 1004  # Both binding and population records, twice.
    assert fixture["private_oracle_snapshot"]["pending_tail_bytes"] > 0


def test_resident_walking_out_of_initial_loaded_view_is_reacquired_by_same_rtsim(tmp_path, monkeypatch):
    world = ResidentWorld(tmp_path)
    world.walk_on_load = True
    world.install(monkeypatch)
    resident, placement = asyncio.run(tasks.place_near_resident(
        world.admin, world.admin, 2203, {"position": [26608, 4912, 144]}))
    assert world.walk_offset == 40 and resident["uid"] == 54
    assert placement["standing"]["position"][0] == 26736
    assert placement["standing"]["on_ground"]
    assert math.dist(placement["current"]["position"], resident["position"]) < 5


def test_deferred_player_own_cold_terrain_load_precedes_fresh_resident_placement(tmp_path, monkeypatch):
    world = ResidentWorld(tmp_path)
    world.player_load_walk = True
    world.install(monkeypatch)
    resident, placement = asyncio.run(tasks.place_near_resident(
        world.admin, world.player, 2203, {"position": [26608, 4912, 144]}))
    assert world.player_visits == 2 and world.walk_offset == 40
    assert resident["uid"] == 78 and placement["opening"]["on_ground"]
    assert placement["standing"]["session_id"] == "player"
    assert math.dist(placement["current"]["position"], resident["position"]) < 5


@pytest.mark.parametrize("changed_source", [False, True])
@pytest.mark.parametrize("stay_near", [False, True])
def test_private_reference_repositions_only_admin_before_native_dialogue_requests(
        monkeypatch, changed_source, stay_near):
    actor = SimpleNamespace(native_events=[])
    resident = {"uid": 7, "control_target": {"uid": 7, "lifecycle": "original-speaker"}}
    town = {"position": [26608, 4912, 144]}
    marker = {"dialogue_id": 10, "marker": {
        "label": {"Localized": {"key": "hud-map-workshop"}},
        "wpos": dict(zip(("x", "y"), tasks.FACILITY["root_xy"]))}}
    state = {"dialogue_markers": [], "survival": {"buff_clock": {"now": 1}, "health": {"current": 100}}}
    distance, placements, sent = 1, [], []

    async def observe(command):
        assert command == "observe"
        return {**copy.deepcopy(state), "position": [0,0,0], "velocity": [0,0,0], "on_ground": True}

    def legal(*args):
        current = {**copy.deepcopy(resident), "position": [distance,0,0]}
        if stay_near and changed_source:
            current["control_target"]["lifecycle"] = "different-speaker"
        return current

    async def qualify(participant, point):
        assert participant is actor and point == [0,0,0] and distance < 5
        return {"scope": "test qualified current footing"}

    actor.request = observe

    def question(tag, key):
        return {"sender_uid": 7, "dialogue_id": 10, "tag": tag, "kind": "Question",
                "responses": [{"response_id": tag + 1, "content": {"Localized": {"key": key}}}]}

    async def place(admin, participant, rtsim_uid, selected_town):
        nonlocal distance
        assert admin is participant is actor and rtsim_uid == 2203 and selected_town == town
        distance = 1
        fresh = copy.deepcopy(resident)
        if changed_source:
            fresh["control_target"]["lifecycle"] = "different-speaker"
        proof = {"rtsim_uid": rtsim_uid, "resident": fresh}
        placements.append(proof)
        return fresh, proof

    async def action(participant, kind, **args):
        nonlocal distance
        assert participant is actor
        if kind == "context_interact":
            state["dialogue"] = question(0, "dialogue-question-directions")
            distance = 1 if stay_near else 6  # Normal resident can remain nearby or walk away.
        else:
            assert distance < 5, "the actual native range must not be weakened"
            sent.append(copy.deepcopy(args))
            if args["operation"] == "ack":
                state["dialogue"] = None
            elif args["response_id"] == 1:
                state["dialogue"] = question(1, "dialogue-direction-workshop")
            else:
                state["dialogue"] = {"sender_uid": 7, "dialogue_id": 10,
                                     "tag": 2, "kind": "Statement"}
                state["dialogue_markers"] = [marker]
            distance = 1 if stay_near else 6
        return {}

    async def wait(participant, predicate, reason):
        nonlocal distance
        assert participant is actor
        if reason == "native statement acknowledgement guard interval":
            assert not predicate(state)
            state["survival"]["buff_clock"]["now"] += 1.5
            distance = 1 if stay_near else 10
        if reason == "actual native resident dialogue End":
            assert not predicate(state)
            actor.native_events.append({"event": "dialogue", "data": {
                "kind": "End", "dialogue_id": 10}})
        assert predicate(state)
        return copy.deepcopy(state)

    monkeypatch.setattr(tasks, "place_near_resident", place)
    monkeypatch.setattr(tasks, "legal_resident", legal)
    monkeypatch.setattr(tasks.native, "qualify", qualify)
    monkeypatch.setattr(tasks.native, "action", action)
    monkeypatch.setattr(tasks.native, "wait_observation", wait)
    if changed_source:
        with pytest.raises(RuntimeError, match="speaker changed"):
            asyncio.run(tasks.ask_workshop(actor, resident, rtsim_uid=2203, town=town))
        assert not sent
    else:
        result = asyncio.run(tasks.ask_workshop(actor, resident, rtsim_uid=2203, town=town))
        assert len(sent) == 3
        assert len(placements) == (0 if stay_near else 3)
        assert len(result["source_checks"]) == (3 if stay_near else 0)
        assert [row["operation"] for row in sent] == ["respond", "respond", "ack"]
        assert all(row["sender_uid"] == 7 and row["dialogue_id"] == 10 for row in sent)
        assert result["source_placements"] == placements
        assert result["marker"] in result["follow_on"]["dialogue_markers"]
