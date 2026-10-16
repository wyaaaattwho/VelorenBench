"""Freeze the finite-number schema repair; benchmark/provider/runtime unchanged."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent
BASE = ROOT.parent
PARENT = BASE / "generic-adapter-002"
HARNESS = BASE.parent / "rpg-benchmark-external-harness-20261015/timing-001/harness"
PARENT_PINS_SHA = "a17bf08275b53f63798f200ba58c27fd071c392c48951b3d23446c20681f7702"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare():
    shutil.copytree(PARENT / "dsh_external_v1", ROOT / "dsh_external_v1",
                    ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("adapter.py", "game-plugin.mjs", "integer-transport.mjs"):
        shutil.copy2(PARENT / name, ROOT / name)


def freeze():
    selected_path = PARENT / "bundle/pins.json"
    if sha(selected_path) != PARENT_PINS_SHA:
        raise ValueError("Admitted parent pins changed")
    selected = json.loads(selected_path.read_text())
    parent = json.loads((PARENT / "freeze.json").read_text())
    files = {}
    for relative, digest in parent["files"].items():
        if sha(BASE / relative) != digest:
            raise ValueError(f"Frozen parent input changed: {relative}")
        if relative.startswith(("runtime/", "upstream/")):
            files[relative] = digest
    for path in [*ROOT.glob("*.py"), *ROOT.glob("*.mjs"), ROOT / "README.md",
                 *ROOT.glob("dsh_external_v1/*.py"), *ROOT.glob("tests/*.py")]:
        files[str(path.relative_to(BASE))] = sha(path)
    freeze_path = ROOT / "freeze.json"
    with freeze_path.open("x") as stream:
        stream.write(json.dumps({
            "root": str(BASE), "files": files, "model": "gpt-6-luna", "effort": "none",
            "parent_freeze_sha256": sha(PARENT / "freeze.json"),
            "repair": "huge_finite_number_schema_bounds_as_exact_text_native_validation_unchanged",
            "official_runtime_reused": True, "output_parameter": "unchanged_catalog_128000",
        }, indent=2) + "\n")
    bundle = ROOT / "bundle"
    bundle.mkdir(exist_ok=False)
    prior_manifest = Path(selected["external_adapters"]["deepseek-harness"]["path"])
    manifest = json.loads(prior_manifest.read_text())
    manifest.update(implementation=str(ROOT / "adapter.py"),
                    implementation_sha256=sha(ROOT / "adapter.py"),
                    freeze={"path": str(freeze_path), "sha256": sha(freeze_path)})
    path = bundle / "deepseek-harness.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    selected["external_adapters"]["deepseek-harness"] = {"path": str(path), "sha256": sha(path)}
    selected["provider_number_schema_successor"] = {
        "parent_pins_sha256": PARENT_PINS_SHA,
        "repair": "number_bound_presentation_only", "output_setting_changed": False,
    }
    p = bundle / "pins.json"
    p.write_text(json.dumps(selected, indent=2) + "\n")
    sys.path.insert(0, str(HARNESS))
    from scripts.rpg_benchmark import pins
    pins.load(p, sha(p))
    print(json.dumps({"pins": str(p), "pins_sha256": sha(p),
                      "adapter_sha256": sha(path), "freeze_sha256": sha(freeze_path)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("prepare", "freeze"))
    options = parser.parse_args()
    prepare() if options.operation == "prepare" else freeze()
