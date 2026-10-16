import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from scripts.rpg_benchmark import npc_contracts as task


def snapshot():
    return {"sequence": 10, **{
        name: {"actor": {"Npc": {"idx": index, "version": 1}}, "uid": index,
               "server_entity": f"Entity({index}, Generation(1))", "rtsim_uid": index - 1,
               "server_position": {"x": x, "y": 0, "z": 0},
               "rtsim_position": {"x": x, "y": 0, "z": 0}}
        for name, index, x in (("target_actor", 3, 48), ("giver_actor", 1, 0))}}


@pytest.mark.parametrize("mutation", [None, "server", "rtsim", "giver", "uid", "entity", "nonfinite", "close"])
def test_capture_consistency_rejects_drift_and_identity_change(mutation):
    before, after = snapshot(), snapshot()
    if mutation in {"server", "rtsim"}:
        after["target_actor"][mutation + "_position"]["x"] += 8
    elif mutation == "giver":
        after["giver_actor"]["rtsim_position"]["x"] += 8
    elif mutation == "uid":
        after["target_actor"]["uid"] += 1
    elif mutation == "entity":
        after["target_actor"]["server_entity"] += "-new"
    elif mutation == "nonfinite":
        after["target_actor"]["server_position"]["x"] = float("nan")
    elif mutation == "close":
        before["target_actor"]["rtsim_position"]["x"] = 31
        after["target_actor"]["rtsim_position"]["x"] = 31
    if mutation == "nonfinite":
        with pytest.raises(RuntimeError, match="non-finite"):
            task.capture_consistent(before, after)
    else:
        assert task.capture_consistent(before, after) is (mutation is None)


def test_every_native_offer_capture_is_preceded_by_placement_and_stale_offer_is_rejected():
    request = {"response_id": 3, "content": {"Localized": {"key": "dialogue-question-quest_req"}}}
    question = {"dialogue_id": 1, "tag": 1, "responses": [request]}
    reject = {"response_id": 0, "content": {"Localized": {"key": "common-no"}}}
    offer = {"kind": "Question", "responses": [reject],
             "content": {"Localized": {"key": "npc-response-quest-slay-ask"}}}
    fixture = {"case": "T12", "definition": {"fixture_version": "t12-native-slay-v6"}}
    sequence = []
    async def place(*unused):
        sequence.append("place")
    async def dialogue(unused, prompt, response):
        sequence.append("request" if response is request else "reject")
    with patch.object(task, "living_giver", AsyncMock(return_value={"uid": 1})), patch.object(
            task, "menu", AsyncMock(return_value=question)), patch.object(
            task, "place_capture_target", AsyncMock(side_effect=place)), patch.object(
            task, "capture_snapshot", side_effect=[snapshot()] * 4), patch.object(
            task, "capture_consistent", side_effect=[False, True]), patch.object(
            task, "dialogue_action", AsyncMock(side_effect=dialogue)), patch.object(
            task, "prompt", AsyncMock(return_value=offer)), patch.object(task.asyncio, "sleep", AsyncMock()):
        asyncio.run(task.prepare_offer(object(), object(), {"uid": 1}, fixture, None))
    assert sequence == ["place", "request", "reject", "place", "request"]
    assert [row["consistent"] for row in fixture["offer_captures"]] == [False, True]
    assert fixture["offer"] == offer


def test_placement_and_native_readbacks_precede_offer_capture():
    state = snapshot()
    target = state["target_actor"]
    fixture = {"designated_monster": {"actor": target["actor"], "rtsim_uid": 2}}
    actor = SimpleNamespace(name="player", request=AsyncMock(return_value={"position": [0, 0, 0]}))
    admin = SimpleNamespace(native_events=[])
    sequence = []
    async def command(unused, *args):
        sequence.append(args)
        if args[0] == "rtsim_info":
            admin.native_events.append({"data": {"kind": "command_info", "content": {
                "Plain": "Pos: Vec3 { x: 48, y: 0, z: 0 }\nRole: Monster\n"}}})
        return {"status": "completed"}
    with patch.object(task.native, "require_living"), patch.object(
            task.native, "settled_point", AsyncMock(return_value=[48, 0, 0])), patch.object(
            task.native, "qualify", AsyncMock(return_value={"ground": True})), patch.object(
            task, "observed", AsyncMock()), patch.object(task, "rows", return_value=[]), patch.object(
            task.private, "bindings", return_value=[target]), patch.object(
            task.native, "command", AsyncMock(side_effect=command)):
        asyncio.run(task.place_capture_target(admin, actor, fixture))
    assert sequence == [("rtsim_tp", 2), ("sudo", "uid@3", "goto", 48, 0, 0),
                        ("tp", "player"), ("rtsim_info", 2)]
    assert fixture["slay_site"]["construction_revision"] == "town-approach-006"


