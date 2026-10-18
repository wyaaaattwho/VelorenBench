"""Strict identity/structure validation of curated engineering records.

This does not verify an artifact's gameplay claims, confer release acceptance,
or infer model success. Records are package data, not a live service or plugin.
"""
import hashlib
import json
from pathlib import Path
import re

from . import runtime, task_templates

SCHEMA = "velorenbench-engineering-qualification/1"
PATH = Path(__file__).with_name("current-qualification.json")
RENDERER_HASHES = {
    "source_tree_sha256", "source_manifest_sha256", "assets_tree_sha256",
    "assets_manifest_sha256", "executable_sha256", "server_sha256",
    "interface_contract_sha256", "build_receipt_sha256",
}
HELPER_HASHES = {"executable_sha256", "source_tree_sha256", "build_receipt_sha256"}
INTERACTION_HASHES = {"contract_sha256", "build_manifest_sha256"}
KEY_FIELDS = {
    "task_id", "task_content_sha256", "template", "terrain", "backend",
    "observation_mode", "action_profile", "native_identity", "harness_code_sha256",
    "setup_dependencies",
}
RUN_MODES = {"setup", "interface-smoke", "task-engineering", "mechanism", "capability"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _hash(value):
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value) is not None


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def validate_native(value):
    if not isinstance(value, dict) or set(value) != {
            "renderer", "fixture_helper", "native_interactions"}:
        raise ValueError("invalid qualification native identity")
    for name, fields in (("renderer", RENDERER_HASHES), ("fixture_helper", HELPER_HASHES),
                         ("native_interactions", INTERACTION_HASHES)):
        row = value[name]
        if name == "native_interactions" and row is None:
            continue
        if not isinstance(row, dict) or set(row) != fields or not all(map(_hash, row.values())):
            raise ValueError("invalid qualification native hashes: " + name)
    if value["renderer"]["source_tree_sha256"] != value["fixture_helper"]["source_tree_sha256"]:
        raise ValueError("qualification fixture helper source mismatch")
    return value


def selected_native(selected):
    """Only portable, already-verified native hashes; never host paths or PIDs."""
    if selected is None:
        return None
    if selected.get("renderer") is None or selected.get("renderer_fixture_helper") is None:
        raise ValueError("unsupported renderer: qualification requires a verified renderer and fixture helper")
    interactions = selected.get("native_interactions")
    try:
        value = {
            "renderer": {key: selected["renderer"][key] for key in RENDERER_HASHES},
            "fixture_helper": {key: selected["renderer_fixture_helper"][key] for key in HELPER_HASHES},
            "native_interactions": ({key: interactions[key] for key in INTERACTION_HASHES}
                                    if interactions is not None else None),
        }
    except KeyError as error:
        raise ValueError("unsupported renderer: selected native lacks qualification hash: " + str(error)) from error
    return validate_native(value)


def code_identity():
    # Exclude evidence data and the distribution's source-manifest: no self-hash
    # cycle when a curator adds records. Task data is bound separately in the key.
    return digest({name: sha for name, sha in runtime.package_files().items()
                   if name.endswith(".py") or name == "rpg_benchmark/native_features.json"})


def key(definition, terrain, backend, mode, profile, native_identity, dependencies):
    return {
        "task_id": definition["id"], "task_content_sha256": task_templates.identity(definition),
        "template": definition["template"], "terrain": terrain, "backend": backend,
        "observation_mode": mode, "action_profile": profile,
        "native_identity": native_identity, "harness_code_sha256": code_identity(),
        "setup_dependencies": {
            role: {"task_id": row["id"], "template": row["template"],
                   "task_content_sha256": task_templates.identity(row)}
            for role, row in dependencies.items()},
    }


