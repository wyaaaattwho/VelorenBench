"""Human-paced visual demo for delivery / LLM showcases.

Requires:
  - running veloren-server-cli (prefer agent_controller/server_userdata)
  - built veloren-voxygen with --agent-bridge support

Usage:
  $env:VELOREN_AGENT_MODE = "voxygen"
  $env:CARGO_TARGET_DIR = "...\\veloren\\target"
  python demo.py
"""

from __future__ import annotations

import math
import time
from typing import Any

from veloren_controller import VelorenController
from veloren_controller.models import ActionResult


def _pause(seconds: float = 2.0) -> None:
    time.sleep(seconds)


def _run(label: str, result: ActionResult) -> ActionResult:
    detail = result.detail if result.detail else {}
    print(f"{label}: {result.status} {detail}")
    return result


def level_horizon(controller: VelorenController, distance: float = 25.0) -> ActionResult:
    """Look straight ahead at eye level (zero pitch) for an intuitive FPV view."""
    obs = controller.observe()
    pos = obs.get("position") or [0.0, 0.0, 0.0]
    look = obs.get("desired_look") or obs.get("look") or [0.0, 1.0, 0.0]
    lx, ly = float(look[0]), float(look[1])
    mag = math.hypot(lx, ly) or 1.0
    fx, fy = lx / mag, ly / mag
    target = [float(pos[0]) + fx * distance, float(pos[1]) + fy * distance, float(pos[2])]
    return controller.look_at(target)


def maybe_world_interact(controller: VelorenController, obs: dict[str, Any]) -> None:
    """Try pickup / collect / mine only when observation exposes a target."""
    nearby = obs.get("nearby_entities") or []
    pickable = next(
        (
            ent
            for ent in nearby
            if isinstance(ent, dict) and (ent.get("pickup") or ent.get("pickable"))
        ),
        None,
    )
    if pickable and pickable.get("uid") is not None:
        _run("pickup", controller.pickup(int(pickable["uid"])))
        _pause()
        return

    collectible = obs.get("aimed_block") or {}
    if collectible.get("collectible"):
        pos = collectible.get("position")
        if pos:
            _run("collect", controller.collect(pos))
            _pause()
            return

    if collectible.get("mine_tool") and collectible.get("position"):
        _run("mine", controller.mine(collectible["position"], duration_ticks=90))
        _pause()


def main() -> None:
    with VelorenController(
        visual_pace=True,
        pace_seconds=3.5,
        hold_open_seconds=12.0,
    ) as controller:
        before = controller.observe()
        print("before look:", before.get("look"))
        print("before pos:", before.get("position"))

        print("settling after spawn (4s)...")
        _pause(4.0)

        # 1) Horizon-level forward view first (intuitive FPV).
        _run("level_horizon", level_horizon(controller))
        _pause(2.5)

        # 2) Locomotion
        _run("turn +45", controller.turn(45))
        _pause()
        _run("move_forward", controller.move_forward(speed=0.6, duration_ticks=120))
        _pause()
        _run("turn -45", controller.turn(-45))
        _pause()
        _run("level_horizon", level_horizon(controller))
        _pause()
        _run("move_left", controller.move_left(speed=0.55, duration_ticks=70))
        _pause()
        _run("move_right", controller.move_right(speed=0.55, duration_ticks=70))
        _pause()
        _run("move_backward", controller.move_backward(speed=0.5, duration_ticks=60))
        _pause()

        # 3) Body actions
        _run("jump", controller.jump(duration_ticks=24))
        _pause()
        _run("roll", controller.roll(duration_ticks=24))
        _pause()

        # 4) Combat / wield (safe even with no target)
        _run("toggle_wield on", controller.toggle_wield(True))
        _pause()
        _run("attack", controller.attack(duration_ticks=36))
        _pause()
        _run("block", controller.block(duration_ticks=36))
        _pause()
        _run("toggle_wield off", controller.toggle_wield(False))
        _pause()

        # 5) Optional world interact if something is in view / nearby
        mid = controller.observe()
        maybe_world_interact(controller, mid)

        _run("stop", controller.stop())
        after = controller.observe()
        print("after look:", after.get("look"))
        print("after pos:", after.get("position"))
        print("demo complete")


if __name__ == "__main__":
    main()
