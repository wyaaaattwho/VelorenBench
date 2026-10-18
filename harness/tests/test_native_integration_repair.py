"""Shared safety regressions from A3's real placement/map/child failures."""
import asyncio
from copy import deepcopy
import errno
import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from scripts import rpg_sdk_agent as common
from scripts.rpg_benchmark import lifecycle, native, run, setup, visual_ui
from scripts.rpg_benchmark.renderer_tools import RendererTools
from tests.test_visual_ui import action as visual_action, native as renderer_native, Evidence
from tests.authoring import Administrator


POINT = [18700, 10800, 500]


def query(status="complete", *, kind="Rock", position=POINT, occupiable=True):
    return {"status": status, "vertical_scan": {"all_cells_loaded": status in {
        "complete", "no_candidate_in_window"}}, "support_candidates": [{
            "occupiable": occupiable,
            "support": {"position": position, "kind": kind, "solid": kind != "Air"}}]
        if status == "complete" else []}


def test_construction_waits_for_loaded_and_exact_effect_before_standing(monkeypatch):
    async def check():
        admin = Mock()
        state = Administrator(admin, delay=3)
        order = []
        reads = iter([query("unloaded_cells"), query("unknown_cells"),
                      query("no_candidate_in_window"), query("no_candidate_in_window"),
                      query("unloaded_cells"), query(), query()])

        async def terrain(*args):
            row = next(reads)
            order.append(("read", row["status"]))
            return state.readback(row, POINT)

        async def permission(*args):
            pass

        async def goto(*args, **kwargs):
            order.append(("goto", kwargs.get("settle", True)))

        async def build(*args, **kwargs):
            order.append(("build", 1))
            return {"status": "completed", "action_id": "one",
                    "detail": {"submitted_to_server": True, "server_mutation_confirmed": None}}

        monkeypatch.setattr(setup, "terrain", terrain)
        monkeypatch.setattr(setup, "command", state.command)
        monkeypatch.setattr(setup, "wait_observation", state.wait)
        monkeypatch.setattr(setup, "goto", goto)
        monkeypatch.setattr(setup, "action", build)
        pose = {k: None for k in ("position", "velocity", "on_ground", "tick", "session_id", "self_identity")}
        monkeypatch.setattr(setup, "ready_observation", state.ready)
        result = await setup.author_voxels(admin, [POINT])
        assert order == [
            ("read", "unloaded_cells"), ("read", "unknown_cells"),
            ("read", "no_candidate_in_window"), ("build", 1),
            ("read", "no_candidate_in_window"), ("read", "unloaded_cells"),
            ("read", "complete")]
        assert result[0]["receipt"]["detail"]["server_mutation_confirmed"] is None
        assert result[0]["support_before_standing"]["support_candidates"][0]["support"]["position"] == POINT
        assert state.hold is None and not state.enabled and not state.areas
        assert state.position == state.initial_position and state.velocity == state.initial_velocity
    asyncio.run(check())


@pytest.mark.parametrize("state", [
    query("unloaded_cells"), query("unknown_cells"), query("no_candidate_in_window"),
    query(kind="Wood"), query(position=[18700, 10800, 499]), query(occupiable=False),
    {"status": "invalid", "support_candidates": []},
])
def test_unconfirmed_wrong_or_unloaded_support_is_never_confirmation(monkeypatch, state):
    async def check():
        admin = Mock()
        monkeypatch.setattr(setup, "terrain", AsyncMock(return_value=state))
        clock = iter([0, 0, 16])
        monkeypatch.setattr(setup, "time", SimpleNamespace(monotonic=lambda: next(clock)))
        with pytest.raises(RuntimeError, match="exact native Rock readback") as error:
            await setup.construction_readback(admin, POINT, phase="effect",
                receipt={"detail": {"submitted_to_server": True, "server_mutation_confirmed": None}})
        assert error.value.construction["phase"] == "effect"
        assert error.value.construction["receipt"]["detail"]["server_mutation_confirmed"] is None
        admin.journal.write.assert_called_once()
    asyncio.run(check())


def test_query_refusal_retains_cause_and_prevents_standing_or_mutation_retry(monkeypatch):
    async def check():
        admin = Mock()
        state = Administrator(admin, delay=4)
        monkeypatch.setattr(setup, "command", state.command)
        monkeypatch.setattr(setup, "wait_observation", state.wait)
        monkeypatch.setattr(setup, "ready_observation", state.ready)
        goto, build = AsyncMock(), AsyncMock()
        monkeypatch.setattr(setup, "goto", goto)
        monkeypatch.setattr(setup, "action", build)
        refusal = native.NativeRefusal("inspect_terrain", {
            "code": "terrain_query_out_of_bounds", "message": "actor fell outside query window"})
        async def terrain(*args):
            if not build.await_count:
                return state.readback(query("no_candidate_in_window"), POINT)
            raise refusal
        monkeypatch.setattr(setup, "terrain", terrain)
        with pytest.raises(RuntimeError, match="terrain_query_out_of_bounds"):
            await setup.author_voxels(admin, [POINT])
        assert build.await_count == 1
        assert goto.await_count == 0
        assert state.hold is None and not state.enabled and not state.areas
    asyncio.run(check())


