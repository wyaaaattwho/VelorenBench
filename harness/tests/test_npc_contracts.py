import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import asyncio

import pytest

from scripts.rpg_benchmark import contract_evidence as private, npc_contracts, providers, renderer_support, run, suite


def test_twenty_definitions_preserved_and_only_three_added():
    base = Path(__file__).with_name("fixtures") / "twenty-scenes.json"
    original = json.loads(base.read_text())
    actual = json.loads(Path(suite.__file__).with_name("scenes.json").read_text())
    entries = {row["id"]: row for row in actual["scenes"]}
    assert len(entries) == 29
    assert set(entries) - {row["id"] for row in original["scenes"]} == npc_contracts.TASK_IDS | {"T01", "T02", "T06", "T07", "T08", "T09"}
    assert all(entries[row["id"]] == row for row in original["scenes"])
    for key in original.keys() - {"version", "scenes"}:
        assert actual[key] == original[key]


def chain():
    player = {"actor": {"Character": 2}, "uid": 10, "server_entity": "Entity(9, Generation(1))"}
    giver, recipient = {"Npc": {"idx": 1, "version": 1}}, {"Npc": {"idx": 2, "version": 1}}
    quest = {"quest_id": 7, "actors": [{"actor": value} for value in (player["actor"], giver, recipient)],
             "quest": {"arbiter": recipient, "kind": {"Courier": {"instance": {
                 "kind": "Message", "messenger": player["actor"], "source_actor": giver, "target_actor": recipient}}}}}
    settlement = {"transaction_id": "native:1", "quest_id": 7, "quest": quest["quest"],
                  "participants": [entry["actor"] for entry in quest["actors"]], "arbiter": recipient,
                  "requested_success": True, "resolution": True, "deposit_restoration": {"after": 150}}
    promised = [{"item_definition_id": npc_contracts.COINS}, 150]
    provenance = {**settlement, "delivery": {
        "sender": recipient, "recipient": player["actor"], "sender_entity": "Entity(3, Generation(1))",
        "recipient_entity": player["server_entity"], "dialogue_id": 123, "tag": 2, "given_item": promised}}
    item = {"amount": 150, "definition_id": {"Simple": npc_contracts.COINS}}
    data = [
        ("quest_settlement", settlement),
        ("quest_reward_created", provenance),
        ("dialogue_sender_removal", {"provenance": provenance, "dialogue_id": 123,
                                     "sender_uid": 4, "recipient_uid": 10, "removed_items": [item]}),
        ("dialogue_item_transfer", {
            "provenance": provenance, "quest_id": 7, "dialogue_id": 123, "sender_uid": 4,
            "recipient_uid": 10, "effect": "removed_from_sender_inserted_into_recipient", "item": item,
            "recipient_before": 0, "recipient_after": 150,
            "dialogue": {"id": 123, "kind": {"Statement": {"given_item": promised, "tag": 2}}}})]
    rows = [{"event": "loaded_actor_bindings", "sequence": 0,
             "data": {"bindings": [player, {"actor": recipient, "uid": 4,
                                           "server_entity": "Entity(3, Generation(1))"}]}}]
    rows += [{"event": name, "sequence": sequence + 1, "data": copy.deepcopy(value)}
            for sequence, (name, value) in enumerate(data)]
    return rows, quest, player


def test_exact_native_reward_chain():
    rows, quest, player = chain()
    rows = rows[1:]
    rows.insert(0, {"event": "loaded_actor_bindings", "sequence": 0,
                   "data": {"bindings": [player, {"actor": quest["quest"]["arbiter"], "uid": 4,
                                                 "server_entity": "Entity(3, Generation(1))"}]}})
    result = private.reward(rows, quest, player, after=0)
    assert result["resolution"] is True and result["actual_reward_inserted"]
    assert result["chains"][0]["transaction_id"] == "native:1"


def test_native_nested_public_identity_clue():
    assert npc_contracts.plain({"Content": {"Plain": "Distinct NPC"}}) == "Distinct NPC"


