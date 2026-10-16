import asyncio
import json
import shutil
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts.rpg_benchmark import cli, lifecycle, pins, run, runtime
from .test_portability import save_fixture, write
from .test_renderer_dependency import renderer_fixture


def fixture(root, *, renderer_only=True):
    config, content = renderer_fixture(root)
    helper = root / "helper"
    source = helper / "source"
    source.mkdir(parents=True)
    shutil.copy2(root / "renderer-inputs/source/Cargo.toml", source / "Cargo.toml")
    executable = helper / "agent_bridge"
    executable.write_text("synthetic matching-source helper; never native execution\n")
    executable.chmod(0o700)
    receipt = {
        "schema": "rpg-renderer-fixture-helper/1",
        "source_tree_sha256": content["renderer"]["source_tree_sha256"],
        "executable_sha256": lifecycle.sha(executable),
        "source_modifications": [], "build_command": "synthetic test-only build provenance",
        "build_log_sha256": "a" * 64,
    }
    write(helper / "build.json", receipt)
    config["renderer_fixture_helper"] = {
        "executable": "helper/agent_bridge", "build_receipt": "helper/build.json",
        "source_root": "helper/source"}
    content["renderer_fixture_helper"] = {
        "executable_sha256": lifecycle.sha(executable),
        "build_receipt_sha256": lifecycle.sha(helper / "build.json"),
        "source_tree_sha256": receipt["source_tree_sha256"]}
    if renderer_only:
        config.pop("native")
        content.pop("native")
    save_fixture(root, config, content)
    return config, content


def load_renderer(root):
    with patch.object(runtime.subprocess, "check_output", side_effect=AssertionError("Git must not run")):
        return pins.load(root / "runtime.json", lifecycle.sha(root / "runtime.json"), backend="renderer")


def test_renderer_only_admission_uses_selected_binaries_and_content_inventory_without_git(tmp_path):
    fixture(tmp_path)
    selected = load_renderer(tmp_path)
    assert selected["backend"] == "renderer"
    assert selected["dependencies"]["server"]["path"] == str(tmp_path / "renderer-inputs/server")
    assert selected["dependencies"]["bridge"]["path"] == str(tmp_path / "helper/agent_bridge")
    assert selected["dependencies"]["build_receipt"]["path"] == str(tmp_path / "helper/build.json")
    assert selected["engine_root"] == str(tmp_path / "helper/source")
    assert selected["native_backend_identity"]["source_tree_sha256"] == "1" * 64
    assert "HEAD" not in json.dumps(selected["native"])
    assert not (tmp_path / "helper/source/.git").exists()
    assert not (tmp_path / "renderer-inputs/source/.git").exists()


def test_inactive_legacy_headless_files_are_never_required_or_hashed_for_renderer(tmp_path):
    fixture(tmp_path, renderer_only=False)
    for name in ("server", "bridge", "receipt.json"):
        (tmp_path / name).unlink()
    for name in ("engine", "native-source"):
        (tmp_path / name).rmdir()
    selected = load_renderer(tmp_path)
    assert selected["dependencies"]["bridge"]["path"].endswith("/helper/agent_bridge")
    with pytest.raises((ValueError, FileNotFoundError)):
        pins.load(tmp_path / "runtime.json", lifecycle.sha(tmp_path / "runtime.json"), backend="headless")


def test_renderer_only_binding_does_not_silently_select_a_headless_fallback(tmp_path):
    fixture(tmp_path)
    with pytest.raises(ValueError, match="missing required.*native"):
        pins.load(tmp_path / "runtime.json", lifecycle.sha(tmp_path / "runtime.json"))
    with pytest.raises(ValueError, match="unsupported native backend"):
        pins.load(tmp_path / "runtime.json", lifecycle.sha(tmp_path / "runtime.json"), backend="spectator")


@pytest.mark.parametrize("which", ["renderer", "renderer_fixture_helper"])
def test_renderer_backend_requires_both_renderer_and_same_source_helper(tmp_path, which):
    config, content = fixture(tmp_path)
    config.pop(which)
    content.pop(which)
    save_fixture(tmp_path, config, content)
    with pytest.raises(ValueError, match="requires.*renderer|requires.*fixture helper"):
        load_renderer(tmp_path)


@pytest.mark.parametrize("target", ["renderer-inputs/source/Cargo.toml", "helper/source/Cargo.toml",
                                  "helper/agent_bridge", "renderer-inputs/server"])
def test_renderer_selected_source_and_binary_tampering_is_still_rejected(tmp_path, target):
    fixture(tmp_path)
    (tmp_path / target).write_text("changed selected content\n")
    with pytest.raises(ValueError, match="changed|mismatch"):
        load_renderer(tmp_path)


@pytest.mark.parametrize("field,value", [
    ("source_tree_sha256", "3" * 64), ("schema", "legacy-head-only"),
    ("source_modifications", ["unadmitted Rust patch"]), ("build_command", ""),
    ("build_log_sha256", "not-a-hash")])
def test_helper_build_provenance_cannot_be_replaced_by_head_or_an_unadmitted_patch(tmp_path, field, value):
    config, content = fixture(tmp_path)
    receipt = tmp_path / "helper/build.json"
    data = runtime.read_json(receipt)
    data[field] = value
    content["renderer_fixture_helper"]["build_receipt_sha256"] = write(receipt, data)
    save_fixture(tmp_path, config, content)
    with pytest.raises(ValueError):
        load_renderer(tmp_path)


def test_renderer_only_bindings_relocate_with_identical_content_and_no_git(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    fixture(first)
    before = load_renderer(first)
    shutil.copytree(first, second)
    after = load_renderer(second)
    assert before["content_sha256"] == after["content_sha256"]
    assert before["runtime_sha256"] == after["runtime_sha256"]
    assert before["dependencies"]["bridge"]["path"] != after["dependencies"]["bridge"]["path"]


def test_cli_check_selects_renderer_without_a_headless_dependency(tmp_path, capsys):
    fixture(tmp_path)
    argv = ["rpg-benchmark", "check", "--config", str(tmp_path / "runtime.json"),
            "--config-sha256", lifecycle.sha(tmp_path / "runtime.json"), "--backend", "renderer"]
    with patch.object(sys, "argv", argv), patch.object(
            runtime.subprocess, "check_output", side_effect=AssertionError("Git must not run")):
        cli.main()
    output = json.loads(capsys.readouterr().out)
    assert output["backend"] == "renderer"
    assert output["dependencies"]["bridge"]["path"] == str(tmp_path / "helper/agent_bridge")
    assert output["native_execution"] == "not-run"


@pytest.mark.parametrize("entry", ["episode", "_episode"])
def test_both_runner_admission_stages_forward_the_explicit_backend(tmp_path, entry):
    args = SimpleNamespace(pins=tmp_path / "runtime.json", pins_sha256="a" * 64, backend="renderer")
    with patch.object(pins, "load", side_effect=ValueError("admission sentinel")) as load:
        with pytest.raises(ValueError, match="admission sentinel"):
            asyncio.run(run.episode(args) if entry == "episode" else run._episode(args, None, tmp_path))
    load.assert_called_once_with(args.pins, args.pins_sha256, backend="renderer")