def validate(value):
    """Fail closed on malformed, duplicate or conflicting records, even unused ones."""
    if (not isinstance(value, dict) or set(value) != {"schema", "records"}
            or value["schema"] != SCHEMA or not isinstance(value["records"], list)):
        raise ValueError("invalid qualification document")
    seen = set()
    for record in value["records"]:
        if not isinstance(record, dict) or set(record) != {
                "key", "status", "scope", "evidence", "allowed_run_modes"}:
            raise ValueError("invalid qualification record fields")
        identity = record["key"]
        if (not isinstance(identity, dict) or set(identity) != KEY_FIELDS
                or not all(_text(identity[name]) for name in
                           ("task_id", "template", "terrain", "action_profile"))
                or not _hash(identity["task_content_sha256"])
                or not _hash(identity["harness_code_sha256"])
                or identity["backend"] != "renderer"
                or not isinstance(identity["observation_mode"], str)
                or identity["observation_mode"] not in {"structured", "hybrid", "image"}):
            raise ValueError("invalid qualification key")
        dependencies = identity["setup_dependencies"]
        if not isinstance(dependencies, dict):
            raise ValueError("invalid qualification setup dependencies")
        for role, row in dependencies.items():
            if (not _text(role) or not isinstance(row, dict)
                    or set(row) != {"task_id", "template", "task_content_sha256"}
                    or not _text(row["task_id"]) or not _text(row["template"])
                    or not _hash(row["task_content_sha256"])):
                raise ValueError("invalid qualification setup dependency identity")
        from .capabilities import default_profile
        mode, profile = identity["observation_mode"], identity["action_profile"]
        if profile not in ({"visual-ui-v1", "visual-motor-v1"} if mode == "image" else
                           {default_profile("renderer", mode)}):
            raise ValueError("invalid qualification mode/profile")
        validate_native(identity["native_identity"])
        fingerprint = digest(identity)
        if fingerprint in seen:
            raise ValueError("duplicate or conflicting qualification key")
        seen.add(fingerprint)
        modes = record["allowed_run_modes"]
        if (not isinstance(record["status"], str)
                or record["status"] not in {"engineering-qualified", "partial", "unqualified"}
                or not _text(record["scope"])
                or not isinstance(modes, list) or any(not isinstance(m, str) for m in modes)
                or len(set(modes)) != len(modes) or set(modes) - RUN_MODES
                or (record["status"] == "engineering-qualified" and not modes)
                or (record["status"] != "engineering-qualified" and modes)):
            raise ValueError("invalid qualification scope/status/run modes")
        evidence = record["evidence"]
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("qualification evidence required")
        for item in evidence:
            if (not isinstance(item, dict) or set(item) != {"reference", "sha256", "scope"}
                    or not _text(item["reference"]) or not _text(item["scope"])
                    or not _hash(item["sha256"])):
                raise ValueError("invalid qualification evidence reference/hash/scope")
    return value


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate qualification JSON field: " + name)
        result[name] = value
    return result


def load(path=None):
    return validate(json.loads((PATH if path is None else Path(path)).read_text(),
                               object_pairs_hook=_unique_object))


def matching(identity, document=None):
    document = load() if document is None else validate(document)
    return next((row for row in document["records"] if row["key"] == identity), None)


def selections(selected=None):
    """Generate exact keys, including existing variants and declared profiles.

    A key inventory is preparation, not evidence or a positive record. Unsupported
    legacy-profile combinations stay visible with their actual resolver reason.
    """
    import copy
    from . import capabilities, suite, support
    from .lifecycle import sha
    manifest = (Path(selected["dependencies"]["scene_manifest"]["path"])
                if selected is not None else Path(suite.__file__).with_name("scenes.json"))
    definitions = suite.load(manifest)
    native = selected_native(selected)
    features = selected["renderer"]["benchmark_features"] if selected is not None else None
    rows = []
    for definition in definitions.values():
        for variant in definition.get("variants", [None]):
            configured = copy.deepcopy(definition)
            suite.runtime_options(configured, renderer=True, variant=variant)
            for terrain in configured["terrains"]:
                for mode in support.MODES:
                    profiles = [capabilities.default_profile("renderer", mode)]
                    if mode == "image":
                        profiles.append("visual-motor-v1")
                    for profile in profiles:
                        cell = support.resolve(
                            configured, terrain, "renderer", mode, profile,
                            native_features=features, native_identity=native,
                            definitions=definitions)
                        rows.append({
                            "task_id": configured["id"], "variant": configured.get("selected_variant"),
                            "default_variant": configured.get("default_variant"),
                            "terrain": terrain, "observation_mode": mode, "action_profile": profile,
                            "implemented": cell["implemented"], "reason": cell["reason"],
                            "allowed_run_modes": cell["allowed_run_modes"],
                            "integration_qualification": cell["integration_qualification"],
                            "key": cell["qualification_key"],
                        })
    return {
        "schema": "velorenbench-qualification-selections/1",
        "task_count": len(definitions),
        "terrain_cells": sum(len(d["terrains"]) for d in definitions.values()),
        "task_manifest_sha256": sha(manifest),
        "qualification_basis": "selected-native-and-code" if native else "source-only",
        "selections": rows, "evidence_generated": False, "accepted": False,
        "model_success": "not-inferred",
    }
