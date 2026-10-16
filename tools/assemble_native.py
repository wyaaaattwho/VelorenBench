"""Assemble a renderer bundle from a successful public-checkout native build."""
import argparse
import json
from pathlib import Path
import shutil

from native_source import (
    ROOT, SELECTED_TREE, copy_source, disjoint, inspect_source, inventory, local,
    sha, tree_digest, write,
)

BINARIES = ("veloren-server-cli", "veloren-voxygen", "examples/agent_bridge")


def assemble(fresh_build, metadata, assets, out):
    fresh_build, metadata, assets = map(local, (fresh_build, metadata, assets))
    out = disjoint(out, fresh_build, metadata, assets)
    if out.exists():
        raise ValueError("assembly output must not exist")
    fresh = json.loads((fresh_build / "receipt.json").read_text())
    identity, paths = inspect_source(fresh_build / "source", metadata / "source.json")
    if (fresh["status"] != "built" or fresh["compiler_artifacts_inherited"] is not False
            or fresh["source_manifest_sha256"] != sha(metadata / "source.json")
            or fresh["source_tree_sha256"] != SELECTED_TREE):
        raise ValueError("fresh build does not match selected source")
    if assets.name != "assets":
        raise ValueError("--assets directory must end in assets")
    expected_assets = json.loads((metadata / "assets-manifest.json").read_text())
    if (inventory(assets) != expected_assets["files_sha256"]
            or tree_digest(expected_assets["files_sha256"]) != expected_assets["tree_sha256"]):
        raise ValueError("external asset inventory mismatch; no asset fallback")
    for name in BINARIES:
        if sha(local(fresh_build / "target/debug" / name)) != fresh["binaries"][name]:
            raise ValueError("fresh native host changed: " + name)
    builds = [row for row in fresh["commands"] if row["argv"][1:2] == ["build"]]
    if len(builds) != 3 or any(row["returncode"] != 0 for row in fresh["commands"]):
        raise ValueError("missing successful selected build commands")
    for row in fresh["commands"]:
        if sha(local(fresh_build / row["log"])) != row["log_sha256"]:
            raise ValueError("build log changed")
    out.mkdir(parents=True, exist_ok=False)
    copy_source(paths, out / "source")
    for name in ("source.json", "frame-contract.json", "rpg-contract.json", "assets-manifest.json"):
        shutil.copy2(metadata / name, out / name)
    binaries = {}
    for name in BINARIES:
        destination = out / "binaries" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fresh_build / "target/debug" / name, destination)
        binaries["debug/" + name] = {
            "path": str(Path("binaries") / name), "sha256": fresh["binaries"][name]}
    write(out / "build.json", {
        "architecture": "aarch64", "binaries": binaries,
        "source_manifest_sha256": sha(out / "source.json"),
        "source_tree_sha256": identity["source_tree_sha256"],
        "assets_tree_sha256": expected_assets["tree_sha256"],
        "assets_manifest": "assets-manifest.json",
        "assets_manifest_sha256": sha(out / "assets-manifest.json"),
        "public_schema_sha256": sha(out / "rpg-contract.json"),
        "frame_contract_sha256": sha(out / "frame-contract.json"),
        "commands": ["cargo " + " ".join(row["argv"][1:]) for row in builds],
        "build_logs": {kind: row["log_sha256"]
                       for kind, row in zip(("server", "helper", "renderer"), builds, strict=True)},
        "rustc": (fresh_build / fresh["commands"][0]["log"]).read_text(),
        "build_provenance": {
            "compiler_artifacts_inherited": False, "clean_machine_build": False,
            "fresh_build_receipt_sha256": sha(fresh_build / "receipt.json"),
            "meaning": "public source checkout, empty compiler target; external system/toolchain/assets",
        },
    })
    files = {str(path.relative_to(out)): sha(path) for path in sorted(out.rglob("*"))
             if path.is_file() and not path.is_relative_to(out / "source")}
    write(out / "bundle.json", {
        "schema": "rpg-native-release/1", "files_sha256": files,
        "source_tree_sha256": identity["source_tree_sha256"], "architecture": "aarch64"})
    return {"descriptor": str(out / "bundle.json"), "sha256": sha(out / "bundle.json")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fresh-build", required=True, type=Path)
    parser.add_argument("--metadata", default=ROOT / "native", type=Path)
    parser.add_argument("--assets", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(assemble(args.fresh_build, args.metadata, args.assets, args.out)))


if __name__ == "__main__":
    main()
