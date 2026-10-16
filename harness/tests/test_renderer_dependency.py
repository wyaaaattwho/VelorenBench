import copy
from pathlib import Path
import platform
import shutil

import pytest

from scripts.rpg_benchmark import lifecycle, pins, renderer_dependency as renderer
from .test_portability import fixture_config, load_fixture, save_fixture, write


def renderer_fixture(root):
    config, content = fixture_config(root)
    inputs = root / "renderer-inputs"
    source = inputs / "source"
    source.mkdir(parents=True)
    (source / "Cargo.toml").write_text("# synthetic source; never compiled\n")
    for name in ("voxygen", "server"):
        path = inputs / name
        path.write_text(f"synthetic {name} binary; never executed\n")
        path.chmod(0o700)
    source_tree, asset_tree = "1" * 64, "2" * 64
    contract = {
        "contract": "veloren-ordinary-native-frame-action", "version": "1.0",
        "native_protocol_version": "0.2", "timing_profile": renderer.CONTRACT_TIMING,
        "frame_response": {"result": {
            "native_tick": None, "image": {"model_attachment_delivered": False},
            "correlation": {"action_effect_ordering": "unconfirmed"}}}}
    write(inputs / "contract.json", contract)
    write(inputs / "build.json", {"architecture": platform.machine(),
                                  "source_tree_sha256": source_tree, "assets_tree_sha256": asset_tree})
    write(inputs / "source.json", {"source_tree_sha256": source_tree,
                                   "files_sha256": pins.inventory(source)})
    write(inputs / "assets.json", {"tree_sha256": asset_tree,
                                   "root": "/old/installation/assets",
                                   "files_sha256": pins.inventory(root / "assets")})
    bindings = {
        "executable": "renderer-inputs/voxygen", "server": "renderer-inputs/server",
        "interface_contract": "renderer-inputs/contract.json", "build_receipt": "renderer-inputs/build.json",
        "source_manifest": "renderer-inputs/source.json", "assets_manifest": "renderer-inputs/assets.json",
        "source_root": "renderer-inputs/source", "assets": "assets"}
    identity = {
        "schema": renderer.SCHEMA, "kind": "ordinary-voxygen",
        "timing_profile": renderer.TIMING_PROFILE, "native_tick": None,
        "headless_timing_equivalent": False, "architecture": platform.machine(),
        "source_tree_sha256": source_tree, "assets_tree_sha256": asset_tree,
        **{key + "_sha256": lifecycle.sha(root / bindings[key]) for key in renderer.HASHED}}
    config["renderer"], content["renderer"] = bindings, identity
    save_fixture(root, config, content)
    return config, content


def test_optional_renderer_is_verified_but_not_selected_or_capability_enabled(tmp_path):
    renderer_fixture(tmp_path)
    selected = load_fixture(tmp_path)
    bound = selected["renderer"]
    assert bound["schema"] == renderer.SCHEMA
    assert bound["timing_profile"] == renderer.TIMING_PROFILE
    assert bound["native_tick"] is None and bound["headless_timing_equivalent"] is False
    assert not bound["selected_for_episode"] and not bound["observation_support_enabled"]
    assert not bound["model_attachment_delivered"] and bound["native_execution"] == "not-run"
    assert selected["dependencies"]["server"]["path"] != bound["paths"]["server"]
    assert bound["build_closure"] == "unqualified"
    assert bound["redistribution_license_closure"] == "unresolved"


def test_renderer_dependency_is_truly_optional_and_never_half_configured(tmp_path):
    config, content = renderer_fixture(tmp_path)
    for absent in ("config", "content"):
        altered_config, altered_content = copy.deepcopy(config), copy.deepcopy(content)
        (altered_config if absent == "config" else altered_content).pop("renderer")
        save_fixture(tmp_path, altered_config, altered_content)
        with pytest.raises(ValueError, match="both local binding and content identity"):
            load_fixture(tmp_path)
    config.pop("renderer")
    content.pop("renderer")
    save_fixture(tmp_path, config, content)
    assert load_fixture(tmp_path)["renderer"] is None