def test_private_rtsim_position_uses_real_command_readback_without_target_contact():
    info = "Seed: 20261099\nPos: Vec3 { x: 1200.5, y: -2e1, z: 256.0 }\nRole: Monster\nHome: None"
    rows = [{"data": {"kind": "command_info", "content": {"Plain": info}}}]
    assert npc_contracts.rtsim_position(rows) == [1200.5, -20, 256]
    with pytest.raises(RuntimeError, match="one exact position"):
        npc_contracts.rtsim_position([])
    with pytest.raises(RuntimeError, match="one exact position"):
        npc_contracts.rtsim_position(rows + rows)

def test_message_preoffer_local_participant_setup_never_accepts_the_quest():
    participant = {"Npc": {"idx": 2, "version": 1}}
    request = {"response_id": 3, "content": {"Localized": {"key": "dialogue-question-quest_req"}}}
    question = {"dialogue_id": 1, "tag": 1, "responses": [request]}
    offer = {"kind": "Question", "content": {"Localized": {"key": "npc-response-quest-messenger-ask",
                                       "args": {"name": {"Content": {"Plain": "Other"}}}}}}
    current = {"position": [10, 20, 0], "nearby_entities": [
        {"uid": 2, "alias": None, "name_text": "Other (Farmer)"}]}
    fixture = {"case": "T11", "prepared_recipient_actor": participant}
    actor, admin = SimpleNamespace(request=AsyncMock(return_value=current)), object()
    journal = [{"event": "loaded_actor_bindings", "data": {"bindings": [{"actor": participant, "uid": 2}]}}]
    with patch.object(npc_contracts, "rows", return_value=journal), patch.object(
            npc_contracts, "menu", AsyncMock(return_value=question)), patch.object(
            npc_contracts, "prompt", AsyncMock(return_value=offer)), patch.object(
            npc_contracts, "dialogue_action", AsyncMock()) as dialogue, patch.object(
            npc_contracts.native, "command", AsyncMock(return_value={"status": "completed"})) as command:
        asyncio.run(npc_contracts.prepare_offer(admin, actor, {"uid": 1}, fixture, None))
    command.assert_awaited_once_with(admin, "sudo", "uid@2", "goto", 13, 20, 0)
    dialogue.assert_awaited_once_with(actor, question, request)
    assert fixture["offer"] == offer and fixture["recipient_actor"] == participant


@pytest.mark.parametrize("mutation", [
    "false", "null", "deposit_only", "no_creation", "no_removal", "no_insertion",
    "wrong_quest", "transaction_mix", "wrong_player", "stale_entity", "wrong_dialogue",
    "wrong_tag", "wrong_item", "partial", "unknown_effect", "unloaded", "rejected",
])
def test_no_amount_or_time_based_reward_association(mutation):
    rows, quest, player = chain()
    binding = rows.pop(0)
    if mutation in {"false", "null"}:
        rows[0]["data"]["resolution"] = False if mutation == "false" else None
    elif mutation in {"deposit_only", "no_insertion"}:
        rows.pop()
    elif mutation == "no_creation":
        rows.pop(1)
    elif mutation == "no_removal":
        rows.pop(2)
    elif mutation == "wrong_quest":
        rows[-1]["data"]["quest_id"] = 8
    elif mutation == "transaction_mix":
        rows[-1]["data"]["provenance"]["transaction_id"] = "native:2"
    elif mutation == "wrong_player":
        rows[-1]["data"]["recipient_uid"] = 11
    elif mutation == "stale_entity":
        player["server_entity"] = "Entity(9, Generation(2))"
    elif mutation == "wrong_dialogue":
        rows[-1]["data"]["dialogue_id"] = 124
    elif mutation == "wrong_tag":
        rows[-1]["data"]["dialogue"]["kind"]["Statement"]["tag"] = 3
    elif mutation == "wrong_item":
        rows[-1]["data"]["item"]["definition_id"] = {"Simple": npc_contracts.SURPLUS}
    elif mutation == "partial":
        rows[-1]["data"]["recipient_after"] = 149
    elif mutation == "unknown_effect":
        rows[-1]["data"]["effect"] = "unknown"
    else:
        rows.append({"sequence": 5, "event": "quest_reward_undelivered" if mutation == "unloaded"
                     else "dialogue_item_transfer_rejected",
                     "data": {"provenance": rows[1]["data"]}})
    assert not private.reward([binding, *rows], quest, player, after=0)["actual_reward_inserted"]


