"""Full-score regressions from exact closed012/013 records, not a new evaluator."""
import copy
import json
from pathlib import Path
import re
from unittest.mock import patch

import pytest

from scripts.rpg_benchmark import contract_evidence as evidence, npc_contracts, run
from .historical import historical_fixture


NAMES = ("final051-structured-012", "final051-hybrid-013")


def captured(name):
    return json.loads(historical_fixture("scorer-identity-052.json").read_text())[name]


def score(trace):
    fixture = trace["fixture"]
    fixture["oracle_path"] = "sparse-unit-only; full derived replay uses original complete journal"
    # Keep actual score/own-sample filtering/activation/reward/combat integration.
    # Sparse rows retain their original sequence numbers; production read_journal
    # validation is covered separately and by the complete installed replay.
    with patch.object(npc_contracts, "rows", return_value=trace["rows"]), patch.object(
            evidence, "journal_tail", return_value=trace["tail"]):
        return npc_contracts.score(fixture, trace["final"], trace["receipts"], [], trace["samples"])


@pytest.mark.parametrize("name", NAMES)
def test_actual_closed_chain_full_score_with_independent_ECS(name):
    trace = captured(name)
    assert trace["original_score"]["success"] is (name == NAMES[1])
    result = score(trace)
    assert result["success"] and all(result["parts"].values())
    assert not result["dead"] and result["same_actor"]
    assert result["settlement"]["chains"][0]["amount"] == 1000
    original_refs = {entry["control_target"]["lifecycle"] for view in trace["samples"]
                     for entry in view["nearby_entities"]}
    assert all(link["lifecycle"] in original_refs for link in result["combat"]["client_uid_lifecycle_links"])
    if name == NAMES[0]:
        assert result["combat"]["death_sequences"] == [2670]
        assert len(result["combat"]["contribution_sequences"]) == 9
        assert {link["lifecycle"].split(":", 1)[1] for link in result["combat"]["client_uid_lifecycle_links"]} == {
            "Entity(30, Generation(4))"}


