#!/usr/bin/env python3
"""Isolated Copilot SDK agent for one externally owned native RPG episode."""
from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import math
import os
import re
import signal
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING, get_args
from uuid import uuid4

if __package__:
    from .rpg_benchmark import output_paths, rpg_interactions
    from .rpg_benchmark.player_types import Invocation as ToolInvocation, Result as ToolResult, Tool
else:
    from rpg_benchmark import output_paths, rpg_interactions
    from rpg_benchmark.player_types import Invocation as ToolInvocation, Result as ToolResult, Tool

if TYPE_CHECKING:
    from copilot import CopilotSession, PermissionRequest
    from copilot.session import ReasoningEffort
    from copilot.session_events import SessionEvent


OUTPUT_ROOT = Path.home() / ".local/state/rpg-benchmark/sdk-adapter"
TRACK = "raw-native-reference"
INTERFACE_VERSION = "rpg-sdk-native-contract/2"
NATIVE_PROTOCOL_VERSION = "0.2"
PINNED_BRIDGE_SHA256 = "a32af6338d712fa16f8af2f4c363f3bc7980de8a04c8671a20fa8b8d17b572e3"
READ_COMMANDS = frozenset({
    "observe", "events", "inspect_terrain", "inspect_map", "goal_recipes", "health", "ping",
})
CONTROL_COMMANDS = frozenset({"step", "set_control_state", "release_control_state"})
DESCRIPTIONS = {
    "observe": (
        "Read the native observation, including current_action.action_id/status, control_actor, "
        "continuous_control generation/protection_generation, inventory, entities and recipes."
    ),
    "events": (
        "Read native events. With after_seq, read a non-draining cursor batch (limit 1..256). "
        "Without after_seq, drain buffered events; limit/filter are not supported in that mode."
    ),
    "inspect_terrain": (
        "Inspect one loaded voxel column at native Z-up [x,y,z], within 10 horizontal metres "
        "and 2 vertical metres of the actor. No global terrain query or route selection."
    ),
    "inspect_map": (
        "Read the normal player's map. bounds=[min_x,min_y,max_x,max_y], min inclusive/max "
        "exclusive; optional cell_stride 1..65535, marker_offset for 16-marker pages. "
        "Maximum raster 48x48; this is visual map data, not walkability."
    ),
    "goal_recipes": (
        "Query native recipes/dependencies by exact output_item_id, offset and page_size 1..32. "
        "Future manifest options are distinct from character_known; this does not unlock recipes."
    ),
    "health": "Read native bridge health.",
    "ping": "Ping the native bridge.",
    "step": (
        "Submit one model-selected native action, not a task script. Returns a LOCAL submission "
        "handle immediately, NOT a native success receipt. Use game_result to collect its "
        "unchanged response; admitted observation tools remain available while it runs. "
        "Consult game_contract for this adapter's pin-grounded schema. Native coordinates are Z-up [x,y,z]; no "
        "GameAgent coordinate conversion. Omitted action_id is generated once and returned; "
        "explicit IDs are preserved. Never resubmit an uncertain/timed-out action with a new ID. "
        "stop cancels the current action and releases inputs, not a selected historical ID. "
        "Native protection/ownership refusals remain authoritative; no automatic fence refresh. "
        "Administration and slash chat are forbidden."
    ),
    "set_control_state": (
        "Replace native fine-grained controls with explicit owner identity. There is no acquire "
        "command: a higher generation takes ownership; same-generation updates require same "
        "owner and increasing sequence. Copy actor/session and current ownership from fresh public "
        "control tokens: structured/hybrid observe or image-only frame actuator metadata. "
        "After protection, set expected_protection_generation to the observed protection_generation. "
        "Movement is native world [x,y] in the unit disk. Applied is not gameplay success. "
        "step is busy while owned."
    ),
    "release_control_state": (
        "Release and retire an owner generation using its identity and a newer sequence; "
        "native actor/session/protection checks apply. Does not cancel a running step."
    ),
}


def redact(value: Any) -> Any:
    """Retain event/envelope structure, excluding authentication material."""
    if isinstance(value, dict):
        return {
            key: ("[REDACTED]" if re.search(
                r"authorization|headers|password|secret|api[_-]?key|github[_-]?token|"
                r"access[_-]?token|refresh[_-]?token|cookie", key, re.I
            ) else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)\b(?:proxy-)?authorization\s*[:=]\s*[^\r\n]+",
                       "Authorization: [REDACTED]", value)
        value = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [REDACTED]", value)
        return re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b",
                      "[REDACTED]", value)
    return value


class Evidence:
    def __init__(self, path: Path):
        self.stream = path.open("x", encoding="utf-8")

    def write(self, kind: str, **row: Any) -> None:
        self.stream.write(json.dumps(redact({
            "wall_monotonic": time.monotonic(), "kind": kind, **row,
        }), allow_nan=False) + "\n")
        self.stream.flush()

    def close(self) -> None:
        self.stream.close()


class ProtocolError(RuntimeError):
    pass


class NativeError(RuntimeError):
    def __init__(self, response: dict[str, Any]):
        self.response = response
        super().__init__(f"native refusal: {response['error']}")


