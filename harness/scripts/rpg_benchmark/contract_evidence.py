"""Server-private contract joins. Never included in the player wire."""
from __future__ import annotations

import json
from pathlib import Path
import re

SLAY_EVALUATOR_VERSION = "slay-evidence/2-independent-ecs"
_ENTITY = r"Entity\((\d+), Generation\(([1-9]\d*)\)\)"
_CLIENT_REF = re.compile(r"([^:\s]+):" + _ENTITY)


def read_journal(path: str, run_id: str) -> list[dict]:
    data = Path(path).read_bytes()
    lines = data.split(b"\n")[:-1]
    rows = [json.loads(line) for line in lines]
    if (not rows or rows[0]["event"] != "opened"
            or rows[0]["data"]["run_id"] != run_id):
        raise ValueError("private native journal run identity mismatch")
    for sequence, row in enumerate(rows):
        if ((row["schema"], row["version"], row["private"], row["sequence"])
                != ("rpg-native-oracle", "1.1", True, sequence)
                or sequence and row["event"] == "opened"):
            raise ValueError("private native journal identity/sequence mismatch")
    return rows


def journal_tail(path: str) -> dict:
    data = Path(path).read_bytes()
    trailing = data.rsplit(b"\n", 1)[-1]
    return {"complete_record_count": data.count(b"\n"),
            "incomplete_trailing_bytes": len(trailing),
            "closed_on_record_boundary": not trailing}


def latest(rows: list[dict], name: str, default=None):
    return next((row["data"] for row in reversed(rows) if row["event"] == name), default)


def bindings(rows: list[dict]) -> list[dict]:
    return latest(rows, "loaded_actor_bindings", {}).get("bindings", [])


def binding_at(rows: list[dict], actor: dict, sequence: int):
    snapshot = next((row["data"]["bindings"] for row in reversed(rows)
                     if row["event"] == "loaded_actor_bindings" and row["sequence"] < sequence), [])
    return next((entry for entry in snapshot if entry["actor"] == actor), None)


def actor_keys(actors):
    return sorted(json.dumps(actor, sort_keys=True) for actor in actors)


def quest_identity(quest):
    # Native Quest.res is the sole mutable field: pending0 -> false1 / true2.
    return {key: value for key, value in quest.items() if key != "res"}


def contributor_uid(value):
    if not isinstance(value, dict):
        return None
    return value.get("Solo", (value.get("Group") or {}).get("entity_uid"))


