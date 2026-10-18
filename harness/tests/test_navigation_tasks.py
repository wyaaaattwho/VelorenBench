import copy
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts.rpg_benchmark import navigation_tasks as tasks, scene, suite
from scripts.rpg_benchmark.renderer_support import admit_selection, support_matrix
from tests.test_crafting_recovery import observed, action_receipt


def sample(tick, seconds, goal, items=None):
    value = observed(items or {}, tick=tick)
    value.update(position=list(goal), velocity=[0, 0, 0], on_ground=True, _elapsed_s=seconds)
    return value


def pickup():
    return action_receipt("pickup", {
        "uid": 8, "operational_success": True, "pickup_feedback": {
            "server_event": "Collected", "operational_collection_justified": True,
            "candidate_request_count": 1, "collected_item": {"id": tasks.SUPPLY, "amount": 1}}},
        action_id="pickup", uid=8)


def route_fixture():
    layout = tasks.route_layout()
    opening = sample(1, 0, layout["start"])
    return {"case": "T02", "opening": opening, "goal": layout["goal"], "layout": layout,
            "supplies": [{"uid": 8, "item": tasks.SUPPLY, "amount": 1}],
            "event_state": {"status": "applied", "applications": 1,
                            "trigger_action_ids": ["pickup"], "applied_monotonic": 10,
                            "native_readback": {"verified_voxels": layout["event_voxels"]},
                            "submissions": [{"action_id": str(i), "action_type": "build_block",
                                             "status": "completed", "detail": {"position": p}}
                                            for i, p in enumerate(layout["event_voxels"])],
                            "alternative_supports_after": [{} for p in layout["alternative"]],
                            "time_visible": None, "exposure": "not-demonstrated", "public_witnesses": []}}


def test_route_initial_geometry_uses_complete_common_author_before_event_permission(monkeypatch):
    layout = tasks.route_layout()
    admin, actor = object(), object()
    author = AsyncMock(return_value=[{"voxel":p} for p in layout["voxels"]])
    monkeypatch.setattr(tasks.native, "author_voxels", author)
    batch = AsyncMock(side_effect=AssertionError("initial geometry must not depend on old author pose"))
    monkeypatch.setattr(tasks, "build_batch", batch)
    permission = AsyncMock()
    monkeypatch.setattr(tasks, "build_permission", permission)
    monkeypatch.setattr(tasks, "verify_blocks", AsyncMock(return_value={"verified_voxels":layout["voxels"]}))
    for name in ("isolate_initial_spawn", "goto", "clean_inventory", "drop_supply"):
        monkeypatch.setattr(tasks.native, name, AsyncMock(return_value={}))
    monkeypatch.setattr(tasks.native, "action", AsyncMock(return_value={}))
    observations = [sample(i,0,p) for i,p in enumerate(layout["alternative"])]
    observations.append(sample(20,0,layout["start"]))
    monkeypatch.setattr(tasks.native, "ready_observation", AsyncMock(side_effect=observations))
    fixture = asyncio.run(tasks.setup_route(admin,actor,{"task":"unchanged authored route"}))
    author.assert_awaited_once_with(admin,layout["voxels"])
    permission.assert_awaited_once_with(admin,layout["voxels"]+layout["event_voxels"])
    batch.assert_not_awaited()
    assert len(fixture["construction"]) == len(layout["voxels"])
    assert fixture["layout"] == layout and fixture["event_state"]["status"] == "armed"


def test_exact_twenty_inputs_preserved():
    baseline = json.loads(Path(__file__).with_name("fixtures").joinpath("parent20-scenes.json").read_text())
    candidate = json.loads(Path(suite.__file__).with_name("scenes.json").read_text())
    old = {row["id"]: row for row in baseline["scenes"]}
    new = {row["id"]: row for row in candidate["scenes"]}
    assert len(old) == 20 and len(new) == 29
    assert new.keys() - old.keys() == tasks.TASK_IDS | {"T06", "T07", "T08", "T09", "T10", "T11", "T12"}
    assert all(new[name] == value for name, value in old.items())
    assert all(candidate[key] == value for key, value in baseline.items() if key not in {"scenes", "version"})


