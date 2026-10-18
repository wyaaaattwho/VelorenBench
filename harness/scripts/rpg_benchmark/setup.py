from __future__ import annotations

import asyncio
import math
import time
from contextlib import asynccontextmanager
from uuid import uuid4

from scripts.rpg_benchmark.scene import counts, item_id
from scripts.rpg_benchmark.native import NativeActionFailure, NativeRefusal


class ContinuousRouteFailure(RuntimeError):
    def __init__(self, admission: dict, observation: dict):
        self.admission, self.observation = admission, observation
        super().__init__(f"native continuous route failed: {observation['continuous_control']}")


async def action(actor, action_type: str, *, origin: str = "setup",
                 require_completed: bool = True, basis: dict | None = None, **arguments) -> dict:
    identifier = uuid4().hex
    timing = {key: arguments.pop(key) for key in ("duration_ticks", "timeout_ticks") if key in arguments}
    if basis is None:
        basis = await actor.request("observe", origin=origin)
    fence = {"generation": basis["continuous_control"]["generation"],
             "expected_session_id": basis["session_id"], "actor": basis["control_actor"]}
    receipt = await actor.request("step", {**timing, "control_fence": fence, "action": {
        "type": action_type, "action_id": identifier, **arguments}}, origin=origin)
    if receipt.get("action_id") != identifier:
        raise RuntimeError(f"uncorrelated native {action_type} receipt")
    if require_completed and receipt.get("status") != "completed":
        raise NativeActionFailure(action_type, receipt)
    return receipt


async def continuous_walk(actor, destination: list[float], *, origin: str) -> dict:
    basis = await actor.request("observe", origin=origin)
    owner = {
        "owner": "m1-native-route-calibration",
        "generation": basis["continuous_control"]["generation"] + 1, "sequence": 1,
        "expected_session_id": basis["session_id"], "actor": basis["control_actor"],
        "expected_protection_generation": basis["continuous_control"]["protection_generation"],
    }
    receipt = await actor.request("set_control_state", {
        **owner, "movement": [0, 0], "speed": 1, "target": None,
        "primary": False, "block": False, "movement_intent": "destination",
        "destination": destination}, origin=origin)
    if receipt.get("status") != "applied":
        raise RuntimeError(f"native continuous route was not applied: {receipt}")
    timeout_seconds = 300 if origin == "setup" else 60
    deadline = time.monotonic() + timeout_seconds
    progress = []
    try:
        while time.monotonic() < deadline:
            observation = await actor.request("observe", origin=origin)
            point = position(observation)
            state = observation["continuous_control"]
            if state["generation"] != owner["generation"]:
                raise RuntimeError(f"native continuous route ownership changed: {state}")
            route = state.get("local_route") or {}
            motion = state.get("motion_feedback") or {}
            if (state.get("local_route_failure") or route.get("blocked_edge")
                    or str(motion.get("status", "")).startswith(("control_motion_", "control_route_"))):
                raise ContinuousRouteFailure(receipt, observation)
            if not progress or math.dist(point, progress[-1]["position"]) > .02:
                progress.append({"position": list(point), "tick": observation.get("tick"),
                                 "horizontal_distance": math.dist(point[:2], destination[:2])})
            if math.hypot(point[0] - destination[0], point[1] - destination[1]) <= .7 and abs(
                    point[2] - destination[2]) <= 1:
                return {"admission": receipt, "achieved": observation,
                        "progress": progress,
                        "primitive": "native-continuous-loaded-astar-destination"}
            await asyncio.sleep(.1)
        raise TimeoutError(
            f"native continuous route operation exceeded {timeout_seconds}-second RPC-equivalent deadline")
    finally:
        current = await actor.request("observe", origin=origin)
        if current["continuous_control"]["generation"] != owner["generation"]:
            raise RuntimeError("native calibration ownership retired before release")
        released = await actor.request("release_control_state", {**owner, "sequence": 2}, origin=origin)
        if released.get("status") != "released":
            raise RuntimeError(f"native calibration control was not released: {released}")


async def command(admin, name: str, *arguments) -> dict:
    receipt = await action(admin, "server_command", name=name,
                           args=[str(value) for value in arguments])
    await asyncio.sleep(.08)
    events = await admin.request("events")
    admin.journal.write(origin="setup", command_events=events)
    errors = [row for row in events if (row.get("data") or {}).get("kind") == "command_error"]
    if errors:
        raise RuntimeError(f"native fixture command error: {name}: {errors}")
    return receipt


