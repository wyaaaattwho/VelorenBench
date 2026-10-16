"""054 authorizes ONLY the stated admission change, not easier task predicates."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts.rpg_benchmark import renderer_support, suite

def test_final054_qualification_and_support_agree_without_ability_or_freeze_claim():
    matrix = renderer_support.support_matrix()
    q = matrix["qualification_evidence"]
    assert q["version"] == "1.0.0"
    final = q["final054"]
    assert final["public_candidate_authorized"]
    assert final["T12_scored_admission"] == {"structured": True, "hybrid": True, "image": False}
    assert not final["private052_driver_qualified"]
    assert not final["original012_result_rewritten"]
    assert not any(final[k] for k in ("capability_claim", "selected", "frozen"))
    for mode, allowed in final["T12_scored_admission"].items():
        assert matrix["renderer"]["T12/natural"][mode]["scored_evaluation"] is allowed
    assert final["chronological_transition_index_zero_based"] == [843, 844]

def test_all29_raw_definitions_and052_scoring_code_are_byte_preserved():
    package = Path(suite.__file__).parent
    expected = {
        "scenes.json": "a3ebef6fe7120633c23da32cc7610bf65a95b05a079216a4202fde415d24c8be",
        "contract_evidence.py": "0afe32bb7e966fbce79c4772d8b1e06bd77730d090fe1ead0759cd049e7d4c91",
        "npc_contracts.py": "a991347b287e57fbd4a1ef396cd268a32ec6dba111c547fd9e1708b155654bde"}
    assert all(hashlib.sha256((package / name).read_bytes()).hexdigest() == value
               for name, value in expected.items())
    assert len(suite.load(package / "scenes.json")) == 29

@pytest.mark.parametrize("mutation", ["image", "flat", "mechanism", "wrong_harness", "missing_helper"])
def test_new_T12_admission_does_not_open_unsupported_or_unverified_entries(tmp_path, mutation):
    args = SimpleNamespace(scene="T12", terrain="natural", observation_mode="structured",
        mode="capability", harness="responses", recordings=tmp_path / "outputs/recordings/test",
        out=tmp_path / "private/test")
    selected = {"renderer": {}, "renderer_fixture_helper": {}, "protected_paths": []}
    if mutation == "image":
        args.observation_mode = "image"
    elif mutation == "flat":
        args.terrain = "flat"
    elif mutation == "mechanism":
        args.mode = "mechanism"
    elif mutation == "wrong_harness":
        args.harness = "gameagent"
    else:
        selected["renderer_fixture_helper"] = None
    with pytest.raises(ValueError):
        renderer_support.admit_selection(args, selected)
    assert not list(tmp_path.iterdir())

def test_no_other_image_task_is_newly_admitted():
    matrix = renderer_support.support_matrix()["renderer"]
    admitted = {key for key, modes in matrix.items()
                if modes.get("image", {}).get("scored_evaluation")}
    assert admitted == {"T05/natural", "N1/flat"}
    assert matrix["T05/natural"]["image"]["allowed_run_modes"] == ["task-engineering"]