def test_setup_waits_for_native_end_not_local_prompt_clear(monkeypatch):
    actor = SimpleNamespace(native_events=[])
    marker = {"dialogue_id": 10, "marker": {
        "label": {"Localized": {"key": "hud-map-workshop"}},
        "wpos": dict(zip(("x", "y"), tasks.FACILITY["root_xy"]))}}
    current = {"dialogue_markers": [], "survival": {"buff_clock": {"now": 1}}}

    def question(tag, key):
        return {"sender_uid": 7, "dialogue_id": 10, "tag": tag, "kind": "Question",
                "responses": [{"response_id": tag + 1, "content": {"Localized": {"key": key}}}]}

    async def action(_actor, kind, **arguments):
        if kind == "context_interact":
            current["dialogue"] = question(0, "dialogue-question-directions")
        elif arguments["operation"] == "ack":
            current["dialogue"] = None
        elif arguments["response_id"] == 1:
            current["dialogue"] = question(1, "dialogue-direction-workshop")
        else:
            current["dialogue"] = {"sender_uid": 7, "dialogue_id": 10, "tag": 2, "kind": "Statement"}
            current["dialogue_markers"] = [marker]
        return {}

    async def wait(_actor, predicate, reason):
        if reason == "native statement acknowledgement guard interval":
            assert not predicate(current)
            current["survival"]["buff_clock"]["now"] += 1.5
        if reason == "actual native resident dialogue End":
            assert not predicate(current)
            actor.native_events.append({"event": "dialogue", "data": {"kind": "End", "dialogue_id": 99}})
            assert not predicate(current)
            actor.native_events.append({"event": "dialogue", "data": {"kind": "End", "dialogue_id": 10}})
        assert predicate(current)
        return copy.deepcopy(current)

    monkeypatch.setattr(tasks.native, "action", action)
    monkeypatch.setattr(tasks.native, "wait_observation", wait)
    result = asyncio.run(tasks.ask_workshop(actor, {"control_target": {"uid": 7}}))
    assert result["marker"] in result["follow_on"]["dialogue_markers"]


def test_arrival_exact_public_thresholds_and_fresh_alive_grounded_pair():
    fix = route_fixture()
    pair = [sample(2, .1, fix["goal"]), sample(3, .35, fix["goal"])]
    # Decimal subtraction is not assumed to make 0.35 - 0.1 exactly 0.25.
    pair[1]["_elapsed_s"] = .350000001
    assert tasks.settled_pair(fix["opening"], fix["goal"], pair)
    for field, value in (("on_ground", False), ("velocity", [.100001, 0, 0]),
                         ("tick", 2), ("_elapsed_s", .34999999),
                         ("session_id", "other"), ("position", [*fix["goal"][:2], fix["goal"][2] + .50001])):
        bad = copy.deepcopy(pair)
        bad[-1][field] = value
        assert not tasks.settled_pair(fix["opening"], fix["goal"], bad)
    bad = copy.deepcopy(pair)
    bad[-1]["position"][0] += 1.00001
    assert not tasks.settled_pair(fix["opening"], fix["goal"], bad)


def test_t02_native_pickup_retention_and_event_are_independent():
    fix = route_fixture()
    pair = [sample(2, 1, fix["goal"], {tasks.SUPPLY: 1}),
            sample(3, 2, fix["goal"], {tasks.SUPPLY: 1})]
    result = tasks.score(fix, pair[-1], [pickup()], [], pair)
    assert result["success"] and result["route_event"]["exposure"] == "not-demonstrated"
    assert not tasks.score(fix, pair[-1], [], [], pair)["success"]
    for status in ("armed", "applying", "failed", "unknown"):
        bad = copy.deepcopy(fix)
        bad["event_state"]["status"] = status
        assert not tasks.score(bad, pair[-1], [pickup()], [], pair)["success"]
    for missing in ("native_readback", "submissions", "alternative_supports_after"):
        bad = copy.deepcopy(fix)
        bad["event_state"].pop(missing)
        assert not tasks.score(bad, pair[-1], [pickup()], [], pair)["success"]
    bad = copy.deepcopy(pair[-1])
    bad["inventory"]["items"] = []
    assert not tasks.score(fix, bad, [pickup()], [], pair)["success"]
    for mutation in ("actor", "session", "unknown"):
        row = pickup()
        if mutation == "actor":
            row["actor"] = 43
        elif mutation == "session":
            row["response"]["session_id"] = "other"
        else:
            row["response"]["result"]["status"] = "unknown"
        assert not tasks.score(fix, pair[-1], [row], [], pair)["success"]


