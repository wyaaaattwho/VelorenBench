"""Authoritative feature/profile/admission computation.

Implementation is not integration qualification, and neither is model success.
Unqualified successor cells are engineering-only. Historical evidence is audit
data, never current-native qualification.
"""
from pathlib import Path
import json

from . import capabilities, lifecycle, qualification, suite, task_templates

MODES = ("structured", "hybrid", "image")
RUN_MODES = {"setup", "interface-smoke", "mechanism", "task-engineering", "capability"}
ENGINEERING_MODES = {"setup", "interface-smoke", "task-engineering"}
NATIVE = json.loads(Path(__file__).with_name("native_features.json").read_text())
RENDERER_FEATURES = frozenset(NATIVE["renderer"])
if NATIVE["schema"] != "velorenbench-native-features/1" or RENDERER_FEATURES - task_templates.FEATURES:
    raise ValueError("unknown native feature declaration")
HEADLESS_FAMILIES = {"combat", "crafting", "navigation", "mixed"}


def catalog():
    return suite.load(Path(suite.__file__).with_name("scenes.json"))


def historical_cell(definition, terrain, mode):
    from .renderer_support import historical_policy
    baseline = catalog().get(definition["id"])
    if baseline is None or task_templates.identity(baseline) != task_templates.identity(definition):
        return {}
    return historical_policy()["renderer"].get(f"{definition['id']}/{terrain}", {}).get(mode, {})


def resolve(definition, terrain, backend, mode, profile=None, *, native_features=None,
            native_identity=None, definitions=None, task_manifest_sha256=None):
    if backend not in {"renderer", "headless"} or mode not in MODES:
        raise ValueError("unknown backend or observation mode")
    definition = task_templates.configure(suite.MODULES[definition["kind"]], definition)
    required = set(definition["requires"])
    if native_features is not None and set(native_features) - task_templates.FEATURES:
        raise ValueError("unknown native features")
    # The shared client implements these semantic operations. Do not turn lack
    # of headless integration qualification into an invented missing feature.
    available = set(native_features if native_features is not None else RENDERER_FEATURES)
    if profile is None:
        profile = capabilities.default_profile(backend, mode)
    profiles = ({"raw-native-v2"} if backend == "headless" else
                {"visual-motor-v1", "visual-ui-v1"} if mode == "image" else
                {"ordinary-renderer-native-v1"})
    # The legacy visual motor profile never gains semantic UI operations.
    if backend == "renderer" and mode == "image" and profile == "visual-motor-v1":
        available &= {"movement", "combat", "coordinate_goal"}
    reasons = []
    if terrain not in definition["terrains"]:
        reasons.append("terrain not defined by task")
    if profile not in profiles:
        reasons.append("unsupported observation mode/action profile")
    if backend == "headless" and mode != "structured":
        reasons.append("headless image/hybrid is unqualified; only structured observations are implemented")
    headless_unqualified = backend == "headless" and definition["kind"] not in HEADLESS_FAMILIES
    missing = sorted(required - available)
    if missing:
        reasons.append("missing native/profile features: " + ", ".join(missing))
    historical = historical_cell(definition, terrain, mode) if backend == "renderer" else {}
    # Raw catalog bytes are provenance only. Only this task and its family's
    # actual setup references may affect the semantic qualification key.
    manifest_sha = task_manifest_sha256
    if manifest_sha is None and definitions is None:
        manifest_sha = lifecycle.sha(Path(suite.__file__).with_name("scenes.json"))
    if manifest_sha is not None and not qualification._hash(manifest_sha):
        raise ValueError("invalid task manifest SHA-256")
    dependencies = suite.setup_dependencies(
        definition, definitions if definitions is not None else catalog())
    identity = qualification.key(definition, terrain, backend, mode, profile, native_identity, dependencies)
    if native_identity is not None:
        qualification.validate_native(native_identity)
    record = qualification.matching(identity)
    qualified = bool(not reasons and native_identity is not None and native_features is not None
                     and record and record["status"] == "engineering-qualified")
    allowed = set(ENGINEERING_MODES)
    if qualified:
        allowed = set(record["allowed_run_modes"])
    if backend == "headless" and not reasons:
        allowed = set() if headless_unqualified else RUN_MODES.copy()
    spec = suite.MODULES[definition["kind"]].TEMPLATES[definition["template"]]
    restriction = spec.get("run_modes_by_observation_mode", {}).get(mode)
    if restriction is not None:
        allowed &= set(restriction)
    if definition.get("mechanism_mode") == "setup-only":
        allowed.discard("mechanism")
    if not spec.get("mechanism", True):
        allowed.discard("mechanism")
    return {
        "historical_evidence": {**historical, "basis": "historical-only; not selected-native proof"},
        "implemented": not reasons, "reason": "; ".join(reasons) or None,
        "required_features": sorted(required), "available_features": sorted(available),
        "missing_features": missing, "action_profile": profile,
        "admission_blocker": ("headless task unqualified; public native delivery is renderer-only"
                              if headless_unqualified else None),
        "allowed_run_modes": sorted(allowed),
        "integration_qualification": "native-bound-engineering" if qualified else "unqualified",
        "qualification_record": record,
        "scored_evaluation": qualified, "accepted": False, "model_success": "not-inferred",
        "task_content_sha256": task_templates.identity(definition),
        "task_manifest_sha256": manifest_sha,
        "native_feature_basis": "verified-selected-contract" if native_features is not None else "source-declaration-only",
        "qualification_basis": ("selected-native-and-code" if native_identity is not None
                                else "source-only; no selected native; cannot certify a later binary"),
        "qualification_key": identity,
    }


