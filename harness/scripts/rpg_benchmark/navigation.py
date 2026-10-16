from __future__ import annotations

import asyncio
import math
import time
from collections import deque

from scripts.rpg_benchmark import layouts
from scripts.rpg_benchmark import setup as native
from scripts.rpg_benchmark.scene import counts
from scripts.rpg_benchmark.oracles import pickup_actions
from scripts.rpg_benchmark.native import NativeRefusal

APPLE = "common.items.food.apple"
RECOVERABLE_ROUTE_REFUSALS = {
    "control_motion_unsupported", "control_motion_obstructed",
    "control_route_step_requires_action", "control_route_no_path",
    "control_route_vertical_unsupported",
}


def xyz(point: list[float]) -> list[float]:
    return [point[0], point[2], point[1]]


def goal_text(goal: list[float], interaction: bool = False) -> str:
    return (
        f"Reach the rendezvous at player-visible location x={goal[0]:.2f}, "
        f"y={goal[2]:.2f} (height), z={goal[1]:.2f}, and settle there alive. "
        + ("Collect and keep the apple at the rendezvous. " if interaction else "")
        + "Choose your own route and tools using the normal map and current terrain observations."
    )


def named_goal_text(name: str) -> str:
    return (f"Travel to {name} and come to rest alive in the town's central area. "
            "Choose your own route and tools using normal game information.")


def named_site(observation: dict, name: str, kind: str) -> dict:
    matches = [row for row in observation["known_sites"]
               if row.get("name") == name and row.get("kind") == kind]
    if len(matches) != 1:
        raise RuntimeError(f"normal observation must expose one named {kind} destination: {name}")
    site = matches[0]
    point = site.get("position")
    if (not isinstance(point, list) or len(point) != 3 or point[2] is not None
            or not all(type(value) in (int, float) and math.isfinite(value) for value in point[:2])
            or not isinstance(site.get("map_id"), str)):
        raise ValueError("native named-site marker must have horizontal block coordinates and unknown altitude")
    return site


def qualify_named_map(view: dict, site: dict, start_xy: list[float]) -> dict:
    if (view.get("schema_version"), view.get("source"), view.get("coordinate_frame"),
            view.get("cell_stride")) != ("1.0", "connected_client_world_data", "native_xy", 1):
        raise ValueError("named-site setup requires an exact normal native map crop")
    markers = [row for row in view["markers"] if row.get("name") == site["name"]
               and row.get("kind") == site["kind"]]
    if len(markers) != 1 or markers[0]["position"] != site["position"][:2]:
        raise RuntimeError("normal map and observation disagree on named destination")
    low_x, low_y, high_x, high_y = view["coverage_bounds"]
    if not low_x <= start_xy[0] < high_x or not low_y <= start_xy[1] < high_y:
        raise ValueError("named-site origin is outside the native map crop")
    scale = view["symbol_blocks"]
    column = math.floor((start_xy[0] - low_x) / scale[0])
    row = math.floor((start_xy[1] - low_y) / scale[1])
    symbol = view["rows"][row][column]
    meaning = view["legend"][symbol].split("+")
    if "road" not in meaning and "bridge" not in meaning:
        raise RuntimeError("named-site origin must be backed by a native public road/bridge map cell")
    return {"destination_marker": markers[0], "origin_cell": [column, row],
            "origin_symbol": symbol, "origin_classes": meaning,
            "limit": "32m visual road cell, not a subcell road center, collision or route certificate"}