async def wait_observation(actor, predicate, reason: str, timeout: float = 15) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observation = await actor.request("observe")
        if predicate(observation):
            return observation
        await asyncio.sleep(.1)
    raise RuntimeError(f"native setup did not establish {reason}")


async def ready_observation(actor) -> dict:
    def ready(row: dict) -> bool:
        cells = row["local_geometry"]["geometry"]["cells"]
        return (row["on_ground"] is True and bool(cells)
                and all(cell["occupancy"] != "unknown" for cell in cells)
                and math.sqrt(sum(value * value for value in row["velocity"])) < .1)
    observation = await wait_observation(actor, ready, "loaded, grounded and settled actor window", timeout=30)
    await qualify(actor, position(observation))
    return observation


def require_living(observation: dict) -> None:
    survival = observation.get("survival") or {}
    health = survival.get("health") or {}
    current = health.get("current")
    if (survival.get("dead") is True or health.get("dead") is True
            or type(current) not in (int, float) or not math.isfinite(current) or current <= 0):
        raise ValueError("native fixture opening actor is dead or lacks positive finite health")


def require_uninjured_start(observation: dict) -> None:
    require_living(observation)
    if observation["survival"]["health"]["current"] != 100 or observation["survival"]["buffs"]:
        raise RuntimeError("craft fixture initialization requires 100 HP and no residual buffs")


async def isolate_initial_spawn(admin, actor) -> dict:
    before = await actor.request("observe")
    require_uninjured_start(before)
    author = await admin.request("observe")
    if math.dist(position(author), position(before)) > 16:
        raise RuntimeError("initial spawn isolation administrator is not near the measured actor")
    receipt = await command(admin, "kill_npcs", 96)
    after = await wait_observation(actor, lambda row: not any(
        entity.get("alias") is None and (entity.get("health") or {}).get("current", 0) > 0
        for entity in row["nearby_entities"]), "initial isolation native NPC removal")
    require_uninjured_start(after)
    if any(row.get("alias") is None and (row.get("health") or {}).get("current", 0) > 0
           for row in after["nearby_entities"]):
        raise RuntimeError("initial spawn retains uncontrolled living nearby NPCs")
    result = {"before": before, "after": after, "receipt": receipt, "radius_m": 96,
              "expected_health": 100, "player_health_reset": False, "player_buff_added": False,
              "scope": "C2 setup-only initial spawn isolation before cleanup and scene preparation; native loot retained"}
    admin.journal.write(origin="setup", event="craft_initial_spawn_isolated",
                        actor_uid=after["self_identity"]["uid"], expected_health=100,
                        native_tick=after["tick"],
                        simulation_time_observed=after["survival"]["buff_clock"]["now"],
                        player_health_reset=False, player_buff_added=False)
    return result


def supplies(fixture: dict) -> list[dict]:
    result = list(fixture.get("supplies", []))
    for name in ("craft", "travel"):
        result.extend(fixture.get(name, {}).get("supplies", []))
    return result