def test_native_resolution_mutates_only_res_and_participant_order_is_not_identity():
    rows, quest, player = chain()
    quest["quest"]["res"] = 0
    for row in rows:
        provenance = row["data"].get("provenance") if isinstance(row["data"], dict) else None
        if row["event"] in {"quest_settlement", "quest_reward_created"}:
            provenance = row["data"]
        if provenance:
            provenance["quest"]["res"] = 2
    rows[1]["data"]["participants"].reverse()
    rows[2]["data"]["participants"].reverse()
    rows[3]["data"]["provenance"]["participants"].reverse()
    rows[4]["data"]["provenance"]["participants"].reverse()
    assert private.reward(rows, quest, player, after=0)["actual_reward_inserted"]


def test_journal_process_run_schema_and_sequence(tmp_path):
    path = tmp_path / "oracle.ndjson"
    header = {"schema": "rpg-native-oracle", "version": "1.1", "private": True, "sequence": 0,
              "event": "opened", "data": {"run_id": "r"}}
    path.write_text(json.dumps(header) + "\n" + '{"partial"')
    assert private.read_journal(str(path), "r") == [header]
    assert private.journal_tail(str(path)) == {
        "complete_record_count": 1, "incomplete_trailing_bytes": 10,
        "closed_on_record_boundary": False}
    with pytest.raises(ValueError, match="run identity"):
        private.read_journal(str(path), "other")
    header["sequence"] = 1
    path.write_text(json.dumps(header) + "\n")
    with pytest.raises(ValueError, match="sequence"):
        private.read_journal(str(path), "r")


def test_despawn_unload_and_unknown_actor_are_not_slay_death():
    player = {"uid": 1, "server_entity": "p"}
    target = {"Npc": {"idx": 4, "version": 1}}
    assert not private.slay([], target, player, [], after=0)["native_target_death"]
    rows = [{"event": "rtsim_entity_unload", "sequence": 2, "data": {"actor": target}}]
    assert not private.slay(rows, target, player, [], after=0)["native_target_death"]


def native_trace(name):
    from .historical import historical_fixture
    return json.loads(historical_fixture("native-contract-traces.json").read_text())[name]


def test_real_message_resolution_transition_and_exact_reward():
    trace = native_trace("message")
    result = private.reward(trace["rows"], trace["quest"], trace["player"], after=0)
    assert result["actual_reward_inserted"]
    assert result["chains"][0]["amount"] == 150
    assert result["chains"][0]["native_quest_before"]["res"] == 0
    assert result["chains"][0]["native_quest_at_settlement"]["res"] == 2


@pytest.mark.parametrize("mutation", [
    "wrong_actor", "stale_actor_version", "stale_loaded_generation", "interleaved_transaction",
    "no_player_insertion", "false_resolution", "null_resolution",
])
def test_real_message_chain_rejects_wrong_unknown_stale_or_interleaved(mutation):
    trace = native_trace("message")
    rows, quest, player = trace["rows"], trace["quest"], trace["player"]
    if mutation == "wrong_actor":
        player["actor"] = {"Character": 999999}
    elif mutation == "stale_actor_version":
        quest["quest"]["arbiter"]["Npc"]["version"] += 1
    elif mutation == "stale_loaded_generation":
        for binding in rows[0]["data"]["bindings"]:
            if binding["actor"] == player["actor"]:
                binding["server_entity"] += "-stale"
    elif mutation == "interleaved_transaction":
        next(row for row in rows if row["event"] == "dialogue_item_transfer")[
            "data"]["provenance"]["transaction_id"] += "-other"
    elif mutation == "no_player_insertion":
        rows = [row for row in rows if row["event"] != "dialogue_item_transfer"]
    else:
        next(row for row in rows if row["event"] == "quest_settlement")[
            "data"]["resolution"] = False if mutation == "false_resolution" else None
    assert not private.reward(rows, quest, player, after=0)["actual_reward_inserted"]