def map_road_approach(view: dict, site: dict, start_xy: list[float], radius: float) -> list[list[float]]:
    start = tuple(qualify_named_map(view, site, start_xy)["origin_cell"])
    low_x, low_y = view["coverage_bounds"][:2]
    scale = view["symbol_blocks"]
    roads = {(x, y) for y, row in enumerate(view["rows"]) for x, symbol in enumerate(row)
             if {"road", "bridge"}.intersection(view["legend"][symbol].split("+"))}

    def center(cell):
        return [low_x + (cell[0] + .5) * scale[0], low_y + (cell[1] + .5) * scale[1]]

    nearest = min(roads, key=lambda cell: math.dist(center(cell), site["position"][:2]))
    if math.dist(center(nearest), site["position"][:2]) > radius:
        raise RuntimeError("normal road map does not offer an approach inside the named arrival region")
    queue = deque([start])
    parents = {start: None}
    while queue and nearest not in parents:
        cell = queue.popleft()
        for dx, dy in ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)):
            neighbor = (cell[0] + dx, cell[1] + dy)
            if neighbor in roads and neighbor not in parents:
                parents[neighbor] = cell
                queue.append(neighbor)
    if nearest not in parents:
        raise RuntimeError("normal road-map candidate is disconnected; native reachability remains unknown")
    cells = []
    cell = nearest
    while cell is not None:
        cells.append(cell)
        cell = parents[cell]
    return [center(cell) for cell in reversed(cells)]


async def physical_route(admin, start: list[float], goal: list[float], *,
                         isolate_ambient: bool = True, allow_ground_level_wood: bool = False) -> list[dict]:
    distance = math.hypot(goal[0] - start[0], goal[1] - start[1])
    route = []
    previous = start
    await native.goto(admin, admin, start)
    for i in range(1, math.ceil(distance / 16) + 1):
        fraction = min(i * 16 / distance, 1)
        point = [start[j] + (goal[j] - start[j]) * fraction for j in range(3)]
        point = await native.settled_point(admin, point)
        await native.goto(admin, admin, previous)
        receipt = await native.action(admin, "move_to", position=point,
                                     tolerance=1, timeout_ticks=1800, require_completed=False)
        alternative = None
        if receipt["status"] != "completed":
            alternative = await recover_route_leg(admin, point, receipt)
        achieved = await native.wait_observation(
            admin, lambda row: row["on_ground"] is True
            and math.sqrt(sum(value * value for value in row["velocity"])) < .1,
            "grounded and settled physical route leg")
        if receipt["status"] == "completed" and (
                math.dist(native.position(achieved)[:2], point[:2]) > 1.5
                or abs(native.position(achieved)[2] - point[2]) > 1):
            displacement = achieved
            correction = await native.action(
                admin, "move_to", position=point, speed=.35, tolerance=.3,
                timeout_ticks=1800, require_completed=False, basis=achieved)
            alternative = {"post_completion_displacement": displacement,
                           "endpoint_correction_receipt": correction, "capability_claim": False}
            if correction["status"] != "completed":
                alternative["recovery"] = await recover_route_leg(admin, point, correction)
            achieved = await native.wait_observation(
                admin, lambda row: row["on_ground"] is True
                and math.sqrt(sum(value * value for value in row["velocity"])) < .1,
                "grounded and settled low-speed endpoint correction")
        if not at_route_endpoint(achieved, point):
            raise RuntimeError(f"native long route reachability failed at {point}: {receipt}")
        route.append({"goal": point, "receipt": receipt, "achieved": achieved,
                      "source_backed_alternative": alternative,
                      "terrain": await native.qualify(
                          admin, native.position(achieved), allow_ground_level_wood=allow_ground_level_wood),
                      "ambient_clear_receipt": await native.command(admin, "kill_npcs", 48)
                      if isolate_ambient else None})
        previous = native.position(achieved)
    return route


def at_route_endpoint(observation: dict, goal: list[float]) -> bool:
    point = native.position(observation)
    return (observation["on_ground"] is True
            and math.dist(point[:2], goal[:2]) <= 1.5 and abs(point[2] - goal[2]) <= 1)


async def recover_climb(admin) -> dict | None:
    before = await admin.request("observe")
    if before["character_state"] != "Climb":
        return None
    receipt = await native.action(admin, "character_action", kind="cancel_climb", basis=before)
    after = await native.wait_observation(
        admin, lambda row: row["character_state"] != "Climb" and row["on_ground"] is True
        and math.sqrt(sum(value * value for value in row["velocity"])) < .1,
        "native cancel_climb followed by grounded route recovery")
    return {"before": before, "receipt": receipt, "after": after}