async def validate_opening(admin, actor, fixture: dict) -> dict:
    if fixture.get("case") in {"T10", "T11", "T12"}:
        from .npc_contracts import validate_opening as validate_contract
        return await validate_contract(actor, fixture)
    if "recovery_contract" in fixture:
        from .crafting_recovery import require_injured_start
        def qualify_initial(row):
            require_injured_start(row, fixture["recovery_contract"])
    else:
        qualify_initial = require_uninjured_start if fixture.get("initialization") is not None else require_living
    opening = await actor.request("observe")
    fixture["opening_admission_observation"] = opening
    qualify_initial(opening)
    original = fixture["opening"]
    if (opening["session_id"], opening["self_identity"]) != (original["session_id"], original["self_identity"]):
        raise RuntimeError("fixture actor identity changed before opening admission")
    for supply in supplies(fixture):
        if not supply["required"]:
            continue
        witness = opening
        entity = next((row for row in witness["nearby_entities"] if row["uid"] == supply["uid"]), None)
        if entity is None:
            await goto(admin, admin, supply["entity"]["position"], settle=False)
            witness = await wait_observation(
                admin, lambda row: any(entity["uid"] == supply["uid"] for entity in row["nearby_entities"]),
                f"required opening pickup UID {supply['uid']}")
            entity = next(row for row in witness["nearby_entities"] if row["uid"] == supply["uid"])
        if item_id(entity.get("pickup") or {}) != supply["item"] or entity["pickup"]["amount"] < supply["amount"]:
            raise RuntimeError(f"required opening pickup UID {supply['uid']} has changed item/amount")
        supply["opening_uid_check"] = {
            "uid": supply["uid"], "entity": entity, "native_tick": witness["tick"],
            "simulation_time_observed": witness["survival"]["buff_clock"]["now"],
            "witness_session_id": witness["session_id"], "wall_monotonic": time.monotonic(),
            "scope": "fresh native UID/item/amount visibility after setup; not exact ECS spawn time"}
    opening = await actor.request("observe")
    fixture["opening_admission_observation"] = opening
    qualify_initial(opening)
    if (opening["session_id"], opening["self_identity"]) != (original["session_id"], original["self_identity"]):
        raise RuntimeError("fixture actor identity changed during opening admission")
    fixture["opening"] = opening
    for name in ("craft", "fight", "travel"):
        if name in fixture:
            fixture[name]["opening"] = opening
    fixture["opening_validation"] = {
        "alive": True, "native_tick": opening["tick"],
        "simulation_time_observed": opening["survival"]["buff_clock"]["now"],
        "required_drop_uids": [row["uid"] for row in supplies(fixture) if row["required"]]}
    return opening


def position(observation: dict) -> list[float]:
    point = observation["position"]
    if not isinstance(point, list) or len(point) != 3:
        raise ValueError("native XYZ position unavailable")
    return point


async def goto(admin, actor, point: list[float], *, settle: bool = True,
               horizontal_tolerance: float = .2) -> dict:
    values = ("sudo", actor.name, "goto", *point) if actor is not admin else ("goto", *point)
    await command(admin, *values)
    return await wait_observation(
        actor, lambda row: math.hypot(position(row)[0] - point[0],
                                     position(row)[1] - point[1]) < horizontal_tolerance
        and (not settle or abs(position(row)[2] - point[2]) < .2),
        f"goto {point}", timeout=10)


def slot(row: dict) -> dict:
    if "slot" in row:
        return {"slot": row["slot"]}
    return {key: row[key] for key in ("loadout_idx", "slot_idx")}


async def gift(admin, actor, name: str, amount: int) -> dict:
    before = counts(await actor.request("observe")).get(name, 0)
    if actor is admin:
        await command(admin, "give_item", name, amount)
    else:
        await command(admin, "sudo", actor.name, "give_item", name, amount)
    return await wait_observation(actor, lambda row: counts(row).get(name, 0) >= before + amount,
                                  f"gift {name} x{amount}")


async def learn(admin, actor, group: str) -> dict:
    observation = await gift(admin, actor, group, 1)
    row = next(row for row in observation["inventory"]["items"] if item_id(row["item"]) == group)
    receipt = await action(actor, "use_item", **slot(row))
    await wait_observation(actor, lambda row: counts(row).get(group, 0) == 0, "recipe group use")
    return receipt


async def clean_inventory(admin, actor) -> dict:
    # Unwanted starter supplies are dropped far from the measured scene.
    # Cache qualified footing, not a transient descending spawn/teleport pose.
    opening = await ready_observation(actor)
    here = position(opening)
    await goto(admin, actor, [here[0] + 100, here[1] + 100, here[2] + 100], settle=False)
    removed = []
    while (observation := await actor.request("observe"))["inventory"]["items"]:
        row = observation["inventory"]["items"][0]
        removed.append({"item": row["item"], "receipt": await action(actor, "drop", **slot(row))})
    await goto(admin, actor, here)
    await command(admin, "sudo", actor.name, "skill_preset", "clear")
    await command(admin, "sudo", actor.name, "buff", "clear")
    await command(admin, "sudo", actor.name, "repair_equipment", "true")
    return {"before": opening, "removed": removed, "remaining": await actor.request("observe"),
            "removed_drop_expiry_game_seconds": 300, "removed_drop_region": "100m offset from spawn"}


