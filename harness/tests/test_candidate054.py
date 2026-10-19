"""054 admission remains historical; the later approved B3 loadout is explicit."""
import hashlib
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts.rpg_benchmark import renderer_support, suite, launching, qualification, support
from .test_admission_closeout import selected_native

def test_final054_qualification_and_support_agree_without_ability_or_freeze_claim(tmp_path):
    matrix = renderer_support.historical_policy()
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

    # Historic054 remains exact audit evidence, not qualification of a later
    # source/native selection. The selected dependency below is TEST ONLY.
    current_source = renderer_support.support_matrix()
    current_native = support.matrix(selected=selected_native(tmp_path))
    assert current_source["renderer"]["T12/natural"]["structured"]["qualification_key"]["native_identity"] is None
    assert current_native["renderer"]["T12/natural"]["structured"]["qualification_key"]["native_identity"] is not None
    for current in (current_source, current_native):
        for mode, allowed in final["T12_scored_admission"].items():
            cell = current["renderer"]["T12/natural"][mode]
            assert cell["historical_evidence"]["scored_evaluation"] is allowed
            assert cell["integration_qualification"] == "unqualified"
            assert cell["qualification_record"] is None
            assert not cell["scored_evaluation"] and not cell["accepted"]
            assert cell["model_success"] == "not-inferred"
            assert "capability" not in cell["allowed_run_modes"]

def test_approved_b3_catalog_and_setup_keep052_scoring_code_byte_preserved():
    package = Path(suite.__file__).parent
    # Researcher-approved B3 mainhand/stock upgrade, not a scorer relaxation.
    # test_generic_release also reconstructs and pins the pre-upgrade catalog.
    expected = {
        "scenes.json": "90496995c7ba1a79acf3f8d26ce9cd552e627ad2914fbea21996ba89efffcddf",
        "combat.py": "ec73ddb25980f2934f6238741c6c4af0f2a274523cb87d54afb15af7596d28ce",
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
        if mutation == "wrong_harness":
            # Task admission no longer knows Harness identities. The actual
            # legacy transport still cannot claim unimplemented renderer access.
            args.backend, args.action_profile = "renderer", None
            asyncio.run(launching.preflight_builtin(args, selected, None))
        else:
            renderer_support.admit_selection(args, selected)
    assert not list(tmp_path.iterdir())

def test_no_other_image_task_is_newly_admitted(tmp_path):
    matrix = renderer_support.historical_policy()["renderer"]
    admitted = {key for key, modes in matrix.items()
                if modes.get("image", {}).get("scored_evaluation")}
    assert admitted == {"T05/natural", "N1/flat"}
    assert matrix["T05/natural"]["image"]["allowed_run_modes"] == ["task-engineering"]

    records = qualification.load()["records"]
    for current in (renderer_support.support_matrix(),
                    support.matrix(selected=selected_native(tmp_path))):
        current_admitted = {key for key, modes in current["renderer"].items()
                            if modes.get("image", {}).get("scored_evaluation")}
        # Neither source-only nor explicitly synthetic native hashes inherit
        # real production evidence, even after a later release fills the registry.
        identities = {qualification.digest(cell["qualification_key"])
                      for modes in current["renderer"].values() for cell in modes.values()}
        assert not identities & {qualification.digest(record["key"]) for record in records}
        assert not current_admitted
        for modes in current["renderer"].values():
            for cell in modes.values():
                assert cell["qualification_record"] is None
                assert not cell["scored_evaluation"] and not cell["accepted"]
        image = current["renderer"]["T05/natural"]["image"]
        assert image["allowed_run_modes"] == ["task-engineering"]