async def recover_route_leg(admin, goal: list[float], failed_receipt: dict) -> dict:
    recovery = {"failed_move_to": failed_receipt, "climb_recovery": await recover_climb(admin),
                "attempts": [], "capability_claim": False}
    try:
        recovery["continuous"] = await native.continuous_walk(admin, goal, origin="setup")
        return recovery
    except native.ContinuousRouteFailure as error:
        recovery["continuous_failure"] = {"admission": error.admission, "observation": error.observation}
    except NativeRefusal as error:
        if error.native_error["code"] not in RECOVERABLE_ROUTE_REFUSALS:
            raise
        recovery["continuous_refusal"] = {"command": error.command, "error": error.native_error}
    admin.journal.write(origin="setup", event="route_leg_recovery", goal=goal, evidence=recovery)
    # Distinct loaded side corridors, not repeated submissions of the failed route.
    for offset in (3, -3, 6, -6):
        climb = await recover_climb(admin)
        current = await admin.request("observe")
        origin = native.position(current)
        dx, dy = goal[0] - origin[0], goal[1] - origin[1]
        length = math.hypot(dx, dy)
        if length < .01:
            raise RuntimeError(f"native route recovery has no horizontal detour axis: {recovery}")
        waypoints = []
        queries = []
        attempt = {"offset_m": offset, "from": current, "climb_recovery": climb,
                   "terrain": queries, "waypoints": waypoints, "legs": []}
        recovery["attempts"].append(attempt)
        for fraction in (0, .5):
            probe = [origin[0] + dx * fraction - dy / length * offset,
                     origin[1] + dy * fraction + dx / length * offset,
                     origin[2] + (goal[2] - origin[2]) * fraction]
            if math.dist(probe[:2], origin[:2]) > 10 or abs(probe[2] - origin[2]) > 2:
                attempt["rejected"] = "candidate outside native 10m horizontal/2m vertical terrain-query window"
                break
            query = await native.terrain(admin, probe)
            queries.append(query)
            candidates = [row["standing_position"] for row in query.get("support_candidates", [])
                          if row["occupiable"] is True and row["support"]["kind"] not in ("Leaves", "Wood")
                          and abs(row["standing_position"][2] - probe[2]) <= 2]
            if query.get("status") != "complete" or not candidates:
                break
            waypoints.append(min(candidates, key=lambda point: abs(point[2] - probe[2])))
        if len(waypoints) != 2:
            attempt.setdefault("rejected", "no loaded occupiable ground corridor")
            continue
        for point in [*waypoints, goal]:
            before = await admin.request("observe")
            receipt = await native.action(admin, "move_to", position=point, tolerance=.4, speed=.6,
                                          timeout_ticks=1800, require_completed=False, basis=before)
            after = await admin.request("observe")
            completed = receipt["status"] == "completed"
            finishing = {}
            if (not completed and after["on_ground"] is True
                    and math.dist(native.position(after), native.position(before)) > .5
                    and math.dist(native.position(after)[:2], point[:2]) <= 4
                    and abs(native.position(after)[2] - point[2]) <= 1):
                # The chaser has reached a new grounded local window; qualify its
                # remaining endpoint with the independently fenced native planner.
                try:
                    finishing["continuous"] = await native.continuous_walk(admin, point, origin="setup")
                    completed = True
                except native.ContinuousRouteFailure as error:
                    finishing["failure"] = {"admission": error.admission, "observation": error.observation}
                except NativeRefusal as error:
                    if error.native_error["code"] not in RECOVERABLE_ROUTE_REFUSALS:
                        raise
                    finishing["refusal"] = {"command": error.command, "error": error.native_error}
                after = await admin.request("observe")
            endpoint_after_failure = None
            if (not completed and at_route_endpoint(after, point)
                    and math.dist(native.position(after), native.position(before)) > .5):
                settled = await native.wait_observation(
                    admin, lambda row: row["on_ground"] is True
                    and math.sqrt(sum(value * value for value in row["velocity"])) < .1,
                    "settled actual endpoint after native movement failure")
                after = settled
                if at_route_endpoint(settled, point):
                    native.require_living(settled)
                    if (settled["session_id"], settled["self_identity"]) != (
                            before["session_id"], before["self_identity"]):
                        raise RuntimeError("native route endpoint actor changed after movement failure")
                    endpoint_after_failure = {
                        "achieved": settled,
                        "terrain": await native.qualify(admin, native.position(settled)),
                        "scope": "unchanged physical-route endpoint gate; native failure receipts retained"}
                    completed = True
            if completed and endpoint_after_failure is None:
                after = await native.wait_observation(
                    admin, lambda row: row["on_ground"] is True
                    and math.sqrt(sum(value * value for value in row["velocity"])) < .1,
                    "grounded and settled native side corridor endpoint")
            leg = {"from": before, "goal": point, "receipt": receipt, "achieved": after,
                   "endpoint_finishing": finishing, "endpoint_after_failure": endpoint_after_failure}
            attempt["legs"].append(leg)
            admin.journal.write(origin="setup", event="native_detour_leg", **leg)
            if not completed or not at_route_endpoint(after, point):
                break
        else:
            recovery["achieved"] = await native.ready_observation(admin)
            recovery["primitive"] = "loaded-ground-side-corridor-native-move_to"
            return recovery
        attempt["failed"] = True
    raise RuntimeError(f"native route could not qualify any loaded side corridor: {recovery}")


