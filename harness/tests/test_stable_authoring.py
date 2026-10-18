"""Shared runtime compatibility and scoped, one-submission fixture authoring."""
import asyncio
import copy
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from scripts.rpg_benchmark import layouts, setup, suite
from tests.authoring import Administrator
from tests.test_native_integration_repair import query


def test_every_selected_runtime_contains_unchanged_authored_layout_and_preserves_natural_small_world():
    definitions = suite.load(Path(suite.__file__).with_name("scenes.json"))
    cells = []
    for definition in definitions.values():
        for terrain in definition["terrains"]:
            selected = copy.deepcopy(definition)
            options = suite.runtime_options(selected, renderer=True, terrain=terrain)
            cells.append((definition["id"], terrain))
            legacy = suite.runtime_options(copy.deepcopy(definition), renderer=True)
            if terrain == "natural":
                assert options["small_world"] == legacy["small_world"]
            if options["small_world"]:
                assert terrain == "natural"  # These templates use real spawn/residents, not flat_origin.
                continue
            # Full-world native default is x_lg=y_lg=10, chunk edge32;
            # compact x_lg=y_lg=7 cannot contain these original flat layouts.
            points = list(definition["placements"].values())
            layout = definition.get("layout")
            if layout == "recovery":
                layout = "wall_side_detour"
            if layout in layouts.CASES:
                points += [[p[0],p[2],p[1]] for p in layouts.scene(layout)["solid_voxels_y_up"]]
            for point in points:
                assert 0 <= point[0] < 32 * 2**10 and 0 <= point[1] < 32 * 2**10
            if terrain == "flat":
                assert not options["small_world"]
    assert len(definitions) == 29 and len(cells) == 37
    with pytest.raises(ValueError, match="terrain"):
        suite.runtime_options(copy.deepcopy(next(iter(definitions.values()))), terrain="unknown")


def install(monkeypatch, state):
    monkeypatch.setattr(setup, "command", state.command)
    monkeypatch.setattr(setup, "ready_observation", state.ready)
    monkeypatch.setattr(setup, "wait_observation", state.wait)
    goto = AsyncMock(side_effect=AssertionError("no airborne or unconfirmed-support teleport"))
    monkeypatch.setattr(setup, "goto", goto)
    return goto


@pytest.mark.parametrize("enabled", [False, True])
def test_delayed_first_and_later_effects_keep_owned_anchor_one_submission_and_restore(monkeypatch, enabled):
    async def check():
        admin = Mock()
        state = Administrator(admin, enabled=enabled, delay=5)
        state.areas.add("preexisting")
        install(monkeypatch, state)
        # Includes anchor reuse, horizontal transition and vertical transition.
        points = [[18700,10800,500], [18701,10800,500], [18720,10800,500], [18720,10800,503]]
        submitted, polls = [], {}
        async def action(actor, kind, **args):
            assert actor is admin and kind == "build_block"
            assert state.hold is not None and state.enabled
            assert args["expected_kind"] == "Air" and args["new_kind"] == "Rock"
            assert args["position"] not in submitted
            state.advance(5)
            submitted.append(args["position"])
            return {"status": "completed", "action_id": f"one-{len(submitted)}",
                    "submitted_to_server": True, "server_mutation_confirmed": None}
        async def terrain(actor, top):
            point = [int(top[0]-.5), int(top[1]-.5), int(top[2]-1)]
            key = (tuple(point), point in submitted)
            count = polls.get(key,0)
            polls[key] = count+1
            if point not in submitted:
                status = "unloaded_cells" if count == 0 else "no_candidate_in_window"
                row = query(status, position=point, kind="Air")
            else:
                row = query("complete" if count else "no_candidate_in_window", position=point)
            return state.readback(row, point)
        monkeypatch.setattr(setup, "action", action)
        monkeypatch.setattr(setup, "terrain", terrain)
        result = await setup.author_voxels(admin, points)
        assert submitted == points and [r["voxel"] for r in result] == points
        assert all(r["receipt"]["server_mutation_confirmed"] is None for r in result)
        assert all(r["administrator_hold"]["not_grounded_evidence"] for r in result)
        assert len([c for c in state.calls if c[0]=="fixture_authoring" and c[1][1]=="move"]) == 3
        assert state.elapsed > 60  # Virtual delayed delivery, not an operation/run budget.
        assert state.hold is None and state.enabled == enabled and state.areas == {"preexisting"}
        assert (state.position,state.velocity) == (state.initial_position,state.initial_velocity)
        assert admin.journal.write.call_args.kwargs["cleanup_complete"] is True
    asyncio.run(check())