MUTATIONS = (
    "wrong_run", "wrong_journal_run", "wrong_session", "wrong_observer_uid",
    "wrong_observer_character", "wrong_observer_control_actor", "wrong_final_control_actor",
    "preopening_sample", "future_sample",
    "missing_player_client_scope", "wrong_player_client_uid", "wrong_player_native_uid",
    "wrong_player_native_entity", "wrong_target_actor", "wrong_target_uid", "wrong_public_uid",
    "wrong_control_uid", "foreign_client_incarnation", "wrong_client_entity_reference",
    "missing_client_lifecycle", "malformed_client_lifecycle", "ambiguous_public_reference",
    "stale_native_generation", "missing_native_binding", "ambiguous_actor_binding",
    "ambiguous_uid_binding", "reused_native_uid_history", "missing_contributor_binding",
    "unload_not_death", "no_death", "unrelated_contributor", "wrong_contributor_actor",
    "stale_contributor_entity", "no_damage", "unchanged_effect", "preactivation",
    "reward_rejection", "player_death",
)


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("mutation", MUTATIONS)
def test_full_score_rejects_scope_reference_binding_effect_and_reward_counterfactuals(name, mutation):
    trace = captured(name)
    f, rows = trace["fixture"], trace["rows"]
    target, player = f["target_actor"], f["player"]
    death = next(row for row in rows if row["event"] == "rtsim_death_hook"
                 and row["data"]["actor"] == target)
    uid = death["data"]["uid"]
    target_entries = [entry for view in trace["samples"] for entry in view["nearby_entities"]
                      if entry["uid"] == uid]
    assert target_entries and all(entry["uid"] != uid for entry in f["opening"]["nearby_entities"])
    effects = [row for row in rows if row["event"] == "health_effect_applied"]
    other_generation = lambda value: re.sub(r"Generation\((\d+)\)",
        lambda match: f"Generation({int(match[1]) + 1})", value)
    snapshots = [row for row in rows if row["event"] == "loaded_actor_bindings"]
    last_binding = next(row for row in reversed(snapshots) if row["sequence"] < death["sequence"])
    if mutation == "wrong_run":
        f["run_id"] += "-other"
    elif mutation == "wrong_journal_run":
        rows[0]["data"]["run_id"] += "-other"
    elif mutation in {"wrong_session", "wrong_observer_uid", "wrong_observer_character",
                      "wrong_observer_control_actor"}:
        for view in trace["samples"]:
            if mutation == "wrong_session":
                view["session_id"] += "-other"
            elif mutation == "wrong_observer_uid":
                view["self_identity"]["uid"] += 1
            elif mutation == "wrong_observer_character":
                view["self_identity"]["native_character_id"] += 1
            else:
                view["control_actor"]["lifecycle"] += "-stale"
    elif mutation == "wrong_final_control_actor":
        trace["final"]["control_actor"]["lifecycle"] += "-stale"
    elif mutation in {"preopening_sample", "future_sample"}:
        for view in trace["samples"]:
            view["tick"] = f["opening"]["tick"] - 1 if mutation == "preopening_sample" else trace["final"]["tick"] + 1
    elif mutation == "missing_player_client_scope":
        player.pop("client_session_id")
    elif mutation == "wrong_player_client_uid":
        player["client_actor"]["uid"] += 1
    elif mutation == "wrong_player_native_uid":
        player["uid"] += 1
    elif mutation == "wrong_player_native_entity":
        player["server_entity"] = other_generation(player["server_entity"])
    elif mutation == "wrong_target_actor":
        target["Npc"]["version"] += 1
    elif mutation == "wrong_target_uid":
        for snapshot in snapshots:
            for binding in snapshot["data"]["bindings"]:
                if binding["actor"] == target:
                    binding["uid"] += 1
    elif mutation in {"wrong_public_uid", "wrong_control_uid", "foreign_client_incarnation",
                      "wrong_client_entity_reference", "missing_client_lifecycle", "malformed_client_lifecycle"}:
        for entry in target_entries:
            if mutation == "wrong_public_uid":
                entry["uid"] += 1
            elif mutation == "wrong_control_uid":
                entry["control_target"]["uid"] += 1
            elif mutation == "foreign_client_incarnation":
                entry["control_target"]["lifecycle"] = "different-bridge:" + entry["control_target"]["lifecycle"].split(":", 1)[1]
            elif mutation == "wrong_client_entity_reference":
                entry["control_target"]["lifecycle"] = player["client_actor"]["lifecycle"]
            elif mutation == "missing_client_lifecycle":
                entry["control_target"].pop("lifecycle")
            else:
                entry["control_target"]["lifecycle"] += "-stale"
    elif mutation == "ambiguous_public_reference":
        for view in trace["samples"]:
            entry = next(row for row in view["nearby_entities"] if row["uid"] == uid)
            view["nearby_entities"].append(copy.deepcopy(entry))
    elif mutation == "stale_native_generation":
        for snapshot in snapshots:
            for binding in snapshot["data"]["bindings"]:
                if binding["actor"] == target:
                    binding["server_entity"] = other_generation(binding["server_entity"])
    elif mutation == "missing_native_binding":
        last_binding["data"]["bindings"] = [entry for entry in last_binding["data"]["bindings"]
                                            if entry["actor"] != target]
    elif mutation in {"ambiguous_actor_binding", "ambiguous_uid_binding"}:
        duplicate = copy.deepcopy(next(entry for entry in last_binding["data"]["bindings"] if entry["actor"] == target))
        if mutation == "ambiguous_uid_binding":
            duplicate["actor"]["Npc"]["idx"] += 100
        last_binding["data"]["bindings"].append(duplicate)
    elif mutation == "reused_native_uid_history":
        old = copy.deepcopy(last_binding)
        old["sequence"] = 1
        binding = next(entry for entry in old["data"]["bindings"] if entry["uid"] == uid)
        binding["server_entity"] = other_generation(binding["server_entity"])
        rows.insert(1, old)
    elif mutation == "missing_contributor_binding":
        for snapshot in snapshots:
            snapshot["data"]["bindings"] = [entry for entry in snapshot["data"]["bindings"]
                                             if entry["actor"] != player["actor"]]
    elif mutation == "unload_not_death":
        death["event"] = "rtsim_entity_unload"
    elif mutation == "no_death":
        rows.remove(death)
    elif mutation in {"unrelated_contributor", "wrong_contributor_actor", "stale_contributor_entity",
                      "no_damage", "unchanged_effect"}:
        for effect in effects:
            data = effect["data"]
            if mutation == "unrelated_contributor":
                data["contributor"] = {"Solo": 999999}
            elif mutation == "wrong_contributor_actor":
                data["contributor_actor"] = {"Character": 999999}
            elif mutation == "stale_contributor_entity":
                data["contributor_entity"] = other_generation(data["contributor_entity"])
            elif mutation == "no_damage":
                data["after"] = data["before"]
            else:
                data["changed"] = False
    elif mutation == "preactivation":
        next(row for row in rows if row["event"] == "quest_states")["sequence"] = death["sequence"] - 1
        rows.sort(key=lambda row: row["sequence"])
    elif mutation == "reward_rejection":
        created = next(row for row in rows if row["event"] == "quest_reward_created")
        rows.append({"sequence": rows[-1]["sequence"] + 1, "event": "quest_reward_delivery_rejected",
                     "data": {"provenance": copy.deepcopy(created["data"])}})
    else:
        own_death = copy.deepcopy(death)
        own_death["sequence"] = rows[-1]["sequence"] + 1
        own_death["data"].update(actor=player["actor"], uid=player["uid"], entity=player["server_entity"])
        rows.append(own_death)
    result = score(trace)
    assert not result["success"], mutation
    if mutation in {"unrelated_contributor", "wrong_contributor_actor", "stale_contributor_entity",
                    "no_damage", "unchanged_effect", "preactivation"}:
        assert result["parts"]["native_target_death"]
        assert not result["parts"]["public_player_contribution"]
    if mutation == "reward_rejection":
        assert result["parts"]["native_target_death"] and result["parts"]["public_player_contribution"]
        assert not result["parts"]["actual_player_reward"]


