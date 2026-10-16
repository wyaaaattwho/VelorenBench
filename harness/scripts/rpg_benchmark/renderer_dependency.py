"""Verify an optional renderer input; never launch it or advertise observation support."""
from __future__ import annotations

import os
from pathlib import Path
import platform

from . import lifecycle, pins
from .runtime import local_path, object_field, read_json

SCHEMA = "rpg-renderer-dependency/1"
TIMING_PROFILE = "voxygen-variable-dt-client-update-v1"
CONTRACT_TIMING = TIMING_PROFILE + "; not headless fixed-60Hz timing equivalence"
BINDINGS = {"executable", "server", "interface_contract", "build_receipt",
            "source_manifest", "assets_manifest", "source_root", "assets"}
HASHED = {"executable", "server", "interface_contract", "build_receipt",
          "source_manifest", "assets_manifest"}


def admit(base: Path, bindings: dict, identity: dict) -> tuple[dict, list[Path]]:
    expected = {"schema", "kind", "timing_profile", "native_tick",
                "headless_timing_equivalent", "architecture",
                "source_tree_sha256", "assets_tree_sha256",
                *(key + "_sha256" for key in HASHED)}
    if set(bindings) != BINDINGS or set(identity) != expected:
        raise ValueError("renderer binding/identity fields do not match rpg-renderer-dependency/1")
    if (identity["schema"] != SCHEMA or identity["kind"] != "ordinary-voxygen"
            or identity["timing_profile"] != TIMING_PROFILE
            or identity["native_tick"] is not None
            or identity["headless_timing_equivalent"] is not False):
        raise ValueError("unsupported renderer type/timing; no headless timing equivalence")
    if identity["architecture"] != platform.machine():
        raise ValueError("renderer architecture does not match this installation")
    pins.validate_files({key: value for key, value in identity.items() if key.endswith("_sha256")})
    paths = {key: local_path(base, bindings[key]) for key in BINDINGS}
    for key in HASHED:
        if (not paths[key].is_file()
                or lifecycle.sha(paths[key]) != identity[key + "_sha256"]):
            raise ValueError(f"missing or changed renderer input: {key}: {paths[key]}")
    for key in ("executable", "server"):
        if not os.access(paths[key], os.X_OK):
            raise ValueError(f"renderer input is not executable: {key}")
    contract = read_json(paths["interface_contract"])
    if (contract.get("contract") != "veloren-ordinary-native-frame-action"
            or contract.get("version") != "1.0"
            or contract.get("native_protocol_version") != "0.2"
            or contract.get("timing_profile") != CONTRACT_TIMING):
        raise ValueError("renderer contract/type/timing mismatch")
    frame = object_field(object_field(contract, "frame_response"), "result")
    if (frame["native_tick"] is not None
            or object_field(frame, "image")["model_attachment_delivered"] is not False
            or object_field(frame, "correlation")["action_effect_ordering"] != "unconfirmed"):
        raise ValueError("renderer contract must retain unknown tick/ordering and undelivered model attachment")
    receipt = read_json(paths["build_receipt"])
    source = read_json(paths["source_manifest"])
    assets = read_json(paths["assets_manifest"])
    if (receipt.get("architecture") != identity["architecture"]
            or receipt.get("source_tree_sha256") != identity["source_tree_sha256"]
            or source.get("source_tree_sha256") != identity["source_tree_sha256"]
            or receipt.get("assets_tree_sha256") != identity["assets_tree_sha256"]
            or assets.get("tree_sha256") != identity["assets_tree_sha256"]):
        raise ValueError("renderer build/source/assets identity mismatch")
    source_files = pins.validate_files(source["files_sha256"])
    if not source_files or not paths["source_root"].is_dir():
        raise ValueError("renderer requires the declared source inventory")
    for name, checksum in source_files.items():
        path = local_path(paths["source_root"], name)
        if not path.is_file() or lifecycle.sha(path) != checksum:
            raise ValueError(f"renderer source inventory changed: {name}")
    if paths["assets"].name != "assets":
        raise ValueError("renderer assets root must end in 'assets' to avoid fallback discovery")
    pins.admit_tree(paths["assets"], assets["files_sha256"])
    return {
        **identity, "paths": {key: str(value) for key, value in paths.items()},
        "status": "dependency-inputs-validated-only", "native_execution": "not-run",
        "selected_for_episode": False, "model_attachment_delivered": False,
        "observation_support_enabled": False,
        "build_closure": "unqualified", "redistribution_license_closure": "unresolved",
        "source_inventory_scope": "all declared source files; generated build outputs are not source closure",
    }, list(paths.values())