def monitor_setup_actor(observation: dict, initial: dict) -> None:
    native.require_living(observation)
    if (observation["session_id"], observation["self_identity"]) != (
            initial["session_id"], initial["self_identity"]):
        raise RuntimeError("named-site setup actor identity changed")
    if (observation["survival"]["health"]["current"] < initial["survival"]["health"]["current"]
            or observation["survival"]["buffs"]):
        raise RuntimeError("named-site setup actor was injured before measurement")


async def setup_named_site(admin, actor, definition: dict, *, start_actor: bool = False) -> dict:
    destination = named_site(await admin.request("observe"), definition["destination_name"],
                             definition["destination_kind"])
    center = destination["position"][:2]
    normal_map = await admin.request("inspect_map", {
        "bounds": [center[0] - 512, center[1] - 512, center[0] + 512, center[1] + 512],
        "cell_stride": 1})
    map_evidence = qualify_named_map(normal_map, destination, definition["origin"][:2])
    road_points = map_road_approach(normal_map, destination, definition["origin"][:2],
                                   definition["arrival_region"]["horizontal_m"])
    entries = [index for index, point in enumerate(road_points)
               if math.dist(point, center) <= definition["setup_road_entry_radius_m"]]
    if not entries:
        raise RuntimeError("normal map has no road entry in the named-site setup approach region")
    route_points = road_points[entries[0]:]
    start = await native.settled_point(admin, definition["origin"])
    footing = await native.settled_point(admin, [*center, definition["origin"][2]])
    goal = [*center, footing[2]]
    distance = math.hypot(start[0] - center[0], start[1] - center[1])
    if distance < definition["minimum_distance_m"]:
        raise RuntimeError("named-site native placement is shorter than the admitted long route")
    await native.goto(admin, admin, start)
    started = time.monotonic()
    author_opening = await native.ready_observation(admin)
    author_opening["_elapsed_s"] = time.monotonic() - started
    route = []
    previous = start
    for point in [*route_points, footing[:2]]:
        route.extend(await physical_route(admin, previous, [*point, footing[2]],
                                          isolate_ambient=False, allow_ground_level_wood=True))
        previous = native.position(route[-1]["achieved"])
    author_samples = [author_opening]
    for _ in range(2):
        await asyncio.sleep(.3)
        observed = await admin.request("observe")
        observed["_elapsed_s"] = time.monotonic() - started
        author_samples.append(observed)
    fixture = {"case": definition["id"], "start": start, "goal": goal,
               "opening": author_opening, "task": named_goal_text(destination["name"]),
               "named_destination": destination, "arrival_region": definition["arrival_region"],
               "qualified_destination_footing": footing,
               "normal_map": normal_map, "map_evidence": map_evidence,
               "setup_road_candidate": {"points_xy": road_points,
                                        "walked_approach_points_xy": route_points,
                                        "entry_radius_m": definition["setup_road_entry_radius_m"],
                                        "source": "normal public road raster; judge setup only",
                                        "limit": "coarse cell adjacency is not physical reachability; every leg is walked natively"},
               "supplies": [], "reachability": route,
               "long_distance_m": distance, "chunk_size_m": 32,
               "goal_source": "normal known_sites and InspectMap; marker altitude unknown",
               "localization_track": "real native named-town center; no coordinates or waypoints in task",
               "reachability_scope": "administrator setup only; native town/NPCs retained; no measured solver"}
    arrival = score(fixture, author_samples[-1], [], [], author_samples)
    if not arrival["success"]:
        raise RuntimeError(f"native named-site setup walking lacks grounded named arrival: {arrival}")
    fixture["setup_arrival_check"] = {"score": arrival, "samples": author_samples,
                                     "capability_claim": False}
    if start_actor:
        await actor.start()
    initial = await actor.request("observe")
    native.require_uninjured_start(initial)
    cleanup = await native.clean_inventory(admin, actor)
    monitor_setup_actor(await actor.request("observe"), initial)
    fixture["cleanup_supplies"] = cleanup
    await native.goto(admin, actor, start)
    opening = await native.ready_observation(actor)
    monitor_setup_actor(opening, initial)
    visible = named_site(opening, destination["name"], destination["kind"])
    if visible["map_id"] != destination["map_id"] or visible["position"] != destination["position"]:
        raise RuntimeError("measured player cannot localize the same normal named destination")
    fixture["opening"] = opening
    fixture["start"] = native.position(opening)
    fixture["long_distance_m"] = math.hypot(
        fixture["start"][0] - center[0], fixture["start"][1] - center[1])
    if fixture["long_distance_m"] < definition["minimum_distance_m"]:
        raise RuntimeError("measured named-site origin lost required long-distance separation")
    fixture["actor_initialization_order"] = {
        "connected_after_admin_arrival": start_actor, "route_arrival_tick": author_samples[-1]["tick"],
        "initial": initial, "opening": opening, "player_buff_added": False,
        "player_health_reset": False, "populated_town_retained": True}
    return fixture


