import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from scripts.rpg_benchmark import npc_contracts as task


@pytest.mark.parametrize("mutation", [None, "capture", "stale", "resident", "readback", "placement",
                                     "unloaded", "distance", "offer", "dead", "health", "slots", "quest"])
def test_actual_v6_validate_opening_retains_common_and_capture_guards(mutation):
    target = {"Npc": {"idx": 3, "version": 1}}
    giver = {"Npc": {"idx": 1, "version": 1}}
    player = {"actor": {"Character": 16}, "uid": 10, "server_entity": "player"}
    binding = {"actor": target, "uid": 40, "rtsim_uid": 2, "server_entity": "target",
               "server_position": {"x": 48, "y": 0, "z": 0},
               "rtsim_position": {"x": 48, "y": 0, "z": 0}}
    giver_binding = {**binding, "actor": giver, "uid": 7, "rtsim_uid": 0, "server_entity": "giver",
                     "server_position": {"x": 0, "y": 0, "z": 0},
                     "rtsim_position": {"x": 0, "y": 0, "z": 0}}
    offer = {"kind": "Question"}
    opening = {"survival": {"health": {"current": 100, "dead": False}},
               "inventory": {"free_slots": 3}, "self_identity": {"uid": 10},
               "dialogue": offer, "position": [0, 0, 0], "session_id": "test",
               "control_actor": {"uid": 10, "lifecycle": "player"}}
    before = {"sequence": 1, "target_actor": copy.deepcopy(binding), "giver_actor": giver_binding}
    fixture = {"case": "T12", "offer": offer, "target_actor": target, "giver_actor": giver,
               "definition": {"fixture_version": "t12-native-slay-v6"},
               "offer_captures": [{"offer": offer, "consistent": True, "before": before, "after": before}],
               "slay_site": {"target_placement": {"actor": target, "before_binding": binding},
                             "target_readback": {"rtsim_uid": 2, "position": [48, 0, 0], "events": [
                                 {"data": {"kind": "command_info", "content": {"Plain":
                                     "Pos: Vec3 { x: 48, y: 0, z: 0 }\nRole: Monster\n"}}}]}}}
    resident = {"actor": target, "role": "Monster", "rtsim_uid": 2}
    bindings = [player, binding, giver_binding]
    combat = [{"uid": row["uid"], "server_entity": row["server_entity"], "health": hp,
               "maximum_health": 100, "dead": False} for row, hp in ((binding, 9.6), (giver_binding, 100))]
    quests = []
    if mutation == "capture":
        fixture["offer_captures"][-1]["consistent"] = False
    elif mutation == "stale":
        binding["server_position"]["x"] = 57
    elif mutation == "resident":
        resident["rtsim_uid"] = 99
    elif mutation == "readback":
        fixture["slay_site"]["target_readback"]["position"][0] = 49
    elif mutation == "placement":
        fixture["slay_site"]["target_placement"]["actor"] = giver
    elif mutation == "unloaded":
        bindings.remove(binding)
    elif mutation == "distance":
        binding["server_position"]["x"] = 120
    elif mutation == "offer":
        opening["dialogue"] = {}
    elif mutation == "health":
        opening["survival"]["health"]["current"] = 99
    elif mutation == "slots":
        opening["inventory"]["free_slots"] = 2
    elif mutation == "quest":
        quests.append({"actors": [{"actor": player["actor"]}]})
    journal = [
        {"event": "private_resident_fixture", "sequence": 1, "data": {"residents": [resident]}},
        {"event": "loaded_actor_bindings", "sequence": 2, "data": {"bindings": bindings}},
        {"event": "combat_entity_bindings", "sequence": 3, "data": {"bindings": combat}},
        {"event": "quest_states", "sequence": 4, "data": quests}]
    if mutation == "dead":
        journal.append({"event": "rtsim_death_hook", "sequence": 5, "data": {"actor": target}})
    actor = SimpleNamespace(request=AsyncMock(return_value=opening))
    with patch.object(task, "rows", return_value=journal):
        if mutation is None:
            asyncio.run(task.validate_opening(actor, fixture))
            assert fixture["slay_site"]["opening_target_distance"] == 48
        else:
            with pytest.raises(RuntimeError):
                asyncio.run(task.validate_opening(actor, fixture))


@pytest.mark.parametrize("distance", [2.5, 4])
def test_actual_menu_reobserves_moving_giver_before_talk_in_native_range(distance):
    old = {"uid": 7, "position": [distance, 0, 0], "distance": distance,
           "control_target": {"uid": 7, "lifecycle": "old"}}
    fresh = {"uid": 7, "position": [1, 0, 0], "distance": 1,
             "control_target": {"uid": 7, "lifecycle": "fresh"}}
    actor = SimpleNamespace(request=AsyncMock(side_effect=[
        {"nearby_entities": [old]}, {"nearby_entities": [fresh]}]))
    question = {"kind": "Question"}
    with patch.object(task.native, "action", AsyncMock(return_value={"status": "completed"})) as action, patch.object(
            task, "prompt", AsyncMock(return_value=question)):
        assert asyncio.run(task.menu(actor, old)) == question
    assert [call.args[1] for call in action.await_args_list] == ["move_to", "context_interact"]
    assert action.await_args_list[-1].kwargs["target"] == fresh["control_target"]