async def rock(admin, point: list[float], *, base_voxel: bool = True) -> dict:
    author = [point[0] + .5, point[1] + .5, point[2] + (.95 if base_voxel else 0)]
    await action(admin, "server_command", name="goto", args=[str(value) for value in author])
    acknowledged = await wait_observation(
        admin, lambda row: [math.floor(value) for value in position(row)] == point
        and math.dist(position(row), author) < (.4 if base_voxel else .05)
        and (base_voxel or row["on_ground"] is True
             and math.sqrt(sum(value * value for value in row["velocity"])) <= .1),
        f"authoring pose inside exact Rock voxel {point}", timeout=3)
    # Use the just-checked native fence without another round trip while the base pose is airborne.
    receipt = await action(admin, "server_command", basis=acknowledged,
                           name="make_block", args=["Rock", "160", "160", "160"])
    top = [point[0] + .5, point[1] + .5, point[2] + 1]
    await goto(admin, admin, [top[0], top[1], top[2] + (0 if base_voxel else .5)], settle=False)
    settled = await wait_observation(
        admin, lambda row: math.dist(position(row), top) < .05 and row["on_ground"] is True
        and math.sqrt(sum(value * value for value in row["velocity"])) <= .1,
        f"settled atop authored Rock voxel {point}", timeout=3)
    query = await terrain(admin, top)
    if query.get("status") != "complete" or not any(
            row["occupiable"] is True and math.dist(row["standing_position"], top) < .01
            and row["support"]["position"] == point
            and row["support"]["solid"] is True and row["support"]["kind"] == "Rock"
            for row in query.get("support_candidates", [])):
        raise RuntimeError(f"authored Rock voxel lacks exact native support readback: {point}")
    def pose(row):
        return {key: row[key] for key in ("position", "velocity", "on_ground", "tick",
                                         "session_id", "self_identity")}
    result = {"voxel": point, "base_voxel": base_voxel,
              "authoring_pose": pose(acknowledged), "settled_on_top": pose(settled),
              "action_id": receipt["action_id"], "terrain": query}
    admin.journal.write(origin="setup", event="authored_rock_voxel", **result)
    return result


async def terrain(actor, point: list[float]) -> dict:
    return await actor.request("inspect_terrain", {"position": point})


async def qualify(actor, point: list[float], *, allow_ground_level_wood: bool = False) -> dict:
    query = await terrain(actor, point)
    if query.get("status") == "complete" and any(
        row.get("occupiable") is True and math.dist(row["standing_position"], point) < .3
        for row in query.get("support_candidates", [])):
        return query
    observation = await actor.request("observe")
    adjacent = []
    wood_support = ground_support = None
    if observation["on_ground"] is True and math.dist(position(observation), point) < .3:
        for dx, dy in ((.65, 0), (-.65, 0), (0, .65), (0, -.65),
                       (.46, .46), (.46, -.46), (-.46, .46), (-.46, -.46)):
            neighbor = [point[0] + dx, point[1] + dy, point[2]]
            readback = await terrain(actor, neighbor)
            adjacent.append(readback)
            if readback.get("status") == "complete" and any(
                row["occupiable"] is True and abs(row["standing_position"][2] - point[2]) < .3
                and row["support"]["kind"] not in ("Leaves", "Wood")
                for row in readback["support_candidates"]
            ):
                return {"center_query": query, "adjacent_queries": adjacent,
                        "physical_observation": observation,
                        "scope": "fresh native on_ground plus loaded support within the fixed Human footprint; not a global walkability certificate"}
            if allow_ground_level_wood and readback.get("status") == "complete":
                for row in readback["support_candidates"]:
                    if row["occupiable"] is not True:
                        continue
                    height = row["standing_position"][2]
                    kind = row["support"]["kind"]
                    if kind == "Wood" and abs(height - point[2]) < .3:
                        wood_support = row
                    elif kind not in ("Leaves", "Wood", "Air") and 0 <= point[2] - height <= 1:
                        ground_support = row
            if wood_support is not None and ground_support is not None:
                return {"center_query": query, "adjacent_queries": adjacent,
                        "physical_observation": observation, "wood_support": wood_support,
                        "nearby_ground_support": ground_support,
                        "scope": "fresh native on_ground and loaded occupiable Wood within the Human footprint, with non-canopy ground at most one block below; ground-level obstacle, not canopy or global walkability"}
    raise RuntimeError(f"native terrain lacks physical/geometry footing at {point}")