def merchant_result(trace):
    return npc_contracts.merchant_score(
        trace["fixture"], trace["final"], trace["receipts"], trace["samples"])


def test_real_merchant_transfer_payment_equipment_and_reserves():
    result = merchant_result(native_trace("merchant"))
    assert all(result["parts"].values())
    assert result["completed_exchanges"][0]["actual_item_deltas"] == {
        npc_contracts.COINS: -44, npc_contracts.SURPLUS: -1, npc_contracts.CHEST: 1}

@pytest.mark.parametrize("mutation", [None, "actor", "session", "lifecycle", "unknown", "future"])
def test_merchant_review_between_judge_polls_uses_only_own_completed_receipt(mutation):
    trace = native_trace("merchant")
    for tick, observation in enumerate([trace["fixture"]["opening"], *trace["samples"], trace["final"]]):
        observation["tick"] = tick
    review = next(observation for observation in trace["samples"]
                  if (observation["social_and_trade"]["pending_trade"] or {}).get("phase") == "Review")
    receipt = next(row for row in trace["receipts"]
                   if row["request"]["args"]["action"].get("phase") == "Review")
    receipt["response"]["result"]["observation"] = copy.deepcopy(review)
    trace["samples"] = [observation for observation in trace["samples"]
                        if (observation["social_and_trade"]["pending_trade"] or {}).get("phase") != "Review"]
    if mutation == "actor":
        receipt["actor"] += 1
    elif mutation == "session":
        receipt["response"]["session_id"] += "-other"
    elif mutation == "lifecycle":
        receipt["response"]["result"]["observation"]["control_actor"]["lifecycle"] += "-stale"
    elif mutation == "unknown":
        receipt["response"]["result"]["status"] = "unknown"
    elif mutation == "future":
        receipt["response"]["result"]["observation"]["tick"] = trace["final"]["tick"] + 1
    assert all(merchant_result(trace)["parts"].values()) == (mutation is None)


@pytest.mark.parametrize("mutation", [
    "wrong_merchant", "uncommitted", "rejected", "unknown_finish", "pending_only",
    "stale_merchant", "wrong_review_trade", "absent_payment", "absent_arrival",
    "wrong_equipment_slot", "dropped_equipment", "reserve_violation", "unknown_offer_slot",
])
def test_real_merchant_counterfactuals_fail(mutation):
    trace = native_trace("merchant")
    observations = [*trace["samples"], trace["final"]]
    fixture = trace["fixture"]
    if mutation == "wrong_merchant":
        fixture["merchant"]["uid"] += 1
    elif mutation in {"uncommitted", "rejected", "unknown_finish", "pending_only", "stale_merchant"}:
        for observation in observations:
            result = observation.get("last_trade_result")
            if not result:
                continue
            if mutation == "uncommitted":
                result["committed"] = False
            elif mutation == "rejected":
                result["result"] = "Declined"
            elif mutation == "unknown_finish":
                result["source"] = "unknown"
            elif mutation == "pending_only":
                observation["last_trade_result"] = None
            else:
                for party in result["parties"]:
                    if party and party["uid"] == fixture["merchant"]["uid"]:
                        party["lifecycle"] += "-stale"
    elif mutation in {"wrong_review_trade", "wrong_equipment_slot"}:
        for row in trace["receipts"]:
            action = row["request"]["args"]["action"]
            if mutation == "wrong_review_trade" and action.get("phase") == "Review":
                action["trade_id"] += 1
            elif mutation == "wrong_equipment_slot" and action["type"] == "equip":
                action["equip_slot"] = "head"
    elif mutation in {"absent_payment", "absent_arrival"}:
        for observation in observations:
            if not observation.get("last_trade_result"):
                continue
            if mutation == "absent_arrival":
                observation["survival"]["equipment"]["chest"] = None
                observation["inventory"]["items"] = [
                    row for row in observation["inventory"]["items"]
                    if npc_contracts.scene.item_id(row["item"]) != npc_contracts.CHEST]
            else:
                for row in observation["inventory"]["items"]:
                    if npc_contracts.scene.item_id(row["item"]) == npc_contracts.COINS:
                        row["item"]["amount"] = 10000
    elif mutation == "dropped_equipment":
        trace["final"]["survival"]["equipment"]["chest"] = None
    elif mutation == "reserve_violation":
        for row in trace["final"]["inventory"]["items"]:
            if npc_contracts.scene.item_id(row["item"]) == npc_contracts.COINS:
                row["item"]["amount"] = 19
    else:
        for observation in [fixture["opening"], *observations]:
            pending = observation["social_and_trade"]["pending_trade"]
            trade = pending or (observation.get("last_trade_result") or {}).get("trade")
            if trade and trade["offers"][0]:
                trade["offers"][0][0]["slot"] = 999999
    assert not all(merchant_result(trace)["parts"].values())


