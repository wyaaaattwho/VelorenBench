"""Server-owner journal consumer. Never imported by public observation/tool code."""
from __future__ import annotations

import json
from pathlib import Path


def read(path: str | Path, run_id: str, *, complete: bool = False) -> list[dict]:
    raw = Path(path).read_bytes()
    lines = raw.splitlines(keepends=True)
    if lines and not lines[-1].endswith(b"\n"):
        if complete:
            raise ValueError("incomplete private native journal")
        lines.pop()
    rows = [json.loads(line) for line in lines]
    if not rows or rows[0]["event"] != "opened" or rows[0]["data"]["run_id"] != run_id:
        raise ValueError("private native journal run mismatch")
    for sequence, row in enumerate(rows):
        if (row["schema"], row["version"], row["private"], row["sequence"]) != (
                "rpg-native-oracle", "1.1", True, sequence):
            raise ValueError("private native journal schema/sequence mismatch")
        if sequence and row["event"] == "opened":
            raise ValueError("multiple native processes in private journal")
    return rows


def contributor_uid(value):
    if value is None:
        return None
    if "Solo" in value:
        return value["Solo"]
    return value["Group"]["entity_uid"]


def binding(rows: list[dict], uid: int) -> dict | None:
    snapshot = next((row for row in reversed(rows) if row["event"] == "combat_entity_bindings"), None)
    if snapshot is None:
        return None
    matches = [row for row in snapshot["data"]["bindings"] if row["uid"] == uid]
    if len(matches) > 1:
        raise ValueError("ambiguous native entity binding")
    return matches[0] if matches else None


def attack_join(rows: list[dict], effect: dict) -> dict:
    if effect["event"] != "health_effect_applied":
        raise ValueError("ability join requires actual native health application")
    applied = effect["data"]
    if not applied["changed"] or applied["before"] == applied["after"]:
        return {"status": "no_health_delta", "queue_event": None}
    if "change_time" not in applied:
        return {"status": "unknown", "reason": "native_change_time_unavailable", "queue_event": None}
    matches = []
    for row in rows:
        if row["event"] != "attack_health_effect_queued" or row["sequence"] >= effect["sequence"]:
            continue
        queued = row["data"]
        if (queued["source_uid"] is not None
                and queued["source_uid"] == contributor_uid(applied["contributor"])
                and queued["source_entity"] == applied["contributor_entity"]
                and queued["target_uid"] == applied["target_uid"]
                and queued["target_entity"] == applied["target_entity"]
                and queued["instance"] == applied["instance"]
                and queued["requested_amount"] == applied["requested_amount"]
                and queued["server_time"] == applied["change_time"]):
            matches.append(row)
    if len(matches) != 1:
        return {"status": "ambiguous" if matches else "unknown", "queue_event": None,
                "matching_queue_events": len(matches)}
    return {"status": "causally_attributed" if matches[0]["data"]["ability"] is not None
            else "ability_unknown", "queue_event": matches[0]}


def sourced_health(row: dict, source: dict, target: dict, *, healing: bool = False) -> bool:
    if row["event"] != "health_effect_applied":
        return False
    data = row["data"]
    return (data["changed"] is True
            and (data["after"] > data["before"] if healing else data["after"] < data["before"])
            and (data["requested_amount"] > 0 if healing else data["requested_amount"] < 0)
            and contributor_uid(data["contributor"]) == source["uid"]
            and data["contributor_entity"] == source["server_entity"]
            and data["target_uid"] == target["uid"]
            and data["target_entity"] == target["server_entity"])
