"""Stdlib-only, read-only verifier for a relocated qualification evidence sidecar.

Relative qualification citations resolve against --root, NOT site-packages or
the original research host. Optional --registry checks the actual installed
registry is byte-identical. Hash verification is closure/integrity, not independent
authentication of gameplay, release acceptance, asset rights or model success.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def local(root, name):
    require(isinstance(name, str) and name and "\\" not in name, "invalid relative reference")
    parts = PurePosixPath(name)
    require(not parts.is_absolute() and str(parts) == name and
            all(p not in ("", ".", "..") for p in parts.parts), "noncanonical relative reference")
    path = root.joinpath(*parts.parts)
    require(not any(p.is_symlink() for p in [path, *path.parents] if p != root.parent),
            "symlink reference")
    require(path.is_file() and path.resolve().is_relative_to(root.resolve()), "missing/escaped reference")
    return path


def inventory(root):
    require(root.is_dir() and not root.is_symlink(), "invalid evidence root")
    require(not any(p.is_symlink() for p in root.rglob("*")), "symlink in evidence root")
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def audit(root, *, registry=None, native=None, source_manifest=None, manifest_sha256=None):
    root = Path(root).absolute()
    manifest_path = local(root, "evidence-manifest.json")
    if manifest_sha256:
        require(sha(manifest_path) == manifest_sha256, "evidence manifest hash mismatch")
    manifest = json.loads(manifest_path.read_text())
    qualification_path = local(root, "qualification.json")
    qualification = json.loads(qualification_path.read_text())
    require(sha(qualification_path) == manifest["qualification"]["sha256"], "qualification hash mismatch")
    if registry is not None:
        require(sha(registry) == sha(qualification_path), "installed registry differs from sidecar")
    records = qualification["records"]
    require(len(records) == manifest["qualification"]["records"], "record count mismatch")
    require(all(r["status"] == "engineering-qualified" for r in records), "nonqualified record")
    require(len({json.dumps(r["key"], sort_keys=True) for r in records}) == len(records), "duplicate key")
    require(all(r["key"]["harness_code_sha256"] == manifest["harness_code_sha256"] for r in records),
            "record code identity mismatch")
    refs = [item for record in records for item in record["evidence"]]
    objects = {row["reference"]: row for row in manifest["objects"]}
    require(len(objects) == len(manifest["objects"]) == manifest["evidence_object_count"], "object count")
    require(sum(row["bytes"] for row in objects.values()) == manifest["evidence_bytes"], "object bytes")
    require(len(refs) == manifest["reference_count"] and {r["reference"] for r in refs} == set(objects),
            "citation closure mismatch")
    checked = set()
    for item in refs:
        name = item["reference"]
        parts = PurePosixPath(name).parts
        digest = item["sha256"]
        require(re.fullmatch("[0-9a-f]{64}", digest) is not None and len(parts) == 4
                and parts[:2] == ("evidence", "sha256") and parts[2] == digest[:2]
                and parts[3] == digest and objects[name]["sha256"] == digest, "content address mismatch")
        path = local(root, name)
        require(path.stat().st_size == objects[name]["bytes"], "object size mismatch")
        if name not in checked:
            require(sha(path) == digest, "object hash mismatch")
            checked.add(name)
    for name, digest in {**manifest["notices"], **manifest["other_files"]}.items():
        require(sha(local(root, name)) == digest, "notice/metadata hash mismatch: " + name)
    files = inventory(root)
    require(files == sorted([*objects, *manifest["notices"], *manifest["other_files"],
                             "qualification.json", "evidence-manifest.json"]), "unexpected/missing files")
    if source_manifest is not None:
        source_manifest = Path(source_manifest)
        require(sha(source_manifest) == manifest["selected_source_manifest_sha256"], "source identity mismatch")
        for name, digest in json.loads(source_manifest.read_text())["files_sha256"].items():
            require(sha(local(source_manifest.parent, name)) == digest, "source member mismatch")
    if native is not None:
        native = Path(native)
        require(sha(native) == manifest["native_bundle_sha256"], "native descriptor mismatch")
        descriptor = json.loads(native.read_text())
        for name, digest in descriptor["files_sha256"].items():
            require(sha(local(native.parent, name)) == digest, "native member mismatch")
        source = json.loads((native.parent / "source.json").read_text())
        for name, digest in source["files_sha256"].items():
            require(sha(local(native.parent / "source", name)) == digest, "native source mismatch")
        for link in manifest["corresponding_sources"]:
            require(sha(local(native.parent, link["native_bundle_reference"])) == link["sha256"]
                    == objects[link["evidence_reference"]]["sha256"], "GPL corresponding-source mismatch")
    tree = [(name, sha(root / name), (root / name).stat().st_size) for name in files]
    return {"status": "passed", "manifest_sha256": sha(manifest_path),
            "qualification_sha256": sha(qualification_path), "files": len(files),
            "objects": len(objects), "references": len(refs), "evidence_bytes": manifest["evidence_bytes"],
            "tree_sha256": hashlib.sha256(json.dumps(tree, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "registry_checked": registry is not None, "native_checked": native is not None,
            "source_checked": source_manifest is not None,
            "release_accepted": False, "model_success": "not-inferred"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--native", type=Path)
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--manifest-sha256")
    args = parser.parse_args()
    print(json.dumps(audit(args.root, registry=args.registry, native=args.native,
                           source_manifest=args.source_manifest, manifest_sha256=args.manifest_sha256),
                     indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
