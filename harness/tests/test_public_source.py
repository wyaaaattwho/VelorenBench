"""Public export/packaging regressions; synthetic inputs, never native results."""
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.rpg_benchmark import lifecycle, runtime
from .historical import historical_fixture


def tools_path():
    for parent in (Path(__file__).parents[2], Path(__file__).parents[1]):
        if (parent / "tools/native_source.py").is_file():
            return parent / "tools"
    raise AssertionError("test assembly must include the source tools")


def tool_module(name):
    path = tools_path() / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tiny_native(tmp_path):
    tools = tool_module("native_source")
    source, assets = tmp_path / "source", tmp_path / "external/assets"
    source.mkdir()
    (assets / "voxygen/audio").mkdir(parents=True)
    (source / "Cargo.toml").write_text("# synthetic manifest, not a real native build\n")
    resource = assets / "voxygen/audio/null.ogg"
    resource.write_bytes(b"synthetic test resource; not game audio")
    files = {"Cargo.toml": tools.sha(source / "Cargo.toml"),
             tools.EXTERNAL_RESOURCE: tools.sha(resource)}
    manifest = tmp_path / "source.json"
    manifest.write_text(json.dumps({"files_sha256": files,
                                    "source_tree_sha256": tools.tree_digest(files)}))
    return tools, source, assets, manifest


def test_omitted_resource_requires_explicit_exact_external_bytes(tmp_path):
    tools, source, assets, manifest = tiny_native(tmp_path)
    with pytest.raises(ValueError, match="external compile-time resource"):
        tools.inspect_source(source, manifest, selected=False)
    identity, paths = tools.inspect_source(source, manifest, assets, selected=False)
    destination = tmp_path / "staged"
    tools.copy_source(paths, destination)
    assert tools.inventory(destination) == identity["files_sha256"]
    (assets / "voxygen/audio/null.ogg").write_bytes(b"wrong")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        tools.inspect_source(source, manifest, assets, selected=False)


@pytest.mark.parametrize("mutation", ["changed", "extra", "symlink", "manifest"])
def test_source_export_never_silently_substitutes_native_code(tmp_path, mutation):
    tools, source, assets, manifest = tiny_native(tmp_path)
    if mutation == "changed":
        (source / "Cargo.toml").write_text("changed")
    elif mutation == "extra":
        (source / "extra.rs").write_text("extra")
    elif mutation == "symlink":
        (source / "Cargo.toml").unlink()
        (source / "Cargo.toml").symlink_to(assets / "voxygen/audio/null.ogg")
    else:
        data = json.loads(manifest.read_text())
        data["source_tree_sha256"] = "0" * 64
        manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        tools.inspect_source(source, manifest, assets, selected=False)


def test_build_tools_have_public_checkout_entrypoints_without_sealed_bundle(tmp_path):
    for name in ("build_native", "assemble_native"):
        result = subprocess.run([sys.executable, "-B", str(tools_path() / (name + ".py")), "--help"],
                                cwd=tmp_path, text=True, capture_output=True, check=True)
        assert "--assets" in result.stdout
        assert "--native " not in result.stdout
    assert "--check" in subprocess.check_output(
        [sys.executable, "-B", str(tools_path() / "build_native.py"), "--help"], text=True)


def test_public_default_has_no_private_host_identity_and_explicit_guards_still_apply():
    data = json.loads(Path(lifecycle.__file__).with_name("host-protection.json").read_text())
    assert data == {"hostname": "", "processes": []}
    guard = {"pid": 999999991, "start_ticks": 10, "port": 18082}
    try:
        lifecycle.configure_guards([guard])
        assert guard in lifecycle.guards()
        with pytest.raises(RuntimeError, match="protected PID"):
            lifecycle.assert_owned(guard["pid"], guard["start_ticks"])
    finally:
        lifecycle.configure_guards([])


