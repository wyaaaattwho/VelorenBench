"""Inventory only public checkout source; never ingest generated/local materials."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOP = {
    ".gitignore", "LICENSE", "README.md", "NOTICE.md", "BENCHMARK_CARD.md",
    "API.md", "TASKS.md", "SUPPORT.md", "BUILD.md", "PROVENANCE.md",
    "TESTING.md", "CONTRIBUTING.md", "pyproject.toml", "MANIFEST.in",
    "requirements-build.lock", "requirements-test.lock", "requirements-test-sdk.lock",
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_inventory(root):
    selected = [root / name for name in TOP]
    for name in ("harness/scripts", "harness/tests", "config", "tools", "native"):
        selected.extend(path for path in (root / name).rglob("*") if path.is_file()
                        and "__pycache__" not in path.parts
                        and not path.name.endswith((".pyc", ".pyo")))
    if any(p.is_symlink() for p in selected):
        raise ValueError("public source cannot contain symlinks")
    return {path.relative_to(root).as_posix(): sha(path) for path in sorted(selected)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true", help="Regenerate after deliberate source edits.")
    group.add_argument("--check", action="store_true", help="Read-only verification.")
    args = parser.parse_args()
    files = source_inventory(ROOT)
    native = {name.removeprefix("native/source/"): digest
              for name, digest in files.items() if name.startswith("native/source/")}
    value = {
        "schema": "velorenbench-public-source/1", "version": "1.0.0",
        "frozen_local_build": "001",
        "frozen_harness_manifest_sha256": "314a6d204e4a5b8953d03e374de2273d38b812bf97813ce78f0c885cbe4352b8",
        "selected_native_tree_sha256": "4c2cc4bb7cb7e6a94f963253866f09c2a0a6fde0df2d23ece9c99f78322944db",
        "exported_native_subset_sha256": hashlib.sha256(
            json.dumps(native, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "native_omitted_external_resource": "assets/voxygen/audio/null.ogg",
        "files_sha256": files,
        "scope": "public Git source checkout; not frozen wheel/sdist identity; manifest excludes itself",
    }
    path = ROOT / "source-manifest.json"
    if args.write:
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    elif json.loads(path.read_text()) != value:
        raise SystemExit("public source inventory differs; inspect edits before --write")
    print(json.dumps({"status": "written" if args.write else "verified",
                      "files": len(files), "manifest_sha256": sha(path),
                      "exported_native_subset_sha256": value["exported_native_subset_sha256"]}))


if __name__ == "__main__":
    main()
