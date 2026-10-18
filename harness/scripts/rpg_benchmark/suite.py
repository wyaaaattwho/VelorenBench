from __future__ import annotations

import json
from pathlib import Path

from scripts.rpg_benchmark import (
    combat, combat_support, crafting, crafting_recovery, mixed, navigation,
    navigation_tasks, npc_family, task_templates,
)

MODULES = {"combat": combat, "crafting": crafting, "navigation": navigation, "mixed": mixed,
           "crafting_recovery": crafting_recovery, "navigation_tasks": navigation_tasks,
           "combat_support": combat_support, "npc_contracts": npc_family}


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
        result[definition["id"]] = task_templates.configure(MODULES[definition["kind"]], {
            **definition, "placements": value["placements"],
            "player_equipment": value["player_equipment"]})
    for definition in result.values():
        setup_dependencies(definition, result)
    return result


def setup_dependencies(definition: dict, definitions: dict) -> dict[str, dict]:
    """Resolve the family's actual direct setup references, not the catalog.

    Mixed currently composes primitive crafting/combat setup, not recursive
    suite.setup calls. Do not pretend nested compositions execute transitively.
    """
    module = MODULES[definition["kind"]]
    result = {}
    for role, (name, kind) in getattr(module, "SETUP_REFERENCES", {}).items():
        matches = [row for row in definitions.values() if row.get("id") == name]
        dependency = definitions.get(name)
        if (len(matches) != 1 or dependency is None or dependency.get("id") != name
                or dependency.get("kind") != kind):
            raise ValueError(f"missing or ambiguous setup dependency {role}: {name}/{kind}")
        dependency_module = MODULES[kind]
        if getattr(dependency_module, "SETUP_REFERENCES", {}):
            raise ValueError("nested setup dependencies are not supported by direct family composition")
        result[role] = task_templates.configure(dependency_module, dependency)
    return result


async def setup(admin, actor, definition: dict, terrain: str, definitions: dict) -> dict:
    if terrain not in definition["terrains"]:
        raise ValueError("scene terrain not admitted")
    module = MODULES[definition["kind"]]
    concrete = task_templates.mechanics(module, definition)
    if getattr(module, "SETUP_REFERENCES", {}):
        dependencies = setup_dependencies(definition, definitions)
        fixture = await module.setup(admin, actor, concrete, terrain, dependencies)
    elif concrete.get("layout") == "named_site_long":
        fixture = await navigation.setup_named_site(admin, actor, concrete, start_actor=True)
    else:
        fixture = await module.setup(admin, actor, concrete, terrain)
    fixture.update(task_id=definition["id"], semantic_template=definition["template"],
                   task_content_sha256=task_templates.identity(definition))
    return fixture


def runtime_options(definition, *, renderer=False, variant=None, terrain=None):
    module = MODULES[definition["kind"]]
    definition.update(task_templates.configure(module, definition))
    return task_templates.runtime(module, definition,
                                  renderer=renderer, variant=variant, terrain=terrain)


def public_observer(definition, fixture):
    function = getattr(MODULES[definition["kind"]], "public_observer", None)
    return function(fixture) if function else None


async def update_event(definition, admin, actor, fixture, final):
    function = getattr(MODULES[definition["kind"]], "update_event", None)
    if function:
        await function(admin, actor, fixture, final)


def finalize_environment(record):
    module = MODULES.get(record.get("task_kind"))
    function = getattr(module, "finalize_environment", None)
    if function:
        function(record)


def mechanism_verified(definition, score):
    function = getattr(MODULES[definition["kind"]], "mechanism_verified", None)
    return function(score) if function else score["success"]


def score(definition: dict, fixture: dict, final: dict, receipts: list[dict],
          events: list[dict], samples: list[dict]) -> dict:
    from .temporal import event_window
    return MODULES[definition["kind"]].score(
        fixture, final, receipts, event_window(fixture["opening"], final, events), samples)


async def mechanism(definition: dict, actor, fixture: dict) -> None:
    module = MODULES[definition["kind"]]
    if module is mixed:
        raise ValueError("mixed capability has no scripted mechanism solver")
    await module.mechanism(actor, fixture)