def test_installed_distribution_has_no_internal_materials_or_raw_fixtures():
    distribution = importlib.metadata.distribution("rpg-benchmark-harness")
    assert distribution.metadata["License-Expression"] == "MIT"
    assert not any(requirement for requirement in distribution.requires or []
                   if "extra ==" not in requirement)
    forbidden = ("materials/", "accepted-contracts.json", "native-contract-traces.json",
                 "scorer-identity-052.json", "stability-repair.json", "native/source/",
                 "delivery-seal.json", "entrypoints.json", "dependency-manifest.json")
    assert not any(token in str(path) for path in distribution.files for token in forbidden)
    assert distribution.metadata.get("Home-page") is None  # project URLs, not a private path
    assert len(runtime.package_files()) > 20


def test_selected_missing_or_changed_historical_data_is_failure_not_skip(tmp_path, monkeypatch):
    monkeypatch.setenv("VELORENBENCH_HISTORICAL_FIXTURES", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        historical_fixture("accepted-contracts.json")
    (tmp_path / "accepted-contracts.json").write_text("{}")
    with pytest.raises(AssertionError):
        historical_fixture("accepted-contracts.json")


def test_source_assembly_emits_existing_bundle_schema_with_exact_fresh_identities(tmp_path, monkeypatch):
    # Fake compiler products test packaging only; never launch or call them
    # native success. Real selected-source hashes are checked independently.
    tools, source, assets, manifest = tiny_native(tmp_path)
    monkeypatch.syspath_prepend(str(tools_path()))
    assembly = tool_module("assemble_native")
    original_inspect = tools.inspect_source
    monkeypatch.setattr(assembly, "inspect_source",
                        lambda source, manifest: original_inspect(source, manifest, selected=False))
    identity, paths = tools.inspect_source(source, manifest, assets, selected=False)
    monkeypatch.setattr(assembly, "SELECTED_TREE", identity["source_tree_sha256"])
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    (metadata / "source.json").write_bytes(manifest.read_bytes())
    for name in ("frame-contract.json", "rpg-contract.json"):
        (metadata / name).write_text('{"synthetic": true}\n')
    asset_files = tools.inventory(assets)
    tools.write(metadata / "assets-manifest.json", {
        "files_sha256": asset_files, "tree_sha256": tools.tree_digest(asset_files)})
    fresh = tmp_path / "build"
    tools.copy_source(paths, fresh / "source")
    (fresh / "logs").mkdir()
    commands = []
    for index, argv in enumerate([
            ["rustc", "-Vv"], ["cargo", "-Vv"],
            ["cargo", "build", "-p", "veloren-server-cli"],
            ["cargo", "build", "-p", "veloren-client"],
            ["cargo", "build", "-p", "veloren-voxygen"]]):
        log = fresh / "logs" / f"{index}.log"
        log.write_text("synthetic test only\n")
        commands.append({"argv": argv, "returncode": 0, "log": str(log.relative_to(fresh)),
                         "log_sha256": tools.sha(log)})
    binaries = {}
    for name in assembly.BINARIES:
        path = fresh / "target/debug" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic compiler product, never executable native evidence\n")
        binaries[name] = tools.sha(path)
    tools.write(fresh / "receipt.json", {
        "status": "built", "compiler_artifacts_inherited": False,
        "source_manifest_sha256": tools.sha(manifest),
        "source_tree_sha256": identity["source_tree_sha256"],
        "commands": commands, "binaries": binaries})
    out = tmp_path / "bundle"
    result = assembly.assemble(fresh, metadata, assets, out)
    assert result["sha256"] == tools.sha(out / "bundle.json")
    bundle = json.loads((out / "bundle.json").read_text())
    from scripts.rpg_benchmark import release
    assert bundle["schema"] == release.SCHEMA
    assert set(bundle["files_sha256"]) == release.FILES
    assert all(tools.sha(out / name) == value for name, value in bundle["files_sha256"].items())
    assert tools.inventory(out / "source") == identity["files_sha256"]
    assert not (out / "assets").exists()
    (fresh / "target/debug/veloren-voxygen").write_text("changed")
    with pytest.raises(ValueError, match="native host changed"):
        assembly.assemble(fresh, metadata, assets, tmp_path / "tampered-bundle")
