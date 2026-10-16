"""Regressions from retained real-native qualification failures; no model calls."""
import asyncio
import ast
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from scripts.rpg_benchmark import native, scene

FIXTURE = Path(__file__).with_name("fixtures") / "stability-repair.json"


def trace_fixture():
    from .historical import historical_fixture
    value = json.loads(historical_fixture("stability-repair.json").read_text())
    assert value["schema"] == "rpg-stability-regression-fixture/1"
    assert value["provenance"]["trace_sha256"] == "595f9f7d85e5559e8c598e835b2862b6b990d9ac77e976b5fc229b22d3e455ac"
    return value


def actor_snapshots():
    snapshots = trace_fixture()["observations"]
    assert len(snapshots) == 2
    return snapshots


def test_failed_boundary_start_preserves_primary_error_and_close_is_idempotent(tmp_path):
    async def check():
        boundary = native.EpisodeBoundary(SimpleNamespace(journal=Mock()), tmp_path / ("x" * 108))
        with pytest.raises(OSError, match="AF_UNIX path too long") as captured:
            try:
                await boundary.start()
            finally:
                boundary.begin_close()
                await boundary.close()
                await boundary.close()
        assert boundary.server is None
        assert boundary.socket_identity is None
        assert native.exception_detail(captured.value)["type"] == "OSError"
    asyncio.run(check())


def test_unstarted_boundary_does_not_unlink_an_unowned_path(tmp_path):
    async def check():
        path = tmp_path / "not-our-socket"
        path.write_text("preserve")
        boundary = native.EpisodeBoundary(SimpleNamespace(journal=Mock()), path)
        await boundary.close()
        await boundary.close()
        assert path.read_text() == "preserve"
        with pytest.raises(RuntimeError, match="closing"):
            await boundary.start()
    asyncio.run(check())


@pytest.mark.parametrize("chmod_failure", [False, True])
def test_started_or_partial_boundary_close_is_idempotent(chmod_failure, tmp_path):
    async def check():
        endpoint = native.SocketEndpoint(tmp_path)
        path = endpoint.path
        boundary = native.EpisodeBoundary(SimpleNamespace(journal=Mock()), path)
        try:
            if chmod_failure:
                with patch.object(Path, "chmod", side_effect=PermissionError("mock chmod denied")):
                    with pytest.raises(PermissionError, match="mock chmod denied"):
                        try:
                            await boundary.start()
                        finally:
                            try:
                                await boundary.close()
                            finally:
                                endpoint.close()
            else:
                await boundary.start()
                assert path.exists()
                boundary.begin_close()
                await boundary.close()
            await boundary.close()
            assert not path.exists()
        finally:
            await boundary.close()
    asyncio.run(check())


def test_boundary_does_not_remove_a_replacement_path(tmp_path):
    async def check():
        endpoint = native.SocketEndpoint(tmp_path)
        path = endpoint.path
        boundary = native.EpisodeBoundary(SimpleNamespace(journal=Mock()), path)
        try:
            await boundary.start()
            path.unlink()
            path.write_text("replacement, not owned")
            try:
                with pytest.raises(RuntimeError, match="identity changed"):
                    await boundary.close()
                assert path.read_text() == "replacement, not owned"
            finally:
                path.unlink()
                await boundary.close()
        finally:
            endpoint.close()
    asyncio.run(check())


class SetupReplay:
    """Replay the actual pre/post-protection actor state; native effect is mocked."""
    def __init__(self, *, failure=None, change_generation=False):
        self.before, self.after = actor_snapshots()
        self.failure, self.change_generation = failure, change_generation
        self.actor_calls, self.admin_calls = [], []
        self.used = False
        self.actor = SimpleNamespace(name="replay-actor", request=self.actor_request)
        self.admin = SimpleNamespace(request=self.admin_request, journal=Mock())

    async def actor_request(self, command, args=None, *, origin="setup"):
        assert origin == "setup"
        self.actor_calls.append((command, copy.deepcopy(args)))
        if command == "observe":
            observations = sum(command == "observe" for command, _ in self.actor_calls)
            if observations == 1:
                return copy.deepcopy(self.before)
            row = copy.deepcopy(self.after)
            if self.used:
                row["inventory"]["items"] = [
                    value for value in row["inventory"]["items"]
                    if scene.item_id(value["item"]) != scene.GROUP]
                row["known_recipes"]["items"].append({"recipe": scene.RECIPE, "satisfied": True})
            return row
        assert command == "step"
        if self.failure is not None:
            raise self.failure
        basis = self.after
        expected = {"generation": basis["continuous_control"]["generation"],
                    "expected_session_id": basis["session_id"], "actor": basis["control_actor"]}
        if self.change_generation or args.get("control_fence") != expected:
            raise native.NativeRefusal("step", {
                "code": "stale_control_owner",
                "message": "native protection retired this proposal's actuator generation"})
        self.used = True
        return {"status": "completed", "action_id": args["action"]["action_id"]}

    async def admin_request(self, command, args=None, *, origin="setup"):
        assert origin == "setup"
        self.admin_calls.append((command, copy.deepcopy(args)))
        if command == "observe":
            return copy.deepcopy(self.after)
        if command == "step":
            assert args["control_fence"]["generation"] == 1
            return {"status": "completed", "action_id": args["action"]["action_id"]}
        assert command == "events"
        return []