def test_unknown_mutation_is_not_retried_and_scope_is_restored(monkeypatch):
    async def check():
        admin = Mock()
        state = Administrator(admin, delay=10)
        install(monkeypatch, state)
        point = [18700,10800,500]
        async def terrain(*args):
            return state.readback(query("no_candidate_in_window", kind="Air"), point)
        monkeypatch.setattr(setup, "terrain", terrain)
        action = AsyncMock(side_effect=TimeoutError("unknown mutation outcome"))
        monkeypatch.setattr(setup, "action", action)
        with pytest.raises(TimeoutError, match="unknown mutation outcome"):
            await setup.author_voxels(admin, [point])
        assert action.await_count == 1
        assert state.hold is None and not state.areas and not state.enabled
        assert (state.position,state.velocity) == (state.initial_position,state.initial_velocity)
    asyncio.run(check())


def test_existing_owner_is_not_adopted_or_cleared(monkeypatch):
    async def check():
        admin = Mock()
        state = Administrator(admin)
        state.hold = {"owner":"not-owned", "position":list(state.position)}
        install(monkeypatch, state)
        with pytest.raises(RuntimeError, match="already has an authoring owner"):
            await setup.author_voxels(admin, [[18700,10800,500]])
        state.command.assert_not_awaited()
        assert state.hold["owner"] == "not-owned"
    asyncio.run(check())


def test_cleanup_failure_is_causal_not_a_false_release_receipt(monkeypatch):
    async def check():
        admin = Mock()
        state = Administrator(admin)
        install(monkeypatch, state)
        async def command(actor, name, *args):
            if name == "fixture_authoring" and args[1] == "end":
                raise RuntimeError("owned release transport failed")
            return await state.command(actor,name,*args)
        monkeypatch.setattr(setup, "command", command)
        with pytest.raises(BaseExceptionGroup) as failure:
            async with setup.fixture_authoring(admin, [0,0,0], [1,1,1]):
                raise ValueError("original body failure")
        assert [str(e) for e in failure.value.exceptions] == [
            "original body failure", "owned release transport failed"]
        row = admin.journal.write.call_args.kwargs
        assert not row["cleanup_complete"] and row["cleanup_errors"] == ["owned release transport failed"]
        assert state.hold is not None  # Honest failure evidence, not a fabricated release.
    asyncio.run(check())


@pytest.mark.parametrize("applied", [False, True])
def test_failed_acquisition_is_not_replayed_and_cleanup_retains_real_outcome(monkeypatch, applied):
    async def check():
        admin = Mock()
        state = Administrator(admin)
        install(monkeypatch,state)
        async def command(actor,name,*args):
            if name == "fixture_authoring" and args[1] == "begin":
                if applied:
                    await state.command(actor,name,*args)
                raise RuntimeError("original acquisition transport failure")
            return await state.command(actor,name,*args)
        transport = AsyncMock(side_effect=command)
        monkeypatch.setattr(setup,"command",transport)
        failure = RuntimeError if applied else BaseExceptionGroup
        with pytest.raises(failure):
            async with setup.fixture_authoring(admin,[0,0,0],[1,1,1]):
                pytest.fail("unknown acquisition must never enter authoring body")
        assert sum(c.args[1]=="fixture_authoring" and c.args[3]=="begin"
                   for c in transport.await_args_list) == 1
        assert state.hold is None and not state.areas and not state.enabled
        row = admin.journal.write.call_args.kwargs
        assert row["cleanup_complete"] is applied
    asyncio.run(check())


def test_owner_binding_change_fails_before_submission_without_clearing_other_state(monkeypatch):
    async def check():
        admin = Mock()
        state = Administrator(admin)
        install(monkeypatch,state)
        async def terrain(*args):
            row = state.readback(query("no_candidate_in_window",kind="Air"), [18700,10800,500])
            row["binding"]["readiness"]["fixture_authoring"]["owner"] = "foreign-observation"
            return row
        monkeypatch.setattr(setup,"terrain",terrain)
        action = AsyncMock()
        monkeypatch.setattr(setup,"action",action)
        with pytest.raises(RuntimeError,match="ownership changed"):
            await setup.author_voxels(admin,[[18700,10800,500]])
        action.assert_not_awaited()
        assert state.hold is None and not state.areas and not state.enabled
    asyncio.run(check())