async def site(admin, actor, terrain_track: str, placements: dict,
               size: tuple[int, int] = (22, 18)) -> dict:
    natural = await actor.request("observe")
    if terrain_track == "natural":
        here = await settled_point(admin, placements["natural_origin"])
        await goto(admin, actor, here)
        await goto(admin, admin, here)
        isolation_receipt = await command(admin, "kill_npcs", 96)
        await asyncio.sleep(.5)
        remaining = await actor.request("observe")
        if any(row.get("alias") is None and (row.get("health") or {}).get("current", 0) > 0
               for row in remaining["nearby_entities"]):
            raise RuntimeError("natural fixture retains uncontrolled living nearby NPCs")
        return {"start": here, "origin": [math.floor(v) for v in here],
                "terrain": await qualify(actor, here), "constructed": False,
                "ambient_isolation": {"receipt": isolation_receipt, "before": natural,
                                      "after": remaining, "radius_m": 96,
                                      "native_loot_retained_as_visible_distractors": True}}
    origin = placements["flat_origin"]
    construction = await author_voxels(admin, [
        [origin[0] + x, origin[1] + y, origin[2]]
        for x in range(size[0]) for y in range(size[1])])
    start = [origin[0] + 2.5, origin[1] + size[1] / 2 + .5, origin[2] + 1]
    await goto(admin, actor, start)
    return {"start": start, "origin": origin, "dimensions_xy": list(size),
            "construction": construction,
            "terrain": await qualify(actor, start), "constructed": True}


@asynccontextmanager
async def fixture_authoring(admin, low, high):
    """Owned privileged hold, including the first voxel; restore on every exit."""
    initial = await ready_observation(admin)
    controls = initial["character_controls"]
    if controls.get("fixture_authoring") is not None:
        raise RuntimeError("fixture administrator already has an authoring owner")
    enabled_before = (controls.get("can_build") or {}).get("enabled") is True
    area = "benchmark_" + uuid4().hex[:12]
    owner = uuid4().hex
    attempted_area = attempted_permission = attempted_hold = False
    errors = []
    try:
        attempted_area = True
        await command(admin, "area_add", area, "build", low[0], high[0], low[1], high[1], low[2], high[2])
        attempted_permission = True
        await command(admin, "permit_build", area)
        if not enabled_before:
            await command(admin, "build")
        await wait_observation(admin, lambda row: (row["character_controls"].get("can_build") or {}).get(
            "enabled") is True, "administrator-only native build permission")
        attempted_hold = True
        await command(admin, "fixture_authoring", owner, "begin")
        await wait_observation(admin, lambda row: (
            row["character_controls"].get("fixture_authoring") or {}).get("owner") == owner,
            "server-authorized fixture administrator hold")
        yield owner
    except BaseException as error:
        errors.append(error)
    finally:
        cleanup_errors = []
        async def release():
            await command(admin, "fixture_authoring", owner, "end")
            await wait_observation(admin, lambda row: row["character_controls"].get(
                "fixture_authoring") is None, "fixture administrator hold released")
        async def restore_permission():
            current = await ready_observation(admin)
            if ((current["character_controls"].get("can_build") or {}).get("enabled") is True) != enabled_before:
                await command(admin, "build")
            await command(admin, "revoke_build", area)
        for attempted, cleanup in (
            (attempted_hold, release),
            (attempted_permission, restore_permission),
            (attempted_area, lambda: command(admin, "area_remove", area, "build")),
        ):
            if attempted:
                try:
                    await cleanup()
                except BaseException as error:
                    errors.append(error)
                    cleanup_errors.append(str(error))
        admin.journal.write(origin="setup", event="fixture_authoring_cleanup", owner=owner,
                            area=area, cleanup_complete=not cleanup_errors, cleanup_errors=cleanup_errors)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("fixture authoring and/or scoped cleanup failed", errors)


