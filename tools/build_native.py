"""Build selected Linux/aarch64 native hosts from this checkout in a NEW prefix."""
import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

from native_source import (
    ROOT, copy_source, disjoint, inspect_source, local, sha, write,
)

TOOLCHAIN = "nightly-2026-06-13"
BUILD_COMMANDS = [
    ["build", "--offline", "--locked", "-p", "veloren-server-cli"],
    ["build", "--offline", "--locked", "-p", "veloren-client",
     "--features", "bin_bot", "--example", "agent_bridge"],
    ["build", "--offline", "--locked", "-p", "veloren-voxygen",
     "--bin", "veloren-voxygen", "--no-default-features",
     "--features", "simd,shaderc-from-source"],
]
BINARIES = ("veloren-server-cli", "veloren-voxygen", "examples/agent_bridge")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "native/source")
    parser.add_argument("--source-manifest", type=Path, default=ROOT / "native/source.json")
    parser.add_argument("--assets", type=Path, required=True,
                        help="Legally obtained external assets; the compile-time resource is hash-checked.")
    parser.add_argument("--cargo-sources", type=Path,
                        help="Optional predownloaded CARGO_HOME registry/git sources only; no target cache.")
    parser.add_argument("--target-cache", type=Path,
                        help="Optional owned Cargo target copied independently; normal Cargo invalidation still runs.")
    parser.add_argument("--fetch", action="store_true",
                        help="Explicitly allow cargo fetch --locked for the selected target before offline builds.")
    parser.add_argument("--cargo", type=Path,
                        default=Path(shutil.which("cargo") or Path.home() / ".cargo/bin/cargo"))
    parser.add_argument("--rustup-home", type=Path,
                        default=Path(os.environ.get("RUSTUP_HOME", Path.home() / ".rustup")))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--check", action="store_true",
                        help="Validate selected source/resource and output paths only; no downloads/compiler/output.")
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    source, manifest, assets = local(args.source), local(args.source_manifest), local(args.assets)
    inputs = [source, manifest.parent, assets, local(args.rustup_home)]
    if args.cargo_sources:
        inputs.append(local(args.cargo_sources))
    if args.target_cache:
        inputs.append(local(args.target_cache))
        if not args.target_cache.is_dir():
            parser.error("--target-cache must be an existing owned directory")
    out = disjoint(args.out, *inputs)
    if out.exists():
        parser.error("--out must not exist")
    identity, paths = inspect_source(source, manifest, assets)
    if args.check:
        print(json.dumps({"status": "source-validated", "source_tree_sha256": identity["source_tree_sha256"],
                          "files": len(paths), "native_execution": "not-run",
                          "compiler": "not-run", "platform": "Linux/aarch64 selected only"}))
        return
    if platform.system() != "Linux" or platform.machine() != "aarch64":
        parser.error("only Linux/aarch64 is selected; other platforms are unqualified")
    if not args.cargo.is_file() or not args.rustup_home.is_dir():
        parser.error("install rustup and nightly-2026-06-13; use --cargo/--rustup-home if nondefault")
    if not args.cargo_sources and not args.fetch:
        parser.error("supply --cargo-sources or explicitly --fetch to obtain locked dependencies")
    out.mkdir(parents=True, exist_ok=False)
    for name in ("home", "tmp", "cargo", "target", "logs"):
        (out / name).mkdir()
    copy_source(paths, out / "source")
    shutil.copy2(manifest, out / "source.json")
    if args.target_cache:
        shutil.copytree(args.target_cache, out / "target", dirs_exist_ok=True)
    if args.cargo_sources:
        for name in ("registry", "git"):
            path = local(args.cargo_sources / name)
            if path.exists():
                shutil.copytree(path, out / "cargo" / name)
    env = {
        "HOME": str(out / "home"), "TMPDIR": str(out / "tmp"),
        "PATH": str(args.cargo.absolute().parent) + ":/usr/bin:/bin",
        "RUSTUP_HOME": str(args.rustup_home.absolute()), "RUSTUP_TOOLCHAIN": TOOLCHAIN,
        "CARGO_HOME": str(out / "cargo"), "CARGO_TARGET_DIR": str(out / "target"),
        "VELOREN_GIT_VERSION": "/f974743d/1788840834", "LANG": "C.UTF-8",
    }
    cargo = str(args.cargo.absolute())
    commands = [["rustc", "-Vv"], [cargo, "-Vv"]]
    if args.fetch:
        commands.append([cargo, "fetch", "--locked", "--target", "aarch64-unknown-linux-gnu"])
    build_commands = [[cargo, *command, "-j", str(args.jobs)] for command in BUILD_COMMANDS]
    commands.extend(build_commands)
    receipt = {
        "schema": "rpg-selected-native-build/1", "status": "running",
        "platform": "aarch64-unknown-linux-gnu", "environment": env,
        "source_manifest_sha256": sha(manifest),
        "source_tree_sha256": identity["source_tree_sha256"],
        "compiler_artifacts_inherited": bool(args.target_cache),
        "compiler_cache": str(local(args.target_cache)) if args.target_cache else None, "commands": [],
        "build_commands": [" ".join(command[1:]) for command in build_commands],
        "scope": ("independent copied compiler cache; all selected Cargo builds revalidated"
                  if args.target_cache else "empty compiler target") + "; external sources/toolchain/system; not clean-machine",
    }
    write(out / "receipt.json", receipt)
    for index, command in enumerate(commands):
        log = out / "logs" / f"{index:02}.log"
        try:
            with log.open("x") as stream:
                result = subprocess.run(command, cwd=out / "source", env=env,
                                        stdout=stream, stderr=subprocess.STDOUT)
            code = result.returncode
        except OSError as error:
            code = 127
            # The log already exists; the failure is also recorded in the receipt.
            receipt["error"] = f"{type(error).__name__}: {error}"
        receipt["commands"].append({
            "argv": command, "returncode": code, "log": str(log.relative_to(out)),
            "log_sha256": sha(log),
        })
        if code:
            receipt["status"] = "failed"
        write(out / "receipt.json", receipt)
        if code:
            print(log.read_text()[-8000:], file=sys.stderr)
            print("Native build failed; see receipt/log. Check toolchain, system packages and locked dependency availability.",
                  file=sys.stderr)
            raise SystemExit(code)
    receipt["binaries"] = {name: sha(out / "target/debug" / name) for name in BINARIES}
    receipt["status"] = "built"
    write(out / "receipt.json", receipt)
    print(json.dumps({"status": "built", "receipt": str(out / "receipt.json")}))


if __name__ == "__main__":
    main()