def test_public_visibility_requires_actual_changed_collision_voxel_not_trigger():
    fix = route_fixture()
    request = {"cmd": "observe", "id": "observed"}
    response = {"ok": True, "session_id": "s", "result": {"nearby_blocks": []}}
    tasks.public_witness(fix, request, response)
    assert fix["event_state"]["time_visible"] is None
    response["result"]["nearby_blocks"] = [{
        "position": fix["layout"]["event_voxels"][0], "kind": "Rock", "solid": True}]
    tasks.public_witness(fix, request, response)
    assert fix["event_state"]["time_visible"] is not None
    assert fix["event_state"]["exposure"] == "public-changed-voxel-observed"
    assert "replanning" in fix["event_state"]["public_witnesses"][0]["interpretation"]


def test_environment_unknown_and_not_exposed_are_not_clean_ability_negatives():
    for status, visible in (("applying", 12), ("applied", None)):
        record = {"scene": "T02", "world_event": {"status": status, "time_visible": visible},
                  "score": {"success": False, "parts": {"runtime_event_qualified": True}},
                  "evaluation_validity": {"status": "valid", "reasons": [], "accepted": True}}
        tasks.finalize_environment(record)
        assert record["evaluation_validity"]["status"] == "unknown"
        assert not record["evaluation_validity"]["accepted"] and not record["capability_claim"]
    record["score"]["success"] = True
    record["evaluation_validity"] = {"status": "valid", "reasons": [], "accepted": True}
    tasks.finalize_environment(record)
    assert record["evaluation_validity"]["accepted"]


def test_route_event_triggers_once_only_on_actual_same_actor_known_pickup(monkeypatch):
    fix = route_fixture()
    fix["event_state"].update(status="armed", applications=0)
    admin = SimpleNamespace(journal=SimpleNamespace(write=lambda **row: None))
    actor = SimpleNamespace(measured_receipts=[], request=AsyncMock(return_value=fix["opening"]))
    batch = AsyncMock(return_value=[{"submitted_to_server": True}])
    verify = AsyncMock(return_value={"verified_voxels": fix["layout"]["event_voxels"]})
    monkeypatch.setattr(tasks, "build_batch", batch)
    monkeypatch.setattr(tasks, "verify_blocks", verify)
    monkeypatch.setattr(tasks.native, "goto", AsyncMock())
    monkeypatch.setattr(tasks.native, "qualify", AsyncMock(return_value={"qualified": True}))
    asyncio.run(tasks.update_event(admin, actor, fix, fix["opening"]))
    batch.assert_not_called()
    bad = pickup()
    bad["actor"] = 43
    actor.measured_receipts.append(bad)
    asyncio.run(tasks.update_event(admin, actor, fix, fix["opening"]))
    batch.assert_not_called()
    actor.measured_receipts.append(pickup())
    asyncio.run(tasks.update_event(admin, actor, fix, fix["opening"]))
    asyncio.run(tasks.update_event(admin, actor, fix, fix["opening"]))
    assert batch.await_count == 1 and verify.await_count == 1
    assert fix["event_state"]["status"] == "applied" and fix["event_state"]["applications"] == 1


def test_route_geometry_really_has_height_occlusion_and_disjoint_alternative():
    layout = tasks.route_layout()
    solids = {tuple(p) for p in layout["voxels"]}
    event = {tuple(p) for p in layout["event_voxels"]}
    assert not solids & event
    assert layout["goal"][2] - layout["start"][2] == 2
    assert max(p[2] for p in event) - min(p[2] for p in event) == 3
    for point in layout["alternative"]:
        assert (int(point[0]), int(point[1]), int(point[2]) - 1) in solids
        assert (int(point[0]), int(point[1]), int(point[2])) not in event


