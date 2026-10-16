"""Hash-checked native source helpers, with no compiler or game execution."""
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
SELECTED_TREE = "4c2cc4bb7cb7e6a94f963253866f09c2a0a6fde0df2d23ece9c99f78322944db"
SELECTED_MANIFEST = "e8f6e3a62c1ba28b2daf1f0be83deb8aa831109811361997d569b943544eaa08"
EXTERNAL_RESOURCE = "assets/voxygen/audio/null.ogg"


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def local(path):
    path = Path(path).absolute()
    if ".." in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("path contains traversal or symlinks: " + str(path))
    return path


def disjoint(output, *inputs):
    output = local(output)
    for value in inputs:
        path = local(value)
        if output == path or output.is_relative_to(path) or path.is_relative_to(output):
            raise ValueError("output overlaps an input: " + str(path))
    return output


def inventory(root):
    root = local(root)
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("symlink in source inventory: " + str(path))
        if path.is_file():
            files[path.relative_to(root).as_posix()] = sha(path)
    return files


def tree_digest(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def inspect_source(source, manifest, assets=None, *, selected=True):
    source, manifest = local(source), local(manifest)
    if selected and sha(manifest) != SELECTED_MANIFEST:
        raise ValueError("selected native source manifest SHA-256 mismatch")
    identity = json.loads(manifest.read_text())
    expected = identity["files_sha256"]
    for name, digest in expected.items():
        if (Path(name).is_absolute() or ".." in Path(name).parts
                or not isinstance(digest, str) or len(digest) != 64):
            raise ValueError("invalid native source inventory entry: " + name)
    if (tree_digest(expected) != identity["source_tree_sha256"]
            or (selected and identity["source_tree_sha256"] != SELECTED_TREE)):
        raise ValueError("selected native source tree digest mismatch")
    actual = inventory(source)
    # A checkout may omit only this one game resource; no Rust/source code is
    # synthesized or substituted. Build output contains the full selected tree.
    omitted = {EXTERNAL_RESOURCE} - actual.keys()
    if {k: v for k, v in expected.items() if k not in omitted} != actual:
        raise ValueError("native source inventory mismatch")
    paths = {name: source / name for name in expected if name not in omitted}
    for name in omitted:
        if assets is None:
            raise ValueError("external compile-time resource required; pass --assets DIR ending in assets")
        assets = local(assets)
        if assets.name != "assets":
            raise ValueError("--assets directory must end in assets")
        resource = local(assets / Path(name).relative_to("assets"))
        if sha(resource) != expected[name]:
            raise ValueError("external compile-time resource SHA-256 mismatch: " + name)
        paths[name] = resource
    return identity, paths


def copy_source(paths, destination):
    for name, source in paths.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
