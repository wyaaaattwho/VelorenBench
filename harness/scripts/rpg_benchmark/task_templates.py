"""Small family-owned template contract, not an action DSL or plugin loader.

Legacy cases are internal mechanics selectors, never backend/runner admission.
The original manifest stays byte-for-byte intact. New definitions select a
semantic template explicitly and do not borrow a legacy task's qualification.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

FEATURES = frozenset({
    "movement", "combat", "inventory", "pickup", "crafting", "equipment",
    "dialogue", "trade", "skills", "map", "coordinate_goal", "terrain_event",
})
COMMON_FIELDS = {"id", "kind", "template", "terrains", "gameplay_mode", "task", "qualification",
                 "requires", "placements", "player_equipment", "selected_variant"}
_legacy = json.loads(Path(__file__).with_name("scenes.json").read_text())["scenes"]
LEGACY_FIELDS = {
    family: set().union(*(set(row) for row in _legacy if row["kind"] == family))
    for family in {row["kind"] for row in _legacy}}


def configure(module, definition):
    legacy = module.LEGACY_TEMPLATES
    name = definition.get("template", legacy.get(definition["id"]))
    if name not in module.TEMPLATES:
        raise ValueError(f"{definition['kind']}: unknown semantic template {name!r}; "
                         "new task IDs must select a supported template")
    spec = module.TEMPLATES[name]
    legacy_case = spec.get("legacy_case")
    legacy_fields = (set(next(row for row in _legacy if row["id"] == legacy_case))
                     if legacy_case is not None else LEGACY_FIELDS.get(definition["kind"], set()))
    unknown = set(definition) - (COMMON_FIELDS | legacy_fields
                                 | set(getattr(module, "PARAMETERS", ())))
    if unknown:
        raise ValueError(f"{name}: unknown task parameters {sorted(unknown)}")
    requirements = set(spec["requires"])
    if requirements - FEATURES:
        raise ValueError(f"unknown task features: {sorted(requirements - FEATURES)}")
    for field in spec.get("required", ()):
        if field not in definition:
            raise ValueError(f"{name}: missing required parameter {field}")
    for field in ("id", "kind", "gameplay_mode"):
        if not isinstance(definition.get(field), str) or not definition[field]:
            raise ValueError(f"{name}: invalid {field}")
    validate = getattr(module, "validate_definition", None)
    if validate:
        validate(definition)
    if definition.get("requires") is not None and set(definition["requires"]) != requirements:
        raise ValueError("task requirements must match the selected family template")
    configured = {**definition, "template": name, "requires": sorted(requirements)}
    if "variants" in definition:
        selected = definition.get("selected_variant", definition.get("default_variant", definition["variants"][0]))
        if selected not in definition["variants"]:
            raise ValueError("variant is not declared by this family/template")
        configured["selected_variant"] = selected
    return configured


def mechanics(module, definition):
    """Adapt unchanged legacy implementations at the owning family boundary."""
    spec = module.TEMPLATES[definition["template"]]
    return {**definition, "task_id": definition["id"],
            "id": spec.get("legacy_case", definition["id"])}


def identity(definition):
    return hashlib.sha256(json.dumps(definition, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def runtime(module, definition, *, renderer=False, variant=None, terrain=None):
    spec = module.TEMPLATES[definition["template"]]
    options = {"small_world": False, "resident_fixture": None, "deferred_actor": False,
               "retain_admin": False, **copy.deepcopy(spec.get("runtime", {}))}
    if terrain is not None and terrain not in definition["terrains"]:
        raise ValueError("terrain is not declared by this definition")
    # Compact natural worlds remain valid. Shared authored flat geometry uses
    # the original full-world coordinates, not the compact world's spawn area.
    if not renderer or terrain == "flat":
        options["small_world"] = False
    if variant is not None:
        if "variants" not in definition or variant not in definition["variants"]:
            raise ValueError("variant is not declared by this family/template")
        definition["selected_variant"] = variant
    elif "variants" in definition:
        definition["selected_variant"] = definition.get(
            "selected_variant", definition.get("default_variant", next(iter(definition["variants"]))))
    return options