@pytest.mark.parametrize("name", NAMES)
def test_actual_score_drives_existing_normal_entry_terminal_detection(name):
    trace = captured(name)
    record = {"score": score(trace)}
    assert run.measurement_stop(record, trace["fixture"]["definition"], {"valid": True},
                                entry_exited=False, horizon_reached=False)
    assert record["stop_reason"] == "native_goal_success"
    # This is private engineering accounting, not a model capability assertion.
    assert record["classification"] == "capability_success"


def test_resolution_and_bag_without_own_contribution_do_not_stop_normal_entry():
    trace = captured(NAMES[0])
    for row in trace["rows"]:
        if row["event"] == "health_effect_applied":
            row["data"]["contributor"] = {"Solo": 99999}
    record = {"score": score(trace)}
    assert record["score"]["parts"]["native_resolution_true"]
    assert record["score"]["parts"]["actual_player_reward"]
    assert not run.measurement_stop(record, trace["fixture"]["definition"], {"valid": True},
                                    entry_exited=False, horizon_reached=False)


def test_full_score_merchant_outcome_is_unchanged():
    trace = json.loads(historical_fixture("native-contract-traces.json").read_text())["merchant"]
    trace["fixture"].update(case="T10", definition={"fixture_version": "captured-T10"},
                            oracle_cutoff=0)
    trace["rows"], trace["tail"] = [], {}
    result = score(trace)
    assert result["success"] and all(result["parts"].values())


def test_full_score_message_outcome_is_unchanged():
    source = json.loads(historical_fixture("native-contract-traces.json").read_text())
    message, merchant = source["message"], source["merchant"]
    instance = message["quest"]["quest"]["kind"]["Courier"]["instance"]
    opening = copy.deepcopy(merchant["fixture"]["opening"])
    opening["self_identity"]["uid"] = message["player"]["uid"]
    offer = captured(NAMES[0])["fixture"]["offer"]
    yes = next(choice["response_id"] for choice in offer["responses"] if npc_contracts.key(choice["content"]) == "common-yes")
    action = {"type": "dialogue_action", "action_id": "message-unit", "operation": "respond",
              "sender_uid": offer["sender_uid"], "dialogue_id": offer["dialogue_id"], "tag": offer["tag"], "response_id": yes}
    receipt = {"actor": message["player"]["uid"], "request": {"args": {"action": action}},
               "response": {"ok": True, "session_id": opening["session_id"], "result": {
                   "status": "completed", "action_type": "dialogue_action", "action_id": "message-unit"}}}
    activation = {"sequence": 1, "event": "quest_states", "data": [{**message["quest"], "resolution": None}]}
    rows = copy.deepcopy(message["rows"])
    for row in rows:
        row["sequence"] += 2
    trace = {"fixture": {"case": "T11", "definition": {"fixture_version": "captured-T11"},
                         "opening": opening, "player": message["player"], "offer": offer,
                         "giver_actor": instance["source_actor"], "recipient_actor": instance["target_actor"],
                         "oracle_cutoff": 0, "preexisting_quest_ids": []},
             "rows": [activation, *rows], "tail": {}, "samples": [], "final": copy.deepcopy(opening), "receipts": [receipt]}
    assert score(trace)["success"]