def test_t01_joins_actual_offer_marker_native_recipient_and_exact_facility(tmp_path):
    opening = sample(1, 0, [0, 0, 0])
    target = {"uid": 9, "lifecycle": "resident"}
    marker = {"wpos": {"x": tasks.FACILITY["root_xy"][0], "y": tasks.FACILITY["root_xy"][1]},
              "label": {"Localized": {"key": "hud-map-workshop"}}, "id": None, "site": None}
    received = {"marker": marker, "source": "validated_server_dialogue", "sender": target,
                "sender_uid": 9, "dialogue_id": 70}
    path = tmp_path / "private.ndjson"
    path.write_text("\n".join(json.dumps({"event": "validated_dialogue", "data": {
        "sender_uid": 9, "recipient_uid": 42, "dialogue": {"id": 70, "kind": kind}}})
        for kind in ("Start", "End", {"Marker": marker})) + "\n")
    fixture = {"case": "T01", "opening": opening, "goal": tasks.FACILITY["goal"],
               "facility": tasks.FACILITY, "private_oracle": str(path)}
    question = {"kind": "Question", "content": {"Localized": {"key": "npc-question-directions"}},
                "sender_uid": 9, "sender": target, "dialogue_id": 70, "tag": 2,
                "responses": [{"response_id": 3, "content": {"Localized": {"key": "dialogue-direction-workshop"}}}]}
    events = [{"session_id": "s", "event": "dialogue", "data": question}]
    receipts = [
        action_receipt("context_interact", {"submitted_to_server": True}, operation="talk", target=target),
        action_receipt("dialogue_action", {"submitted_to_server": True}, action_id="answer",
                       operation="respond", sender_uid=9, dialogue_id=70, tag=2, response_id=3)]
    pair = [sample(2, 1, fixture["goal"]), sample(3, 2, fixture["goal"])]
    pair[-1]["dialogue_markers"] = [received]
    assert tasks.score(fixture, pair[-1], receipts, events, pair)["success"]
    assert not tasks.score(fixture, pair[-1], receipts[:1], events, pair)["success"]
    for field, value in (("response_id", 4), ("sender_uid", 8), ("dialogue_id", 71), ("tag", 3)):
        bad = copy.deepcopy(receipts)
        bad[1]["request"]["args"]["action"][field] = value
        assert not tasks.score(fixture, pair[-1], bad, events, pair)["success"]
    bad = copy.deepcopy(pair[-1])
    bad["dialogue_markers"][0]["marker"]["wpos"]["x"] += 1
    assert not tasks.score(fixture, bad, receipts, events, pair)["success"]
    path.write_text(json.dumps({"event": "validated_dialogue", "data": {
        "sender_uid": 9, "recipient_uid": 43, "dialogue": {"id": 70, "kind": {"Marker": marker}}}}) + "\n")
    failed = tasks.score(fixture, pair[-1], receipts, events, pair)
    assert not failed["success"] and failed["oracle_validity"] == "unknown"
    assert failed["parts"]["received_actual_facility_information"]
    assert not failed["private_marker_recipient_validated"]


def test_live_private_oracle_reads_only_committed_records_and_rejects_malformed_records(tmp_path):
    path = tmp_path / "private.ndjson"
    fixture = {"private_oracle": str(path)}
    path.write_bytes(b'{"event":"opened"}\n{"event":"validated_')
    assert tasks.private_dialogues(fixture) == [{"event": "opened"}]
    assert fixture["private_oracle_snapshot"]["pending_tail_bytes"] > 0
    path.write_bytes(b'{"event":"opened"}\n{"event":"validated_dialogue"}\n')
    assert tasks.private_dialogues(fixture)[-1]["event"] == "validated_dialogue"
    assert fixture["private_oracle_snapshot"]["pending_tail_bytes"] == 0
    path.write_bytes(b'{"event":"opened"}\nnot-json\n')
    with pytest.raises(json.JSONDecodeError):
        tasks.private_dialogues(fixture)


def test_image_and_unqualified_scored_paths_have_no_fallback(tmp_path):
    for task, terrain in (("T01", "natural"), ("T02", "flat")):
        assert support_matrix()["renderer"][f"{task}/{terrain}"]["image"]["scored_evaluation"] is False
        args = SimpleNamespace(scene=task, terrain=terrain, observation_mode="image",
                               mode="task-engineering", harness="responses", recordings=None)
        with pytest.raises(ValueError, match="unsupported"):
            admit_selection(args, {"renderer": {}, "renderer_fixture_helper": {}})