def test_later_legal_reacquisition_is_not_a_hidden_net_spending_requirement():
    trace = native_trace("merchant")
    for row in trace["final"]["inventory"]["items"]:
        if npc_contracts.scene.item_id(row["item"]) == npc_contracts.COINS:
            row["item"]["amount"] = 10010
        elif npc_contracts.scene.item_id(row["item"]) == npc_contracts.SURPLUS:
            row["item"]["amount"] = 11
    assert all(merchant_result(trace)["parts"].values())


def slay_chain():
    target = {"Npc": {"idx": 3, "version": 1}}
    player = {"actor": {"Character": 16}, "uid": 23, "server_entity": "Entity(22, Generation(1))",
              "client_session_id": "unit-session",
              "client_actor": {"uid": 23, "lifecycle": "101-202-1:Entity(9, Generation(1))"}}
    enemy = {"actor": target, "uid": 40, "server_entity": "Entity(0, Generation(2))"}
    rows = [
        {"sequence": 0, "event": "opened", "data": {"run_id": "unit-slay"}},
        {"sequence": 1, "event": "loaded_actor_bindings", "data": {"bindings": [player, enemy]}},
        {"sequence": 2, "event": "health_effect_applied", "data": {
            "target_actor": target, "target_uid": 40, "target_entity": enemy["server_entity"],
            "changed": True, "before": 10, "after": 0, "contributor": {"Solo": 23},
            "contributor_actor": player["actor"], "contributor_entity": player["server_entity"]}},
        {"sequence": 4, "event": "rtsim_death_hook", "data": {
            "actor": target, "uid": 40, "entity": enemy["server_entity"],
            "scope": "actual_native_DestroyEvent_death_hook;not_nearby_disappearance"}}]
    observations = [{"session_id": "unit-session",
                     "self_identity": {"uid": 23, "native_character_id": 16, "entity_id": 9},
                     "control_actor": player["client_actor"],
                     "nearby_entities": [{"uid": 40, "entity_id": 7, "control_target": {
                         "uid": 40, "lifecycle": "101-202-1:Entity(7, Generation(6))"}}]}]
    return rows, target, player, observations


def test_slay_unit_chain_requires_contribution_not_an_ability_or_last_blow():
    rows, target, player, observations = slay_chain()
    result = private.slay(rows, target, player, observations, after=0, run_id="unit-slay")
    assert result["native_target_death"] and result["player_contribution"]
    assert result["ability_requirement"] is None and result["last_blow_requirement"] is False