@pytest.mark.parametrize("operation", ["agent", "owned"])
@pytest.mark.parametrize("race", ["absent", "between", "reaped", "live", "mismatch", "metadata", "permission"])
def test_child_reaping_only_accepts_exact_absence_races(monkeypatch, operation, race):
    async def check():
        child = Mock(pid=999999991, returncode=0 if race == "reaped" else None)
        sent = []

        async def wait():
            child.returncode = 0
            return 0

        def send(sig):
            if race == "between":
                raise ProcessLookupError(errno.ESRCH, "child exited")
            sent.append(sig)
            child.returncode = 0

        child.wait = wait
        child.send_signal = send
        problem = {
            "absent": FileNotFoundError(errno.ENOENT, "gone", f"/proc/{child.pid}/stat"),
            "mismatch": RuntimeError("PID identity changed; refusing to signal"),
            "metadata": FileNotFoundError(errno.ENOENT, "missing", "/test/host-protection.json"),
            "permission": PermissionError(errno.EACCES, "denied", f"/proc/{child.pid}/stat"),
        }.get(race)
        owned = Mock(side_effect=problem)
        monkeypatch.setattr(lifecycle, "assert_owned", owned)
        record = {}
        stop = run.stop_agent if operation == "agent" else native.stop_owned
        if race in {"mismatch", "metadata", "permission"}:
            with pytest.raises(type(problem)):
                await stop(child, 123, record)
            assert not sent and "stopped" not in record
        else:
            await stop(child, 123, record)
            assert record["stopped"] is True and record["exit_code"] == 0
            assert sent == ([signal.SIGINT if operation == "agent" else signal.SIGTERM]
                            if race == "live" else [])
            if operation == "agent" and race != "reaped":
                assert record["stop_signal"]["sent"] is (race == "live")
    asyncio.run(check())


def test_real_subprocess_already_reaped_is_idempotent():
    async def check():
        import sys
        child = await asyncio.create_subprocess_exec(sys.executable, "-I", "-c", "pass")
        start = lifecycle.process_start_ticks(child.pid)
        await child.wait()
        for _ in range(2):
            record = {}
            await run.stop_agent(child, start, record)
            assert record["stopped"] and record["exit_code"] == 0
    asyncio.run(check())


def test_protected_live_child_and_escalation_identity_change_are_refused(monkeypatch):
    async def check():
        child = Mock(pid=999999991, returncode=None)
        child.wait = AsyncMock()
        monkeypatch.setattr(lifecycle, "guards", lambda: ({"pid": child.pid},))
        with pytest.raises(RuntimeError, match="protected"):
            await native.signal_owned(child, 123, signal.SIGTERM)
        child.send_signal.assert_not_called()

        ownership = Mock(side_effect=[None, RuntimeError("PID identity changed")])
        monkeypatch.setattr(lifecycle, "assert_owned", ownership)

        async def timed_out(awaitable, timeout):
            awaitable.close()
            raise TimeoutError

        monkeypatch.setattr(native.asyncio, "wait_for", timed_out)
        with pytest.raises(RuntimeError, match="identity changed"):
            await native.stop_owned(child, 123, {})
        child.send_signal.assert_called_once_with(signal.SIGTERM)
    asyncio.run(check())


@pytest.mark.parametrize("gesture", ["left_click", "right_click", "middle_click"])
def test_public_pointer_schema_and_exact_private_map_resolution(gesture):
    selected = visual_action()
    selected["gesture"] = gesture
    common.validate_request("step", {"action": selected}, renderer=True)
    selected["to_x"] = 1
    with pytest.raises(ValueError):
        common.validate_request("step", {"action": selected}, renderer=True)
    selected.pop("to_x")
    original = {"id": "r", "cmd": "step", "args": {"action": selected}}
    canonical = {"type": "map_marker", "operation": "update", "world_xy": [1, 2], "action_id": "a"}
    response = {"id": "r", "ok": True, "result": {
        "action_id": "a", "action_type": "map_marker",
        "visual_resolution": {"source": "native-hud-hit-test-v1",
                              "original_args": deepcopy(original["args"]), "action": canonical}}}
    receipt = visual_ui.private_receipt(original, response, 1, 0)
    assert receipt["public_request"] == original
    assert "world_xy" not in original["args"]["action"]
    response["result"]["visual_resolution"]["original_args"]["action"]["x"] += 1
    with pytest.raises(ValueError):
        visual_ui.private_receipt(original, response, 1, 0)


def test_middle_button_requires_actual_native_capability(tmp_path):
    peer = renderer_native()
    peer.ready["renderer"]["benchmark_features"] = ["map"]

    def gestures():
        tools = RendererTools(peer, Evidence(), "image", tmp_path, "test", visual_ui.PROFILE)
        action = next(x for x in tools.schemas["step"]["properties"]["action"]["oneOf"]
                      if x["properties"]["type"]["enum"] == ["visual_input"])
        return tools, action["properties"]["gesture"]["enum"]

    assert "middle_click" not in gestures()[1]  # Old binary must not advertise it.
    peer.ready["renderer"]["visual_ui"]["gestures"] = ["hover", "left_click", "right_click", "middle_click"]
    tools, offered = gestures()
    assert offered == ["hover", "left_click", "right_click", "middle_click"]
    selected = visual_action()
    selected["gesture"] = "middle_click"
    tools.validate_request("step", {"action": selected})
    selected["frame_id"] = ""
    with pytest.raises(ValueError):
        tools.validate_request("step", {"action": selected})
