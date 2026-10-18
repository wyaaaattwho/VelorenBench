"""Locally prepare exact benchmark assets from separately obtained stock assets.

Only this original preparation tool is distributed, not upstream asset contents.
Generated entity configurations retain the underlying upstream terms; this tool
does not grant rights to redistribute assets or the art their identifiers refer to.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

from native_source import ROOT, disjoint, inventory, local, sha, tree_digest, write


# These are body parameters, not copies of upstream entity configurations. Fixed
# bodies must be selected before native FromBody loadout construction.
OVERLAYS = {
    "common/entity/benchmark/male_green_legoom.ron": (
        "common/entity/wild/aggressive/green_legoom.ron",
        "green_legoom", "BipedSmall", "GreenLegoom"),
    "common/entity/benchmark/male_ogre.ron": (
        "common/entity/wild/aggressive/ogre.ron", "ogre", "BipedLarge", "Ogre"),
    "common/entity/benchmark/male_wolf.ron": (
        "common/entity/wild/aggressive/wolf.ron", "wolf", "QuadrupedMedium", "Wolf"),
}


def entity_overlay(contents, creature, body, species):
    original = f'    body: RandomWith("{creature}"),'.encode()
    if contents.count(original) != 1:
        raise ValueError("expected one stock random-body declaration: " + creature)
    fixed = (f"    body: Exact({body}((\n"
             f"        species: {species},\n"
             "        body_type: Male,\n"
             "    ))),").encode()
    result = contents.replace(original, fixed)
    if creature == "green_legoom":
        # Preserve the byte format of the existing, pinned benchmark derivative.
        result = result.replace(b")),    ),", b")),\n    ),")
    return result if result.endswith(b"\n") else result + b"\n"


def prepare(assets, out, *, manifest=ROOT / "native/assets-manifest.json", check=False):
    assets, manifest = map(local, (assets, manifest))
    out = disjoint(out, assets, manifest, ROOT)
    if out.exists():
        raise ValueError("asset preparation output must not exist")
    if assets.name != "assets" or not assets.is_dir():
        raise ValueError("--assets must be an existing directory ending in assets")
    identity = json.loads(manifest.read_text())
    expected = identity["files_sha256"]
    for name, digest in expected.items():
        if (Path(name).is_absolute() or ".." in Path(name).parts
                or not isinstance(digest, str) or len(digest) != 64):
            raise ValueError("invalid asset inventory entry: " + name)
    required = set(OVERLAYS) | {value[0] for value in OVERLAYS.values()}
    if not required <= expected.keys() or tree_digest(expected) != identity["tree_sha256"]:
        raise ValueError("asset manifest inventory/tree digest mismatch")
    actual = inventory(assets)
    missing = expected.keys() - actual.keys()
    extra = actual.keys() - expected.keys()
    changed = {name for name in actual.keys() & expected.keys()
               if actual[name] != expected[name]}
    if missing - OVERLAYS.keys() or extra or changed:
        raise ValueError("external stock asset inventory mismatch; "
                         f"missing={len(missing - OVERLAYS.keys())}, "
                         f"extra={len(extra)}, changed={len(changed)}")
    generated, provenance = {}, {}
    for name, (base, creature, body, species) in OVERLAYS.items():
        contents = entity_overlay((assets / base).read_bytes(), creature, body, species)
        digest = hashlib.sha256(contents).hexdigest()
        if digest != expected[name]:
            raise ValueError("generated overlay SHA-256 mismatch: " + name)
        generated[name] = contents
        provenance[name] = {"base": base, "base_sha256": actual[base], "sha256": digest}
    combined = {**actual, **{name: row["sha256"] for name, row in provenance.items()}}
    if combined != expected:
        raise ValueError("prepared asset inventory mismatch")
    receipt = {
        "schema": "velorenbench-asset-preparation/1",
        "status": "checked" if check else "prepared",
        "source_assets": str(assets), "source_files": len(actual),
        "source_tree_sha256": tree_digest(actual),
        "assets_manifest_sha256": sha(manifest),
        "output_assets": str(out / "assets"), "output_files": len(expected),
        "assets_tree_sha256": identity["tree_sha256"],
        "generated": provenance,
        "license_scope": "Generated configurations retain upstream terms; no asset redistribution grant.",
    }
    if check:
        return receipt
    out.mkdir(parents=True, exist_ok=False)
    for name in sorted(actual.keys() - OVERLAYS.keys()):
        target = out / "assets" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(assets / name, target)
    for name, contents in generated.items():
        target = out / "assets" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
    if inventory(out / "assets") != expected:
        raise ValueError("prepared asset inventory mismatch after copying")
    write(out / "receipt.json", receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--check", action="store_true",
                        help="verify stock pins and exact generated hashes without writing output")
    args = parser.parse_args()
    print(json.dumps(prepare(args.assets, args.out, check=args.check)))


if __name__ == "__main__":
    main()