@pytest.mark.parametrize("mutation", [
    "wrong_actor", "stale_actor", "stale_server_generation", "unknown_target_uid",
    "foreign_client_incarnation", "wrong_client_entity_reference", "unknown_client_uid", "unload", "wrong_contributor",
    "unknown_contributor_actor", "stale_player_entity", "not_damage", "before_activation", "unloaded",
])
def test_slay_unit_unknown_stale_and_non_death_boundaries(mutation):
    rows, target, player, observations = slay_chain()
    after = 0
    if mutation == "wrong_actor":
        rows[-1]["data"]["actor"] = {"Npc": {"idx": 9, "version": 1}}
    elif mutation == "stale_actor":
        rows[-1]["data"]["actor"] = {"Npc": {"idx": 3, "version": 2}}
    elif mutation == "stale_server_generation":
        rows[1]["data"]["bindings"][1]["server_entity"] = "Entity(0, Generation(3))"
    elif mutation == "unknown_target_uid":
        rows[1]["data"]["bindings"][1]["uid"] = None
    elif mutation == "foreign_client_incarnation":
        observations[0]["nearby_entities"][0]["control_target"]["lifecycle"] = "different-bridge:Entity(7, Generation(6))"
    elif mutation == "wrong_client_entity_reference":
        observations[0]["nearby_entities"][0]["control_target"]["lifecycle"] = "101-202-1:Entity(8, Generation(6))"
    elif mutation == "unknown_client_uid":
        observations[0]["nearby_entities"][0]["control_target"]["uid"] = None
    elif mutation == "unload":
        rows[-1]["event"] = "rtsim_entity_unload"
    elif mutation == "wrong_contributor":
        rows[2]["data"]["contributor"] = {"Solo": 999}
    elif mutation == "unknown_contributor_actor":
        rows[2]["data"].pop("contributor_actor")
    elif mutation == "stale_player_entity":
        rows[2]["data"]["contributor_entity"] = "Entity(22, Generation(2))"
    elif mutation == "not_damage":
        rows[2]["data"]["after"] = 10
    elif mutation == "before_activation":
        after = 2
    else:
        rows.insert(3, {"sequence": 3, "event": "loaded_actor_bindings", "data": {"bindings": [player]}})
    result = private.slay(rows, target, player, observations, after=after, run_id="unit-slay")
    assert not (result["native_target_death"] and result["player_contribution"])


def test_native_player_death_cannot_be_hidden_between_alive_samples():
    rows, _, player, _ = slay_chain()
    death = copy.deepcopy(rows[-1])
    death["data"].update(actor=player["actor"], uid=player["uid"], entity=player["server_entity"])
    assert private.player_deaths([death], player, after=0) == [4]
    death["data"]["entity"] = "Entity(22, Generation(2))"
    assert private.player_deaths([death], player, after=0) == []


@pytest.mark.parametrize("case", sorted(npc_contracts.TASK_IDS))
@pytest.mark.parametrize("mode", ["image", "headless"])
def test_unqualified_selections_rejected_before_allocation(case, mode, tmp_path, tmp_path_factory):
    from .test_admission_closeout import selected_native
    selected = selected_native(tmp_path_factory.mktemp("TEST-ONLY-selected"))
    args = SimpleNamespace(scene=case, terrain="natural", observation_mode="image" if mode == "image" else "structured",
                           mode="capability" if mode == "capability" else "task-engineering",
                           harness="responses", backend="headless" if mode == "headless" else "renderer",
                           recordings=None, pins=tmp_path / "not-created.json", pins_sha256="0" * 64,
                           action_profile="visual-motor-v1" if mode == "image" else "raw-native-v2",
                           adapter_config=json.dumps({"mock_provider": "http://127.0.0.1:1234", "provider": providers.LUNA}))
    with patch.object(run.pins, "load", return_value=selected):
        with pytest.raises(ValueError, match="unqualified|unsupported renderer"):
            asyncio.run(run.episode(args))
    assert list(tmp_path.iterdir()) == []
    assert not renderer_support.support_matrix()["renderer"][f"{case}/natural"]["image"]["scored_evaluation"]