@pytest.mark.parametrize("dead", [False, True])
def test_enemy_perception_excludes_original_participants_and_players_by_identity(dead):
    identities = [{"Npc": {"idx": index, "version": 1}} for index in (1, 2, 3)]
    fixture = {"definition": {"fixture_version": "t12-native-slay-v6"},
               "giver_actor": identities[0], "prepared_recipient_actor": identities[1],
               "target_actor": identities[2]}
    entities = [{"uid": uid, "name_text": "Dreadhorn", "alias": None,
                 "health": {"current": 10, "dead": dead if uid == 8 else False}}
                for uid in (1, 2, 3, 4, 8)]
    entities.append({"uid": 9, "name_text": "Player", "alias": "other-player", "health": {"current": 10}})
    view = {"position": [0, 0, 0], "self_identity": {"uid": 4}, "nearby_entities": entities,
            "combat_perception": {"nearby_combat": [
                {"target": {"uid": row["uid"]}, "alignment": "Enemy"} for row in entities]}}
    actor = SimpleNamespace(name="player", request=AsyncMock(return_value=view))
    async def observed(unused, predicate, reason):
        actual = {"nearby_entities": [{"uid": 8, "distance": 1, "health": {"current": 10}}]}
        assert predicate(actual)
        return actual
    with patch.object(task, "rows", return_value=[]), patch.object(
            task.private, "bindings", return_value=[
                {"actor": identity, "uid": uid} for uid, identity in enumerate(identities, 1)]), patch.object(
            task.native, "settled_point", AsyncMock(return_value=[-96, 0, 0])), patch.object(
            task.native, "command", AsyncMock(return_value={"status": "completed"})) as command, patch.object(
            task, "observed", AsyncMock(side_effect=observed)):
        asyncio.run(task.separate_opening_threats(object(), actor, fixture))
    assert [row.args[1:] for row in command.await_args_list] == (
        [] if dead else [("sudo", "uid@8", "goto", -96, 0, 0), ("tp", "player")])


@pytest.mark.parametrize("health,missing", [(10, False), (0, False), (10, True)])
def test_original_giver_reobserved_after_threat_separation(health, missing):
    giver = {"uid": 1, "distance": 1, "health": {"current": health}}
    actor = SimpleNamespace(request=AsyncMock(return_value={"nearby_entities": [] if missing else [giver]}))
    fixture = {"giver_actor": {"Npc": {"idx": 1, "version": 1}}}
    with patch.object(task, "separate_opening_threats", AsyncMock()) as separate, patch.object(
            task.native, "require_living"), patch.object(task, "rows", return_value=[]), patch.object(
            task.private, "bindings", return_value=[{"actor": fixture["giver_actor"], "uid": 1}]):
        if health > 0 and not missing:
            assert asyncio.run(task.living_giver(object(), actor, fixture)) == giver
            assert fixture["giver_reobservations"] == [giver]
        else:
            with pytest.raises(RuntimeError, match="not living"):
                asyncio.run(task.living_giver(object(), actor, fixture))
    separate.assert_awaited_once()


@pytest.mark.parametrize("dead,full,occupied", [(False, False, False), (True, False, False),
                                             (False, True, False), (False, False, True)])
def test_snapshot_requires_original_injured_living_unbound_target(dead, full, occupied):
    state = snapshot()
    fixture = {name: state[name]["actor"] for name in ("giver_actor", "target_actor")}
    combat = [{"uid": state[name]["uid"], "server_entity": state[name]["server_entity"],
               "health": 100 if full or name == "giver_actor" else 9.6, "maximum_health": 100,
               "dead": dead and name == "target_actor"} for name in fixture]
    journal = [
        {"event": "loaded_actor_bindings", "sequence": 1, "data": {"bindings": [state[name] for name in fixture]}},
        {"event": "combat_entity_bindings", "sequence": 2, "data": {"bindings": combat}},
        {"event": "quest_states", "sequence": 3, "data": [
            {"actors": [{"actor": fixture["target_actor"]}]}] if occupied else []}]
    with patch.object(task, "rows", return_value=journal):
        if dead or full or occupied:
            with pytest.raises(RuntimeError, match="living|injury|already bound"):
                task.capture_snapshot(fixture)
        else:
            assert task.capture_snapshot(fixture)["target_actor"]["health_readback"]["health"] == 9.6


def test_town_protection_precedes_inventory_cleanup_and_stock_setup(tmp_path):
    residents = [{"actor": {"Npc": {"idx": index, "version": 1}}, "profession": "Farmer"}
                 for index in (1, 2)]
    residents.append({"actor": {"Npc": {"idx": 3, "version": 1}}, "role": "Monster"})
    journal = [{"event": "private_resident_fixture", "data": {"residents": residents}}]
    actor = SimpleNamespace(out=tmp_path)
    sequence = []
    async def npc(*unused):
        sequence.append("original-giver")
    async def living(*unused):
        sequence.append("separate-and-reobserve")
    async def cleanup(*unused):
        sequence.append("inventory")
        raise RuntimeError("test stopped before stock")
    with patch.object(task, "observed", AsyncMock()), patch.object(task, "rows", return_value=journal), patch.object(
            task, "native_npc", AsyncMock(side_effect=npc)), patch.object(
            task, "living_giver", AsyncMock(side_effect=living)), patch.object(
            task.native, "clean_inventory", AsyncMock(side_effect=cleanup)):
        with pytest.raises(RuntimeError, match="test stopped"):
            asyncio.run(task.setup(object(), actor, {"id": "T12", "task": "unchanged",
                                                    "fixture_version": "t12-native-slay-v6"}, "natural"))
    assert sequence == ["original-giver", "separate-and-reobserve", "inventory"]
