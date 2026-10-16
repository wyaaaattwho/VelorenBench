"""Create-only, content-verified native bindings from a relocatable local bundle."""
from pathlib import Path
import shutil

from . import lifecycle, pins, renderer_dependency, runtime

SCHEMA = "rpg-native-release/1"
FILES = {
    "source.json", "assets-manifest.json", "frame-contract.json", "rpg-contract.json",
    "build.json", "binaries/veloren-voxygen", "binaries/veloren-server-cli",
    "binaries/examples/agent_bridge",
}


def bind_native(descriptor: Path, digest: str, assets: Path, destination: Path) -> dict:
    descriptor = runtime.local_path(Path.cwd(), str(descriptor.absolute()))
    assets = runtime.local_path(Path.cwd(), str(assets.absolute()))
    destination = runtime.local_path(Path.cwd(), str(destination.absolute()))
    bundle = runtime.read_json(descriptor, digest)
    if set(bundle) != {"schema", "files_sha256", "source_tree_sha256", "architecture"}:
        raise ValueError("invalid native release descriptor fields")
    if bundle["schema"] != SCHEMA or set(bundle["files_sha256"]) != FILES:
        raise ValueError("unsupported native release bundle")
    files = pins.validate_files(bundle["files_sha256"])
    source = runtime.read_json(descriptor.parent / "source.json", files["source.json"])
    source_files = pins.validate_files(source["files_sha256"])
    if source["source_tree_sha256"] != bundle["source_tree_sha256"]:
        raise ValueError("native bundle source identity mismatch")
    protected = [descriptor.parent, assets, runtime.PACKAGE]
    for path in protected:
        if destination == path or destination.is_relative_to(path) or path.is_relative_to(destination):
            raise ValueError("binding destination overlaps an input")
    for name, checksum in files.items():
        path = runtime.local_path(descriptor.parent, name)
        if lifecycle.sha(path) != checksum:
            raise ValueError("native release input changed: " + name)
    for name, checksum in source_files.items():
        path = runtime.local_path(descriptor.parent / "source", name)
        if lifecycle.sha(path) != checksum:
            raise ValueError("native release source changed: " + name)
    asset_manifest = runtime.read_json(descriptor.parent / "assets-manifest.json")
    if assets.name != "assets":
        raise ValueError("native assets root must end in 'assets'")
    pins.admit_tree(assets, asset_manifest["files_sha256"])
    destination.mkdir(parents=True, exist_ok=False)
    native = destination / "native"
    native.mkdir()
    for name in (*files, *(str(Path("source") / name) for name in source_files)):
        target = native / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(descriptor.parent / name, target)
    build = runtime.read_json(native / "build.json")
    tree = source["source_tree_sha256"]
    lifecycle.write(destination / "renderer-build.json", {
        "architecture": bundle["architecture"], "source_tree_sha256": tree,
        "assets_tree_sha256": asset_manifest["tree_sha256"],
        "upstream_build_manifest_sha256": lifecycle.sha(native / "build.json"),
        "scope": "explicit local bundle; build provenance remains in native/build.json",
    })
    helper = "binaries/examples/agent_bridge"
    lifecycle.write(destination / "helper-build.json", {
        "schema": "rpg-renderer-fixture-helper/1", "source_tree_sha256": tree,
        "executable_sha256": files[helper], "source_modifications": [],
        "build_command": build["commands"][1],
        "build_log_sha256": build["build_logs"]["helper"],
    })
    bindings = {
        "executable": "native/binaries/veloren-voxygen",
        "server": "native/binaries/veloren-server-cli",
        "source_root": "native/source", "source_manifest": "native/source.json",
        "interface_contract": "native/frame-contract.json",
        "assets_manifest": "native/assets-manifest.json",
        "build_receipt": "renderer-build.json", "assets": str(assets),
    }
    identity = {
        "schema": renderer_dependency.SCHEMA, "kind": "ordinary-voxygen",
        "timing_profile": renderer_dependency.TIMING_PROFILE, "native_tick": None,
        "headless_timing_equivalent": False, "architecture": bundle["architecture"],
        "source_tree_sha256": tree, "assets_tree_sha256": asset_manifest["tree_sha256"],
        **{key + "_sha256": lifecycle.sha(destination / bindings[key])
           for key in renderer_dependency.HASHED},
    }
    interactions = {"contract": "native/rpg-contract.json", "build_manifest": "native/build.json"}
    lifecycle.write(destination / "content.json", {
        "schema": runtime.CONTENT, "package_files": runtime.package_files(),
        "seeds": runtime.read_json(Path(__file__).with_name("scenes.json"))["seeds"],
        "sources": {}, "external_adapters": {}, "renderer": identity,
        "renderer_fixture_helper": {
            "executable_sha256": files[helper], "source_tree_sha256": tree,
            "build_receipt_sha256": lifecycle.sha(destination / "helper-build.json"),
        },
        "native_interactions": {
            key + "_sha256": lifecycle.sha(destination / value)
            for key, value in interactions.items()
        },
    })
    config = destination / "runtime.json"
    guards = [dict(zip(("pid", "start_ticks", "port"), row)) for row in sorted({
        (row["pid"], row["start_ticks"], row["port"]) for row in lifecycle.guards()
    })]
    lifecycle.write(config, {
        "schema": runtime.SCHEMA,
        "content": {"path": "content.json", "sha256": lifecycle.sha(destination / "content.json")},
        "output_root": "runs", "sources": {}, "external_adapters": {},
        "renderer": bindings, "renderer_fixture_helper": {
            "executable": "native/" + helper, "build_receipt": "helper-build.json",
            "source_root": "native/source",
        },
        "native_interactions": interactions, "protected_processes": guards,
    })
    checksum = lifecycle.sha(config)
    runtime.load_runtime(config, checksum, backend="renderer")
    return {"runtime": str(config), "runtime_sha256": checksum,
            "content_sha256": lifecycle.sha(destination / "content.json"),
            "native_bundle_sha256": digest, "native_execution": "not-run",
            "external_inputs": {"assets": str(assets)},
            "relocation": "native/config paths relative; assets explicitly external; interpreter is executing installation"}
