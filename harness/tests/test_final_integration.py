import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from scripts.rpg_benchmark import npc_contracts, suite
from .test_rpg_b3_upgrade import approved_b3_loadout


def test_release041_catalog_preservation():
    before = json.loads((Path(__file__).parent / "fixtures/release041-scenes.json").read_text())
    after = json.loads(Path(suite.__file__).with_name("scenes.json").read_text())
    assert len(before["scenes"]) == len(after["scenes"]) == 29
    for original, current in zip(before["scenes"], after["scenes"], strict=True):
        if original["id"] == "T12":
            assert current["fixture_version"] == "t12-native-slay-v6"
            assert {key: value for key, value in original.items() if key not in {
                "fixture_version", "qualification"}} == {
                    key: value for key, value in current.items() if key not in {
                        "fixture_version", "qualification"}}
        else:
            assert current == approved_b3_loadout(original)
    assert {key: value for key, value in before.items() if key != "scenes"} == {
        key: value for key, value in after.items() if key != "scenes"}


@pytest.mark.parametrize("distance,mutation", [
    (32, None), (32.1, None), (48, None), (96, None), (96.1, None),
    (48, "wrong_actor"), (48, "unloaded"), (48, "wrong_rtsim"),
])
def test_v5_opening_binds_native_monster_and_client_identity(distance, mutation):
    target = {"Npc": {"idx": 3, "version": 1}}
    offer = {"kind": "Question"}
    opening = {"survival": {"health": {"current": 100}}, "inventory": {"free_slots": 3},
               "self_identity": {"uid": 10}, "dialogue": offer, "position": [0, 0, 0],
               "session_id": "test", "control_actor": {"uid": 10, "lifecycle": "client:Entity(0, Generation(1))"}}
    binding = {"actor": target, "uid": 40, "rtsim_uid": 2,
               "server_position": {"x": distance, "y": 0, "z": 0}}
    bindings = [{"uid": 10, "actor": {"Character": 16}, "server_entity": "player"}]
    if mutation != "unloaded":
        bindings.append(binding)
    journal = [{"event": "private_resident_fixture", "sequence": 0, "data": {"residents": [
        {"actor": target, "role": "Monster", "rtsim_uid": 2}]}},
        {"event": "loaded_actor_bindings", "sequence": 1, "data": {"bindings": bindings}}]
    fixture = {"case": "T12", "offer": offer, "target_actor": target,
               "definition": {"fixture_version": "t12-native-slay-v5"},
               "slay_site": {"target_placement": {
                   "actor": {"Npc": {"idx": 9, "version": 1}} if mutation == "wrong_actor" else target,
                   "before_binding": binding},
                   "target_readback": {"rtsim_uid": 3 if mutation == "wrong_rtsim" else 2,
                       "position": [distance, 0, 0], "events": [
                           {"data": {"kind": "command_info", "content": {
                               "Plain": f"Pos: Vec3 {{ x: {distance}, y: 0, z: 0 }}\nRole: Monster\n"}}}]}}}
    actor = SimpleNamespace(request=AsyncMock(return_value=opening))
    with patch.object(npc_contracts, "rows", return_value=journal):
        if mutation is None and 32 < distance <= 96:
            asyncio.run(npc_contracts.validate_opening(actor, fixture))
            assert fixture["slay_site"]["opening_target_distance"] == distance
            assert fixture["player"]["client_actor"] == opening["control_actor"]
            assert fixture["player"]["client_session_id"] == "test"
        else:
            with pytest.raises(RuntimeError, match="native Monster|approach|opening guard"):
                asyncio.run(npc_contracts.validate_opening(actor, fixture))


def test_v5_only_finitely_separates_same_living_ambient_uid_before_acceptance():
    ambient = {"uid": 2, "name_text": "Green Legoom", "alias": None, "health": {"current": 14}}
    monster = {"uid": 40, "name_text": "Ogre", "alias": None, "health": {"current": 9.6}}
    actor = SimpleNamespace(name="ordinary-player", request=AsyncMock(return_value={
        "position": [0, 0, 0], "nearby_entities": [ambient, monster]}))
    fixture, admin = {}, object()
    async def observed(unused, predicate, reason):
        actual = {**ambient, "distance": 1, "position": [-96, 0, 0]}
        assert predicate({"nearby_entities": [actual]})
        assert not predicate({"nearby_entities": [{**actual, "health": {"current": 0}}]})
        assert not predicate({"nearby_entities": [{**actual, "uid": 3}]})
        return {"nearby_entities": [actual]}
    with patch.object(npc_contracts, "observed", AsyncMock(side_effect=observed)), patch.object(
            npc_contracts.native, "settled_point", AsyncMock(return_value=[-96, 0, 0])), patch.object(
            npc_contracts.native, "command", AsyncMock(return_value={"status": "completed"})) as command:
        asyncio.run(npc_contracts.separate_opening_threats(admin, actor, fixture))
        assert [row.args[1:] for row in command.await_args_list] == [
            ("sudo", "uid@2", "goto", -96, 0, 0), ("tp", "ordinary-player")]
        with pytest.raises(RuntimeError, match="returned after its initial placement"):
            asyncio.run(npc_contracts.separate_opening_threats(admin, actor, fixture))