def verify_voxels(readbacks: list[dict], required: list[list[float]]) -> int:
    cells = {}
    for readback in readbacks:
        queries = ([readback["center_query"], *readback["adjacent_queries"]]
                   if "center_query" in readback else [readback])
        for query in queries:
            for row in query["support_candidates"]:
                for cell in [row["support"], *row["foot_clearance"]["cells"], *row["head_clearance"]["cells"]]:
                    cells[tuple(cell["position"])] = cell
    missing = [point for point in required if cells.get(tuple(point), {}).get("solid") is not True
               or cells.get(tuple(point), {}).get("kind") != "Rock"]
    if missing:
        raise RuntimeError(f"authored native solid voxel readback incomplete: {len(missing)} missing; first {missing[0]}")
    return len(required)


async def setup(admin, actor, definition: dict, terrain: str) -> dict:
    if definition["layout"] == "named_site_long":
        return await setup_named_site(admin, actor, definition)
    if definition["layout"] == "natural_long":
        cleanup = await native.clean_inventory(admin, actor)
        start = await native.settled_point(admin, definition["origin"])
        destination = [*definition["destination_xy"], start[2]]
        goal = await native.settled_point(admin, destination)
        distance = math.hypot(goal[0] - start[0], goal[1] - start[1])
        if distance < 500:
            raise RuntimeError("native long route has less than 500m separation")
        normal_map = await actor.request("inspect_map", {"bounds": [
            min(start[0], goal[0]) - 32, min(start[1], goal[1]) - 32,
            max(start[0], goal[0]) + 32, max(start[1], goal[1]) + 32], "cell_stride": 1})
        route = await physical_route(admin, start, goal)
        await native.goto(admin, actor, start)
        opening = await native.ready_observation(actor)
        native.require_living(opening)
        start = native.position(opening)
        supply = await native.drop_supply(admin, goal, APPLE, 1, "natural")
        goal = supply["entity"]["position"]
        return {"case": definition["id"], "opening": opening, "start": start, "goal": goal,
                "task": goal_text(goal, True), "normal_map": normal_map, "supplies": [supply],
                "reachability": route, "cleanup_supplies": cleanup, "long_distance_m": distance,
                "chunk_size_m": 32, "goal_source": definition["goal_source"],
                "localization_track": "explicit player-visible location; no name-discovery requirement"}
    layout_name = definition["layout"]
    recovery = layout_name == "recovery"
    config = layouts.scene("wall_side_detour" if recovery else layout_name)
    initialization = None
    if definition["id"] == "N1":
        construction = await native.author_voxels(
            admin, [xyz(voxel) for voxel in config["solid_voxels_y_up"]])
    else:
        initialization = await native.isolate_initial_spawn(admin, actor)
        construction = await native.author_voxels(
            admin, [xyz(voxel) for voxel in config["solid_voxels_y_up"]])
    readback = []
    for column in config["standing_columns"]:
        point = xyz(column["position"])
        if column["occupiable"]:
            await native.goto(admin, admin, point)
            readback.append(await native.qualify(admin, point))
        else:
            neighbor = min((xyz(row["position"]) for row in config["standing_columns"]
                            if row["occupiable"]), key=lambda candidate: math.dist(candidate, point))
            await native.goto(admin, admin, neighbor)
            query = await native.terrain(admin, point)
            if query.get("status") != "complete" or any(
                    row.get("occupiable") is True
                    and math.dist(row["standing_position"], point) < .1
                    for row in query.get("support_candidates", [])):
                raise RuntimeError("authored navigation wall lacks native obstruction readback")
            readback.append(query)
    start, goal = xyz(config["start_y_up"]), xyz(config["goal_y_up"])
    verified_voxels = verify_voxels(readback, [xyz(point) for point in config["solid_voxels_y_up"]])
    if recovery:
        start = [start[0], start[1] - 4, start[2]]
    await native.goto(admin, actor, start)
    opening = await actor.request("observe")
    return {"case": definition["id"], "opening": opening, "start": start, "goal": goal,
            "initialization": initialization,
            "task": goal_text(goal), "layout": config, "terrain_readback": readback, "supplies": [],
            "construction": construction,
            "historical_oracle": layouts.ARRIVAL,
            "native_solid_voxels_verified": verified_voxels,
            "recovery": {"type": "setup-only displacement", "original_start": xyz(config["start_y_up"]),
                         "displaced_start": start} if recovery else None,
            "historical_protocols_unchanged": True}


