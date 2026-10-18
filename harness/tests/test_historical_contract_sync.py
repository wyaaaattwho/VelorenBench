"""Meaningful negative checks for the narrowly evolved historical contracts."""
import ast
import copy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts.rpg_benchmark import suite, task_templates
from .historical import historical_fixture
from .historical_contracts import check_module


def test_loaded_legacy_mechanics_preserve_every_original_task_and_global_field():
    path = Path(__file__).with_name("fixtures") / "legacy-scenes.json"
    raw = json.loads(path.read_text())
    configured = suite.load(path)
    assert len(configured) == len(raw["scenes"]) == 17
    assert sum(len(row["terrains"]) for row in raw["scenes"]) == 25
    for original in raw["scenes"]:
        selected = configured[original["id"]]
        mechanics = task_templates.mechanics(suite.MODULES[original["kind"]], selected)
        assert {name: mechanics[name] for name in original} == original
        assert mechanics["placements"] == raw["placements"]
        assert mechanics["player_equipment"] == raw["player_equipment"]
        dependencies = suite.setup_dependencies(selected, configured)
        if original["kind"] == "mixed":
            assert {role: row["id"] for role, row in dependencies.items()} == {
                "craft": "C2", "encounter": "B1"}
            for role, task in (("craft", "C2"), ("encounter", "B1")):
                assert dependencies[role] == configured[task]
        else:
            assert dependencies == {}


@pytest.mark.parametrize("module,name,before,after", [
    ("oracles.py", "pickup_actions", "detail.get('uid') == uid", "True"),
    ("oracles.py", "pickup_actions", "feedback.get('candidate_request_count') == 1", "True"),
    ("mixed.py", "setup", "dependencies['craft']", "dependencies['unrelated']"),
    ("setup.py", "clean_inventory", "await ready_observation(actor)", "await actor.request('observe')"),
    ("setup.py", "author_voxels",
     "loaded = await construction_readback(admin, point, phase='loaded-input', owner=owner)", "loaded = {}"),
    ("setup.py", "author_voxels",
     "support = await construction_readback(admin, point, phase='effect', receipt=receipt, owner=owner)", "support = {}"),
    ("setup.py", "author_voxels", "fixture_authoring(admin, low, high)", "fixture_authoring(actor, low, high)"),
    ("suite.py", "score", "event_window(fixture['opening'], final, events)", "events"),
    ("run.py", "agent_command", "str(Path(python).parent / 'rpg-episode-bridge')", "'unowned-bridge'"),
    ("run.py", "stop_agent", "await signal_owned(process, start, signal.SIGINT)", "await signal_owned(process, start + 1, signal.SIGINT)"),
    ("native.py", "stop_owned", "await signal_owned(process, start, signal.SIGTERM)", "await signal_owned(process, start + 1, signal.SIGTERM)"),
    ("bridge_client.py", "main", "writer.close()", "writer.write_eof()"),
])
def test_contract_translations_reject_changes_to_actual_attribution_authoring_or_cleanup(
        module, name, before, after):
    baseline = json.loads(historical_fixture("accepted-contracts.json").read_text())
    package = Path(suite.__file__).parent
    tree = ast.parse((package / module).read_text())
    check_module(baseline, package, module, [name], tree=tree)
    left = ast.parse(before).body[0]
    replacement = ast.parse(after).body[0]
    if isinstance(left, ast.Expr):
        left = left.value
        assert isinstance(replacement, ast.Expr)
        replacement = replacement.value
    target = ast.dump(left)
    count = 0

    class Change(ast.NodeTransformer):
        def visit(self, node):
            nonlocal count
            if ast.dump(node) == target:
                count += 1
                return copy.deepcopy(replacement)
            return super().visit(node)

    tree = Change().visit(tree)
    assert count == 1
    with pytest.raises(AssertionError):
        check_module(baseline, package, module, [name], tree=tree)


def test_suite_score_keeps_headless_events_and_filters_renderer_original_window(monkeypatch):
    """Exercise the public scorer seam, not just the presence of a call name."""
    scorer = Mock(return_value={"success": False})
    module = Mock(score=scorer)
    monkeypatch.setitem(suite.MODULES, "test-only", module)
    definition = {"kind": "test-only"}
    receipts, samples = [], []
    opening = {"session_id": "same", "tick": 10}
    final = {"session_id": "same", "tick": 12}
    events = [{"tick": 9}, {"tick": 11}, {"tick": 13}]
    assert suite.score(definition, {"opening": opening}, final, receipts, events, samples) == {"success": False}
    assert scorer.call_args.args[3] is events  # Historical headless predicates own the inclusive tick gate.
    domain = {"kind": "client-update-counter", "session_id": "same"}
    opening.update(sequence_domain=domain, event_cursor=4)
    final.update(sequence_domain=domain, event_cursor=6)
    events = [{"session_id": "same", "sequence_domain": domain, "tick": tick, "seq": sequence}
              for tick, sequence in ((9, 4), (11, 5), (12, 6), (13, 7))]
    suite.score(definition, {"opening": opening}, final, receipts, events, samples)
    assert scorer.call_args.args == ({"opening": opening}, final, receipts, events[1:3], samples)
    invalid = copy.deepcopy(events)
    invalid[1]["sequence_domain"]["session_id"] = "foreign"
    with pytest.raises(ValueError, match="sequence domain"):
        suite.score(definition, {"opening": opening}, final, receipts, invalid, samples)
