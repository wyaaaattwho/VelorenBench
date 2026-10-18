"""Test transport double for the real scoped authoring context, not a launcher."""
from copy import deepcopy
import math
from unittest.mock import AsyncMock


class Administrator:
    def __init__(self, admin, *, enabled=False, velocity=(0, 0, -40), delay=0):
        self.admin, self.delay = admin, delay
        self.enabled, self.areas, self.hold = enabled, set(), None
        self.position, self.velocity = [1., 2., 10.], list(velocity)
        self.initial_position, self.initial_velocity = list(self.position), list(self.velocity)
        self.calls, self.elapsed = [], 0
        self.command = AsyncMock(side_effect=self._command)

    def advance(self, seconds):
        self.elapsed += seconds
        if self.hold is not None:
            self.position = list(self.hold["position"])
            self.velocity = [0, 0, 0]
        # Delayed RPCs must never start free fall over the target: only the
        # native hold, not a timed Rock effect, keeps this position stable.

    def observation(self):
        return {"position": list(self.position), "velocity": list(self.velocity),
                "on_ground": False, "tick": self.elapsed, "session_id": "test",
                "self_identity": {"uid": 1}, "character_controls": {
                    "can_build": {"enabled": self.enabled, "assigned_area_count": len(self.areas)},
                    "fixture_authoring": deepcopy(self.hold)}}

    async def ready(self, actor):
        assert actor is self.admin
        self.advance(self.delay)
        return self.observation()

    async def wait(self, actor, predicate, reason, **kwargs):
        row = await self.ready(actor)
        assert predicate(row), reason
        return row

    async def _command(self, actor, name, *args):
        assert actor is self.admin, "only private fixture administrator"
        self.calls.append((name, args))
        if name == "area_add":
            self.areas.add(args[0])
        elif name == "build":
            self.enabled = not self.enabled
        elif name == "fixture_authoring":
            owner, operation, *point = args
            if operation == "begin":
                assert self.hold is None and self.enabled
                self.hold = {"owner": owner, "position": list(self.position),
                             "restore_position": list(self.position),
                             "restore_velocity": list(self.velocity)}
            elif operation == "move":
                assert self.hold["owner"] == owner
                self.hold["position"] = list(point)
            elif operation == "end":
                if self.hold is None:
                    raise RuntimeError("fixture authoring requires one begin, owned move, then owned end")
                assert self.hold["owner"] == owner
                self.position = self.hold["restore_position"]
                self.velocity = self.hold["restore_velocity"]
                self.hold = None
            else:
                raise AssertionError("unknown scoped operation")
        elif name == "revoke_build":
            self.areas.remove(args[0])
        else:
            assert name in {"permit_build", "area_remove"}
        self.advance(self.delay)
        return {"status": "completed"}

    def readback(self, row, point):
        assert self.hold is not None, "queries must be protected, including first target"
        self.advance(self.delay)
        top = [point[0] + .5, point[1] + .5, point[2] + 1]
        assert math.dist(self.position[:2], top[:2]) <= 10
        assert abs(self.position[2] - top[2]) <= 2
        assert self.velocity == [0, 0, 0]
        return {**deepcopy(row), "binding": {"actor_position": list(self.position),
            "readiness": {"fixture_authoring": deepcopy(self.hold)}}}
