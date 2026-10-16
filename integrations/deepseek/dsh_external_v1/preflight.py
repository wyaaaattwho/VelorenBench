"""No-model check of actual installed entry, plugin, runtime and dependencies."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--runtime-freeze", type=Path)
    parser.add_argument("--runtime-freeze-sha256")
    args = parser.parse_args()
    from . import entry, dsh_tools
    from scripts.rpg_benchmark import runtime as player_runtime
    from .binding import absolute, package_inventory, runtime_inventory
    if importlib.metadata.version("aiohttp") != "3.13.3":
        raise ValueError("Install the adapter requirements.lock in this interpreter")
    runtime = absolute(args.runtime)
    if importlib.metadata.version("rpg-benchmark-harness") != "1.0.1":
        raise ValueError("Install the same pinned core artifact in both environments")
    cli = runtime / "node_modules/.bin/dsh"
    if not cli.is_file() or not os.access(cli, os.X_OK):
        raise ValueError("Official dsh executable is missing from selected runtime")
    root = Path(__file__).parent
    package = json.loads((runtime / "node_modules/@deepseek-ai/dsh/package.json").read_text())
    for name, version in {
        "@deepseek-ai/dsh": "0.2.1-alpha.3",
        "@deepseek-ai/dsh-llm-pi-ai": "0.2.1-alpha.3",
        "@deepseek-ai/dsh-tools": "0.2.1-alpha.3",
        "@earendil-works/pi-ai": "1.2.0",
    }.items():
        installed = json.loads((runtime / "node_modules" / name / "package.json").read_text())
        if installed["version"] != version:
            raise ValueError(f"Pinned official runtime dependency mismatch: {name}")
    # Compare the complete published lock graph; root project naming is immaterial.
    expected_lock = json.loads((root / "data/package-lock.json").read_text())
    actual_lock = json.loads((runtime / "package-lock.json").read_text())
    def graph(lock):
        return {key: value for key, value in lock["packages"].items() if key}
    expected_deps = expected_lock["packages"][""]["dependencies"]
    manifest = json.loads((runtime / "package.json").read_text())
    if (manifest["dependencies"] != expected_deps
            or actual_lock["packages"][""]["dependencies"] != expected_deps
            or graph(actual_lock) != graph(expected_lock)):
        raise ValueError("Runtime must use the shipped pinned npm lock graph")
    if bool(args.runtime_freeze) != bool(args.runtime_freeze_sha256):
        raise ValueError("Runtime freeze requires its SHA-256")
    if args.runtime_freeze:
        freeze = player_runtime.read_json(absolute(args.runtime_freeze), args.runtime_freeze_sha256)
        if freeze["root"] != str(runtime) or freeze["files"] != runtime_inventory(runtime):
            raise ValueError("Frozen official runtime content changed")
    subprocess.run(["node", "--check", str(root / "game-plugin.mjs")], check=True)
    environment = {**os.environ, "DSH_RUNTIME": str(runtime)}
    # Actually resolves the external runtime's schemastery dependency.
    subprocess.run(["node", "--input-type=module", "-e",
                    "await import(process.argv[1])", (root / "game-plugin.mjs").as_uri()],
                   env=environment, check=True)
    print(json.dumps({"schema": "rpg-adapter-preflight/1",
                      "player_package_files": player_runtime.package_files(),
                      "status": "ready", "model_calls": 0, "native_worlds": 0,
                      "runtime": str(runtime), "dsh": package["version"],
                      "entry": entry.__file__, "sdk_optional": True,
                      "adapter_root": str(root),
                      "adapter_package_files": package_inventory(root)}))


if __name__ == "__main__":
    main()
