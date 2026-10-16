"""Unchanged authored geometry and arrival predicates, extracted from the parent."""
import hashlib
import json
import math


CASES = ("wall_side_detour", "staggered_s_turn", "elevation_side_steps",
         "long_distance_windows")



ORIGIN = (18700, 410, 10800)



ARRIVAL = {"horizontal_m": 1.0, "vertical_m": 1.0, "settled_speed_m_s": 0.1,
           "fresh_samples": 2, "minimum_pair_seconds": 0.25}



class FixtureError(RuntimeError):
    pass



def digest_json(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()



def scene(case: str) -> dict:
    if case not in CASES:
        raise ValueError(case)
    length, width = {
        "wall_side_detour": (17, 13), "staggered_s_turn": (32, 13),
        "elevation_side_steps": (24, 11), "long_distance_windows": (226, 7),
    }[case]
    floors = {(x, z): 0 for x in range(length) for z in range(width)}
    walls = []
    start, goal = (1.5, 1, width // 2 + 0.5), (length - 2.5, 1, width // 2 + 0.5)
    if case == "wall_side_detour":
        walls = [(8, z) for z in range(2, 9)]
    elif case == "staggered_s_turn":
        walls = [(9, z) for z in range(9)] + [(19, z) for z in range(4, 13)]
    elif case == "elevation_side_steps":
        floors.update({(x, z): 2 for x in range(12, length) for z in range(width)})
        floors.update({(10, z): 1 for z in range(3)})
        floors.update({(11, z): 2 for z in range(3)})
        goal = (length - 2.5, 3, width // 2 + 0.5)
    else:
        goal = (221.5, 1, 3.5)
        walls = [(75, z) for z in range(4)] + [(150, z) for z in range(3, 7)]
    origin = (ORIGIN[0], 500 if case == "long_distance_windows" else ORIGIN[1], ORIGIN[2])
    blocks = {(x, y, z) for (x, z), top in floors.items() for y in range(top + 1)}
    blocks.update((x, y, z) for x, z in walls for y in range(1, 5))
    def absolute(point):
        return [point[i] + origin[i] for i in range(3)]
    result = {
        "name": case, "origin_y_up": list(origin), "dimensions_xz": [length, width],
        "start_y_up": absolute(start), "goal_y_up": absolute(goal),
        "solid_voxels_y_up": [absolute(p) for p in sorted(blocks)],
        "standing_columns": [
            {"position": absolute((x + 0.5, top + 1, z + 0.5)),
             "occupiable": (x, z) not in walls} for (x, z), top in sorted(floors.items())],
        "block_kind": "Rock", "wall_height_m": 4 if walls else None,
        "direct_path_obstruction": "four-metre wall" if walls else "two-metre abrupt rise; side one-metre steps",
        "route_exists_by_design": True,
        "setup_mechanism": "private admin goto + make_block before measurement only",
        "natural_terrain": "elevated constructed platform; off-platform terrain is not a certified route",
    }
    validate_scene(result)
    result["geometry_sha256"] = digest_json(result)
    return result



def validate_scene(value: dict) -> None:
    voxels = {tuple(p) for p in value["solid_voxels_y_up"]}
    columns = value["standing_columns"]
    open_columns = {
        (math.floor(row["position"][0]), math.floor(row["position"][2])): row["position"][1]
        for row in columns if row["occupiable"]
    }
    def column(point):
        return (math.floor(point[0]), math.floor(point[2]))
    start, goal = value["start_y_up"], value["goal_y_up"]
    for point in (start, goal):
        if open_columns.get(column(point)) != point[1] or (
            math.floor(point[0]), point[1] - 1, math.floor(point[2])) not in voxels:
            raise FixtureError("start/goal lacks constructed occupiable support")
    visited, pending = {column(start)}, [column(start)]
    while pending:
        x, z = pending.pop()
        for next_column in ((x + 1, z), (x - 1, z), (x, z + 1), (x, z - 1)):
            if (next_column in open_columns and next_column not in visited
                    and abs(open_columns[next_column] - open_columns[(x, z)]) <= 1):
                visited.add(next_column)
                pending.append(next_column)
    if column(goal) not in visited:
        raise FixtureError("design has no open one-metre-step route")
    direct = [row for row in columns
              if start[0] < row["position"][0] < goal[0]
              and math.floor(row["position"][2]) == math.floor(start[2])]
    if not any(not row["occupiable"] or row["position"][1] - start[1] > 1 for row in direct):
        raise FixtureError("claimed obstruction does not intersect the direct path")
    if value["name"] == "long_distance_windows" and math.hypot(
        goal[0] - start[0], goal[2] - start[2]) < 200:
        raise FixtureError("long-distance scene is shorter than 200 m")



def speed(row: dict) -> float:
    velocity = row.get("velocity")
    if not isinstance(velocity, dict) or any(
        type(velocity.get(axis)) not in (int, float) or not math.isfinite(velocity[axis])
        for axis in ("x", "y", "z")):
        return math.inf
    return math.sqrt(sum(velocity[axis] ** 2 for axis in ("x", "y", "z")))



def correlated(row: dict, initial: dict) -> bool:
    return bool(row.get("backend") == "veloren" and row.get("session_id")
                and row["session_id"] == initial.get("session_id")
                and row.get("self_identity") and row["self_identity"] == initial.get("self_identity")
                and type(row.get("tick")) is int and row["tick"] >= 0
                and (row.get("coordinate_frame") or {}).get("frame") == "gameagent_y_up"
                and isinstance(row.get("position_y_up"), list)
                and len(row["position_y_up"]) == 3
                and all(type(v) in (int, float) and math.isfinite(v) for v in row["position_y_up"]))



def errors(row: dict, goal: list) -> tuple[float, float]:
    p = row["position_y_up"]
    return math.hypot(p[0] - goal[0], p[2] - goal[2]), abs(p[1] - goal[1])



def arrival_pair(before: dict, after: dict, initial: dict, goal: list) -> bool:
    return bool(
        correlated(before, initial) and correlated(after, initial)
        and after["tick"] > before["tick"]
        and after["elapsed_s"] - before["elapsed_s"] >= ARRIVAL["minimum_pair_seconds"]
        and all(errors(row, goal)[0] <= ARRIVAL["horizontal_m"]
                and errors(row, goal)[1] <= ARRIVAL["vertical_m"]
                and speed(row) <= ARRIVAL["settled_speed_m_s"] for row in (before, after))
    )

