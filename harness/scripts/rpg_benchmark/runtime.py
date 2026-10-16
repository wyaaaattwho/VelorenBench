"""Explicit local bindings for independently hashed release content."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from . import lifecycle, output_paths, pins

SCHEMA = "rpg-runtime/1"
CONTENT = "rpg-content/1"
PACKAGE = Path(__file__).resolve().parents[1]


def local_path(base: Path, value: object, *, python: bool = False) -> Path:
    if not isinstance(value, str) or not value or ".." in Path(value).parts:
        raise ValueError(f"invalid local binding path: {value!r}")
    selected = Path(value)
    path = selected if selected.is_absolute() else base / selected
    if not python and any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError(f"symlink in local binding path: {path}")
    return path


def read_json(path: Path, digest: str | None = None) -> dict:
    if digest is not None and lifecycle.sha(path) != digest:
        raise ValueError(f"SHA-256 mismatch: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def object_field(value: dict, key: str) -> dict:
    field = value[key]
    if not isinstance(field, dict):
        raise ValueError(f"{key} must be a JSON object")
    return field


def package_files() -> dict[str, str]:
    return {name: digest for name, digest in pins.inventory(PACKAGE).items()
            if Path(name).suffix in {".py", ".json"}}


def admit_package(expected: object) -> None:
    if package_files() != pins.validate_files(expected):
        raise ValueError("executing package content changed; install the matching wheel")


def exclude_outputs(root: Path, protected: list[Path]) -> None:
    output_paths.validate_root(root)
    for path in protected:
        if (path == root or path.is_relative_to(root)
                or (path.is_dir() and root.is_relative_to(path))):
            raise ValueError(f"output root overlaps protected input: {path}")


def admit_headless(base: Path, config: dict, content: dict) -> tuple[dict, dict]:
    native, identities = object_field(config, "native"), object_field(content, "native")
    required = {"server", "bridge", "build_receipt", "assets", "engine_root", "native_source"}
    if set(native) != required:
        raise ValueError(f"native bindings must contain exactly {sorted(required)}")
    paths = {key: local_path(base, native[key]) for key in required}
    for key in ("server", "bridge", "build_receipt"):
        if not paths[key].is_file() or lifecycle.sha(paths[key]) != identities[key]:
            raise ValueError(f"missing or changed native input: {key}: {paths[key]}")
    for key in ("server", "bridge"):
        if not os.access(paths[key], os.X_OK):
            raise ValueError(f"native input is not executable: {key}")
    receipt = read_json(paths["build_receipt"])
    if receipt.get("bridge_sha256") != identities["bridge"]:
        raise ValueError("native bridge build receipt does not match selected bridge")
    if paths["assets"].name != "assets":
        raise ValueError("native assets root must end in 'assets' to avoid fallback discovery")
    pins.admit_tree(paths["assets"], identities["assets_files"])
    for key, identity in (("engine_root", "server_source_head_current"),
                          ("native_source", "native_source_head_current")):
        if not paths[key].is_dir():
            raise ValueError(f"missing native source directory: {key}")
        current = subprocess.check_output(
            ["git", "-C", str(paths[key]), "rev-parse", "HEAD"],
            text=True, stderr=subprocess.PIPE).strip()
        if current != identities[identity]:
            raise ValueError(f"native source HEAD changed: {key}")
    return paths, identities


def load_runtime(path: Path, digest: str, *, backend: str = "headless") -> dict:
    if backend not in {"headless", "renderer"}:
        raise ValueError(f"unsupported native backend: {backend}")
    path = local_path(Path.cwd(), str(path))
    config = read_json(path, digest)
    if config.get("schema") != SCHEMA:
        raise ValueError("runtime requires rpg-runtime/1; migrate legacy pins explicitly")
    if set(config) - {"schema", "content", "output_root", "native", "python",
                      "sources", "external_adapters", "protected_processes", "renderer",
                      "renderer_fixture_helper", "native_interactions"}:
        raise ValueError("unknown runtime configuration field")
    try:
        reference = object_field(config, "content")
        content_path = local_path(path.parent, reference["path"])
        content = read_json(content_path, reference["sha256"])
        if content.get("schema") != CONTENT:
            raise ValueError("unsupported content manifest")
        admit_package(content["package_files"])
        paths, identities = (admit_headless(path.parent, config, content)
                             if backend == "headless" else ({}, {}))
        scenes = read_json(Path(__file__).with_name("scenes.json"))
        python = local_path(path.parent, config.get("python", sys.executable), python=True)
        if not python.is_file() or not os.path.samefile(python, sys.executable):
            raise ValueError("runtime python must identify the executing installation interpreter")
        sources = {}
        protected = [PACKAGE, path, content_path, python, *paths.values()]
        bindings, frozen = object_field(config, "sources"), object_field(content, "sources")
        if set(bindings) != set(frozen):
            raise ValueError("source bindings do not match content identities")
        for name, files in frozen.items():
            root = local_path(path.parent, bindings[name])
            pins.admit_tree(root, files)
            if "tools/run_veloren_agent.py" not in files:
                raise ValueError("GameAgent source lacks normal entry")
            sources[name] = {"root": str(root), "commit": "unknown"}
            protected.append(root)
        adapters = {}
        bindings, frozen = object_field(config, "external_adapters"), object_field(content, "external_adapters")
        if set(bindings) != set(frozen):
            raise ValueError("adapter bindings do not match content identities")
        from .external import verify_freeze
        for name, checksum in frozen.items():
            declaration_path = local_path(path.parent, bindings[name])
            declaration = read_json(declaration_path, checksum)
            from . import capabilities, external
            if declaration.get("id") != name or declaration.get("player_interface") != external.PLAYER:
                raise ValueError(f"invalid adapter identity: {name}")
            capabilities.negotiate(declaration, {})
            implementation = local_path(declaration_path.parent, declaration["implementation"])
            if lifecycle.sha(implementation) != declaration["implementation_sha256"]:
                raise ValueError(f"adapter implementation changed: {name}")
            freeze = declaration["freeze"]
            freeze_path = local_path(declaration_path.parent, freeze["path"])
            verify_freeze(freeze_path, freeze["sha256"])
            freeze_data = read_json(freeze_path)
            protected.extend([declaration_path, freeze_path,
                              local_path(freeze_path.parent, freeze_data["root"])])
            adapters[name] = {"path": str(declaration_path), "sha256": checksum}
        renderer = None
        if ("renderer" in config) != ("renderer" in content):
            raise ValueError("optional renderer requires both local binding and content identity")
        if "renderer" in config:
            from .renderer_dependency import admit
            renderer, renderer_paths = admit(
                path.parent, object_field(config, "renderer"), object_field(content, "renderer"))
            protected.extend(renderer_paths)
        helper = None
        if ("renderer_fixture_helper" in config) != ("renderer_fixture_helper" in content):
            raise ValueError("renderer fixture helper requires explicit binding and content identity")
        if "renderer_fixture_helper" in config:
            if renderer is None:
                raise ValueError("renderer fixture helper requires matching renderer identity")
            binding = object_field(config, "renderer_fixture_helper")
            frozen = object_field(content, "renderer_fixture_helper")
            if set(binding) != {"executable", "build_receipt", "source_root"} or set(frozen) != {
                    "executable_sha256", "build_receipt_sha256", "source_tree_sha256"}:
                raise ValueError("invalid renderer fixture helper contract")
            helper_paths = {key: local_path(path.parent, value) for key, value in binding.items()}
            for key in ("executable", "build_receipt"):
                if lifecycle.sha(helper_paths[key]) != frozen[key + "_sha256"]:
                    raise ValueError("renderer fixture helper changed: " + key)
            helper_receipt = read_json(helper_paths["build_receipt"])
            if (helper_receipt.get("schema") != "rpg-renderer-fixture-helper/1"
                    or frozen["source_tree_sha256"] != renderer["source_tree_sha256"]
                    or helper_receipt.get("source_tree_sha256") != frozen["source_tree_sha256"]
                    or helper_receipt.get("executable_sha256") != frozen["executable_sha256"]
                    or helper_receipt.get("source_modifications") != []
                    or not isinstance(helper_receipt.get("build_command"), str)
                    or not helper_receipt["build_command"]
                    or not os.access(helper_paths["executable"], os.X_OK)):
                raise ValueError("renderer fixture helper source/binary identity mismatch")
            pins.validate_files({"build_log": helper_receipt["build_log_sha256"]})
            source_files = read_json(Path(renderer["paths"]["source_manifest"]))["files_sha256"]
            for name, checksum in source_files.items():
                source_path = local_path(helper_paths["source_root"], name)
                if lifecycle.sha(source_path) != checksum:
                    raise ValueError("renderer fixture helper corresponding source changed: " + name)
            protected.extend(helper_paths.values())
            helper = {key: str(value) for key, value in helper_paths.items()}
        if backend == "renderer":
            if renderer is None or helper is None:
                raise ValueError("renderer backend requires verified renderer and matching-source fixture helper")
            paths = {
                "server": Path(renderer["paths"]["server"]), "bridge": Path(helper["executable"]),
                "build_receipt": Path(helper["build_receipt"]),
                "assets": Path(renderer["paths"]["assets"]),
                "engine_root": Path(helper["source_root"]), "native_source": Path(helper["source_root"]),
            }
            identities = {
                "server": renderer["server_sha256"],
                "bridge": content["renderer_fixture_helper"]["executable_sha256"],
                "build_receipt": content["renderer_fixture_helper"]["build_receipt_sha256"],
                "source_tree_sha256": renderer["source_tree_sha256"],
                "source_manifest_sha256": renderer["source_manifest_sha256"],
                "renderer_build_receipt_sha256": renderer["build_receipt_sha256"],
            }
        interactions = None
        if ("native_interactions" in config) != ("native_interactions" in content):
            raise ValueError("native RPG interfaces require binding and content identities")
        if "native_interactions" in config:
            from .native_bundle import admit
            interactions, extra = admit(path.parent, object_field(config, "native_interactions"),
                                        object_field(content, "native_interactions"), renderer, helper)
            protected.extend(extra)
        for definition in scenes["scenes"]:
            if "asset" in definition and not (
                    paths["assets"] / (definition["asset"].replace(".", "/") + ".ron")).is_file():
                raise ValueError(f"native assets lack concrete scene entity: {definition['asset']}")
        output = local_path(path.parent, config["output_root"])
        exclude_outputs(output, protected)
        lifecycle.configure_guards(config.get("protected_processes", []))
        scene_seeds = scenes["seeds"]
        if (not isinstance(content["seeds"], list)
                or any(type(seed) is not int for seed in content["seeds"])
                or content["seeds"] != scene_seeds):
            raise ValueError("content seeds disagree with the unchanged task manifest")
    except KeyError as error:
        raise ValueError(f"missing required runtime/content field: {error.args[0]}") from error
    dependencies = {key: {"path": str(paths[key]), "sha256": identities[key]}
                    for key in ("server", "bridge", "build_receipt")}
    for key, file in (("scene_manifest", Path(__file__).with_name("scenes.json")),
                      ("sdk", PACKAGE / "rpg_sdk_agent.py")):
        dependencies[key] = {"path": str(file), "sha256": lifecycle.sha(file)}
    dependencies["python"] = {"path": str(python), "sha256": lifecycle.sha(python)}
    native_identity = {"backend": backend, **{key: identities[key] for key in (
        "server", "bridge", "build_receipt")}}
    if backend == "renderer":
        native_identity.update({key: identities[key] for key in (
            "source_tree_sha256", "source_manifest_sha256", "renderer_build_receipt_sha256")})
        native_identity["source_identity_policy"] = "content-inventory-and-build-receipts; no Git HEAD"
    else:
        native_identity["source_identity_policy"] = "legacy-headless-receipt-and-source-HEAD; not renderer identity"
    return {"schema": SCHEMA, "backend": backend, "native_backend_identity": native_identity,
            "content_sha256": reference["sha256"],
            "runtime_sha256": digest, "runtime_path": str(path),
            "dependencies": dependencies, "sources": sources, "external_adapters": adapters,
            "seeds": content["seeds"], "native": identities,
            "engine_root": str(paths["engine_root"]), "assets": {"root": str(paths["assets"])},
            "output_root": str(output), "protected_paths": [str(p) for p in protected],
            "protected_processes": lifecycle.guards(),
            "renderer": renderer,
            "renderer_fixture_helper": helper,
            "native_interactions": interactions,
            "harness": {"root": str(Path(__file__).parent)}}


def migrate(legacy: Path, digest: str, destination: Path, native_source: Path,
            *, with_gameagent: bool = False) -> dict:
    """Preserve native/task identities, not historical locations as content identity."""
    parent = read_json(legacy, digest)
    if parent.get("schema") != 2:
        raise ValueError("migration requires explicit schema 2 pins")
    if lifecycle.sha(Path(__file__).with_name("scenes.json")) != parent["dependencies"]["scene_manifest"]["sha256"]:
        raise ValueError("old task manifest changed")
    native = {key: parent["dependencies"][key]["sha256"]
              for key in ("server", "bridge", "build_receipt")}
    native.update({key: parent["native"][key] for key in (
        "native_source_head_current", "server_source_head_current")})
    native["assets_files"] = parent["assets"]["files"]
    content = {"schema": CONTENT, "parent_pins_sha256": digest, "package_files": package_files(),
               "seeds": parent["seeds"], "native": native, "sources": {}, "external_adapters": {}}
    config = {"schema": SCHEMA, "content": {"path": "content.json"},
              "output_root": "runs", "native": {
                  **{key: parent["dependencies"][key]["path"]
                     for key in ("server", "bridge", "build_receipt")},
                  "assets": parent["assets"]["root"], "engine_root": parent["engine_root"],
                  "native_source": str(native_source)},
              "sources": {}, "external_adapters": {}, "protected_processes": []}
    if with_gameagent:
        for name, selected in parent["sources"].items():
            manifest = read_json(Path(selected["manifest"]), selected["manifest_sha256"])
            pins.admit_tree(Path(selected["root"]), manifest["files"])
            content["sources"][name] = manifest["files"]
            config["sources"][name] = selected["root"]
    destination.mkdir(parents=True, exist_ok=False)
    lifecycle.write(destination / "content.json", content)
    config["content"]["sha256"] = lifecycle.sha(destination / "content.json")
    lifecycle.write(destination / "runtime.json", config)
    return {"runtime": str(destination / "runtime.json"),
            "runtime_sha256": lifecycle.sha(destination / "runtime.json"),
            "content_sha256": config["content"]["sha256"],
            "status": "created-not-validated; run check with explicit local inputs"}


def bind_renderer(path: Path, digest: str, descriptor_path: Path, descriptor_sha: str,
                  destination: Path) -> dict:
    from .renderer_dependency import admit
    selected = load_runtime(path, digest)
    if selected["renderer"] is not None:
        raise ValueError("renderer already bound; supply an explicit renderer-free base binding")
    path = Path(selected["runtime_path"])
    descriptor_path = local_path(Path.cwd(), str(descriptor_path))
    descriptor = read_json(descriptor_path, descriptor_sha)
    if set(descriptor) != {"content_renderer", "runtime_renderer"}:
        raise ValueError("renderer descriptor requires content_renderer and runtime_renderer")
    identity = object_field(descriptor, "content_renderer")
    _, protected = admit(descriptor_path.parent, object_field(descriptor, "runtime_renderer"), identity)
    destination = local_path(Path.cwd(), str(destination))
    exclude_outputs(destination / "runs", [Path(p) for p in selected["protected_paths"]] + protected)
    config = read_json(path, digest)
    content_path = local_path(path.parent, config["content"]["path"])
    content = read_json(content_path, config["content"]["sha256"])
    for field in ("native", "sources", "external_adapters"):
        config[field] = {key: str(local_path(path.parent, value))
                         for key, value in config[field].items()}
    if "python" in config:
        config["python"] = str(local_path(path.parent, config["python"], python=True))
    config["renderer"] = {key: str(local_path(descriptor_path.parent, value))
                          for key, value in descriptor["runtime_renderer"].items()}
    content["renderer"] = identity
    config["output_root"] = "runs"
    destination.mkdir(parents=True, exist_ok=False)
    lifecycle.write(destination / "content.json", content)
    config["content"] = {"path": "content.json", "sha256": lifecycle.sha(destination / "content.json")}
    lifecycle.write(destination / "runtime.json", config)
    checksum = lifecycle.sha(destination / "runtime.json")
    admitted = load_runtime(destination / "runtime.json", checksum)
    return {"runtime": str(destination / "runtime.json"), "runtime_sha256": checksum,
            "content_sha256": admitted["content_sha256"], "renderer": admitted["renderer"],
            "status": "inputs-validated-not-executed"}
