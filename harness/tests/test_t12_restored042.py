import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from scripts.rpg_benchmark import npc_contracts

def test_slay_preacceptance_rejects_moved_offer_before_reconstructing_never_accepts():
    reject = {"content": {"Localized": {"key": "common-no"}}, "response_id": 1}
    fixture = {"case": "T12", "definition": {"fixture_version": "t12-native-slay-v2"},
               "offer": {"responses": [reject]}}
    actor = SimpleNamespace(request=AsyncMock(return_value={"position": [0, 0, 0]}))
    with patch.object(npc_contracts, "slay_site", AsyncMock()) as site, patch.object(
            npc_contracts, "native_npc", AsyncMock(side_effect=["recipient", "giver"] * 2)), patch.object(
            npc_contracts, "prepare_offer", AsyncMock()), patch.object(
            npc_contracts, "slay_distance", side_effect=[113.39, 64]), patch.object(
            npc_contracts, "dialogue_action", AsyncMock()) as dialogue, patch.object(
            npc_contracts.asyncio, "sleep", AsyncMock()):
        result = asyncio.run(npc_contracts.slay_offer(object(), actor, fixture, [], [1, 2]))
    assert result == ("giver", "recipient") and site.await_count == 2
    dialogue.assert_awaited_once_with(actor, fixture["offer"], reject)
    assert [row["qualified"] for row in fixture["preacceptance_approaches"]] == [False, True]


def test_town_slay_offer_uses_original_residents_and_read_only_monster_information():
    target = {"Npc": {"idx": 3, "version": 1}}
    fixture = {"case": "T12", "definition": {"fixture_version": "t12-native-slay-v3"}}
    admin, actor = SimpleNamespace(native_events=[]), object()
    information = {"data": {"kind": "command_info", "content": {"Plain":
        "Pos: Vec3 { x: 220.0, y: 0.0, z: 0.0 }\nRole: Monster\n"}}}
    async def command(*args):
        admin.native_events.append(information)
        return {"status": "completed"}
    with patch.object(npc_contracts, "native_npc", AsyncMock(side_effect=["recipient", "giver"])), patch.object(
            npc_contracts, "prepare_offer", AsyncMock()), patch.object(
            npc_contracts, "slay_site", AsyncMock()) as camp, patch.object(
            npc_contracts.native, "command", AsyncMock(side_effect=command)) as readback:
        assert asyncio.run(npc_contracts.slay_offer(
            admin, actor, fixture, [{"role": "Monster", "actor": target, "rtsim_uid": 2}], [1, 2])) == ("giver", "recipient")
    camp.assert_not_awaited()
    readback.assert_awaited_once_with(admin, "rtsim_info", 2)
    assert fixture["slay_site"]["target_readback"]["position"] == [220, 0, 0]


def test_authored_town_approach_moves_only_the_original_monster_before_acceptance():
    target = {"Npc": {"idx": 3, "version": 1}}
    fixture = {"case": "T12", "definition": {"fixture_version": "t12-native-slay-v4"}}
    admin = SimpleNamespace(native_events=[])
    actor = SimpleNamespace(name="ordinary-player", request=AsyncMock(return_value={"position": [0, 0, 0]}))
    binding = {"actor": target, "uid": 40, "rtsim_uid": 2,
               "server_position": {"x": 48, "y": 0, "z": 0},
               "rtsim_position": {"x": 48, "y": 0, "z": 0}}
    sequence = []
    async def prepare(*args):
        sequence.append("fresh_unaccepted_offer")
    async def command(*args):
        sequence.append(args[1:])
        if args[1] == "rtsim_info":
            admin.native_events.append({"data": {"kind": "command_info", "content": {
                "Plain": "Pos: Vec3 { x: 48, y: 0, z: 0 }\nRole: Monster\n"}}})
        return {"status": "completed"}
    with patch.object(npc_contracts, "native_npc", AsyncMock(side_effect=["recipient", "giver"])), patch.object(
            npc_contracts, "prepare_offer", AsyncMock(side_effect=prepare)), patch.object(
            npc_contracts, "observed", AsyncMock()), patch.object(npc_contracts, "rows", return_value=[]), patch.object(
            npc_contracts.private, "bindings", return_value=[binding]), patch.object(
            npc_contracts.native, "settled_point", AsyncMock(return_value=[48, 0, 0])), patch.object(
            npc_contracts.native, "qualify", AsyncMock(return_value={"status": "complete"})), patch.object(
            npc_contracts.native, "command", AsyncMock(side_effect=command)):
        asyncio.run(npc_contracts.slay_offer(
            admin, actor, fixture, [{"role": "Monster", "actor": target, "rtsim_uid": 2}], [1, 2]))
    assert sequence == ["fresh_unaccepted_offer", ("rtsim_tp", 2),
                        ("sudo", "uid@40", "goto", 48, 0, 0), ("tp", "ordinary-player"), ("rtsim_info", 2)]
    assert fixture["slay_site"]["target_placement"]["before_binding"] == binding
    assert fixture["slay_site"]["target_placement"]["actor"] == target


