from __future__ import annotations

import json
from pathlib import Path

from scripts.rpg_benchmark import (
    combat, combat_support, crafting, crafting_recovery, mixed, navigation,
    navigation_tasks, npc_contracts,
)

MODULES = {"combat": combat, "crafting": crafting, "navigation": navigation, "mixed": mixed,
           "crafting_recovery": crafting_recovery, "navigation_tasks": navigation_tasks,
           "combat_support": combat_support, "npc_contracts": npc_contracts}


def load(path: Path) -> dict[str, dict]:
    value = json.loads(path.read_text())
    if value.get("schema") != 1 or not value.get("version") or not value.get("seeds"):
        raise ValueError("invalid scene manifest header")
    result = {}
    for definition in value["scenes"]:
        if (definition["id"] in result or definition["kind"] not in MODULES
                or definition["gameplay_mode"] not in ("economy", "work-state")
                or not definition["terrains"] or set(definition["terrains"]) - {"flat", "natural"}):
            raise ValueError("invalid or duplicate concrete scene")
        if definition["kind"] != "navigation" and not definition.get("task"):
            raise ValueError("missing macro task")
        result[definition["id"]] = {**definition, "placements": value["placements"],
                                    "player_equipment": value["player_equipment"]}
    return result


async def setup(admin, actor, definition: dict, terrain: str, definitions: dict) -> dict:
    if terrain not in definition["terrains"]:
        raise ValueError("scene terrain not admitted")
    if definition["kind"] == "mixed":
        return await mixed.setup(admin, actor, definition, terrain, definitions)
    return await MODULES[definition["kind"]].setup(admin, actor, definition, terrain)


def score(definition: dict, fixture: dict, final: dict, receipts: list[dict],
          events: list[dict], samples: list[dict]) -> dict:
    return MODULES[definition["kind"]].score(fixture, final, receipts, events, samples)


async def mechanism(definition: dict, actor, fixture: dict) -> None:
    module = MODULES[definition["kind"]]
    if module is mixed:
        raise ValueError("mixed capability has no scripted mechanism solver")
    await module.mechanism(actor, fixture)