def validate_request(command: str, arguments: dict[str, Any], *, renderer: bool = False) -> None:
    if command not in READ_COMMANDS | CONTROL_COMMANDS | {"close"}:
        raise ValueError(f"command is not allowlisted: {command}")
    validate_schema(arguments, tool_schema(command, renderer=renderer))
    if command == "step":
        action = arguments["action"]
        if action["type"] == "visual_input":
            from scripts.rpg_benchmark.visual_ui import validate
            validate(action)
        if action["type"] in rpg_interactions.NAMES:
            rpg_interactions.validate(action)
        if action["type"] == "chat":
            if action["message"].lstrip().startswith("/"):
                raise ValueError("slash chat is forbidden")
        if action["type"] == "give_up" and arguments.keys() - {"action", "control_fence"}:
            raise ValueError("give_up accepts no duration_ticks or timeout_ticks")
        if action["type"] != "craft" and action["type"] != "stop" and (
                arguments.get("timeout_ticks", 600) < arguments.get("duration_ticks", 60)):
            raise ValueError("timeout_ticks must be >= duration_ticks (native defaults: 600 and 60)")
        if action["type"] == "move_vector" and sum(v * v for v in action["direction"]) < .0001:
            raise ValueError("action.direction must have squared length >= 0.0001; native normalizes it")
        if action["type"] in {"use_item", "drop", "equip"}:
            selectors = sum((
                "slot" in action,
                "loadout_idx" in action or "slot_idx" in action,
                "equip_slot" in action and action["type"] == "drop",
            ))
            if selectors != 1 or (("loadout_idx" in action) != ("slot_idx" in action)):
                raise ValueError("select exactly one inventory slot or loadout_idx/slot_idx pair"
                                 + (" or equip_slot" if action["type"] == "drop" else ""))
            if action.get("split") and "equip_slot" in action:
                raise ValueError("split drop requires an inventory slot, not equip_slot")
    elif command == "events" and "limit" in arguments and "after_seq" not in arguments:
        raise ValueError("events.limit requires after_seq; without a cursor native drains all events")
    elif command == "inspect_map" and "bounds" in arguments:
        a, b, c, d = arguments["bounds"]
        if a >= c or b >= d:
            raise ValueError("bounds must be [min_x,min_y,max_x,max_y] with positive area")
    elif command == "set_control_state":
        validate_controls(arguments)
    json.dumps(arguments, allow_nan=False)