async def author_voxels(admin, voxels: list[list[int]]) -> list[dict]:
    low = [min(point[i] for point in voxels) for i in range(3)]
    high = [max(point[i] for point in voxels) + 1 for i in range(3)]
    results = []
    async with fixture_authoring(admin, low, high) as owner:
        anchor = None
        for point in voxels:
            top = [point[0] + .5, point[1] + .5, point[2] + 1]
            # Reuse a stable observation anchor while it remains comfortably
            # inside the unchanged native 10m horizontal / 2m vertical bounds.
            if anchor is None or math.dist(anchor[:2], top[:2]) > 8 or abs(anchor[2] - top[2]) > 1.5:
                anchor = [top[0], top[1], top[2] + .8]
                await command(admin, "fixture_authoring", owner, "move", *anchor)
                await wait_observation(admin, lambda row: (
                    row["character_controls"].get("fixture_authoring") or {}).get("owner") == owner
                    and math.dist(position(row), anchor) < .01,
                    "stable fixture authoring observation anchor")
            loaded = await construction_readback(admin, point, phase="loaded-input", owner=owner)
            receipt = await action(admin, "build_block", operation="place", position=point,
                                   expected_kind="Air", new_kind="Rock", color_rgb=[160, 160, 160])
            support = await construction_readback(admin, point, phase="effect", receipt=receipt, owner=owner)
            # No standing teleport: a held admin is not physical support proof.
            # Actor opening qualification below the family boundary is unchanged.
            result = {"voxel": point, "receipt": receipt, "loaded_input": loaded,
                      "support_before_standing": support, "terrain": support,
                      "administrator_hold": {"owner": owner, "position": list(anchor),
                                             "not_grounded_evidence": True}}
            results.append(result)
            admin.journal.write(origin="setup", event="explicit_authored_rock_voxel", **result)
    return results


async def construction_readback(admin, point, *, phase, receipt=None, timeout=15, owner=None):
    """Read-only readiness/effect polling; never retry an unknown mutation."""
    top = [point[0] + .5, point[1] + .5, point[2] + 1]
    deadline = time.monotonic() + timeout
    last = None
    reason = "no readback"

    def fail(cause):
        evidence = {"voxel": point, "phase": phase, "cause": cause,
                    "receipt": receipt, "last_terrain": last,
                    "scope": "native client loaded-terrain readback, not a server acknowledgement"}
        admin.journal.write(origin="setup", event="construction_readback_failed", **evidence)
        error = RuntimeError(f"construction {phase} lacks exact native Rock readback at {point}: {cause}")
        error.construction = evidence
        raise error

    while time.monotonic() < deadline:
        try:
            last = await terrain(admin, top)
        except NativeRefusal as error:
            # Includes leaving the bounded column while falling. Do not teleport
            # back onto unconfirmed support, resubmit, or relabel the refusal.
            fail(f"native query refused: {error}")
        status = last.get("status")
        if owner is not None and (
                last.get("binding", {}).get("readiness", {}).get("fixture_authoring") or {}).get("owner") != owner:
            fail("fixture administrator authoring ownership changed")
        candidates = last.get("support_candidates", [])
        if phase == "loaded-input":
            if (status in {"complete", "no_candidate_in_window"}
                    and last.get("vertical_scan", {}).get("all_cells_loaded") is True):
                return last
            reason = f"target/clearance not loaded: {status}"
        elif phase == "effect":
            if status == "complete" and any(
                    row.get("occupiable") is True and row["support"]["position"] == point
                    and row["support"]["kind"] == "Rock" and row["support"]["solid"] is True
                    for row in candidates):
                return last
            wrong = [row["support"] for row in candidates
                     if row.get("support", {}).get("position") == point
                     and row["support"].get("kind") not in {"Air", "Rock"}]
            if wrong:
                fail(f"conflicting target block: {wrong}")
            reason = f"submitted effect still unconfirmed: {status}"
        else:
            raise ValueError("unknown construction readback phase")
        if status not in {"complete", "no_candidate_in_window", "unloaded_cells", "unknown_cells"}:
            fail(f"invalid native terrain readback status: {status}")
        await asyncio.sleep(.1)
    fail(reason)


async def settled_point(admin, point: list[float]) -> list[float]:
    point = [math.floor(point[0]) + .5, math.floor(point[1]) + .5, point[2]]
    await goto(admin, admin, [point[0], point[1], point[2] + 20], settle=False,
               horizontal_tolerance=5)
    deadline = time.monotonic() + 20
    previous = None
    while time.monotonic() < deadline:
        await asyncio.sleep(.25)
        observation = await admin.request("observe")
        current = position(observation)
        if previous is not None and math.dist(current, previous) < .02 and observation["on_ground"] is True:
            query = await terrain(admin, current)
            candidates = [row["standing_position"] for row in query.get("support_candidates", [])
                          if row.get("occupiable") is True
                          and row["support"]["kind"] not in ("Leaves", "Wood")]
            if query.get("status") != "complete" or not candidates:
                return await ground_below_canopy(admin, current)
            await qualify(admin, current)
            return current
        previous = current
    raise RuntimeError(f"native natural placement did not settle: {point}")


