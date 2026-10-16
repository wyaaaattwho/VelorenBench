"""Portable local CLI. Native/model execution is always explicit."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path
from subprocess import CalledProcessError
import sys

from . import lifecycle, pins, runtime, suite


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("identity", help="Installed package/interpreter identity; not native readiness.")
    sub.add_parser("tasks", help="List 29 definitions: original17 plus T01-T12; inspect support for exact task/modality admission.")
    sub.add_parser("support", help="Explicit task/mode/action-profile support and qualification boundaries.")
    native = sub.add_parser("bind-native", help="Create a relocatable renderer binding from an exact local native bundle; no launch.")
    native.add_argument("--native", required=True, type=Path, help="Explicit bundle.json; no implicit source/cache discovery.")
    native.add_argument("--native-sha256", required=True)
    native.add_argument("--assets", required=True, type=Path, help="Explicit external assets directory; never redistributed.")
    native.add_argument("--out", required=True, type=Path, help="New directory; native inputs are copied locally, assets remain external.")
    check = sub.add_parser("check", help="Validate the selected backend's local bindings without launching a process.")
    check.add_argument("--config", required=True, type=Path)
    check.add_argument("--config-sha256", required=True)
    check.add_argument("--backend", choices=("headless", "renderer"), default="headless")
    migrate = sub.add_parser("migrate", help="Create NEW content/config from exact legacy pins; no launch.")
    migrate.add_argument("--legacy-pins", required=True, type=Path)
    migrate.add_argument("--legacy-sha256", required=True)
    migrate.add_argument("--native-source", required=True, type=Path)
    migrate.add_argument("--out", required=True, type=Path)
    migrate.add_argument("--with-gameagent", action="store_true",
                         help="Bind separately supplied source; does not grant distribution rights.")
    bind = sub.add_parser("bind-renderer", help="Create a NEW binding with verified optional renderer inputs; no launch.")
    bind.add_argument("--config", required=True, type=Path)
    bind.add_argument("--config-sha256", required=True)
    bind.add_argument("--renderer", required=True, type=Path, help="Explicit local renderer descriptor JSON.")
    bind.add_argument("--renderer-sha256", required=True)
    bind.add_argument("--out", required=True, type=Path)
    sub.add_parser("run", help="Native episode; use run --help for explicit config and run arguments.")
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        from .run import main as run_main
        del sys.argv[1]
        run_main()
        return
    args = parser.parse_args()
    try:
        if args.command == "identity":
            value = {"package": "rpg-benchmark-harness",
                     "version": importlib.metadata.version("rpg-benchmark-harness"),
                     "package_root": str(runtime.PACKAGE), "python": sys.executable,
                     "python_sha256": lifecycle.sha(Path(sys.executable)),
                     "package_files": runtime.package_files(), "native_validated": False}
        elif args.command == "support":
            from .renderer_support import support_matrix
            value = support_matrix()
        elif args.command == "tasks":
            definitions = suite.load(Path(suite.__file__).with_name("scenes.json"))
            value = {"task_count": len(definitions), "tasks": list(definitions),
                     "scene_manifest_sha256": lifecycle.sha(Path(suite.__file__).with_name("scenes.json"))}
        elif args.command == "check":
            selected = pins.load(args.config, args.config_sha256, backend=args.backend)
            value = {key: selected[key] for key in (
                "runtime_path", "runtime_sha256", "content_sha256", "output_root",
                "backend", "native_backend_identity", "dependencies", "engine_root", "assets",
                "sources", "protected_processes", "renderer", "renderer_fixture_helper")}
            value.update(status="inputs-validated", native_execution="not-run", build_closure="partial")
        elif args.command == "bind-renderer":
            value = runtime.bind_renderer(args.config, args.config_sha256, args.renderer,
                                          args.renderer_sha256, args.out)
        elif args.command == "bind-native":
            from .release import bind_native
            value = bind_native(args.native, args.native_sha256, args.assets, args.out)
        else:
            value = runtime.migrate(args.legacy_pins.absolute(), args.legacy_sha256,
                                    args.out.absolute(), args.native_source.absolute(),
                                    with_gameagent=args.with_gameagent)
    except (ValueError, OSError, KeyError, CalledProcessError) as error:
        parser.exit(2, f"rpg-benchmark: {type(error).__name__}: {error}\n")
    print(json.dumps(value, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