def score(fixture: dict, final: dict, receipts: list[dict], events: list[dict],
          samples: list[dict]) -> dict:
    opening, goal = fixture["opening"], fixture["goal"]
    health = final.get("survival", {}).get("health") or {}
    dead = health.get("dead") is True or health.get("current") == 0
    same = final.get("session_id") == opening["session_id"] and final.get("self_identity") == opening["self_identity"]
    converted = []
    for raw in samples:
        velocity = raw["velocity"]
        if not isinstance(velocity, list) or len(velocity) != 3:
            raise ValueError("native XYZ velocity unavailable")
        converted.append({
            "backend": "veloren", "session_id": raw["session_id"],
            "self_identity": raw["self_identity"], "tick": raw["tick"],
            "coordinate_frame": {"frame": "gameagent_y_up"},
            "position_y_up": xyz(raw["position"]),
            "velocity": dict(zip(("x", "y", "z"), xyz(velocity))),
            "elapsed_s": raw["_elapsed_s"],
        })
    site_observed = None
    if "named_destination" in fixture:
        region = fixture["arrival_region"]
        pair = converted[-2:]
        site_observed = len(pair) == 2 and all(
            raw.get("current_site", {}).get("location_name") == fixture["named_destination"]["name"]
            and raw["current_site"].get("loaded_chunk") is True
            and raw["current_site"].get("source") == "native_current_site_and_loaded_chunk"
            for raw in samples[-2:])
        arrived = bool(
            len(pair) == 2 and site_observed
            and all(layouts.correlated(row, converted[0]) for row in pair)
            and pair[1]["tick"] > pair[0]["tick"]
            and pair[1]["elapsed_s"] - pair[0]["elapsed_s"] >= layouts.ARRIVAL["minimum_pair_seconds"]
            and all(layouts.errors(row, xyz(goal))[0] <= region["horizontal_m"]
                    and layouts.errors(row, xyz(goal))[1] <= region["vertical_m"]
                    and layouts.speed(row) <= layouts.ARRIVAL["settled_speed_m_s"] for row in pair)
            and all(raw.get("on_ground") is True for raw in samples[-2:]))
    else:
        arrived = len(converted) >= 2 and layouts.arrival_pair(
            converted[-2], converted[-1], converted[0], xyz(goal))
    pickup = not fixture["supplies"]
    pickup_ids = []
    if fixture["supplies"]:
        uid = fixture["supplies"][0]["uid"]
        pickup_ids = pickup_actions(opening, receipts, uid, APPLE)
        pickup = bool(pickup_ids) and counts(final).get(APPLE, 0) > counts(opening).get(APPLE, 0)
    result = {"success": same and arrived and pickup and health.get("current", 0) > 0 and not dead,
              "dead": dead, "arrival": arrived, "interaction": pickup,
              "pickup_action_ids": pickup_ids, "same_actor": same,
              "horizontal_error_m": math.hypot(final["position"][0] - goal[0], final["position"][1] - goal[1]),
              "vertical_error_m": abs(final["position"][2] - goal[2]),
              "scope": "historical two-fresh-sample settled horizontal/vertical region + separate requested interaction"}
    if "named_destination" in fixture:
        result.update(
            named_site_observed=site_observed,
            approach=result["horizontal_error_m"] <= fixture["arrival_region"]["approach_horizontal_m"],
            scope="two fresh grounded/settled samples in native named-town center region; native location name required")
    return result


async def mechanism(actor, fixture: dict) -> None:
    if "named_destination" in fixture:
        raise ValueError("N7 physical reachability is setup only; there is no measured whole-task solver")
    start, goal = fixture["start"], fixture["goal"]
    distance = math.hypot(goal[0] - start[0], goal[1] - start[1])
    points = ([row["goal"] for row in fixture["reachability"]] if "reachability" in fixture else
              [[start[j] + (goal[j] - start[j]) * min(i * 16 / distance, 1) for j in range(3)]
               for i in range(1, math.ceil(distance / 16) + 1)])
    if fixture["supplies"]:
        points.append(goal)
    for point in points:
        if "reachability" in fixture:
            receipt = await native.action(actor, "move_to", origin="mechanism", position=point,
                                          tolerance=.6, timeout_ticks=1800, require_completed=False)
            if receipt["status"] != "completed":
                await native.continuous_walk(actor, point, origin="mechanism")
        else:
            await native.action(actor, "move_to", origin="mechanism", position=point,
                                tolerance=.6, timeout_ticks=1800)
    if fixture["supplies"]:
        await native.action(actor, "pickup", origin="mechanism", uid=fixture["supplies"][0]["uid"])
    await asyncio.sleep(.5)
