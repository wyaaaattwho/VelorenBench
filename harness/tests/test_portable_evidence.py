"""Portable closure verification uses synthetic bytes, never synthetic qualification."""
import hashlib
import json
import shutil

import pytest

from .test_public_source import tool_module


@pytest.fixture
def sidecar(tmp_path):
    tool = tool_module("verify_evidence")
    root = tmp_path / "evidence"
    raw = b"synthetic closure-test object, not native gameplay evidence\n"
    digest = hashlib.sha256(raw).hexdigest()
    ref = f"evidence/sha256/{digest[:2]}/{digest}"
    path = root / ref
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    records = {"schema": "velorenbench-engineering-qualification/1", "records": [{
        "key": {"harness_code_sha256": "0" * 64}, "status": "engineering-qualified",
        "evidence": [{"reference": ref, "sha256": digest, "scope": "TEST ONLY closure bytes"}]}]}
    qualification = root / "qualification.json"
    qualification.write_text(json.dumps(records))
    manifest = {
        "qualification": {"records": 1, "sha256": tool.sha(qualification)},
        "harness_code_sha256": "0" * 64, "reference_count": 1,
        "evidence_object_count": 1, "evidence_bytes": len(raw),
        "objects": [{"reference": ref, "sha256": digest, "bytes": len(raw)}],
        "notices": {}, "other_files": {}}
    (root / "evidence-manifest.json").write_text(json.dumps(manifest))
    return tool, root, ref


def test_standalone_verifier_is_relocation_invariant_and_checks_actual_registry(sidecar, tmp_path):
    tool, root, ref = sidecar
    original = tool.audit(root, registry=root / "qualification.json")
    moved = tmp_path / "a/different/location"
    shutil.copytree(root, moved)
    assert tool.audit(moved, registry=moved / "qualification.json") == original
    original_pin = tool.sha(root / "evidence-manifest.json")
    assert tool.audit(moved, manifest_sha256=original_pin)["tree_sha256"] == original["tree_sha256"]


@pytest.mark.parametrize("mutation", [
    "bytes", "missing", "extra", "symlink", "traversal", "absolute", "registry",
    "manifest_pin", "count", "citation", "code", "status",
])
def test_portable_verifier_fails_closed_on_missing_changed_or_escaped_proof(sidecar, tmp_path, mutation):
    tool, root, ref = sidecar
    args = {}
    manifest_path = root / "evidence-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    qualification = root / "qualification.json"
    records = json.loads(qualification.read_text())
    if mutation == "bytes":
        (root / ref).write_bytes(b"altered")
    elif mutation == "missing":
        (root / ref).unlink()
    elif mutation == "extra":
        (root / "unstaged").write_bytes(b"extra")
    elif mutation == "symlink":
        outside = tmp_path / "outside"
        outside.write_bytes((root / ref).read_bytes())
        (root / ref).unlink()
        (root / ref).symlink_to(outside)
    elif mutation in ("traversal", "absolute"):
        records["records"][0]["evidence"][0]["reference"] = "../outside" if mutation == "traversal" else "/outside"
    elif mutation == "registry":
        wrong = tmp_path / "other-registry.json"
        wrong.write_text("{}")
        args["registry"] = wrong
    elif mutation == "manifest_pin":
        args["manifest_sha256"] = "1" * 64
    elif mutation == "count":
        manifest["evidence_object_count"] = 2
    elif mutation == "citation":
        records["records"][0]["evidence"][0]["sha256"] = "1" * 64
    elif mutation == "code":
        records["records"][0]["key"]["harness_code_sha256"] = "1" * 64
    elif mutation == "status":
        records["records"][0]["status"] = "partial"
    qualification.write_text(json.dumps(records))
    manifest["qualification"]["sha256"] = tool.sha(qualification)
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        tool.audit(root, **args)
