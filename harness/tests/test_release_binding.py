import json
from pathlib import Path
import platform
import shutil

import pytest

from scripts.rpg_benchmark import lifecycle, release, runtime
from .test_portability import write
from .test_renderer_dependency import renderer_fixture


def bundle_fixture(root):
    renderer_fixture(root)
    bundle = root / "bundle"
    bundle.mkdir()
    shutil.copytree(root / "renderer-inputs/source", bundle / "source")
    for original, selected in (
        ("voxygen", "binaries/veloren-voxygen"), ("server", "binaries/veloren-server-cli"),
        ("server", "binaries/examples/agent_bridge"), ("contract.json", "frame-contract.json"),
        ("source.json", "source.json"), ("assets.json", "assets-manifest.json"),
    ):
        target = bundle / selected
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / "renderer-inputs" / original, target)
    contract_sha = write(bundle / "rpg-contract.json", {
        "schema": "rpg-native-interactions", "version": "1.0", "native_protocol_version": "0.2"})
    write(bundle / "build.json", {
        "source_manifest_sha256": lifecycle.sha(bundle / "source.json"),
        "public_schema_sha256": contract_sha, "commands": ["server", "helper", "renderer"],
        "build_logs": {"helper": "3" * 64},
        "binaries": {name: {"sha256": lifecycle.sha(bundle / path)} for name, path in (
            ("debug/veloren-voxygen", "binaries/veloren-voxygen"),
            ("debug/veloren-server-cli", "binaries/veloren-server-cli"),
            ("debug/examples/agent_bridge", "binaries/examples/agent_bridge"))},
    })
    digest = write(bundle / "bundle.json", {
        "schema": release.SCHEMA, "architecture": platform.machine(),
        "source_tree_sha256": "1" * 64,
        "files_sha256": {name: lifecycle.sha(bundle / name) for name in release.FILES},
    })
    return bundle / "bundle.json", digest


def test_new_binding_is_relocatable_and_installed_package_bound(tmp_path):
    descriptor, digest = bundle_fixture(tmp_path / "inputs")
    out = tmp_path / "first"
    guards = {tuple(row[key] for key in ("pid", "start_ticks", "port"))
              for row in lifecycle.guards()}
    receipt = release.bind_native(descriptor, digest, tmp_path / "inputs/assets", out)
    config = runtime.read_json(out / "runtime.json")
    assert all(not Path(value).is_absolute() for key, value in config["renderer"].items()
               if key != "assets")
    assert {tuple(row[key] for key in ("pid", "start_ticks", "port"))
            for row in config["protected_processes"]} == guards
    before = runtime.load_runtime(out / "runtime.json", receipt["runtime_sha256"], backend="renderer")
    shutil.copytree(out, tmp_path / "moved")
    after = runtime.load_runtime(tmp_path / "moved/runtime.json", receipt["runtime_sha256"], backend="renderer")
    assert before["content_sha256"] == after["content_sha256"]
    assert before["renderer"]["paths"]["executable"] != after["renderer"]["paths"]["executable"]
    assert after["native_interactions"] == before["native_interactions"]
    assert before["renderer"]["native_tick"] is None
    assert not before["renderer"]["headless_timing_equivalent"]
    with pytest.raises(FileExistsError):
        release.bind_native(descriptor, digest, tmp_path / "inputs/assets", out)


@pytest.mark.parametrize("name", sorted(release.FILES) + ["source/Cargo.toml"])
def test_changed_native_release_inputs_fail_before_allocation(tmp_path, name):
    descriptor, digest = bundle_fixture(tmp_path / "inputs")
    (descriptor.parent / name).write_text("tampered")
    with pytest.raises((ValueError, json.JSONDecodeError)):
        release.bind_native(descriptor, digest, tmp_path / "inputs/assets", tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_asset_tamper_descriptor_tamper_and_overlap_fail_closed(tmp_path):
    descriptor, digest = bundle_fixture(tmp_path / "inputs")
    for out in (descriptor.parent / "child", descriptor.parent, tmp_path):
        with pytest.raises(ValueError, match="overlaps"):
            release.bind_native(descriptor, digest, tmp_path / "inputs/assets", out)
    with pytest.raises(ValueError, match="SHA-256"):
        release.bind_native(descriptor, "0" * 64, tmp_path / "inputs/assets", tmp_path / "output")
    (tmp_path / "inputs/assets/unpinned").write_text("changed")
    with pytest.raises(ValueError, match="inventory changed"):
        release.bind_native(descriptor, digest, tmp_path / "inputs/assets", tmp_path / "output")
    assert not (tmp_path / "output").exists()
