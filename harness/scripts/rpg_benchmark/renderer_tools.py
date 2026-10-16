"""Provider-independent multimodal tools; native state remains behind the boundary."""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

from scripts import rpg_sdk_agent as common
from . import observations
from .player_types import Invocation, Result, Tool


class RendererTools:
    def __init__(self, native: common.NativeClient, evidence, mode: str,
                 frames: Path, episode: str, action_profile: str | None = None):
        self.native, self.evidence, self.mode = native, evidence, mode
        self.frames, self.episode = frames, episode
        self.action_profile = action_profile or (
            "visual-motor-v1" if mode == "image" else "ordinary-renderer-native-v1")
        visual = native.ready.get("renderer", {}).get("visual_ui", {}).get("schema") == "native-visual-ui/1"
        if self.action_profile == "visual-ui-v1" and (mode != "image" or not visual):
            raise ValueError("visual-ui-v1 requires image mode and matching native visual UI capability")
        self.visual = visual and (mode != "image" or self.action_profile == "visual-ui-v1")
        self.pending = {}
        self.sequence, self.previous_capture = 0, None
        self.frame_lock = asyncio.Lock()
        self.seen_frames = set()
        commands = {"step", "set_control_state", "release_control_state", "ping"}
        if mode != "image":
            commands |= common.READ_COMMANDS - {"health"}
        self.schemas = {name: copy.deepcopy(common.tool_schema(name, renderer=self.visual))
                        for name in sorted(commands)}
        self.schemas["step"]["description"] = (
            "Submit asynchronously. Durations count variable-dt Voxygen client updates, "
            "not server ticks or fixed 60Hz. Never resubmit an unknown mutation.")
        actions = self.schemas["step"]["properties"]["action"]["oneOf"]
        from .rpg_interactions import NAMES
        rpg = self.native.ready.get("capabilities", {}).get("rpg_interactions") is not None
        self.schemas["step"]["properties"]["action"]["oneOf"] = [
            action for action in actions if action["properties"]["type"]["enum"][0] != "chat"
            and (rpg or action["properties"]["type"]["enum"][0] not in NAMES)
            and (mode != "image" or action["properties"]["type"]["enum"][0] in observations.IMAGE_ACTIONS)]
        if mode == "image":
            properties = self.schemas["set_control_state"]["properties"]
            for key in ("movement_intent", "destination"):
                properties.pop(key)
            properties["target"] = {"type": "null"}
        empty = common.object_schema("No arguments.")
        if mode != "structured":
            self.schemas["frame"] = empty
        self.schemas["ui"] = common.object_schema(
            "Open a normal UI view, then request a fresh frame before any visual gesture.",
            required=["panel"], properties={"panel": {
                "type": "string", "enum": ["world", "inventory", "equipment", "map"]}})
        self.schemas["contract"] = empty
        self.schemas["result"] = common.object_schema(
            "Collect the original asynchronous receipt; pending is not native success.",
            required=["request_id"], properties={
                "request_id": common.string(), "wait": {"type": "boolean"}})
        self.tools = [Tool(f"game_{name}", self.description(name), schema, self.handler(name))
                      for name, schema in self.schemas.items()]

    def description(self, name: str) -> str:
        return {
            "frame": "Read the real same-player world/HUD as an image block. Not a synchronized state pair.",
            "ui": "Open world, bag/equipment or map; no hidden item or entity lookup.",
            "contract": "Read static interface rules and public session/actor/control tokens.",
            "step": self.schemas["step"]["description"],
            "result": "Get the unchanged receipt for one prior local submission; never executes an action.",
        }.get(name, common.DESCRIPTIONS.get(name, name))

    def validate_request(self, command: str, args: dict) -> None:
        if command == "close":
            common.validate_schema(args, common.object_schema("Close attachment."))
            return
        if command in {"frame", "ui"}:
            fence = {"schema_version": common.string(), "expected_session_id": common.string(),
                     "actor": common.ENTITY_REF}
            if command == "ui":
                fence["panel"] = self.schemas["ui"]["properties"]["panel"]
            common.validate_schema(args, common.object_schema(
                "Fenced renderer request.", required=list(fence), properties=fence))
        elif command in self.schemas and command not in {"contract", "result"}:
            common.validate_request(command, args, renderer=self.visual)
            common.validate_schema(args, self.schemas[command])
        else:
            raise ValueError("command unavailable in selected observation/action profile")
        if self.mode == "image" and not observations.image_allowed({"cmd": command, "args": args}):
            raise ValueError("command needs unsupported visual addressing")

    def contract(self) -> dict:
        renderer = self.native.ready["renderer"]
        identity = {key: renderer[key] for key in ("actor", "control")}
        return {
            "schema": "rpg-multimodal-tools/1", "observation_schema": observations.SCHEMA,
            "requested_observation_mode": self.mode,
            "effective_action_profile": self.action_profile,
            "visibility_profile": ("image-receipts-v1" if self.mode == "image"
                                   else "structured-native-v2"),
            "identity": identity, "session_id": self.native.ready["session_id"],
            "control_identity_freshness": (
                "initial ready snapshot only; use a fresh frame's actuator control tokens "
                "or structured observe for current generation/protection/owner"),
            "timing_profile": observations.TIMING, "native_tick": None,
            "action_effect_ordering": "unconfirmed", "headless_timing_equivalent": False,
            "structured_visibility": (None if self.mode == "image" else
                                     "native observation; may include non-line-of-sight entities/positions"),
            "ui_views": ["world", "inventory", "equipment", "map"],
            "coordinate_ui": (
                "Debug coordinates are disabled in the native agent renderer; no coordinate tool "
                "exists in image mode. Use the task's public visual goal description and normal "
                "frames/UI. Movement vectors use native horizontal [X,Y], with X east and Y north."),
            "visual_addressing": {
                "items": self.visual, "inventory_equip_selection": self.visual,
                "entities": "native visible Talk prompt only" if self.visual else False,
                "map_markers": False, "dialogue_choices": self.visual, "trade_offers": False,
                "schema": "native-visual-ui/1" if self.visual else None,
                "overlay": "none; stock native HUD pixels",
                "unsupported": ["crafting", "trade", "skills", "map targeting", "drag", "unequip"]},
            "unknown_effect_policy": "do not resubmit; collect original request receipt",
            "judge_state": "private; never available to a model tool",
        }

    def fence(self) -> dict:
        return {"schema_version": "1.0", "expected_session_id": self.native.ready["session_id"],
                "actor": self.native.ready["renderer"]["actor"]}

    def handler(self, command: str):
        async def handle(invocation: Invocation) -> Result:
            self.evidence.write("tool_invocation", tool_call_id=invocation.tool_call_id,
                                tool_name=invocation.tool_name, arguments=invocation.arguments)
            images = ()
            try:
                if invocation.tool_name != f"game_{command}":
                    raise ValueError("tool name differs from selected handler")
                args = invocation.arguments
                common.validate_schema(args, self.schemas[command])
                if command == "contract":
                    payload = self.contract()
                elif command == "result":
                    request, task = self.pending[args["request_id"]]
                    response = (await asyncio.shield(task) if args.get("wait") or task.done() else None)
                    payload = {"state": "pending" if response is None else "responded",
                               "request_id": request["id"],
                               "action_id": request["args"]["action"]["action_id"]}
                    if response is not None:
                        payload["response"] = (observations.project_image(response)
                                               if self.mode == "image" else response)
                elif command == "frame":
                    async with self.frame_lock:
                        response = await self.native.request("frame", self.fence())
                        frame = response["result"]
                        if frame["frame_id"] in self.seen_frames:
                            raise ValueError("duplicate renderer frame identity")
                        payload, image = observations.read_frame(
                            frame, self.frames, self.native.ready["session_id"],
                            self.native.ready["renderer"]["actor"], self.episode,
                            self.sequence + 1, self.previous_capture)
                        self.sequence += 1
                        self.previous_capture = int(frame["capture_host"]["begin_ns"])
                        self.seen_frames.add(frame["frame_id"])
                    images = (image,)
                else:
                    request, task = self.native.submit(
                        command, {**self.fence(), **args} if command == "ui" else args)
                    if command in common.CONTROL_COMMANDS:
                        self.evidence.write("tool_native_submission", tool_call_id=invocation.tool_call_id,
                                            request=request)
                    if command == "step":
                        self.pending[request["id"]] = (request, task)
                        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
                        payload = {"state": "submitted", "request_id": request["id"],
                                   "action_id": request["args"]["action"]["action_id"],
                                   "receipt_available_via": "game_result"}
                    else:
                        response = await asyncio.shield(task)
                        payload = {"response": (observations.project_image(response)
                                                if self.mode == "image" else response)}
                result = Result(json.dumps(payload, allow_nan=False), images=images)
            except (ValueError, RuntimeError, OSError, TimeoutError, KeyError) as error:
                self.evidence.write("tool_error_private", error=common.error_record(error))
                # Exceptions can contain native state, paths and even nested receipt payloads.
                payload = {"error": {"code": ("native_refusal" if isinstance(error, common.NativeError)
                                            else "tool_operation_failed")},
                           "effect": "unknown-unless-original-receipt-known",
                           "retry_policy": "never automatically resubmit"}
                if isinstance(error, common.NativeError):
                    payload["response"] = (observations.project_image(error.response)
                                           if self.mode == "image" else error.response)
                result = Result(json.dumps(payload), "failure", payload["error"]["code"])
            self.evidence.write("tool_result", tool_call_id=invocation.tool_call_id,
                                tool_name=invocation.tool_name, payload=payload,
                                image_sha256=[image.sha256 for image in images],
                                result_type=result.result_type)
            return result
        return handle
