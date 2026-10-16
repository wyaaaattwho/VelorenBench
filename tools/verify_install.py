"""Verify a wheel or sdist in a new HOME/venv with installed-only imports."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--wheelhouse", type=Path, help="Optional offline directory of hash-locked build/test wheels.")
    parser.add_argument("--sdk", action="store_true", help="Install optional SDK lock and allow full suite.")
    parser.add_argument("--tests", nargs="+", help="Explicit focused relative test filenames; --sdk defaults to full suite.")
    args = parser.parse_args()
    root, artifact = args.root.absolute(), args.artifact.absolute()
    if not args.sdk and not args.tests:
        parser.error("select --tests for SDK-free checks or --sdk for full collection")
    if root == ROOT or root.is_relative_to(ROOT) or ROOT.is_relative_to(root):
        parser.error("--root must not overlap the source checkout")
    root.mkdir(parents=True, exist_ok=False)
    for name in ("home", "tmp", "cache", "work", "logs"):
        (root / name).mkdir()
    env = {"HOME": str(root / "home"), "TMPDIR": str(root / "tmp"),
           "XDG_CACHE_HOME": str(root / "cache"), "PATH": "/usr/bin:/bin",
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
           "PIP_CONFIG_FILE": "/dev/null", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
           "PIP_CACHE_DIR": str(root / "cache/pip"), "LANG": "C.UTF-8"}
    if os.environ.get("VELORENBENCH_HISTORICAL_FIXTURES"):
        env["VELORENBENCH_HISTORICAL_FIXTURES"] = os.environ["VELORENBENCH_HISTORICAL_FIXTURES"]
    index = (["--no-index", "--find-links", str(args.wheelhouse.absolute())]
             if args.wheelhouse else [])
    receipt = {"artifact_sha256": sha(artifact), "artifact": artifact.name,
               "environment": env, "commands": [], "status": "running"}

    def command(argv, name):
        result = subprocess.run([str(item) for item in argv], cwd=root / "work", env=env,
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        log = root / "logs" / (name + ".log")
        log.write_text(result.stdout)
        receipt["commands"].append({"argv": [str(item) for item in argv],
                                    "returncode": result.returncode,
                                    "log": str(log.relative_to(root)), "log_sha256": sha(log)})
        (root / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        if result.returncode:
            receipt["status"] = "failed"
            (root / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
            print(result.stdout[-10000:], file=sys.stderr)
            raise SystemExit(result.returncode)
        return result.stdout

    command([sys.executable, "-I", "-m", "venv", "--copies", root / "env"], "venv")
    python = root / "env/bin/python"
    env["PATH"] = str(root / "env/bin") + ":/usr/bin:/bin"
    if artifact.name.endswith(".tar.gz"):
        command([python, "-I", "-m", "pip", "install", "--quiet", *index, "--require-hashes",
                 "-r", ROOT / "requirements-build.lock"], "build-dependencies")
    command([python, "-I", "-m", "pip", "install", "--quiet", "--no-index", "--no-deps",
             "--no-build-isolation", artifact], "artifact-install")
    proof = (
        "import importlib.util,json,site,sys; from pathlib import Path; "
        "from scripts.rpg_benchmark import runtime,run; "
        f"root=Path({str(root)!r}); "
        "assert Path(run.__file__).is_relative_to(root/'env'); "
        "assert not site.ENABLE_USER_SITE; assert sys.prefix != sys.base_prefix; "
        "assert importlib.util.find_spec('copilot') is None; "
        "assert all(not p or not Path(p).is_relative_to(root/'work') for p in sys.path); "
        "print(json.dumps({'python':sys.executable,'base_prefix':sys.base_prefix,"
        "'sys_path':sys.path,'module':run.__file__,'home':str(Path.home())}))"
    )
    command([python, "-I", "-B", "-c", proof], "installed-only-proof")
    entry = root / "env/bin/rpg-benchmark"
    for name in ("identity", "tasks", "support"):
        command([entry, name], name)
    command([entry, "bind-native", "--help"], "bind-help")
    command([entry, "run", "--help"], "run-help")
    locks = ["-r", ROOT / "requirements-test.lock"]
    if args.sdk:
        locks += ["-r", ROOT / "requirements-test-sdk.lock"]
    command([python, "-I", "-m", "pip", "install", "--quiet", *index,
             "--require-hashes", *locks], "test-dependencies")
    shutil.copytree(ROOT / "harness/tests", root / "work/tests",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(ROOT / "tools", root / "work/tools",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    tests = args.tests or [""]
    if any(Path(name).is_absolute() or ".." in Path(name).parts for name in tests):
        raise ValueError("test selection must stay within the copied installed-only test tree")
    receipt["test_selection"] = tests
    command([python, "-I", "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "--basetemp", root / "pytest-tmp", *[Path("tests") / name for name in tests]],
            "targeted-installed-suite" if args.tests else "full-installed-suite")
    command([python, "-I", "-m", "pip", "check"], "dependency-check")
    command([python, "-I", "-m", "pip", "list", "--format=json"], "installed-distributions")
    receipt["status"] = "passed"
    (root / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"root": str(root), "artifact_sha256": sha(artifact), "status": "passed"}))


if __name__ == "__main__":
    main()