def reward(rows: list[dict], quest: dict, player: dict, *, after: int) -> dict:
    """Require one exact settlement -> statement -> removal -> insertion chain."""
    failures, chains, resolutions = [], [], []
    core = ("transaction_id", "quest_id", "quest", "participants", "arbiter",
            "requested_success", "resolution", "deposit_restoration")
    for settled in rows:
        data = settled["data"]
        if (settled["event"] != "quest_settlement" or settled["sequence"] <= after
                or data.get("quest_id") != quest["quest_id"]):
            continue
        resolutions.append(data.get("resolution"))
        if (quest_identity(data.get("quest") or {}) != quest_identity(quest["quest"])
                or actor_keys(data.get("participants", [])) != actor_keys(
                    entry["actor"] for entry in quest["actors"])
                or data.get("arbiter") != quest["quest"]["arbiter"]
                or data.get("requested_success") is not True or data.get("resolution") is not True):
            continue
        created = [row for row in rows if row["event"] == "quest_reward_created"
                   and row["sequence"] > settled["sequence"]
                   and all(row["data"].get(key) == data.get(key) for key in core)]
        if len(created) != 1:
            failures.append("missing_or_ambiguous_reward_statement")
            continue
        statement = created[0]
        provenance = statement["data"]
        delivery = provenance.get("delivery") or {}
        promised = delivery.get("given_item")
        if (delivery.get("recipient") != player["actor"]
                or delivery.get("recipient_entity") != player["server_entity"]
                or delivery.get("sender") != data["arbiter"]
                or not isinstance(delivery.get("sender_entity"), str)
                or not isinstance(promised, list) or len(promised) != 2
                or type(promised[1]) is not int or promised[1] <= 0):
            failures.append("wrong_or_unknown_reward_actor_entity_or_item")
            continue
        expected_item = {"Simple": promised[0]["item_definition_id"]}
        transaction = data["transaction_id"]
        rejected = [row for row in rows if row["sequence"] > statement["sequence"]
                    and row["event"] in {
                        "dialogue_item_transfer_rejected", "dialogue_item_transfer_failed",
                        "quest_reward_delivery_rejected", "quest_reward_provenance_rejected",
                        "quest_reward_undelivered"}
                    and (row["data"].get("provenance") or row["data"]).get("transaction_id") == transaction]
        transfers = [row for row in rows if row["event"] == "dialogue_item_transfer"
                     and row["sequence"] > statement["sequence"]
                     and row["data"].get("provenance") == provenance]
        removals = [row for row in rows if row["event"] == "dialogue_sender_removal"
                    and statement["sequence"] < row["sequence"]
                    and row["data"].get("provenance") == provenance]
        if rejected or not transfers or not removals:
            failures.append("reward_rejected_partial_unloaded_or_absent")
            continue
        amount, sequences = 0, []
        for transfer in transfers:
            entry = transfer["data"]
            dialogue = entry.get("dialogue") or {}
            content = (dialogue.get("kind") or {}).get("Statement") or {}
            removed = [row for row in removals if row["sequence"] < transfer["sequence"]
                       and row["data"].get("dialogue_id") == delivery["dialogue_id"]
                       and row["data"].get("sender_uid") == entry.get("sender_uid")
                       and row["data"].get("recipient_uid") == player["uid"]
                       and row["data"].get("removed_items") == [entry.get("item")]]
            item = entry.get("item") or {}
            quantity = item.get("amount")
            sender = binding_at(rows, delivery["sender"], transfer["sequence"])
            recipient = binding_at(rows, player["actor"], transfer["sequence"])
            if (len(removed) != 1 or entry.get("quest_id") != quest["quest_id"]
                    or sender is None or recipient is None
                    or sender.get("uid") != entry.get("sender_uid")
                    or sender.get("server_entity") != delivery["sender_entity"]
                    or recipient.get("uid") != player["uid"]
                    or recipient.get("server_entity") != player["server_entity"]
                    or entry.get("recipient_uid") != player["uid"]
                    or entry.get("effect") != "removed_from_sender_inserted_into_recipient"
                    or dialogue.get("id") != delivery["dialogue_id"]
                    or entry.get("dialogue_id") != delivery["dialogue_id"]
                    or content.get("tag") != delivery["tag"]
                    or content.get("given_item") != promised
                    or item.get("definition_id") != expected_item
                    or type(quantity) is not int or quantity <= 0
                    or type(entry.get("recipient_before")) is not int
                    or entry.get("recipient_after") != entry["recipient_before"] + quantity):
                failures.append("unbound_reward_transfer")
                break
            amount += quantity
            sequences.append(transfer["sequence"])
        else:
            if amount == promised[1]:
                chains.append({"transaction_id": transaction, "quest_id": quest["quest_id"],
                               "settlement_sequence": settled["sequence"],
                               "creation_sequence": statement["sequence"],
                               "removal_sequences": [row["sequence"] for row in removals],
                               "insertion_sequences": sequences, "item": expected_item, "amount": amount,
                               "native_quest_before": quest["quest"], "native_quest_at_settlement": data["quest"]})
    return {"resolution": resolutions[-1] if resolutions else None,
            "actual_reward_inserted": len(chains) == 1, "chains": chains, "unattributed": failures}


def _client_reference(ref):
    if not isinstance(ref, dict) or type(ref.get("uid")) is not int or not 0 < ref["uid"] < 2**64:
        return None
    match = _CLIENT_REF.fullmatch(ref.get("lifecycle", "")) if isinstance(ref.get("lifecycle"), str) else None
    return (match[1], int(match[2]), int(match[3])) if match else None


def slay_player_scope(observation: dict, player: dict) -> bool:
    """The fixture pins the actual client stream, separately from server ECS.

    Native entity_ref uses bridge-incarnation:client-Entity(index,generation).
    The UID is network universal within ONE server lifetime, not across runs.
    self_identity additionally binds the native Character and local player slot.
    """
    ref = observation.get("control_actor")
    parsed = _client_reference(ref)
    identity = observation.get("self_identity") or {}
    return bool(parsed and player.get("client_session_id")
                and observation.get("session_id") == player["client_session_id"]
                and ref == player.get("client_actor") and ref["uid"] == player.get("uid")
                and identity.get("uid") == player.get("uid")
                and identity.get("native_character_id") == (player.get("actor") or {}).get("Character")
                and identity.get("entity_id") == parsed[1])


def _slay_binding_at(rows, actor, sequence):
    """Unique current native actor/UID/server-generation; never first-match."""
    snapshot = next((row["data"]["bindings"] for row in reversed(rows)
                     if row["event"] == "loaded_actor_bindings" and row["sequence"] < sequence), [])
    matches = [entry for entry in snapshot if entry.get("actor") == actor]
    if len(matches) != 1:
        return None
    bound = matches[0]
    uid, entity = bound.get("uid"), bound.get("server_entity")
    if (type(uid) is not int or not 0 < uid < 2**64 or not isinstance(entity, str)
            or not re.fullmatch(_ENTITY, entity)
            or sum(entry.get("uid") == uid for entry in snapshot) != 1
            or sum(entry.get("server_entity") == entity for entry in snapshot) != 1):
        return None
    return bound