class NativeClient:
    """Concurrent ID-correlated NDJSON attachment; never owns a native process."""

    def __init__(self, socket: Path, evidence: Evidence, rpc_timeout: float = 60,
                 request_validator=validate_request):
        self.socket, self.evidence, self.rpc_timeout = socket, evidence, rpc_timeout
        self.ready: dict[str, Any] = {}
        self.writer: asyncio.StreamWriter | None = None
        self.reader_task: asyncio.Task[None] | None = None
        self.pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.tasks: set[asyncio.Task[dict[str, Any]]] = set()
        self.abandoned: set[str] = set()
        self.failure: asyncio.Future[Exception] = asyncio.get_running_loop().create_future()
        self.closing = False
        self.request_validator = request_validator

    async def connect(self) -> None:
        reader, self.writer = await asyncio.open_unix_connection(
            str(self.socket), limit=16 * 1024 * 1024)
        try:
            row = await asyncio.wait_for(self._row(reader), self.rpc_timeout)
            if (row.get("event") != "ready" or not isinstance(row.get("session_id"), str)
                    or not row["session_id"]
                    or type(row.get("protocol_version")) not in {str, int}
                    or row.get("protocol_version") in {"", 0}
                    or type(row.get("uid")) not in {str, int}):
                raise ProtocolError("expected native ready identity/session/protocol envelope")
            self.ready = row
            if row["protocol_version"] != NATIVE_PROTOCOL_VERSION:
                raise ProtocolError(f"adapter contract requires native protocol {NATIVE_PROTOCOL_VERSION}")
            self.evidence.write("native_ready", envelope=row)
            self.reader_task = asyncio.create_task(self._read(reader))
        except BaseException:
            self.writer.close()
            await self.writer.wait_closed()
            self.writer = None
            raise

    @staticmethod
    async def _row(reader: asyncio.StreamReader) -> dict[str, Any]:
        line = await reader.readline()
        if not line:
            raise ProtocolError("episode socket closed")
        row = json.loads(line, parse_constant=lambda value: _invalid_json_constant(value))
        if not isinstance(row, dict):
            raise ProtocolError("NDJSON envelope is not an object")
        return row

    async def _read(self, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                row = await self._row(reader)
                self.evidence.write("native_response", envelope=row)
                if (row.get("type") != "response"
                        or row.get("session_id") != self.ready["session_id"]
                        or type(row.get("protocol_version")) is not type(self.ready["protocol_version"])
                        or row.get("protocol_version") != self.ready["protocol_version"]
                        or not isinstance(row.get("id"), str)
                        or type(row.get("ok")) is not bool
                        or ("result" if row.get("ok") else "error") not in row):
                    raise ProtocolError("invalid native response identity/envelope")
                request_id = row["id"]
                future = self.pending.get(request_id)
                if future is not None and not future.done():
                    future.set_result(row)
                elif request_id in self.abandoned:
                    self.abandoned.remove(request_id)
                    self.evidence.write("late_native_response", request_id=request_id)
                else:
                    raise ProtocolError(f"unknown or duplicate response ID: {request_id}")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not self.closing:
                self.evidence.write("transport_error", error=error_record(error))
                if not self.failure.done():
                    self.failure.set_result(error)
                for future in self.pending.values():
                    if not future.done():
                        future.set_exception(error)

    def submit(self, command: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], asyncio.Task]:
        self.request_validator(command, arguments)
        if self.closing or not self.ready or self.writer is None:
            raise ProtocolError("native attachment is not open")
        if self.failure.done():
            raise self.failure.result()
        arguments = json.loads(json.dumps(arguments, allow_nan=False))
        if command == "step" and "action_id" not in arguments["action"]:
            arguments["action"]["action_id"] = uuid4().hex
        request = {"id": uuid4().hex, "protocol_version": self.ready["protocol_version"],
                   "cmd": command, "args": arguments}
        task = asyncio.create_task(self._exchange(request))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return request, task

    async def _exchange(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request["id"]
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            if self.failure.done():
                raise self.failure.result()
            writer = self.writer
            if writer is None:
                raise ProtocolError("native attachment is not open")
            self.evidence.write("native_request", envelope=request)
            writer.write((json.dumps(request, allow_nan=False) + "\n").encode())
            await writer.drain()
            row = await asyncio.wait_for(asyncio.shield(future), self.rpc_timeout)
            if not row["ok"]:
                raise NativeError(row)
            return row
        except BaseException as error:
            self.evidence.write("request_error", request_id=request_id,
                                action_id=request["args"].get("action", {}).get("action_id"),
                                error=error_record(error))
            if not future.done():
                self.abandoned.add(request_id)
                future.cancel()
            elif not future.cancelled():
                future.exception()
            raise
        finally:
            self.pending.pop(request_id, None)

    async def request(self, command: str, arguments: dict[str, Any]) -> dict[str, Any]:
        _, task = self.submit(command, arguments)
        return await task

    async def close(self) -> None:
        if self.writer is None or self.closing:
            return
        try:
            if self.ready and not self.failure.done():
                await self.request("close", {})
        finally:
            self.closing = True
            tasks = list(self.tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.reader_task is not None:
                self.reader_task.cancel()
                await asyncio.gather(self.reader_task, return_exceptions=True)
            self.writer.close()
            await self.writer.wait_closed()
            self.writer = None


def _invalid_json_constant(value: str) -> None:
    raise ProtocolError(f"non-finite JSON constant: {value}")


def error_record(error: BaseException, seen: set[int] | None = None) -> dict[str, Any]:
    seen = set() if seen is None else seen
    if id(error) in seen:
        return {"type": type(error).__name__, "message": str(error), "recursive_reference": True}
    seen = seen | {id(error)}
    value = {"type": type(error).__name__, "message": str(error)}
    if isinstance(error, BaseExceptionGroup):
        value["exceptions"] = [error_record(child, seen) for child in error.exceptions]
    if error.__cause__ is not None:
        value["cause"] = error_record(error.__cause__, seen)
    elif error.__context__ is not None and not error.__suppress_context__:
        value["context"] = error_record(error.__context__, seen)
    return value


def object_schema(description: str, *, required: list[str] | None = None,
                  properties: dict[str, Any] | None = None,
                  additional: bool = False) -> dict[str, Any]:
    return {"type": "object", "description": description, "properties": properties or {},
            "required": required or [], "additionalProperties": additional}


def integer(minimum: int = 0, maximum: int = 2**64 - 1) -> dict[str, Any]:
    return {"type": "integer", "minimum": minimum, "maximum": maximum}


def string(max_bytes: int | None = None) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, **(
        {"maxLength": max_bytes, "x-maxUtf8Bytes": max_bytes} if max_bytes is not None else {})}


def vector(size: int, *, voxel: bool = False) -> dict[str, Any]:
    return {"type": "array", "minItems": size, "maxItems": size,
            "items": integer(-(2**31), 2**31 - 1) if voxel else {
                "type": "number", "minimum": -3.4028234663852886e38,
                "maximum": 3.4028234663852886e38}}


ENTITY_REF = object_schema(
    "Exact observed UID/lifecycle, not a name or position.",
    required=["uid", "lifecycle"], properties={"uid": integer(1), "lifecycle": string()})
FENCE = object_schema(
    "Explicit proposal fence from fresh public control tokens: structured/hybrid observe "
    "or image-only frame actuator metadata. Never automatically refreshed by the adapter.",
    required=["generation", "expected_session_id", "actor"], properties={
        "generation": integer(), "expected_session_id": string(), "actor": ENTITY_REF})
OWNER_FIELDS = {
    "owner": string(128), "generation": integer(1), "sequence": integer(1),
    "expected_session_id": string(), "actor": ENTITY_REF,
    "expected_protection_generation": integer(),
}
EQUIP_SLOTS = [
    "active_mainhand", "active_offhand", "inactive_mainhand", "inactive_offhand", "lantern",
    "glider", "head", "neck", "shoulders", "chest", "hands", "ring1", "ring2", "back",
    "belt", "legs", "feet", "tabard", "bag1", "bag2", "bag3", "bag4",
]
SLOT_FIELDS = {"slot": integer(0, 2**32 - 1), "loadout_idx": integer(0, 65535),
               "slot_idx": integer(0, 65535)}
SPEED = {"type": "number", "minimum": 0.05000000074505806, "maximum": 1}
ACTION_FIELDS: dict[str, tuple[list[str], dict[str, Any]]] = {
    name: ([], {}) for name in (
        "stop", "jump", "wall_jump", "block", "roll", "swap_weapons", "respawn", "give_up", "sit")
}
ACTION_FIELDS.update({
    **{name: ([], {"speed": SPEED}) for name in (
        "move_forward", "move_backward", "move_right", "move_left")},
    "move_vector": (["direction"], {"direction": vector(2), "speed": SPEED}),
    "move_to": (["position"], {"position": vector(3), "speed": SPEED,
                             "tolerance": {"type": "number", "minimum": 0.10000000149011612,
                                           "maximum": 10}}),
    "look_at": (["position"], {"position": vector(3)}),
    "turn": (["degrees"], {"degrees": {"type": "number", "minimum": -3600, "maximum": 3600}}),
    **{name: ([], {"target_uid": integer(1)}) for name in (
        "attack", "attack_primary", "attack_secondary")},
    "toggle_wield": ([], {"wield": {"type": "boolean"}}),
    "pickup": ([], {"uid": {**integer(1), "description": (
        "Selected observed pickup UID. If omitted, native selects the nearest pickup in range.")}}),
    **{name: (["position"], {"position": vector(3, voxel=True)}) for name in ("collect", "mine")},
    "use_item": ([], SLOT_FIELDS),
    "drop": ([], {**SLOT_FIELDS, "equip_slot": {"type": "string", "enum": EQUIP_SLOTS},
                 "split": {"type": "boolean"}}),
    "equip": ([], {**SLOT_FIELDS, "equip_slot": {"type": "string", "enum": EQUIP_SLOTS}}),
    "craft": (["recipe"], {"recipe": {**string(128), "description": "Exact native recipe key."},
                          "amount": {**integer(1), "description": "Default 1; native clamps to 100."},
                          "station": {**vector(3, voxel=True), "description": (
                              "Exact observed station voxel; native resolves a station if omitted.")}}),
    "chat": (["message"], {"message": string(256)}),
})
ACTION_FIELDS.update(rpg_interactions.fields(
    object_schema=object_schema, integer=integer, string=string, entity_ref=ENTITY_REF))


def tool_schema(command: str, *, renderer: bool = False) -> dict[str, Any]:
    if command == "step":
        actions = [object_schema(
            f"Native {name}." + (
                " Requires exactly one slot OR loadout_idx/slot_idx pair."
                + (" OR equip_slot (split only applies to inventory)." if name == "drop" else "")
                if name in {"use_item", "equip", "drop"} else ""),
            required=["type", *required], properties={
                "type": {"type": "string", "enum": [name]},
                "action_id": {**string(128), "description": (
                    "Optional transport identity: adapter generates once if absent. Preserve "
                    "this ID and the exact arguments for any explicit native duplicate query.")},
                **fields,
            }) for name, (required, fields) in ACTION_FIELDS.items()]
        if renderer:
            from scripts.rpg_benchmark.visual_ui import action_schema
            import sys
            actions.append(action_schema(sys.modules[__name__]))
        return object_schema(
            "Raw native step; only action_id is added when omitted. Timings are envelope fields "
            "in 60 Hz ticks (defaults duration=60, timeout=600), not fields inside action.",
            required=["action"], properties={
                "action": {"oneOf": actions},
                "duration_ticks": integer(1, 3600), "timeout_ticks": integer(1, 36000),
                "control_fence": FENCE,
            })
    if command == "events":
        return object_schema(DESCRIPTIONS[command], properties={
            "after_seq": integer(), "limit": integer(1, 256)})
    if command == "inspect_terrain":
        return object_schema(DESCRIPTIONS[command], required=["position"],
                             properties={"position": vector(3)})
    if command == "inspect_map":
        return object_schema(DESCRIPTIONS[command], properties={
            "bounds": vector(4), "cell_stride": integer(1, 65535), "marker_offset": integer()})
    if command == "goal_recipes":
        return object_schema(DESCRIPTIONS[command], required=["output_item_id"], properties={
            "output_item_id": string(512), "offset": integer(), "page_size": integer(1, 32)})
    if command in {"set_control_state", "release_control_state"}:
        properties = dict(OWNER_FIELDS)
        required = ["owner", "generation", "sequence", "expected_session_id", "actor"]
        if command == "set_control_state":
            required += ["movement", "target", "primary", "block"]
            properties.update({
                "movement": vector(2), "speed": {"type": "number", "minimum": 0, "maximum": 1},
                "target": {"anyOf": [ENTITY_REF, {"type": "null"}]},
                **{key: {"type": "boolean"} for key in (
                    "primary", "secondary", "block", "fly", "jump")},
                "auxiliary": {"anyOf": [integer(), {"type": "null"}]},
                "vertical_input": {"type": "string", "enum": [
                    "neutral", "swim_up", "swim_down", "fly_up", "fly_down"]},
                "movement_intent": {"anyOf": [{"type": "string", "enum": [
                    "approach", "retreat", "left", "right", "destination"]}, {"type": "null"}]},
                "destination": {"anyOf": [vector(3), {"type": "null"}]},
                "look_direction": {"anyOf": [vector(3), {"type": "null"}]},
                "facing": {"type": "string", "enum": ["auto", "travel", "track"]},
                "melee": {"anyOf": [{"type": "string", "enum": ["primary"]}, {"type": "null"}]},
            })
        return object_schema(DESCRIPTIONS[command], required=required, properties=properties)
    if command in READ_COMMANDS | {"close", "contract"}:
        return object_schema("No arguments.")
    raise ValueError(f"no declared native schema for {command}")


def validate_controls(args: dict[str, Any]) -> None:
    movement, target = args["movement"], args["target"]
    intent, destination = args.get("movement_intent"), args.get("destination")
    vertical, fly, jump = args.get("vertical_input", "neutral"), args.get("fly", False), args.get("jump", False)
    facing, look = args.get("facing", "auto"), args.get("look_direction")
    if sum(v * v for v in movement) > 1.000001:
        raise ValueError("movement must lie in the world XY unit disk")
    if intent is not None and any(movement):
        raise ValueError("movement_intent requires movement=[0,0]")
    if (intent == "destination") != (destination is not None):
        raise ValueError("destination is required only with movement_intent='destination'")
    if intent is not None and intent != "destination" and target is None:
        raise ValueError("relative movement_intent requires an observed target UID/lifecycle")
    if ((intent == "destination" and (vertical != "neutral" or fly or jump))
            or (jump and (fly or vertical != "neutral"))
            or (fly and vertical in {"swim_up", "swim_down"})
            or (not fly and vertical in {"fly_up", "fly_down"})):
        raise ValueError("incompatible walk-route, jump, swim or fly inputs")
    if look is not None and (not any(look) or target is not None or facing != "auto"):
        raise ValueError("look_direction must be nonzero and cannot override target/facing")
    if facing == "track" and target is None:
        raise ValueError("track facing requires an observed target UID/lifecycle")
    if args.get("melee") is not None and (
            not args["primary"] or args["block"] or target is None or facing == "travel"):
        raise ValueError("melee requires primary=true, block=false, target and non-travel facing")


def validate_schema(value: Any, schema: dict[str, Any], path: str = "arguments") -> None:
    """Validate the small JSON-schema subset declared by this adapter."""
    for union in ("oneOf", "anyOf"):
        if union not in schema:
            continue
        variants = schema[union]
        if union == "oneOf" and isinstance(value, dict):
            selected = [item for item in variants if value.get("type") in (
                item.get("properties", {}).get("type", {}).get("enum", []))]
            if not selected:
                raise ValueError(f"{path}.type must select a declared native action: {list(ACTION_FIELDS)}")
            validate_schema(value, selected[0], path)
            return
        errors = []
        for variant in variants:
            try:
                validate_schema(value, variant, path)
                return
            except ValueError as error:
                errors.append(str(error))
        raise ValueError("; ".join(errors))
    kind = schema["type"]
    if kind == "object":
        if not isinstance(value, dict):
            raise ValueError(f"{path} must be an object")
        properties = schema["properties"]
        if missing := set(schema["required"]) - value.keys():
            raise ValueError(f"{path} missing required fields: {sorted(missing)}")
        if not schema["additionalProperties"] and (extra := value.keys() - properties.keys()):
            raise ValueError(f"{path} has unknown fields: {sorted(extra)}")
        for key, item in value.items():
            if key in properties:
                validate_schema(item, properties[key], f"{path}.{key}")
    elif kind == "string":
        if not isinstance(value, str) or len(value) < schema.get("minLength", 0):
            raise ValueError(f"{path} must be a nonempty string")
        if "x-maxUtf8Bytes" in schema and len(value.encode("utf-8")) > schema["x-maxUtf8Bytes"]:
            raise ValueError(f"{path} must be at most {schema['x-maxUtf8Bytes']} UTF-8 bytes")
    elif kind == "boolean":
        if type(value) is not bool:
            raise ValueError(f"{path} must be boolean")
    elif kind in {"number", "integer"}:
        if type(value) not in ({int} if kind == "integer" else {int, float}):
            raise ValueError(f"{path} must be {kind}, not boolean or string")
        if (not schema.get("minimum", -math.inf) <= value <= schema.get("maximum", math.inf)
                or not math.isfinite(value)):
            raise ValueError(f"{path} must be finite in [{schema.get('minimum')}, {schema.get('maximum')}]")
    elif kind == "array":
        if not isinstance(value, list) or not schema["minItems"] <= len(value) <= schema["maxItems"]:
            raise ValueError(f"{path} must be an array with {schema['minItems']} items")
        for index, item in enumerate(value):
            validate_schema(item, schema["items"], f"{path}[{index}]")
    elif kind == "null":
        if value is not None:
            raise ValueError(f"{path} must be null")
    else:
        raise ValueError(f"undeclared schema type: {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} must be one of {schema['enum']}")
    json.dumps(value, allow_nan=False)


class GameTools:
    def __init__(self, native: NativeClient, evidence: Evidence):
        self.native, self.evidence = native, evidence
        self.submissions: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self.submission_requests: dict[str, dict[str, Any]] = {}
        self.schemas = {command: tool_schema(command) for command in DESCRIPTIONS}
        self.schemas["contract"] = tool_schema("contract")
        self.schemas["result"] = object_schema(
            "Collect a local asynchronous submission; pending is not native success.",
            required=["request_id"], properties={
                "request_id": {"type": "string", "minLength": 1},
                "wait": {"type": "boolean", "description": "Wait for the native receipt; default false."},
            }, additional=False)
        self.tools = [
            Tool(name=f"game_{command}", description=description,
                 parameters=self.schemas[command], handler=self._handler(command))
            for command, description in {
                **DESCRIPTIONS, "contract": (
                    "Read adapter-provided interface metadata/schema for the pinned bridge, "
                    "NOT a native capabilities query. Includes separately labeled ready-event "
                    "metadata. Only the declared minimal action subset is exposed."
                ), "result": (
                    "Get an asynchronous game_step receipt by returned request_id. With wait=false "
                    "(default), returns an explicitly local pending state or the native response. "
                    "With wait=true, waits for that response. Does not execute an action."
                ),
            }.items()
        ]
        self.names = frozenset(tool.name for tool in self.tools)

    def source(self) -> dict[str, Any]:
        return {"tool_track": TRACK, "transport": "native-ndjson",
                "interface_version": INTERFACE_VERSION,
                "session_id": self.native.ready["session_id"],
                "protocol_version": self.native.ready["protocol_version"],
                "uid": self.native.ready["uid"]}

    def _handler(self, command: str):
        async def handle(invocation: ToolInvocation) -> ToolResult:
            self.evidence.write("tool_invocation", session_id=invocation.session_id,
                                tool_call_id=invocation.tool_call_id,
                                tool_name=invocation.tool_name, arguments=invocation.arguments)
            try:
                if invocation.tool_name != f"game_{command}":
                    raise ValueError("tool invocation name does not match registered handler")
                arguments = invocation.arguments
                validate_schema(arguments, self.schemas[command])
                if command == "contract":
                    payload = {
                        "source": self.source(), "metadata_origin": "adapter-provided",
                        "declared_for_pinned_bridge_sha256": PINNED_BRIDGE_SHA256,
                        "native_protocol_version": NATIVE_PROTOCOL_VERSION,
                        "native_commands": sorted(READ_COMMANDS | CONTROL_COMMANDS),
                        "action_types": sorted(ACTION_FIELDS),
                        "schemas": {key: self.schemas[key] for key in DESCRIPTIONS},
                        "native_ready_metadata": {
                            key: self.native.ready[key] for key in (
                                "capabilities", "observation_schema_version") if key in self.native.ready},
                        "unsupported": [
                            "native capabilities query", "action_status command",
                            "acquire_control command", "cancel-by-action-ID command",
                            "undeclared action types/fields", "step preconditions",
                        ],
                    }
                elif command == "result":
                    request_id = arguments["request_id"]
                    if request_id not in self.submissions:
                        raise ValueError("unknown local submission ID")
                    task = self.submissions[request_id]
                    if arguments.get("wait", False):
                        response = await asyncio.shield(task)
                    elif not task.done():
                        response = None
                    else:
                        response = task.result()
                    payload = {"source": self.source(), "request_id": request_id,
                               "action_id": self.submission_requests[request_id]["args"]["action"]["action_id"],
                               "state": "pending" if response is None else "responded"}
                    if response is not None:
                        payload["response"] = response
                elif command == "step":
                    request, task = self.native.submit(command, arguments)
                    self.submissions[request["id"]] = task
                    self.submission_requests[request["id"]] = request
                    task.add_done_callback(self._submission_done)
                    payload = {"source": self.source(), "state": "submitted",
                               "request": request, "request_id": request["id"],
                               "action_id": request["args"]["action"]["action_id"],
                               "receipt_available_via": "game_result"}
                elif command in CONTROL_COMMANDS:
                    request, task = self.native.submit(command, arguments)
                    self.evidence.write("tool_native_submission", tool_call_id=invocation.tool_call_id,
                                        tool_name=invocation.tool_name, request=request)
                    response = await asyncio.shield(task)
                    payload = {"source": self.source(), "request": request, "response": response}
                else:
                    response = await self.native.request(command, arguments)
                    payload = {"source": self.source(), "response": response}
                result = ToolResult(text_result_for_llm=json.dumps(payload, allow_nan=False))
            except (ValueError, RuntimeError, OSError, TimeoutError) as error:
                payload = {"source": self.source(), "error": error_record(error)}
                if command == "result" and isinstance(invocation.arguments, dict):
                    identifier = invocation.arguments.get("request_id")
                    request = self.submission_requests.get(identifier) if isinstance(identifier, str) else None
                    if request is not None:
                        payload.update(request_id=request["id"],
                                       action_id=request["args"]["action"]["action_id"])
                if isinstance(error, NativeError):
                    payload["response"] = error.response
                result = ToolResult(result_type="failure", error=str(error),
                                    text_result_for_llm=json.dumps(payload, allow_nan=False))
            self.evidence.write("tool_result", tool_call_id=invocation.tool_call_id,
                                tool_name=invocation.tool_name, result_type=result.result_type,
                                payload=payload)
            return result
        return handle

    def _submission_done(self, task: asyncio.Task[dict[str, Any]]) -> None:
        if not task.cancelled():
            task.exception()  # Receipts/errors are durable even if the model never collects them.

    def permission(self, request: PermissionRequest, context: dict[str, str]):
        from copilot.generated.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject
        from copilot.generated.session_events import PermissionRequestCustomTool

        permitted = (isinstance(request, PermissionRequestCustomTool)
                     and request.tool_name in self.names
                     and not request.managed_approval_required)
        feedback = "Only valid declared native game tools are permitted."
        if permitted:
            command = request.tool_name.removeprefix("game_")
            try:
                validate_schema(request.args, self.schemas[command])
                if command not in {"result", "contract"}:
                    validate_request(command, request.args)
            except ValueError as error:
                permitted = False
                feedback = str(error)
        self.evidence.write("permission_decision", request=request.to_dict(),
                            context=context, allowed=permitted)
        if permitted:
            return PermissionDecisionApproveOnce()
        return PermissionDecisionReject(feedback=feedback)


def session_options(tools: GameTools, work: Path, config: Path, model: str,
                    effort: ReasoningEffort | None) -> dict[str, Any]:
    from copilot import ToolSet
    from copilot.tools import Tool as SdkTool, ToolResult as SdkResult

    def sdk_handler(handler):
        async def execute(invocation):
            result = await handler(ToolInvocation(
                session_id=invocation.session_id, tool_call_id=invocation.tool_call_id,
                tool_name=invocation.tool_name, arguments=invocation.arguments))
            return SdkResult(text_result_for_llm=result.text_result_for_llm,
                             result_type=result.result_type, error=result.error)
        return execute

    sdk_tools = [SdkTool(name=tool.name, description=tool.description, parameters=tool.parameters,
                         handler=sdk_handler(tool.handler), skip_permission=False, defer="never")
                 for tool in tools.tools]
    allowlist = ToolSet()
    for name in sorted(tools.names):
        allowlist.add_custom(name)
    return {
        "model": model, "reasoning_effort": effort, "tools": sdk_tools,
        "on_permission_request": tools.permission, "available_tools": allowlist,
        "excluded_tools": ToolSet().add_builtin("*").add_mcp("*"),
        "system_message": {"mode": "append", "content": (
            "You control a native RPG player through the declared game tools. Pursue the user's "
            "task using your own decisions. Tool track: raw native reference, without GameAgent "
            "composites. game_contract is adapter-provided pin-grounded schema, not a native "
            "capabilities query. Use declared required fields; do not guess action names, field "
            "aliases or target IDs. Native world coordinates are Z-up [x,y,z]. observe.current_action "
            "reports current native action status; game_result collects local submissions. No "
            "native cancel-by-ID/acquire command exists; step stop cancels current inputs, and "
            "set/release_control_state use explicit ownership. Native protection, lifecycle and "
            "mechanical refusals remain visible and authoritative. Asynchronous action "
            "submission is not completion; use its correlated receipt and fresh observations. "
            "Generated action_id is returned alongside request_id; never replay an uncertain "
            "or timed-out submission with a fresh action_id. "
            "No administration or slash chat. Report stopping or uncertainty honestly; an external "
            "episode evaluator, not your statement, determines success."
        )},
        "working_directory": str(work), "config_directory": str(config),
        "additional_directories": [], "skip_custom_instructions": True,
        "enable_config_discovery": False, "enable_on_demand_instruction_discovery": False,
        "enable_file_hooks": False, "enable_host_git_operations": False,
        "enable_session_store": False, "enable_skills": False, "memory": {"enabled": False},
        "skip_embedding_retrieval": True, "skill_directories": [], "plugin_directories": [],
        "instruction_directories": [], "mcp_servers": {}, "disabled_mcp_servers": ["*"],
        "excluded_builtin_agents": [], "custom_agents": [], "custom_agents_local_only": True,
        "hooks": {}, "commands": [], "manage_schedule_enabled": False,
        "request_extensions": False, "enable_mcp_apps": False,
        "tool_search": {"enabled": False}, "enable_file_change_tracking": False,
        "coauthor_enabled": False, "infinite_sessions": {"enabled": False},
        "mcp_oauth_token_storage": "in-memory", "embedding_cache_storage": "in-memory",
    }


class SdkEvents:
    def __init__(self, evidence: Evidence, metadata: dict[str, Any]):
        self.loop = asyncio.get_running_loop()
        self.evidence, self.metadata = evidence, metadata
        self.ended: asyncio.Future[None] = self.loop.create_future()
        self.active = False

    def process(self, event: SessionEvent, active: bool) -> None:
        try:
            row = event.to_dict()
            self.evidence.write("sdk_event", event=row)
            if row["type"] == "assistant.usage":
                self.metadata["provider_usage"].append({
                    key: row["data"][key] for key in ("model", "reasoningEffort", "providerCallId",
                                                      "apiCallId", "serviceRequestId")
                    if key in row["data"]
                })
            if row["type"] == "session.error" and not self.ended.done():
                self.ended.set_exception(RuntimeError(f"SDK session error: {row['data']['message']}"))
            elif row["type"] == "session.idle" and active and not self.ended.done():
                self.metadata["idle_event"] = row
                self.ended.set_result(None)
        except Exception as error:
            if not self.ended.done():
                self.ended.set_exception(error)
            else:
                self.loop.call_exception_handler({"message": "SDK evidence recording failed",
                                                  "exception": error})

    def receive(self, event: SessionEvent) -> None:
        self.loop.call_soon_threadsafe(self.process, event, self.active)


async def sdk_turn(session: CopilotSession, task: str, native: NativeClient,
                   evidence: Evidence, metadata: dict[str, Any],
                   events: SdkEvents | None = None) -> None:
    subscribed = events is None
    events = events or SdkEvents(evidence, metadata)
    unsubscribe = session.on(events.receive) if subscribed else None
    try:
        if events.ended.done():
            await events.ended
        events.active = True
        evidence.write("task_submission", task=task)
        metadata["message_id"] = await session.send(task)
        await asyncio.wait({events.ended, native.failure}, return_when=asyncio.FIRST_COMPLETED)
        if native.failure.done():
            raise native.failure.result()
        await events.ended
    finally:
        if unsubscribe is not None:
            unsubscribe()
        if not events.ended.done():
            events.ended.cancel()
        elif not events.ended.cancelled():
            events.ended.exception()


def reserve_output(path: Path, root: Path | None = None) -> Path:
    return output_paths.reserve_output(path.absolute(), OUTPUT_ROOT if root is None else root)


def authentication_token(cache: Path | None) -> str | None:
    token = os.environ.get("COPILOT_GITHUB_TOKEN", "").strip()
    if token:
        return token
    if cache is None:
        return None
    value = json.loads(cache.read_text())
    token = value.get("oauth_token") if isinstance(value, dict) else None
    if not isinstance(token, str) or not token.strip():
        raise ValueError("configured OAuth cache has no authentication token")
    return token.strip()


def process_start_ticks(pid: int) -> int:
    return int(Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19])


