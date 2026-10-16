import math
import threading
import time
import uuid

from veloren_controller import (
    BridgeCommandError,
    TelemetryCollector,
    VelorenController,
)


def distance(left: list[float], right: list[float]) -> float:
    return math.dist(left, right)


# Give the previous bridge session a moment to release the character login.
time.sleep(2.0)

with VelorenController(startup_timeout=90.0) as controller:
    before = controller.observe()
    action_id = f"integration-{uuid.uuid4().hex}"
    action = {"type": "move_forward", "speed": 0.5}

    first = controller.step(action, action_id=action_id, duration_ticks=30)
    replay = controller.step(action, action_id=action_id, duration_ticks=30)
    after = controller.observe()

    assert first.status == "completed", first
    assert replay.finished_tick == first.finished_tick, (first, replay)
    assert replay.duplicate is True
    try:
        controller.step(
            {"type": "move_forward", "speed": 0.75},
            action_id=action_id,
            duration_ticks=30,
        )
    except BridgeCommandError as error:
        assert error.code == "action_id_conflict", error
    else:
        raise AssertionError("reusing action_id with different parameters must fail")
    assert before["position"] is not None and after["position"] is not None
    assert distance(before["position"], after["position"]) > 0.25, (before, after)
    assert after["protocol_version"] == controller.PROTOCOL_VERSION
    assert after["inventory"] is not None
    assert after["health"] is not None
    assert after["energy"] is not None
    assert (
        after["observation_schema_version"]
        == VelorenController.OBSERVATION_SCHEMA_VERSION
    )
    assert after["coordinate_frame"]["z_axis"] == "up"
    assert after["look"] is not None
    assert after["desired_look"] is not None
    assert "wielding" in after
    assert "nearby_entities" in after
    assert isinstance(after["nearby_entities"], list)

    turn = controller.turn(90)
    turned = controller.observe()
    assert turn.status == "completed"
    assert turned["look"] != before["look"], (before["look"], turned["look"])

    sideways = controller.move_forward(speed=0.5, duration_ticks=15)
    after_turn_move = controller.observe()
    assert sideways.status == "completed"
    assert distance(after["position"], after_turn_move["position"]) > 0.1

    target = [
        after_turn_move["position"][0] + 2.0,
        after_turn_move["position"][1],
        after_turn_move["position"][2],
    ]
    move_to = controller.move_to(
        target,
        speed=0.5,
        tolerance=0.75,
        timeout_ticks=300,
    )
    assert move_to.status == "completed", move_to

    interrupted: dict[str, object] = {}

    def run_long_move() -> None:
        interrupted["result"] = controller.move_forward(
            speed=0.5, duration_ticks=300
        )

    movement_thread = threading.Thread(target=run_long_move)
    movement_thread.start()
    time.sleep(0.25)
    stop = controller.stop()
    movement_thread.join(timeout=5)
    assert not movement_thread.is_alive()
    assert stop.status == "completed", stop
    assert interrupted["result"].status == "cancelled", interrupted
    assert interrupted["result"].detail["code"] == "action_cancelled", interrupted

    jump = controller.jump(duration_ticks=3)
    assert jump.status == "completed", jump
    assert jump.detail.get("landed") is True

    telemetry = TelemetryCollector(controller, trajectory_limit=10)
    telemetry_frame = telemetry.sample_once()
    telemetry_snapshot = telemetry.snapshot()
    assert telemetry_frame.observation["position"] is not None
    assert (
        telemetry_snapshot["trajectory"][-1]["tick"]
        == telemetry_frame.observation["tick"]
    )

    batch = controller.events_since(0)
    assert batch.truncated is False, batch
    assert [event["seq"] for event in batch.events] == sorted(
        event["seq"] for event in batch.events
    )
    events = controller.events()
    event_names = {event["event"] for event in events}
    assert "action_started" in event_names
    assert "action_completed" in event_names

    print(
        {
            "session_id": controller.session_id,
            "distance_moved": distance(before["position"], after["position"]),
            "finished_tick": first.finished_tick,
            "idempotent_replay_tick": replay.finished_tick,
            "turn_status": turn.status,
            "move_to_status": move_to.status,
            "jump_status": jump.status,
            "stop_status": stop.status,
            "telemetry_sequence": telemetry_frame.sequence,
            "events": sorted(event_names),
        }
    )
