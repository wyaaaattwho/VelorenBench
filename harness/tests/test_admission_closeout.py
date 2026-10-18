"""Production-path closeout tests. ALL positive evidence here is synthetic TEST ONLY."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import sys

import pytest

from scripts.rpg_benchmark import (
    capabilities, cli, launching, lifecycle, providers, qualification, renderer_support,
    run, suite, support, task_templates,
)
from scripts.rpg_benchmark.responses_adapter import Adapter
from .test_backend_admission import fixture, load_renderer
from .test_generic_release import example_manifest
from .test_portability import save_fixture


def selected_native(tmp_path):
    config, content = fixture(tmp_path)
    path = tmp_path / config["renderer"]["interface_contract"]
    value = json.loads(path.read_text())
    value["benchmark_features"] = sorted(support.RENDERER_FEATURES)
    path.write_text(json.dumps(value))
    content["renderer"]["interface_contract_sha256"] = lifecycle.sha(path)
    save_fixture(tmp_path, config, content)
    return load_renderer(tmp_path)


def args_for(tmp_path, scene="C1", terrain="natural", mode="image", profile=None):
    # Use the real ordinary CLI parser, including omitted profile/mode handling.
    return run.parser().parse_args([
        "--config", str(tmp_path / "runtime.json"), "--config-sha256", "0" * 64,
        "--out", str(tmp_path / "runs/run"), "--mode", "task-engineering",
        "--backend", "renderer", "--harness", "responses", "--scene", scene,
        "--terrain", terrain, "--recordings", str(tmp_path / "outputs/recordings/test"),
        "--adapter-config", json.dumps({"mock_provider": "http://127.0.0.1:1234",
                                       "provider": providers.LUNA}),
        "--unqualified-engineering",
        *(["--observation-mode", mode] if mode is not None else []),
        *(["--action-profile", profile] if profile is not None else []),
    ])


def record_for(definition, selected, *, terrain="flat", mode="image", profile=None):
    identity = qualification.selected_native(selected)
    cell = support.resolve(definition, terrain, "renderer", mode, profile,
                           native_identity=identity, native_features=support.RENDERER_FEATURES,
                           definitions=suite.load(Path(selected["dependencies"]["scene_manifest"]["path"])),
                           task_manifest_sha256=lifecycle.sha(Path(
                               selected["dependencies"]["scene_manifest"]["path"])))
    artifact = b"TEST ONLY: synthetic engineering evidence. Not native gameplay or model success.\n"
    return {
        "key": cell["qualification_key"], "status": "engineering-qualified",
        "scope": "TEST ONLY synthetic structure/admission regression; NOT production evidence",
        "evidence": [{"reference": "test-artifact:synthetic-no-gameplay",
                      "sha256": hashlib.sha256(artifact).hexdigest(),
                      "scope": "TEST ONLY; does not demonstrate gameplay"}],
        "allowed_run_modes": sorted(support.RUN_MODES),
    }


def install_records(tmp_path, monkeypatch, records):
    path = tmp_path / "test-only-qualification.json"
    path.write_text(json.dumps({"schema": qualification.SCHEMA, "records": records}))
    monkeypatch.setattr(qualification, "PATH", path)
    return path


def test_production_registry_is_valid_non_synthetic_and_historical_artifact_untouched():
    records = qualification.load()["records"]  # validates identities, conflicts and evidence structure
    for record in records:
        assert record["key"]["native_identity"] is not None
        assert all("TEST ONLY" not in item["scope"] and
                   not item["reference"].startswith("test-artifact:")
                   for item in record["evidence"])
    assert hashlib.sha256(qualification.PATH.with_name("qualification.json").read_bytes()).hexdigest() == (
        "913bbd1c02bd60f967ea55e4720757dd9006e5530c2a9e6fcb846f09b6666430")
    historical = renderer_support.historical_policy()
    assert historical["renderer"]["T12/natural"]["structured"]["scored_evaluation"]
    assert historical["renderer"]["T05/natural"]["image"]["allowed_run_modes"] == ["task-engineering"]


@pytest.mark.parametrize("mode,profile", [
    ("image", None), ("image", "visual-ui-v1"), ("image", "visual-motor-v1"),
    ("structured", None), ("hybrid", None), (None, None),
])
def test_actual_responses_declaration_cli_negotiation_and_renderer_admission(tmp_path, mode, profile):
    selected = selected_native(tmp_path)
    args = args_for(tmp_path, mode=mode, profile=profile)
    effective = launching.configuration(args, selected)
    adapter = Adapter(args)
    assert effective == adapter.configuration
    expected_mode = mode or "structured"
    expected_profile = profile or capabilities.default_profile("renderer", expected_mode)
    assert effective["observation_mode"] == expected_mode
    assert effective["action_profile"] == expected_profile
    args.observation_mode, args.action_profile = effective["observation_mode"], effective["action_profile"]
    # C1 needs crafting: a genuinely motor-only profile must NOT admit it.
    if expected_profile == "visual-motor-v1":
        with pytest.raises(ValueError, match="missing native/profile features"):
            renderer_support.admit_selection(args, selected)
    else:
        cell = renderer_support.admit_selection(args, selected)
        assert cell["action_profile"] == expected_profile and not cell["scored_evaluation"]
        assert cell["qualification_key"]["native_identity"] == qualification.selected_native(selected)
        assert cell["qualification_basis"] == "selected-native-and-code"
    assert not args.out.exists() and not args.recordings.exists()


def external_declaration(tmp_path, mode="image", profile="visual-motor-v1"):
    declaration = providers.manifest(providers.LUNA, mode)
    declaration["default_configuration"]["action_profile"] = profile
    declaration["capabilities"]["action_profiles_by_observation_mode"]["image"] = ["visual-motor-v1"]
    declaration["capabilities"]["action_profiles"] = ["visual-motor-v1", "ordinary-renderer-native-v1"]
    path = tmp_path / "external.json"
    path.write_text(json.dumps(declaration))
    return {"external_adapters": {"motor-only": {"path": str(path), "sha256": lifecycle.sha(path)}}}


@pytest.mark.parametrize("default_mode,explicit", [("image", None), ("image", "visual-ui-v1"),
                                                 ("hybrid", None), ("structured", None)])
def test_external_defaults_not_silently_upgraded_before_ordinary_episode_allocation(
        tmp_path, monkeypatch, default_mode, explicit):
    selected = selected_native(tmp_path)
    selected.update(external_declaration(tmp_path, default_mode,
        "visual-motor-v1" if default_mode == "image" else "ordinary-renderer-native-v1"))
    args = args_for(tmp_path, mode=None, profile=explicit)
    args.harness, args.adapter = "external", "motor-only"
    monkeypatch.setattr(run.pins, "load", lambda *a, **k: selected)
    def no_allocation(*a, **k):
        raise RuntimeError("TEST ONLY allocation boundary reached")
    monkeypatch.setattr(run, "reserve_output", no_allocation)
    if explicit is not None:
        with pytest.raises(ValueError, match="Unsupported observation mode/action profile mapping"):
            asyncio.run(run.episode(args))
    elif default_mode == "image":
        with pytest.raises(ValueError, match="missing native/profile features"):
            asyncio.run(run.episode(args))
        assert args.observation_mode == "image" and args.action_profile == "visual-motor-v1"
    else:
        with pytest.raises(RuntimeError, match="allocation boundary"):
            asyncio.run(run.episode(args))
        assert args.observation_mode == default_mode
        assert args.action_profile == "ordinary-renderer-native-v1"
    assert not args.out.exists() and not args.recordings.exists()


@pytest.mark.parametrize("profile", [None, "visual-ui-v1", "visual-motor-v1"])
def test_real_ordinary_episode_uses_default_or_explicit_responses_profile_before_allocation(
        tmp_path, monkeypatch, profile):
    selected = selected_native(tmp_path)
    args = args_for(tmp_path, profile=profile)
    monkeypatch.setattr(run.pins, "load", lambda *a, **k: selected)
    def no_allocation(*a, **k):
        raise RuntimeError("TEST ONLY allocation boundary reached")
    monkeypatch.setattr(run, "reserve_output", no_allocation)
    if profile == "visual-motor-v1":
        with pytest.raises(ValueError, match="missing native/profile features"):
            asyncio.run(run.episode(args))
    else:
        with pytest.raises(RuntimeError, match="allocation boundary"):
            asyncio.run(run.episode(args))
        assert args.action_profile == "visual-ui-v1"
    assert not args.out.exists()


def test_external_motor_default_still_works_for_motor_only_task(tmp_path, monkeypatch):
    selected = selected_native(tmp_path)
    selected.update(external_declaration(tmp_path))
    args = args_for(tmp_path, scene="N1", terrain="flat", mode=None)
    args.harness, args.adapter = "external", "motor-only"
    monkeypatch.setattr(run.pins, "load", lambda *a, **k: selected)
    def no_allocation(*a, **k):
        raise RuntimeError("TEST ONLY allocation boundary reached")
    monkeypatch.setattr(run, "reserve_output", no_allocation)
    with pytest.raises(RuntimeError, match="allocation boundary"):
        asyncio.run(run.episode(args))
    assert args.observation_mode == "image" and args.action_profile == "visual-motor-v1"
    assert args.support_admission["implemented"] and not args.support_admission["scored_evaluation"]


def test_legacy_headless_and_explicit_motor_stay_narrow():
    definition = support.catalog()["B1"]
    cell = support.admit(definition, "flat", "headless", "structured", None, "capability")
    assert cell["action_profile"] == "raw-native-v2" and not cell["scored_evaluation"]
    assert support.resolve(support.catalog()["N1"], "flat", "renderer", "image",
                           "visual-motor-v1")["implemented"]
    assert not support.resolve(definition, "flat", "renderer", "image", "visual-motor-v1")["implemented"]
    for mode in ("image", "hybrid"):
        assert not support.resolve(definition, "flat", "headless", mode)["implemented"]
    for profile in ("raw-native-v2", "ordinary-renderer-native-v1"):
        assert not support.resolve(definition, "flat", "renderer", "image", profile)["implemented"]


def test_source_matrix_default_matches_all_ordinary_omitted_profiles_and_never_certifies_native():
    matrix = support.matrix()
    assert "source-only" in matrix["qualification_basis"]
    for definition in support.catalog().values():
        for terrain in definition["terrains"]:
            for mode in support.MODES:
                ordinary = support.resolve(definition, terrain, "renderer", mode)
                assert matrix["renderer"][f"{definition['id']}/{terrain}"][mode] == ordinary
                assert not ordinary["scored_evaluation"] and not ordinary["accepted"]
                assert ordinary["qualification_key"]["native_identity"] is None
                assert ordinary["historical_evidence"]["basis"].startswith("historical-only")


def test_arbitrary_id_promotes_only_by_native_bound_test_data_in_shared_admission(tmp_path, monkeypatch):
    selected = selected_native(tmp_path)
    path, name = example_manifest(tmp_path)
    selected["dependencies"]["scene_manifest"]["path"] = str(path)
    definition = suite.load(path)[name]
    args = args_for(tmp_path, scene=name, terrain="flat")
    args.mode, args.unqualified_engineering = "capability", False
    with pytest.raises(ValueError, match="unqualified"):
        renderer_support.admit_selection(args, selected)
    record = record_for(definition, selected)
    install_records(tmp_path, monkeypatch, [record])
    cell = renderer_support.admit_selection(args, selected)
    assert cell["scored_evaluation"] and cell["integration_qualification"] == "native-bound-engineering"
    assert cell["qualification_record"] == record
    assert not cell["accepted"] and cell["model_success"] == "not-inferred"
    assert support.matrix(suite.load(path), selected=selected)["renderer"][f"{name}/flat"]["image"] == cell
    assert support.matrix(selected=selected)["renderer"][f"{name}/flat"]["image"] == cell
    source_cell = support.resolve(definition, "flat", "renderer", "image")
    assert not source_cell["scored_evaluation"] and source_cell["qualification_record"] is None
    # This simple task does not consume the changed sibling. Its semantic key
    # remains qualified after a real catalog provenance change.
    catalog = json.loads(path.read_text())
    catalog["scenes"][0]["task"] += " TEST ONLY changed sibling content"
    path.write_text(json.dumps(catalog))
    selected["dependencies"]["scene_manifest"]["sha256"] = lifecycle.sha(path)
    assert renderer_support.admit_selection(args, selected)["qualification_key"] == cell["qualification_key"]
    assert not args.out.exists() and not args.recordings.exists()


@pytest.mark.parametrize("mismatch", [
    "id", "content", "template", "terrain", "undefined-terrain", "mode", "profile", "backend",
    "code", "dependency-key", *sorted(qualification.RENDERER_HASHES), "helper-executable", "helper-receipt",
    "interactions", "missing-selected-features", "missing-feature",
])
def test_task_native_mode_profile_and_source_mismatches_cannot_inherit(tmp_path, monkeypatch, mismatch):
    selected = selected_native(tmp_path)
    path, name = example_manifest(tmp_path)
    definition = suite.load(path)[name]
    record = record_for(definition, selected)
    install_records(tmp_path, monkeypatch, [record])
    terrain, backend, mode, profile = "flat", "renderer", "image", None
    identity = qualification.selected_native(selected)
    features = set(support.RENDERER_FEATURES)
    if mismatch == "id":
        definition["id"] = "not-the-qualified-task"
    elif mismatch == "content":
        definition["task"] += " changed"
    elif mismatch == "template":
        record["key"]["template"] = "different-template"
        install_records(tmp_path, monkeypatch, [record])
    elif mismatch == "terrain":
        definition["terrains"].append("natural")
        terrain = "natural"
    elif mismatch == "undefined-terrain":
        terrain = "natural"
    elif mismatch == "mode":
        mode = "hybrid"
    elif mismatch == "profile":
        profile = "visual-motor-v1"
    elif mismatch == "backend":
        backend, mode = "headless", "structured"
    elif mismatch == "code":
        record["key"]["harness_code_sha256"] = "f" * 64
        install_records(tmp_path, monkeypatch, [record])
    elif mismatch == "dependency-key":
        record["key"]["setup_dependencies"] = {
            "unused": {"task_id": "unrelated", "template": "single_creature",
                       "task_content_sha256": "f" * 64}}
        install_records(tmp_path, monkeypatch, [record])
    elif mismatch in qualification.RENDERER_HASHES:
        identity["renderer"][mismatch] = "f" * 64
        if mismatch == "source_tree_sha256":
            identity["fixture_helper"][mismatch] = "f" * 64
    elif mismatch.startswith("helper-"):
        identity["fixture_helper"]["executable_sha256" if mismatch == "helper-executable"
                                    else "build_receipt_sha256"] = "f" * 64
    elif mismatch == "interactions":
        identity["native_interactions"] = dict.fromkeys(qualification.INTERACTION_HASHES, "f" * 64)
    elif mismatch == "missing-selected-features":
        features = None
    elif mismatch == "missing-feature":
        features.remove("combat")
    cell = support.resolve(definition, terrain, backend, mode, profile,
                           native_identity=identity, native_features=features)
    assert not cell["scored_evaluation"] and not cell["accepted"]
    assert cell["model_success"] == "not-inferred"


@pytest.mark.parametrize("mutation", [
    "duplicate", "conflict", "empty-evidence", "missing-evidence", "bad-evidence-hash",
    "missing-reference", "missing-scope", "unknown-field", "missing-native",
    "bad-native", "wrong-helper-source", "bad-task-hash", "wrong-mode-profile",
    "bad-mode-type", "bad-status", "bad-modes", "empty-positive-modes", "accepted",
])
def test_malformed_ambiguous_conflicting_or_missing_evidence_fails_closed(tmp_path, monkeypatch, mutation):
    selected = selected_native(tmp_path)
    path, name = example_manifest(tmp_path)
    definition = suite.load(path)[name]
    record = record_for(definition, selected)
    records = [record]
    if mutation in {"duplicate", "conflict"}:
        records.append(copy.deepcopy(record))
        if mutation == "conflict":
            records[1]["status"], records[1]["allowed_run_modes"] = "partial", []
    elif mutation == "empty-evidence":
        record["evidence"] = []
    elif mutation == "missing-evidence":
        del record["evidence"]
    elif mutation == "bad-evidence-hash":
        record["evidence"][0]["sha256"] = "invalid"
    elif mutation == "missing-reference":
        del record["evidence"][0]["reference"]
    elif mutation == "missing-scope":
        record["scope"] = " "
    elif mutation == "unknown-field":
        record["key"]["extra"] = True
    elif mutation == "missing-native":
        record["key"]["native_identity"] = None
    elif mutation == "bad-native":
        record["key"]["native_identity"]["renderer"]["server_sha256"] = "bad"
    elif mutation == "wrong-helper-source":
        record["key"]["native_identity"]["fixture_helper"]["source_tree_sha256"] = "f" * 64
    elif mutation == "bad-task-hash":
        record["key"]["task_content_sha256"] = "bad"
    elif mutation == "wrong-mode-profile":
        record["key"]["action_profile"] = "raw-native-v2"
    elif mutation == "bad-mode-type":
        record["key"]["observation_mode"] = []
    elif mutation == "bad-status":
        record["status"] = "model-success"
    elif mutation == "bad-modes":
        record["allowed_run_modes"] = ["made-up"]
    elif mutation == "empty-positive-modes":
        record["allowed_run_modes"] = []
    elif mutation == "accepted":
        record["accepted"] = True
    install_records(tmp_path, monkeypatch, records)
    with pytest.raises(ValueError):
        support.resolve(definition, "flat", "renderer", "image")


def test_duplicate_json_fields_rejected(tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text('{"schema":"velorenbench-engineering-qualification/1","records":[],"records":[]}')
    with pytest.raises(ValueError, match="duplicate"):
        qualification.load(path)


def test_source_digest_excludes_evidence_and_catalog_data_not_implementation(monkeypatch):
    files = {"__init__.py": "1" * 64, "rpg_benchmark/run.py": "2" * 64,
             "rpg_benchmark/native_features.json": "3" * 64,
             "rpg_benchmark/scenes.json": "4" * 64,
             "rpg_benchmark/qualification.json": "5" * 64,
             "rpg_benchmark/current-qualification.json": "6" * 64}
    monkeypatch.setattr(qualification.runtime, "package_files", lambda: files)
    before = qualification.code_identity()
    files["rpg_benchmark/current-qualification.json"] = "7" * 64
    files["rpg_benchmark/qualification.json"] = "8" * 64
    assert qualification.code_identity() == before  # no qualification/source hash cycle
    files["rpg_benchmark/scenes.json"] = "9" * 64
    assert qualification.code_identity() == before
    files["rpg_benchmark/native_features.json"] = "a" * 64
    assert qualification.code_identity() != before
    files["rpg_benchmark/native_features.json"] = "3" * 64
    files["rpg_benchmark/run.py"] = "b" * 64
    assert qualification.code_identity() != before


@pytest.mark.parametrize("status", ["partial", "unqualified"])
def test_partial_records_do_not_promote_or_imply_model_success(tmp_path, monkeypatch, status):
    selected = selected_native(tmp_path)
    path, name = example_manifest(tmp_path)
    definition = suite.load(path)[name]
    record = record_for(definition, selected)
    record["status"], record["allowed_run_modes"] = status, []
    install_records(tmp_path, monkeypatch, [record])
    cell = support.resolve(definition, "flat", "renderer", "image",
        native_identity=qualification.selected_native(selected), native_features=support.RENDERER_FEATURES)
    assert cell["qualification_record"]["status"] == status and not cell["scored_evaluation"]
    assert not cell["accepted"] and cell["model_success"] == "not-inferred"
    with pytest.raises(ValueError, match="unqualified"):
        support.admit(definition, "flat", "renderer", "image", None, "capability",
            unqualified_engineering=True, native_identity=qualification.selected_native(selected),
            native_features=support.RENDERER_FEATURES)


def test_t05_template_cannot_be_loosened_even_by_positive_record_or_alias(tmp_path, monkeypatch):
    selected = selected_native(tmp_path)
    definition = support.catalog()["T05"]
    for selected_definition in (definition, {**definition, "id": "arbitrary-recovery-alias"}):
        args = args_for(tmp_path, scene=selected_definition["id"])
        if selected_definition is not definition:
            path = tmp_path / "alias.json"
            value = json.loads(Path(suite.__file__).with_name("scenes.json").read_text())
            value["scenes"].append(selected_definition)
            path.write_text(json.dumps(value))
            selected["dependencies"]["scene_manifest"]["path"] = str(path)
        record = record_for(selected_definition, selected, terrain="natural")
        install_records(tmp_path, monkeypatch, [record])
        args.unqualified_engineering = False
        cell = renderer_support.admit_selection(args, selected)
        assert cell["scored_evaluation"] and cell["allowed_run_modes"] == ["task-engineering"]
        assert not cell["accepted"] and cell["model_success"] == "not-inferred"
        for run_mode in support.RUN_MODES - {"task-engineering"}:
            args.mode = run_mode
            with pytest.raises(ValueError, match="unsupported"):
                renderer_support.admit_selection(args, selected)
        args.mode, args.action_profile = "task-engineering", "visual-motor-v1"
        with pytest.raises(ValueError, match="missing native/profile features"):
            renderer_support.admit_selection(args, selected)


def test_mechanism_restrictions_survive_positive_qualification(tmp_path, monkeypatch):
    selected = selected_native(tmp_path)
    definition = support.catalog()["T12"]
    record = record_for(definition, selected, terrain="natural", mode="structured")
    install_records(tmp_path, monkeypatch, [record])
    with pytest.raises(ValueError, match="unqualified"):
        support.admit(definition, "natural", "renderer", "structured", None, "mechanism",
            native_identity=qualification.selected_native(selected), native_features=support.RENDERER_FEATURES)


def test_variants_are_effective_configured_content_not_a_shared_task_id(tmp_path, monkeypatch):
    selected = selected_native(tmp_path)
    definition = support.catalog()["T06"]
    record = record_for(definition, selected, terrain="natural", mode="structured")
    assert definition["selected_variant"] == definition["default_variant"]
    assert record["key"]["task_content_sha256"] == task_templates.identity(definition)
    install_records(tmp_path, monkeypatch, [record])
    identity = qualification.selected_native(selected)
    assert support.resolve(definition, "natural", "renderer", "structured",
        native_identity=identity, native_features=support.RENDERER_FEATURES)["scored_evaluation"]
    suite.runtime_options(definition, variant="bow")
    assert not support.resolve(definition, "natural", "renderer", "structured",
        native_identity=identity, native_features=support.RENDERER_FEATURES)["scored_evaluation"]


def test_qualification_cli_and_source_support_flags(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["rpg-benchmark", "validate-qualification"])
    cli.main()
    value = json.loads(capsys.readouterr().out)
    assert value["records"] == len(qualification.load()["records"])
    assert not value["accepted"] and not value["gameplay_evidence_verified"]
    monkeypatch.setattr(sys, "argv", ["rpg-benchmark", "support", "--observation-mode", "image"])
    cli.main()
    value = json.loads(capsys.readouterr().out)
    assert all(v["image"]["action_profile"] == "visual-ui-v1" for v in value["renderer"].values())
    assert all(not v["image"]["scored_evaluation"] for v in value["renderer"].values())
    monkeypatch.setattr(sys, "argv", ["rpg-benchmark", "support", "--observation-mode", "image",
                                    "--action-profile", "visual-motor-v1"])
    cli.main()
    value = json.loads(capsys.readouterr().out)
    assert not value["renderer"]["C1/natural"]["image"]["implemented"]
    selected = selected_native(tmp_path)
    monkeypatch.setattr(cli.pins, "load", lambda *a, **k: selected)
    monkeypatch.setattr(sys, "argv", ["rpg-benchmark", "support", "--config", str(tmp_path / "runtime.json"),
                                    "--config-sha256", "0" * 64])
    cli.main()
    value = json.loads(capsys.readouterr().out)
    assert value["qualification_basis"] == "selected-native-and-code"
    assert value["renderer"]["C1/natural"]["image"]["qualification_key"]["native_identity"] == (
        qualification.selected_native(selected))


def test_readiness_tracks_exact_records_not_release_stage(tmp_path, monkeypatch):
    selected = selected_native(tmp_path)
    definitions = support.catalog()
    record = record_for(definitions["T06"], selected, terrain="natural", mode="structured")
    install_records(tmp_path, monkeypatch, [record])
    source = support.matrix()
    bound = support.matrix(selected=selected)
    assert source["successor"]["status"] == "source-only"
    assert source["successor"]["registry_records"] == 1
    assert source["successor"]["qualified_cells"] == 0
    assert bound["successor"]["status"] == "native-evidence-partial"
    assert bound["successor"]["qualified_cells"] == 1
    assert bound["successor"]["selected_cells"] == 37 * 3
    assert bound["qualification_evidence_basis"].startswith("exact selected-native/code")


def test_generated_keys_include_all_original_cells_modes_variants_and_profiles(tmp_path):
    inventory = qualification.selections(selected_native(tmp_path))
    assert inventory["task_count"] == 29 and inventory["terrain_cells"] == 37
    assert not inventory["evidence_generated"] and not inventory["accepted"]
    rows = inventory["selections"]
    assert len({qualification.digest(row["key"]) for row in rows}) == len(rows)
    assert {row["variant"] for row in rows if row["task_id"] == "T06"} == {
        "sword", "bow", "staff", "sceptre"}
    for variant in ("sword", "bow", "staff", "sceptre"):
        chosen = [row for row in rows if row["task_id"] == "T06" and row["variant"] == variant]
        assert {row["observation_mode"] for row in chosen} == set(support.MODES)
        assert {row["action_profile"] for row in chosen} == {
            "ordinary-renderer-native-v1", "visual-ui-v1", "visual-motor-v1"}
        assert len({row["key"]["task_content_sha256"] for row in chosen}) == 1
    assert len({row["key"]["task_content_sha256"] for row in rows if row["task_id"] == "T06"}) == 4
    image = next(row for row in rows if row["task_id"] == "T05" and
                 row["observation_mode"] == "image" and row["action_profile"] == "visual-ui-v1")
    assert image["implemented"] and image["allowed_run_modes"] == ["task-engineering"]
    assert all(row["key"]["native_identity"] is not None for row in rows)
    assert all(row["key"]["native_identity"] is None for row in qualification.selections()["selections"])
