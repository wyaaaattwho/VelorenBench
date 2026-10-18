"""Exercise cleanup's real readiness/qualification and strict return path."""
import asyncio
import copy
from collections import deque
from unittest.mock import Mock

import pytest

from scripts.rpg_benchmark import setup


class CleanupWorld:
    def __init__(self, height=382, descending=True, failure=None, empty=False):
        self.name = "ordinary-player"
        self.home = [18703.94921875, 10800.3564453125, height]
        self.failure = failure
        self.now = 0
        self.commands, self.drops, self.queries = [], [], []
        self.items = [] if empty else [
            {"slot": {"idx": i}, "item": {"definition_id": f"stock-{i}"}} for i in range(2)]
        self.state = self.pose(self.home)
        self.samples = deque()
        if descending:
            self.samples.extend([
                self.pose([18704, 10800, height-1], ground=False, velocity=[0, 0, -.857291]),
                self.pose(self.home, ground=False),
                self.pose(self.home, velocity=[0, 0, -.2]),
                self.pose(self.home, occupancy="unknown"),
            ])
        self.samples.append(self.pose(self.home))

    def pose(self, point, ground=True, velocity=None, occupancy="air"):
        return {"position": list(point), "velocity": velocity or [0, 0, 0], "on_ground": ground,
                "local_geometry": {"geometry": {"cells": [{"occupancy": occupancy}]}}}

    async def sleep(self, seconds):
        self.now += seconds

    async def request(self, command, args=None):
        if command == "observe":
            if self.failure == "initial_observe" and not self.commands:
                raise RuntimeError("initial_observe failure")
            if self.samples:
                self.state = self.samples.popleft()
            return copy.deepcopy({**self.state, "inventory": {"items": self.items}})
        assert command == "inspect_terrain" and args == {"position": self.home}
        self.queries.append(copy.deepcopy(args))
        assert not self.commands  # Qualify the return witness BEFORE the excursion.
        if self.failure == "terrain":
            raise RuntimeError("terrain failure")
        return {"status": "complete", "support_candidates": [
            {"occupiable": True, "standing_position": list(self.home)}]}

    async def command(self, admin, *args):
        self.commands.append(args)
        if args[:3] == ("sudo", self.name, "goto"):
            target = list(args[3:])
            if len(self.commands) == 1:
                assert target == [v+100 for v in self.home]
                assert len(self.queries) == 1
                self.state = self.pose(target, ground=False, velocity=[0, 0, -1])
            else:
                assert target == self.home  # No integer-rounded or original descending z381.
                if self.failure == "return":
                    raise RuntimeError("return failure")
                returned = [*self.home[:2], self.home[2]+(1 if self.failure == "wrong_height" else 0)]
                self.state = self.pose(returned)
        return {"status": "completed"}

    async def action(self, actor, kind, **args):
        assert actor is self and kind == "drop" and args == {"slot": self.items[0]["slot"]}
        self.drops.append(copy.deepcopy(args))
        if self.failure == "drop":
            raise RuntimeError("drop failure")
        self.items.pop(0)
        return {"status": "completed", "action_id": f"drop-{len(self.drops)}"}

    def install(self, monkeypatch):
        monkeypatch.setattr(setup, "command", self.command)
        monkeypatch.setattr(setup, "action", self.action)
        monkeypatch.setattr(setup.asyncio, "sleep", self.sleep)
        monkeypatch.setattr(setup.time, "monotonic", lambda: self.now)


@pytest.mark.parametrize("height", [382, 501], ids=["natural", "flat"])
@pytest.mark.parametrize("descending", [True, False], ids=["initially-descending", "already-settled"])
def test_cleanup_uses_real_settled_qualified_witness_and_exact_return(monkeypatch, height, descending):
    world = CleanupWorld(height, descending)
    world.install(monkeypatch)
    result = asyncio.run(setup.clean_inventory(Mock(), world))
    assert result["before"]["position"] == result["remaining"]["position"] == world.home
    assert result["before"]["velocity"] == [0, 0, 0] and result["before"]["on_ground"]
    assert len(world.drops) == 2 and len(result["removed"]) == 2
    assert [row["item"]["definition_id"] for row in result["removed"]] == ["stock-0", "stock-1"]
    assert result["remaining"]["inventory"]["items"] == []
    assert world.commands[-3:] == [
        ("sudo", world.name, "skill_preset", "clear"),
        ("sudo", world.name, "buff", "clear"),
        ("sudo", world.name, "repair_equipment", "true")]
    assert result["removed_drop_expiry_game_seconds"] == 300
    assert result["removed_drop_region"] == "100m offset from spawn"


@pytest.mark.parametrize("failure", ["initial_observe", "terrain", "drop", "return"])
def test_cleanup_prerequisite_and_mutation_errors_propagate_without_retry(monkeypatch, failure):
    world = CleanupWorld(failure=failure)
    world.install(monkeypatch)
    with pytest.raises(RuntimeError, match=failure+" failure"):
        asyncio.run(setup.clean_inventory(Mock(), world))
    if failure in {"initial_observe", "terrain"}:
        assert not world.commands and not world.drops
    elif failure == "drop":
        assert len(world.drops) == 1 and len(world.commands) == 1
    else:
        assert len(world.drops) == 2 and len(world.commands) == 2


def test_cleanup_return_keeps_existing_strict_vertical_check(monkeypatch):
    world = CleanupWorld(failure="wrong_height")
    world.install(monkeypatch)
    with pytest.raises(RuntimeError, match="did not establish goto"):
        asyncio.run(setup.clean_inventory(Mock(), world))
    assert len(world.drops) == 2 and len(world.commands) == 2
    assert world.state["position"][2] == world.home[2]+1


def test_empty_inventory_still_preserves_excursion_return_and_reset_behavior(monkeypatch):
    world = CleanupWorld(empty=True)
    world.install(monkeypatch)
    result = asyncio.run(setup.clean_inventory(Mock(), world))
    assert result["before"]["position"] == result["remaining"]["position"] == world.home
    assert not world.drops and result["removed"] == []
    assert len(world.commands) == 5