def _npc_uid_lifetime(rows, bound, before):
    # IdMaps allocates monotonically in one server lifetime. NPC unload/reload
    # may get a new UID; a given NPC UID must not alias another actor/generation.
    # Character exit DOES support UID remapping, so do not generalize this
    # invariant to players: their exact fixture entity is checked at each effect.
    for row in rows:
        if row["event"] == "loaded_actor_bindings" and row["sequence"] < before:
            aliases = [entry for entry in row["data"]["bindings"] if entry.get("uid") == bound["uid"]]
            if aliases and (len(aliases) != 1 or aliases[0].get("actor") != bound["actor"]
                            or aliases[0].get("server_entity") != bound["server_entity"]):
                return False
    return True


def _slay_client_links(observations, player, uid):
    links = []
    player_ref = _client_reference(player.get("client_actor"))
    for observation in observations:
        if not slay_player_scope(observation, player):
            continue
        nearby = observation.get("nearby_entities") or []
        entries = [entry for entry in nearby if entry.get("uid") == uid]
        if len(entries) != 1:
            continue
        entry = entries[0]
        ref = entry.get("control_target")
        parsed = _client_reference(ref)
        if (not parsed or ref["uid"] != uid or parsed[0] != player_ref[0]
                or type(entry.get("entity_id")) is not int or entry["entity_id"] != parsed[1]
                or parsed[1] == player_ref[1]
                or sum(row.get("entity_id") == parsed[1] for row in nearby) != 1):
            continue
        # Retain the authentic client generation as evidence, not as a server
        # Entity suffix. Mutation-time validation of that reference is unchanged.
        links.append({"uid": uid, "lifecycle": ref["lifecycle"]})
    return links


def slay(rows: list[dict], target: dict, player: dict, observations: list[dict], *,
         after: int, run_id: str | None = None) -> dict:
    deaths, contributions, client_links = [], [], []
    same_run = (isinstance(run_id, str) and bool(run_id) and bool(rows)
                and rows[0]["event"] == "opened" and rows[0]["sequence"] == 0
                and rows[0]["data"].get("run_id") == run_id
                and sum(row["event"] == "opened" for row in rows) == 1)
    for death in rows if same_run else []:
        if (death["event"] != "rtsim_death_hook" or death["sequence"] <= after
                or death["data"].get("actor") != target
                or death["data"].get("scope") != "actual_native_DestroyEvent_death_hook;not_nearby_disappearance"):
            continue
        data = death["data"]
        bound = _slay_binding_at(rows, target, death["sequence"])
        if (bound is None or bound.get("uid") != data.get("uid")
                or bound.get("server_entity") != data.get("entity")
                or not _npc_uid_lifetime(rows, bound, death["sequence"])):
            continue
        links = _slay_client_links(observations, player, data["uid"])
        effects = [row for row in rows if row["event"] == "health_effect_applied"
                   and after < row["sequence"] < death["sequence"]
                   and row["data"].get("target_actor") == target
                   and row["data"].get("target_uid") == data["uid"]
                   and row["data"].get("target_entity") == data["entity"]
                   and row["data"].get("changed") is True
                   and row["data"].get("after", 0) < row["data"].get("before", 0)
                   and contributor_uid(row["data"].get("contributor")) == player["uid"]
                   and row["data"].get("contributor_actor") == player["actor"]
                   and row["data"].get("contributor_entity") == player["server_entity"]
                   and (_slay_binding_at(rows, target, row["sequence"]) or {}).get(
                       "server_entity") == data["entity"]
                   and (_slay_binding_at(rows, target, row["sequence"]) or {}).get("uid") == data["uid"]
                   and (_slay_binding_at(rows, player["actor"], row["sequence"]) or {}).get(
                       "server_entity") == player["server_entity"]
                   and (_slay_binding_at(rows, player["actor"], row["sequence"]) or {}).get("uid") == player["uid"]]
        if links:
            deaths.append(death["sequence"])
            contributions.extend(row["sequence"] for row in effects)
            client_links.extend(links)
    return {"native_target_death": bool(deaths), "player_contribution": bool(contributions),
            "death_sequences": deaths, "contribution_sequences": sorted(set(contributions)),
            "client_uid_lifecycle_links": client_links,
            "ability_requirement": None, "last_blow_requirement": False,
            "evaluator_version": SLAY_EVALUATOR_VERSION,
            "identity_join": "same-run UID + native actor/server-generation; distinct scoped client lifecycle"}


def player_deaths(rows: list[dict], player: dict, *, after: int) -> list[int]:
    return [row["sequence"] for row in rows if row["sequence"] > after
            and row["event"] == "rtsim_death_hook"
            and row["data"].get("actor") == player["actor"]
            and row["data"].get("uid") == player["uid"]
            and row["data"].get("entity") == player["server_entity"]
            and row["data"].get("scope") ==
            "actual_native_DestroyEvent_death_hook;not_nearby_disappearance"]