def test_opening_separation_keeps_the_same_ambient_creature_alive_without_touching_the_ogre():
    ambient = {"uid": 2, "name_text": "Green Legoom", "alias": None, "health": {"current": 14},
               "position": [0, 0, 0]}
    ogre = {"uid": 40, "name_text": "Ogre", "alias": None, "health": {"current": 9.6}}
    actual = {**ambient, "distance": 1, "position": [-96, 0, 0]}
    actor = SimpleNamespace(name="ordinary-player", request=AsyncMock(return_value={
        "position": [0, 0, 0], "nearby_entities": [ambient, ogre]}))
    fixture, admin = {}, object()
    async def observed(unused, predicate, reason):
        result = {"nearby_entities": [actual]}
        assert predicate(result)
        assert not predicate({"nearby_entities": [{**actual, "health": {"current": 0}}]})
        assert not predicate({"nearby_entities": [{**actual, "uid": 3}]})
        return result
    with patch.object(npc_contracts, "observed", AsyncMock(side_effect=observed)), patch.object(
            npc_contracts.native, "settled_point", AsyncMock(return_value=[-96, 0, 0])), patch.object(
            npc_contracts.native, "command", AsyncMock(return_value={"status": "completed"})) as command:
        asyncio.run(npc_contracts.separate_opening_threats(admin, actor, fixture))
    assert [row.args[1:] for row in command.await_args_list] == [
        ("sudo", "uid@2", "goto", -96, 0, 0), ("tp", "ordinary-player")]
    assert fixture["preacceptance_ambient_placements"][0]["after"]["health"]["current"] == 14


def test_returning_opening_creature_is_not_repeatedly_repositioned():
    actor = SimpleNamespace(request=AsyncMock(return_value={
        "position": [0, 0, 0], "nearby_entities": [{"uid": 2, "name_text": "Green Legoom"}]}))
    with pytest.raises(RuntimeError, match="returned after its initial placement"):
        asyncio.run(npc_contracts.separate_opening_threats(
            object(), actor, {"preacceptance_ambient_placements": [{"uid": 2}]}))


@pytest.mark.parametrize("mutation", [None, "wrong_actor", "unloaded", "distant"])
def test_authored_town_approach_requires_exact_loaded_target_and_actual_approach(mutation):
    target = {"Npc": {"idx": 3, "version": 1}}
    offer = {"kind": "Question"}
    opening = {"survival": {"health": {"current": 100}}, "inventory": {"free_slots": 3},
               "self_identity": {"uid": 10}, "dialogue": offer, "position": [0, 0, 0],
               "session_id": "test", "control_actor": {"uid": 10, "lifecycle": "client:Entity(0, Generation(1))"}}
    target_binding = {"actor": target, "uid": 40, "rtsim_uid": 2,
                      "server_position": {"x": 120 if mutation == "distant" else 48, "y": 0, "z": 0}}
    bindings = [{"uid": 10, "actor": {"Character": 16}, "server_entity": "player"}]
    if mutation != "unloaded":
        bindings.append(target_binding)
    journal = [{"event": "private_resident_fixture", "sequence": 0, "data": {"residents": [
        {"actor": target, "role": "Monster", "rtsim_uid": 2}]}},
        {"event": "loaded_actor_bindings", "sequence": 1, "data": {"bindings": bindings}}]
    fixture = {"case": "T12", "offer": offer, "target_actor": target,
               "definition": {"fixture_version": "t12-native-slay-v4"},
               "slay_site": {"target_placement": {
                   "actor": {"Npc": {"idx": 9, "version": 1}} if mutation == "wrong_actor" else target,
                   "before_binding": target_binding},
                   "target_readback": {"rtsim_uid": 2, "position": [48, 0, 0], "events": [
                       {"data": {"kind": "command_info", "content": {
                           "Plain": "Pos: Vec3 { x: 48, y: 0, z: 0 }\nRole: Monster\n"}}}]}}}
    actor = SimpleNamespace(request=AsyncMock(return_value=opening))
    with patch.object(npc_contracts, "rows", return_value=journal):
        if mutation is None:
            asyncio.run(npc_contracts.validate_opening(actor, fixture))
            assert fixture["slay_site"]["opening_target_distance"] == 48
        else:
            with pytest.raises(RuntimeError, match="same loaded native Monster|opening guard"):
                asyncio.run(npc_contracts.validate_opening(actor, fixture))


@pytest.mark.parametrize("matching", [True, False])
def test_town_slay_opening_qualifies_real_unloaded_monster_without_fabricated_uid(matching):
    target = {"Npc": {"idx": 3, "version": 1}}
    offer = {"kind": "Question"}
    opening = {"survival": {"health": {"current": 100}}, "inventory": {"free_slots": 3},
               "self_identity": {"uid": 10}, "dialogue": offer, "position": [0, 0, 0],
               "session_id": "test", "control_actor": {"uid": 10, "lifecycle": "client:Entity(0, Generation(1))"}}
    journal = [
        {"event": "private_resident_fixture", "sequence": 0, "data": {"residents": [
            {"actor": target, "role": "Monster", "rtsim_uid": 2}]}},
        {"event": "loaded_actor_bindings", "sequence": 1, "data": {"bindings": [
            {"uid": 10, "actor": {"Character": 16}, "server_entity": "player"}]}}]
    fixture = {"case": "T12", "offer": offer, "target_actor": target,
               "definition": {"fixture_version": "t12-native-slay-v3"},
               "slay_site": {"target_readback": {"rtsim_uid": 2 if matching else 3,
                   "position": [220, 0, 0], "events": [{"data": {"kind": "command_info", "content": {
                       "Plain": "Pos: Vec3 { x: 220, y: 0, z: 0 }\nRole: Monster\n"}}}]}}}
    actor = SimpleNamespace(request=AsyncMock(return_value=opening))
    with patch.object(npc_contracts, "rows", return_value=journal):
        if matching:
            asyncio.run(npc_contracts.validate_opening(actor, fixture))
            assert fixture["slay_site"]["opening_target_distance"] == 220
        else:
            with pytest.raises(RuntimeError, match="exact native Monster"):
                asyncio.run(npc_contracts.validate_opening(actor, fixture))