def test_actual_failed_snapshot_transition_is_damage_then_retirement_not_an_unknown_effect():
    before, after = actor_snapshots()
    state = after["continuous_control"]
    protection = state["native_protection"]
    assert (before["tick"], after["tick"]) == (9, 70)
    assert before["continuous_control"]["generation"] == 0
    assert state["generation"] == state["protection_generation"] == 1
    assert state["legacy_control_fence_required"] is True
    assert protection["retired_generation"] == 0 and protection["tick"] == 10
    assert protection["evidence"]["cause"] == "Attack"
    assert protection["evidence"]["attack_source"] == "Melee"
    assert protection["evidence"]["attacker_uid"] == 10
    assert protection["cancelled_action_id"] is None
    assert protection["status"] == "settled"
    assert after["survival"]["health"]["current"] < before["survival"]["health"]["current"]
    fixture = trace_fixture()
    request = fixture["request"]
    response = fixture["response"]
    assert request["cmd"] == "step" and request["args"]["action"]["type"] == "use_item"
    assert "control_fence" not in request["args"]
    assert response["id"] == request["id"] and response["ok"] is False
    assert response["error"]["code"] == "stale_control_owner"


def test_scene_constants_and_scoring_helpers_keep_the_trace_baseline_semantics():
    baseline = trace_fixture()["unchanged_scene_semantics"]
    module = ast.parse(Path(scene.__file__).read_text())
    helpers = {node.name: hashlib.sha256(ast.dump(node).encode()).hexdigest()
               for node in module.body
               if isinstance(node, ast.FunctionDef) and node.name in baseline["helper_ast_sha256"]}
    assert helpers == baseline["helper_ast_sha256"]
    constants = "\n".join(ast.dump(node) for node in module.body if isinstance(node, ast.Assign))
    assert hashlib.sha256(constants.encode()).hexdigest() == baseline["constant_ast_sha256"]


def test_recipe_setup_uses_exact_observed_fence_and_keeps_opening_semantics():
    replay = SetupReplay()
    async def check():
        with patch.object(scene.asyncio, "sleep"):
            return await scene.setup(replay.admin, replay.actor)
    fixture = asyncio.run(check())
    uses = [args for cmd, args in replay.actor_calls if cmd == "step"]
    assert len(uses) == 1
    assert uses[0]["control_fence"] == {
        "generation": 1, "expected_session_id": replay.after["session_id"],
        "actor": replay.after["control_actor"]}
    assert sum(cmd == "observe" for cmd, _ in replay.actor_calls) == 3
    assert fixture["opening"]["survival"] == replay.after["survival"]
    assert fixture["native_player_privilege"] == "not-admin"
    assert scene.known_recipe(fixture["opening"])
    assert scene.counts(fixture["opening"]).get(scene.GROUP, 0) == 0
    assert not any((args or {}).get("action", {}).get("name") in {"health", "buff", "kill_npcs"}
                   for cmd, args in replay.admin_calls)


@pytest.mark.parametrize("failure", [TimeoutError("unknown effect"), native.NativeRefusal(
    "step", {"code": "stale_control_owner", "message": "known no-effect refusal"})])
def test_setup_never_refreshes_or_retries_a_failed_mutation(failure):
    replay = SetupReplay(failure=failure)
    async def check():
        with patch.object(scene.asyncio, "sleep"):
            with pytest.raises(type(failure)) as captured:
                await scene.setup(replay.admin, replay.actor)
            assert captured.value is failure
    asyncio.run(check())
    assert sum(command == "step" for command, _ in replay.actor_calls) == 1
    assert sum(command == "observe" for command, _ in replay.actor_calls) == 2


def test_concurrent_new_protection_still_refuses_without_fence_refresh():
    replay = SetupReplay(change_generation=True)
    async def check():
        with patch.object(scene.asyncio, "sleep"):
            with pytest.raises(native.NativeRefusal):
                await scene.setup(replay.admin, replay.actor)
    asyncio.run(check())
    assert sum(command == "step" for command, _ in replay.actor_calls) == 1
