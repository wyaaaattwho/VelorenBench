from pathlib import Path


def validate_root(root: Path) -> Path:
    if not root.is_absolute() or ".." in root.parts or root == Path(root.anchor):
        raise ValueError("output root must be a dedicated absolute directory without traversal")
    if any(part.is_symlink() for part in (root, *root.parents)):
        raise ValueError("output root symlink traversal refused")
    if root.exists() and not root.is_dir():
        raise ValueError("output root is not a directory")
    return root


def validate_output(path: Path, root: Path) -> Path:
    validate_root(root)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("output must be an absolute path without traversal")
    if path == root or not path.is_relative_to(root):
        raise ValueError(f"output must be a unique child directory below {root}")
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("output symlink traversal refused")
    if path.exists():
        raise FileExistsError(f"output already exists: {path}")
    return path


def reserve_output(path: Path, root: Path) -> Path:
    validate_output(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    validate_output(path, root)
    path.mkdir(mode=0o700, exist_ok=False)
    return path