async def ground_below_canopy(admin, landing: list[float]) -> list[float]:
    # A sky landing can select leaves, not the walking surface below them.
    for dy in (0, 2, -2, 4, -4):
        for dz in range(0, 81, 4):
            probe = [landing[0], landing[1] + dy, landing[2] - dz]
            await goto(admin, admin, probe, settle=False, horizontal_tolerance=5)
            actual_probe = position(await admin.request("observe"))
            query = await terrain(admin, actual_probe)
            if query.get("status") not in ("complete", "no_candidate_in_window"):
                raise RuntimeError("ground-column calibration encountered unloaded terrain")
            candidates = [row["standing_position"] for row in query["support_candidates"]
                          if row["occupiable"] is True and row["support"]["kind"] not in ("Leaves", "Wood", "Air")]
            if candidates:
                selected = max(candidates, key=lambda point: point[2])
                await goto(admin, admin, selected, settle=False, horizontal_tolerance=5)
                await asyncio.sleep(.3)
                actual = position(await admin.request("observe"))
                await qualify(admin, actual)
                admin.journal.write(origin="setup", ground_below_canopy={
                    "sky_landing": landing, "ground": actual,
                    "search_geometry": "one local column and ±2/4m adjacent columns; up to 80m below canopy"})
                return actual
    raise RuntimeError(f"no occupiable walking surface below native canopy: {landing}")


async def station(admin, point: list[float], sprite: str, terrain_track: str) -> dict:
    if terrain_track == "natural":
        point = await settled_point(admin, point)
    await goto(admin, admin, point)
    receipt = await command(admin, "make_sprite", sprite)
    await asyncio.sleep(.3)
    observed = await admin.request("observe")
    available = observed.get("nearby_interactable_sprites", [])
    if not any(row.get("sprite") == sprite for row in available):
        raise RuntimeError(f"station not visible in native observation: {sprite}: {available}")
    approach = [point[0] - 2, point[1], point[2]]
    if terrain_track == "natural":
        approach = await settled_point(admin, approach)
    await goto(admin, admin, approach)
    return {"sprite": sprite, "position": point, "receipt": receipt,
            "approach": approach, "readback": available, "terrain": await qualify(admin, approach)}


async def drop_supply(admin, point: list[float], name: str, amount: int,
                      terrain_track: str, *, required: bool = True) -> dict:
    if terrain_track == "natural":
        point = await settled_point(admin, point)
    await goto(admin, admin, point)
    observation = await gift(admin, admin, name, amount)
    row = next(row for row in observation["inventory"]["items"] if item_id(row["item"]) == name)
    existing = {row["uid"] for row in observation.get("nearby_entities", [])}
    submitted_wall = time.monotonic()
    receipt = await action(admin, "drop", **slot(row))
    observed = await wait_observation(admin, lambda value: any(
        row.get("uid") not in existing and (row.get("pickup") or {}).get("amount") == amount
        and item_id(row.get("pickup") or {}) == name for row in value.get("nearby_entities", [])),
        f"native pickup {name} x{amount}")
    pickup = next(row for row in observed["nearby_entities"]
                  if row.get("uid") not in existing and item_id(row.get("pickup") or {}) == name)
    created_tick = observed["tick"]
    created_game_time = observed["survival"]["buff_clock"]["now"]
    observed_wall = time.monotonic()
    await asyncio.sleep(.8)
    settled = await admin.request("observe")
    current = next((row for row in settled["nearby_entities"] if row.get("uid") == pickup["uid"]), None)
    if current is None or item_id(current.get("pickup") or {}) != name:
        raise RuntimeError("native supply disappeared during setup settling")
    return {"item": name, "amount": amount, "uid": pickup["uid"], "entity": current,
            "required": required,
            "initial_entity": pickup, "settled_tick": settled["tick"],
            "created_game_time_observed": created_game_time,
            "expiry_game_time_estimate": created_game_time + 300,
            "clock_limit": "client synchronized physics time at first observation, not native Object spawned_at",
            "created_tick": created_tick, "creation_wall_interval": [submitted_wall, observed_wall],
            "expires_after_game_seconds": 300, "replenish_measured": False, "receipt": receipt}
