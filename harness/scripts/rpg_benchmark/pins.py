"""Content admission helpers. Installation locations belong in runtime config."""
from __future__ import annotations

import json
from pathlib import Path
import sys

from . import lifecycle

PYTHON = Path(sys.executable).absolute()
ROOT = None
IGNORED = {"__pycache__", ".pytest_cache", ".ruff_cache", ".git"}


def inventory(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if IGNORED.intersection(relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"symlink in pinned tree: {path}")
        if path.is_file():
            result[str(relative)] = lifecycle.sha(path)
    return result


def validate_files(files: object) -> dict[str, str]:
    if not isinstance(files, dict):
        raise ValueError("content inventory must be an object")
    for name, checksum in files.items():
        if (not isinstance(name, str) or not name or Path(name).is_absolute()
                or ".." in Path(name).parts or name != Path(name).as_posix()
                or not isinstance(checksum, str) or len(checksum) != 64
                or any(c not in "0123456789abcdef" for c in checksum)):
            raise ValueError(f"invalid source inventory entry: {name}")
    return files


def admit_tree(root: Path, files: dict[str, str]) -> None:
    validate_files(files)
    if (not root.is_absolute() or ".." in root.parts or not root.is_dir()
            or any(part.is_symlink() for part in (root, *root.parents))):
        raise ValueError(f"invalid or symlink selected root: {root}")
    if inventory(root) != files:
        raise ValueError(f"pinned file inventory changed: {root}")


def source_profile(manifest_path: Path, source: Path | None = None,
                   expected_sha256: str | None = None) -> dict:
    if (not manifest_path.is_absolute() or ".." in manifest_path.parts
            or any(part.is_symlink() for part in (manifest_path, *manifest_path.parents))):
        raise ValueError(f"invalid source manifest path: {manifest_path}")
    digest = lifecycle.sha(manifest_path)
    if expected_sha256 is not None and digest != expected_sha256:
        raise ValueError("source manifest changed")
    manifest = json.loads(manifest_path.read_text())
    if not isinstance(manifest, dict):
        raise ValueError("source manifest must be an object")
    declared = manifest.get("source")
    if declared is not None and (not isinstance(declared, str) or not declared):
        raise ValueError("manifest.source must be an absolute source directory")
    if source is None:
        if declared is None:
            raise ValueError("source root required: supply an explicit source binding")
        source = Path(declared)
    if declared is not None and Path(declared) != source:
        raise ValueError("source root disagrees with manifest.source")
    if not source.is_absolute() or ".." in source.parts or not source.is_dir():
        raise ValueError(f"invalid source root: {source}")
    files = validate_files(manifest.get("files"))
    if "tools/run_veloren_agent.py" not in files:
        raise ValueError("source manifest must inventory the normal GameAgent entry")
    admit_tree(source, files)
    if lifecycle.sha(manifest_path) != digest:
        raise ValueError("source manifest changed during admission")
    return {"root": str(source), "manifest": str(manifest_path),
            "manifest_sha256": digest, "commit": manifest.get("source_git_commit", "unknown")}


def load(path: Path, digest: str, *, backend: str = "headless") -> dict:
    from .runtime import load_runtime
    return load_runtime(path, digest, backend=backend)