def admit(definition, terrain, backend, mode, profile, run_mode, *, unqualified_engineering=False,
          native_features=None, native_identity=None, definitions=None, task_manifest_sha256=None):
    cell = resolve(definition, terrain, backend, mode, profile, native_features=native_features,
                   native_identity=native_identity, definitions=definitions,
                   task_manifest_sha256=task_manifest_sha256)
    if (run_mode not in RUN_MODES or not cell["implemented"]
            or run_mode not in cell["allowed_run_modes"]):
        raise ValueError(f"unsupported {backend} task/mode selection: " + (cell["reason"] or
                         cell["admission_blocker"] or
                         ("scene reachability is setup only; use --mode setup or capability"
                          if run_mode == "mechanism" and definition.get("mechanism_mode") == "setup-only" else
                          "integration unqualified for this run mode/profile; use explicit engineering")))
    if (backend == "renderer" and not cell["scored_evaluation"]
            and not unqualified_engineering):
        raise ValueError("unsupported unqualified selection: explicitly opt into "
                         "--unqualified-engineering; no capability qualification is inherited")
    return cell


def matrix(definitions=None, *, selected=None, mode=None, profile=None, task_manifest_sha256=None):
    from .renderer_support import historical_policy
    value = historical_policy()
    if mode is not None and mode not in MODES:
        raise ValueError("unknown observation mode")
    if profile is not None and mode is None:
        raise ValueError("an explicit matrix profile requires an observation mode")
    native_identity = qualification.selected_native(selected)
    features = selected["renderer"].get("benchmark_features") if selected is not None else None
    if selected is not None:
        manifest = selected.get("dependencies", {}).get("scene_manifest", {}).get("path")
        if manifest is not None:
            task_manifest_sha256 = lifecycle.sha(Path(manifest))
            if definitions is None:
                definitions = suite.load(Path(manifest))
    if definitions is None:
        definitions = catalog()
        if task_manifest_sha256 is None:
            task_manifest_sha256 = lifecycle.sha(Path(suite.__file__).with_name("scenes.json"))
    value["renderer"] = {
        f"{definition['id']}/{terrain}": {
            selected_mode: resolve(definition, terrain, "renderer", selected_mode, profile,
                                   native_features=features, native_identity=native_identity,
                                   definitions=definitions, task_manifest_sha256=task_manifest_sha256)
            for selected_mode in ((mode,) if mode is not None else MODES)}
        for definition in definitions.values()
        for terrain in definition["terrains"]
    }
    value["schema"] = "rpg-task-mode-support/2"
    value["other_renderer_combinations"] = "undefined combinations rejected"
    cells = [cell for modes in value["renderer"].values() for cell in modes.values()]
    qualified = sum(cell["scored_evaluation"] for cell in cells)
    readiness = ("source-only" if selected is None else
                 "native-evidence-complete" if cells and qualified == len(cells) else
                 "native-evidence-partial" if qualified else "native-evidence-pending")
    value["successor"] = {
        "status": readiness,
        "selected_cells": len(cells),
        "implemented_cells": sum(cell["implemented"] for cell in cells),
        "qualified_cells": qualified,
        "registry_records": len(qualification.load()["records"]),
        "engineering_image_profile": "visual-ui-v1",
        "default_image_profile": capabilities.default_profile("renderer", "image"),
        "policy": "implemented features do not qualify a task or imply model success",
    }
    value["qualification_evidence_basis"] = (
        "exact selected-native/code records; historical evidence separate"
        if qualified else "no matching selected-native qualification; historical evidence separate")
    value["qualification_basis"] = ("selected-native-and-code" if selected is not None
                                    else "source-only; no selected native")
    value["visual_primitives"]["historical_only"] = True
    value["visual_primitives"]["successor_implementation"] = {
        "features": sorted(RENDERER_FEATURES), "implementation_revision": NATIVE["implementation_revision"],
        "qualification": readiness,
        "profile": "visual-ui-v1"}
    return value
