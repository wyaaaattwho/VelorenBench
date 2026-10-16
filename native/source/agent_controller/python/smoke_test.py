"""Live smoke checks for locomotion and interaction actions.

Requires a running veloren-server-cli on 127.0.0.1:14004 and a built
agent_bridge. Prints a JSON report and exits non-zero on hard failures.

Conditional checks (pickup / collect / mine) are marked skipped when the
current world state does not provide a suitable target.
"""

from __future__ import annotations

import json
import math
import time
import traceback
from dataclasses import asdict, dataclass
from typing import Any, Callable

from veloren_controller import VelorenController


@dataclass
class CheckResult:
    name: str
    status: str  # pass | fail | skip
    detail: dict[str, Any]


def distance(left: list[float], right: list[float]) -> float:
    return math.dist(left, right)


def run_check(name: str, fn: Callable[[], dict[str, Any] | None]) -> CheckResult:
    try:
        detail = fn() or {}
        status = str(detail.pop("status", "pass"))
        return CheckResult(name=name, status=status, detail=detail)
    except Exception as error:  # noqa: BLE001 - smoke report must catch all
        return CheckResult(
            name=name,
            status="fail",
            detail={
                "error": str(error),
                "traceback": traceback.format_exc(limit=4),
            },
        )


def wait_until_movable(
    controller: VelorenController, *, attempts: int = 30
) -> dict[str, Any]:
    """Wait briefly after spawn so physics / chunk load can settle."""
    last: dict[str, Any] = {}
    for index in range(attempts):
        last = controller.observe()
        if last.get("position") is None:
            time.sleep(0.1)
            continue
        if last.get("on_ground") is True:
            return last
        # After enough samples, accept null on_ground rather than blocking forever.
        if index >= 10 and last.get("on_ground") is not False:
            return last
        time.sleep(0.1)
    return last


def main() -> int:
    results: list[CheckResult] = []

    with VelorenController(startup_timeout=90.0) as controller:
        baseline = wait_until_movable(controller)

        def schema() -> dict[str, Any]:
            assert (
                baseline["observation_schema_version"]
                == VelorenController.OBSERVATION_SCHEMA_VERSION
            )
            assert baseline["protocol_version"] == VelorenController.PROTOCOL_VERSION
            assert baseline["position"] is not None
            assert "wielding" in baseline
            assert isinstance(baseline.get("nearby_entities"), list)
            return {
                "schema": baseline["observation_schema_version"],
                "tick": baseline.get("tick"),
                "on_ground": baseline.get("on_ground"),
            }

        results.append(run_check("observe_schema_1_1", schema))

        def move_forward() -> dict[str, Any]:
            # Short moves can finish before stall detection; spawn may also be
            # blocked. Retry once with a longer pulse after a small turn.
            attempts: list[dict[str, Any]] = []
            for index, duration in enumerate((45, 90)):
                if index == 1:
                    controller.turn(20)
                before = controller.observe()["position"]
                result = controller.move_forward(speed=0.75, duration_ticks=duration)
                after = controller.observe()["position"]
                assert before is not None and after is not None
                moved = distance(before, after)
                attempts.append(
                    {
                        "duration_ticks": duration,
                        "action_status": result.status,
                        "distance": moved,
                        "detail_code": result.detail.get("code"),
                    }
                )
                if result.status == "completed" and moved > 0.2:
                    return {
                        "distance": moved,
                        "action_status": result.status,
                        "attempts": attempts,
                    }
            raise AssertionError(
                f"move_forward did not displace avatar; attempts={attempts}"
            )

        results.append(run_check("move_forward", move_forward))

        def turn_and_jump() -> dict[str, Any]:
            look_before = controller.observe()["look"]
            turn = controller.turn(45)
            look_after = controller.observe()["look"]
            jump = controller.jump(duration_ticks=6)
            assert turn.status == "completed", turn
            assert jump.status == "completed", jump
            assert look_before != look_after
            return {
                "turn_status": turn.status,
                "jump_status": jump.status,
                "jump_landed": jump.detail.get("landed"),
            }

        results.append(run_check("turn_and_jump", turn_and_jump))

        def move_to_near() -> dict[str, Any]:
            position = controller.observe()["position"]
            assert position is not None
            target = [position[0] + 1.5, position[1], position[2]]
            result = controller.move_to(
                target, speed=0.5, tolerance=0.8, timeout_ticks=300
            )
            assert result.status in {"completed", "failed"}, result
            return {
                "action_status": result.status,
                "detail_code": result.detail.get("code"),
            }

        results.append(run_check("move_to", move_to_near))

        def combat_basics() -> dict[str, Any]:
            wield = controller.toggle_wield(True)
            attack = controller.attack(duration_ticks=12)
            block = controller.block(duration_ticks=8)
            roll = controller.roll(duration_ticks=8)
            swap = controller.swap_weapons()
            for result in (wield, attack, block, roll, swap):
                assert result.status in {"completed", "failed", "cancelled"}, result
            return {
                "wield": wield.status,
                "attack": attack.status,
                "block": block.status,
                "roll": roll.status,
                "swap_weapons": swap.status,
                "saw_attack": attack.detail.get("saw_attack"),
            }

        results.append(run_check("combat_basics", combat_basics))

        def pickup_if_available() -> dict[str, Any]:
            observation = controller.observe()
            pickable = [
                entity
                for entity in observation.get("nearby_entities") or []
                if entity.get("pickable") and entity.get("uid") is not None
            ]
            if not pickable:
                return {
                    "status": "skip",
                    "reason": "no pickable entity within range",
                }
            target = pickable[0]
            result = controller.pickup(uid=int(target["uid"]))
            assert result.status in {"completed", "failed"}, result
            return {
                "uid": target["uid"],
                "action_status": result.status,
                "detail": result.detail,
            }

        results.append(run_check("pickup", pickup_if_available))

        def collect_or_mine_if_aimed() -> dict[str, Any]:
            observation = controller.observe()
            aimed = observation.get("aimed_block")
            if not isinstance(aimed, dict) or not aimed.get("position"):
                return {
                    "status": "skip",
                    "reason": "no aimed_block in observation",
                }
            position = [int(value) for value in aimed["position"]]
            if aimed.get("collectible") or aimed.get("directly_collectible"):
                result = controller.collect(position)
                kind = "collect"
            else:
                result = controller.mine(
                    position, duration_ticks=60, timeout_ticks=180
                )
                kind = "mine"
            assert result.status in {"completed", "failed"}, result
            return {
                "kind": kind,
                "position": position,
                "block_kind": aimed.get("kind"),
                "action_status": result.status,
                "detail_code": result.detail.get("code"),
            }

        results.append(run_check("collect_or_mine_aimed", collect_or_mine_if_aimed))

        def inventory_ops_if_slot() -> dict[str, Any]:
            inventory = controller.observe().get("inventory") or {}
            items = inventory.get("items") or []
            if not items:
                return {"status": "skip", "reason": "inventory empty"}
            slot = items[0].get("slot")
            assert slot is not None
            use = controller.use_item(slot=int(slot))
            assert use.status in {"completed", "failed"}, use
            return {"slot": slot, "use_status": use.status, "detail": use.detail}

        results.append(run_check("use_item_first_slot", inventory_ops_if_slot))

    report = {
        "passed": sum(1 for item in results if item.status == "pass"),
        "failed": sum(1 for item in results if item.status == "fail"),
        "skipped": sum(1 for item in results if item.status == "skip"),
        "checks": [asdict(item) for item in results],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