async def run(args: argparse.Namespace) -> dict[str, Any]:
    from copilot import CopilotClient, CopilotSession

    output_root = getattr(args, "output_root", None) or OUTPUT_ROOT
    out = reserve_output(args.out, output_root)
    evidence = Evidence(out / "events.ndjson")
    work, config = out / "working", out / "config"
    work.mkdir(mode=0o700)
    config.mkdir(mode=0o700)
    metadata: dict[str, Any] = {
        "status": "starting", "capability_success": "not-evaluated",
        "output_root": str(output_root),
        "tool_track": TRACK, "source_commit": "unknown",
        "interface_version": INTERFACE_VERSION,
        "declared_for_pinned_bridge_sha256": PINNED_BRIDGE_SHA256,
        "control_baseline": "e95ed63946c48845e88e6f359560df524f9bc75f",
        "control_baseline_status": "dirty", "sdk_version": importlib.metadata.version("github-copilot-sdk"),
        "configured_model": args.model, "configured_reasoning_effort": args.reasoning_effort,
        "actual_served_model": "unknown", "actual_served_effort": "unknown",
        "provider_usage": [], "episode_socket": str(args.episode_socket),
        "task": args.task, "cleanup_errors": [],
    }
    native = NativeClient(args.episode_socket, evidence, args.rpc_timeout)
    client: CopilotClient | None = None
    session: CopilotSession | None = None
    events: SdkEvents | None = None
    primary: BaseException | None = None
    runtime_identity = None
    try:
        await native.connect()
        metadata["native_ready"] = native.ready
        tools = GameTools(native, evidence)
        options = session_options(tools, work, config, args.model, args.reasoning_effort)
        evidence.write("sdk_configuration", client_mode="empty",
                       options={key: value for key, value in options.items()
                                if key not in {"tools", "on_permission_request", "available_tools",
                                               "excluded_tools"}},
                       available_tools=options["available_tools"].to_list(),
                       excluded_tools=options["excluded_tools"].to_list(),
                       tool_catalog=[{"name": tool.name, "description": tool.description,
                                      "parameters": tool.parameters} for tool in tools.tools])
        token = authentication_token(getattr(args, "auth_cache", None))
        client = CopilotClient(mode="empty", working_directory=str(work),
                               github_token=token, use_logged_in_user=token is None,
                               base_directory=str(out / "sdk-runtime"),
                               builtin_plugin_directories=[], log_level="error")
        events = SdkEvents(evidence, metadata)
        session = await client.create_session(**options, on_event=events.receive)
        process = getattr(client, "_cli_process", None)
        pid = getattr(process, "pid", None)
        if type(pid) is int:
            runtime_identity = {"pid": pid, "start_ticks": process_start_ticks(pid)}
            metadata["sdk_runtime_identity"] = runtime_identity
            evidence.write("sdk_runtime_identity", **runtime_identity)
        else:
            metadata["sdk_runtime_identity"] = "unknown"
        metadata["sdk_session_id"] = session.session_id
        metadata["status"] = "running"
        await sdk_turn(session, args.task, native, evidence, metadata, events)
        metadata["status"] = "idle"
        metadata["asynchronous_submissions"] = [
            {"request_id": key, "action_id": tools.submission_requests[key]["args"]["action"]["action_id"],
             "state": "cancelled" if task.cancelled() else
             "pending" if not task.done() else "failed" if task.exception() else "responded"}
            for key, task in tools.submissions.items()
        ]
    except BaseException as error:
        primary = error
        metadata["status"] = "interrupted" if isinstance(error, asyncio.CancelledError) else "failed"
        metadata["error"] = error_record(error)
        evidence.write("adapter_error", error=metadata["error"])
    finally:
        async def cleanup(name: str, operation) -> None:
            try:
                await operation()
            except Exception as error:
                row = {"operation": name, **error_record(error)}
                metadata["cleanup_errors"].append(row)
                evidence.write("cleanup_error", **row)

        if session is not None and metadata["status"] != "idle":
            await cleanup("sdk_abort", session.abort)
        if session is not None:
            await cleanup("sdk_disconnect", session.disconnect)
        if client is not None:
            await cleanup("sdk_stop_owned_runtime", client.stop)
        if runtime_identity is not None:
            async def verify_runtime_retired() -> None:
                try:
                    current = process_start_ticks(runtime_identity["pid"])
                except FileNotFoundError:
                    runtime_identity["retired"] = True
                else:
                    runtime_identity["retired"] = current != runtime_identity["start_ticks"]
                if runtime_identity["retired"] is not True:
                    raise RuntimeError("owned SDK runtime PID/start identity is still active")
            await cleanup("sdk_runtime_identity_check", verify_runtime_retired)
        await cleanup("native_detach", native.close)
        if events is not None:
            if not events.ended.done():
                events.ended.cancel()
            elif not events.ended.cancelled():
                events.ended.exception()
        if metadata["cleanup_errors"]:
            metadata["status"] = "failed"
        evidence.write("adapter_outcome", **metadata)
        (out / "result.json").write_text(json.dumps(redact(metadata), indent=2, allow_nan=False) + "\n")
        evidence.close()
    if primary is not None:
        raise primary
    if metadata["cleanup_errors"]:
        raise RuntimeError(f"adapter cleanup failed: {redact(metadata['cleanup_errors'])}")
    return metadata


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode-socket", type=Path, required=True)
    parser.add_argument("--task", required=True, help="Goal text, not a fixture solution.")
    parser.add_argument("--model", help="Explicit SDK model selection.")
    parser.add_argument("--reasoning-effort")
    parser.add_argument("--config", type=Path, help="JSON containing only model and reasoning_effort.")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--output-root", type=Path,
                        help="Explicit dedicated output allowlist root; default is this snapshot's sdk-adapter.")
    parser.add_argument("--auth-cache", type=Path,
                        help="Read existing OAuth cache in memory only; never copy or archive credentials.")
    parser.add_argument("--rpc-timeout", type=float, default=60, help="Per native RPC deadline, not episode horizon.")
    args = parser.parse_args(argv)
    settings: dict[str, Any] = {}
    if args.config is not None:
        settings = json.loads(args.config.read_text())
        if not isinstance(settings, dict) or settings.keys() - {"model", "reasoning_effort"}:
            parser.error("config may contain only model and reasoning_effort")
    args.model = args.model or settings.get("model")
    if args.reasoning_effort is None:
        args.reasoning_effort = settings.get("reasoning_effort")
    if not isinstance(args.model, str) or not args.model.strip():
        parser.error("a nonempty --model or config model is required")
    if args.reasoning_effort is not None:
        try:
            from copilot.session import ReasoningEffort
        except ImportError as error:
            parser.error(f"Copilot adapter requires the optional [copilot] dependency: {error}")
        if args.reasoning_effort not in get_args(ReasoningEffort):
            parser.error("invalid reasoning_effort")
    if not math.isfinite(args.rpc_timeout) or args.rpc_timeout <= 0:
        parser.error("--rpc-timeout must be finite and positive")
    if not args.task.strip() or not args.episode_socket.is_absolute():
        parser.error("task must be nonempty and episode socket must be absolute")
    return args


async def run_with_signals(args: argparse.Namespace) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    owner = asyncio.current_task()
    if owner is None:
        raise RuntimeError("signal handling requires an owned asyncio task")
    loop.add_signal_handler(signal.SIGTERM, owner.cancel)
    try:
        return await run(args)
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


def main() -> None:
    args = parse_args()
    try:
        result = asyncio.run(run_with_signals(args))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except asyncio.CancelledError:
        raise SystemExit(143)
    except Exception as error:
        raise SystemExit(json.dumps(redact(error_record(error))))
    print(json.dumps({"status": result["status"], "capability_success": "not-evaluated",
                      "out": str(args.out)}))


if __name__ == "__main__":
    main()