@pytest.mark.parametrize("mode", ["structured", "hybrid"])
def test_slay_scored_admission054_keeps_recording_boundary_before_allocation(tmp_path, tmp_path_factory, monkeypatch, mode):
    from .test_admission_closeout import selected_native, record_for, install_records
    selected = selected_native(tmp_path_factory.mktemp("TEST-ONLY-selected"))
    args = SimpleNamespace(scene="T12", terrain="natural", observation_mode="structured",
                           mode="capability", harness="responses", recordings=None)
    args.observation_mode = mode
    assert renderer_support.historical_policy()["renderer"]["T12/natural"][mode]["scored_evaluation"]
    assert not renderer_support.support_matrix()["renderer"]["T12/natural"][mode]["scored_evaluation"]
    definition = suite.load(Path(selected["dependencies"]["scene_manifest"]["path"]))["T12"]
    install_records(tmp_path_factory.mktemp("TEST-ONLY-qualified"), monkeypatch, [
        record_for(definition, selected, terrain="natural", mode=mode)])
    with pytest.raises(ValueError, match="recordings must be"):
        renderer_support.admit_selection(args, selected)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("case", ["T10", "T11", "T12"])
@pytest.mark.parametrize("mode", ["structured", "hybrid"])
def test_qualified_scored_selections_use_the_ordinary_renderer_profile(case, mode, tmp_path, tmp_path_factory, monkeypatch):
    from .test_admission_closeout import selected_native, record_for, install_records
    assert renderer_support.historical_policy()["renderer"][f"{case}/natural"][mode]["scored_evaluation"]
    assert not renderer_support.support_matrix()["renderer"][f"{case}/natural"][mode]["scored_evaluation"]
    args = SimpleNamespace(scene=case, terrain="natural", observation_mode=mode,
                           mode="capability", harness="responses",
                           recordings=tmp_path / "outputs/recordings/run", out=tmp_path / "private/run")
    selected = selected_native(tmp_path_factory.mktemp("TEST-ONLY-selected"))
    with pytest.raises(ValueError, match="unqualified"):
        renderer_support.admit_selection(args, selected)
    definition = suite.load(Path(selected["dependencies"]["scene_manifest"]["path"]))[case]
    install_records(tmp_path_factory.mktemp("TEST-ONLY-qualified"), monkeypatch, [
        record_for(definition, selected, terrain="natural", mode=mode)])
    with patch.object(renderer_support.output_paths, "validate_root"), patch.object(
            renderer_support.output_paths, "validate_output"), patch.object(
            renderer_support.runtime, "exclude_outputs"):
        cell = renderer_support.admit_selection(args, selected)
        assert cell["scored_evaluation"] and cell["action_profile"] == "ordinary-renderer-native-v1"
        assert not cell["accepted"] and cell["model_success"] == "not-inferred"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("distance,loaded,valid", [
    (0, True, False), (32, True, False), (48, True, True),
    (96, True, True), (97, True, False), (48, False, False),
])
def test_slay_opening_requires_an_actual_loaded_target_and_qualified_approach(distance, loaded, valid):
    target = {"Npc": {"idx": 3, "version": 1}}
    offer = {"kind": "Question"}
    opening = {"survival": {"health": {"current": 100}}, "inventory": {"free_slots": 3},
               "self_identity": {"uid": 10}, "dialogue": offer, "position": [0, 0, 0],
               "session_id": "test", "control_actor": {"uid": 10, "lifecycle": "client:Entity(0, Generation(1))"}}
    bindings = [{"uid": 10, "actor": {"Character": 16}, "server_entity": "player"}]
    if loaded:
        bindings.append({"uid": 40, "actor": target, "server_position": {"x": distance, "y": 0, "z": 0}})
    journal = [{"event": "loaded_actor_bindings", "data": {"bindings": bindings}, "sequence": 0}]
    fixture = {"case": "T12", "offer": offer, "target_actor": target, "slay_site": {}}
    actor = SimpleNamespace(request=AsyncMock(return_value=opening))
    with patch.object(npc_contracts, "rows", return_value=journal):
        if valid:
            asyncio.run(npc_contracts.validate_opening(actor, fixture))
            assert fixture["slay_site"]["opening_target_distance"] == distance
        else:
            with pytest.raises(RuntimeError, match="Slay opening"):
                asyncio.run(npc_contracts.validate_opening(actor, fixture))