def test_renderer_binding_relocates_without_changing_content_identity(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    renderer_fixture(first)
    old = load_fixture(first)
    shutil.copytree(first, second)
    moved = load_fixture(second)
    assert old["content_sha256"] == moved["content_sha256"]
    assert old["renderer"]["paths"]["source_root"] != moved["renderer"]["paths"]["source_root"]
    assert moved["renderer"]["timing_profile"] == renderer.TIMING_PROFILE


@pytest.mark.parametrize("key", sorted(renderer.HASHED))
def test_missing_renderer_input_fails_explicitly_not_as_optional_absence(tmp_path, key):
    config, _ = renderer_fixture(tmp_path)
    (tmp_path / config["renderer"][key]).rename(tmp_path / "unbound-input")
    with pytest.raises(ValueError, match="missing or changed renderer input"):
        load_fixture(tmp_path)


@pytest.mark.parametrize("field,value", [
    ("kind", "spectator"), ("timing_profile", "headless-fixed-60Hz"),
    ("native_tick", 0), ("headless_timing_equivalent", True),
    ("headless_timing_equivalent", 0), ("architecture", "different-architecture")])
def test_renderer_type_timing_and_architecture_are_not_coerced(tmp_path, field, value):
    config, content = renderer_fixture(tmp_path)
    content["renderer"][field] = value
    save_fixture(tmp_path, config, content)
    with pytest.raises(ValueError):
        load_fixture(tmp_path)


@pytest.mark.parametrize("part,value", [
    ("native_tick", 1), ("model_attachment_delivered", True), ("action_effect_ordering", "confirmed")])
def test_contract_cannot_claim_server_tick_provider_delivery_or_confirmed_ordering(tmp_path, part, value):
    from scripts.rpg_benchmark.runtime import read_json
    config, content = renderer_fixture(tmp_path)
    path = tmp_path / config["renderer"]["interface_contract"]
    contract = read_json(path)
    frame = contract["frame_response"]["result"]
    target = frame if part == "native_tick" else frame["image"] if part == "model_attachment_delivered" else frame["correlation"]
    target[part] = value
    content["renderer"]["interface_contract_sha256"] = write(path, contract)
    save_fixture(tmp_path, config, content)
    with pytest.raises(ValueError, match="unknown tick/ordering"):
        load_fixture(tmp_path)


def test_renderer_source_change_and_build_identity_mismatch_are_rejected(tmp_path):
    from scripts.rpg_benchmark.runtime import read_json
    config, content = renderer_fixture(tmp_path)
    source = tmp_path / "renderer-inputs/source/Cargo.toml"
    original = source.read_text()
    source.write_text("changed")
    with pytest.raises(ValueError, match="source inventory changed"):
        load_fixture(tmp_path)
    source.write_text(original)
    receipt = tmp_path / config["renderer"]["build_receipt"]
    value = read_json(receipt)
    value["source_tree_sha256"] = "3" * 64
    content["renderer"]["build_receipt_sha256"] = write(receipt, value)
    save_fixture(tmp_path, config, content)
    with pytest.raises(ValueError, match="build/source/assets identity"):
        load_fixture(tmp_path)


def test_renderer_inputs_reject_relative_escape_symlinks_and_output_overlap(tmp_path):
    config, content = renderer_fixture(tmp_path)
    original = copy.deepcopy(config)
    for output in ("renderer-inputs/source/world", "renderer-inputs"):
        config["output_root"] = output
        save_fixture(tmp_path, config, content)
        with pytest.raises(ValueError, match="overlaps protected input"):
            load_fixture(tmp_path)
    config = copy.deepcopy(original)
    config["renderer"]["source_root"] = "renderer-inputs/../source"
    save_fixture(tmp_path, config, content)
    with pytest.raises(ValueError, match="binding path"):
        load_fixture(tmp_path)
    config = copy.deepcopy(original)
    link = tmp_path / "renderer-link"
    link.symlink_to("renderer-inputs/voxygen")
    config["renderer"]["executable"] = "renderer-link"
    save_fixture(tmp_path, config, content)
    with pytest.raises(ValueError, match="symlink"):
        load_fixture(tmp_path)


def test_declared_source_inventory_cannot_escape_or_weaken_ordinary_contract(tmp_path):
    from scripts.rpg_benchmark.runtime import read_json
    config, content = renderer_fixture(tmp_path)
    manifest = tmp_path / config["renderer"]["source_manifest"]
    value = read_json(manifest)
    value["files_sha256"]["../escape"] = "0" * 64
    content["renderer"]["source_manifest_sha256"] = write(manifest, value)
    save_fixture(tmp_path, config, content)
    with pytest.raises(ValueError, match="inventory entry"):
        load_fixture(tmp_path)


def test_bind_renderer_cli_helper_creates_new_admitted_binding_without_overwriting(tmp_path):
    from unittest.mock import patch
    from scripts.rpg_benchmark import runtime
    base = tmp_path / "base"
    config, content = renderer_fixture(base)
    descriptor = {"content_renderer": content.pop("renderer"),
                  "runtime_renderer": config.pop("renderer")}
    save_fixture(base, config, content)
    descriptor_path = base / "renderer.json"
    descriptor_sha = write(descriptor_path, descriptor)
    out = tmp_path / "with-renderer"
    original_sha = lifecycle.sha(base / "runtime.json")
    with patch.object(runtime.subprocess, "check_output", return_value="mock-head\n"):
        result = runtime.bind_renderer(base / "runtime.json", original_sha,
                                       descriptor_path, descriptor_sha, out)
        assert result["status"] == "inputs-validated-not-executed"
        assert result["renderer"]["selected_for_episode"] is False
        assert runtime.load_runtime(out / "runtime.json", result["runtime_sha256"])["renderer"] is not None
        with pytest.raises(FileExistsError):
            runtime.bind_renderer(base / "runtime.json", original_sha, descriptor_path, descriptor_sha, out)
        with pytest.raises(ValueError, match="already bound"):
            runtime.bind_renderer(out / "runtime.json", result["runtime_sha256"], descriptor_path,
                                  descriptor_sha, tmp_path / "unexpected")
    assert lifecycle.sha(base / "runtime.json") == original_sha
    assert not (tmp_path / "unexpected").exists()


def test_renderer_never_substitutes_the_headless_server(tmp_path):
    config, content = renderer_fixture(tmp_path)
    config["renderer"]["server"] = "server"
    save_fixture(tmp_path, config, content)
    with pytest.raises(ValueError, match="missing or changed renderer input: server"):
        load_fixture(tmp_path)
