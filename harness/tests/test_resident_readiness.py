"""Fresh same-identity 3D placement; native f32 support coordinates stay authoritative."""
import asyncio
import math

import pytest

from .test_resident_placement import ResidentWorld
from scripts.rpg_benchmark import navigation_tasks as tasks


@pytest.mark.parametrize("elevated_only", [False, True])
def test_all_cardinals_rank_fresh_3d_without_banning_elevated_surfaces(
        tmp_path, monkeypatch, elevated_only):
    world = ResidentWorld(tmp_path)
    world.first_already_grounded = True
    world.install(monkeypatch)
    original = world.admin.request
    queries = []
    choices = [[26697.0, 4790.4, 146.4], [26697.5, 4790.4, 144.0],
               [26694.0, 4790.4, 144.0], [26696.0, 4792.4, 144.0]]

    async def request(command, args=None):
        value = await original(command, args)
        if command == "inspect_terrain" and args["position"] == world.admin.point:
            value["support_candidates"][0]["standing_position"] = list(world.admin.point)
        if command == "inspect_terrain" and args["position"][:2] != world.admin.point[:2]:
            index = len(queries)
            queries.append(args["position"])
            value["support_candidates"] = [] if elevated_only and index else [{
                "occupiable": True, "standing_position": choices[index],
                "support": {"kind": "Air" if index == 0 else "Rock"}}]
        return value

    async def goto(admin, actor, point, **kwargs):
        assert kwargs == {"horizontal_tolerance": 2}
        # Only this mechanical fake replaces strict goto: elevated is known valid.
        world.gotos.append((world.stage, actor.name, point))
        actor.point = point

    world.admin.request = request
    monkeypatch.setattr(tasks.native, "goto", goto)
    resident, proof = asyncio.run(tasks.place_near_resident(
        world.admin, world.admin, 2203, {"position": [26608, 4912, 144]}))
    assert len(queries) == 4
    expected = choices[0] if elevated_only else choices[1]
    assert proof["target"] == expected == world.gotos[0][2]
    assert math.dist(proof["current"]["position"], resident["position"]) < 5
    assert len(proof["placement_queries"]) == 4


def test_moving_resident_is_reranked_before_single_strict_mutation(tmp_path, monkeypatch):
    world = ResidentWorld(tmp_path)
    world.first_already_grounded = True
    world.install(monkeypatch)
    original = world.admin.request
    points = [[26694, 4790.4, 144], [26698, 4790.4, 144],
              [26696, 4788.4, 144], [26696, 4792.4, 144]]
    queries = []

    async def request(command, args=None):
        value = await original(command, args)
        if command == "inspect_terrain" and args["position"][:2] != world.admin.point[:2]:
            index = len(queries)
            queries.append(args["position"])
            value["support_candidates"] = [{
                "occupiable": True, "standing_position": points[index],
                "support": {"kind": "Rock"}}]
            if len(queries) == 4:
                world.walk_offset = 3
        return value

    world.admin.request = request
    _, proof = asyncio.run(tasks.place_near_resident(
        world.admin, world.admin, 2203, {"position": [26608, 4912, 144]}))
    assert len(world.gotos) == 1
    assert proof["target"] == points[1]  # Not the first valid cardinal.
    assert proof["placement_reference"]["position"][0] > 26699


def test_horizontal_in_range_is_not_the_native_3d_interaction_rule():
    standing = [26586.94140625, 4904.61767578125, 146.39999389648438]
    resident = [26588.66015625, 4900.26123046875, 144.0]
    assert math.dist(standing[:2], resident[:2]) < 5
    assert math.dist(standing, resident) > 5
