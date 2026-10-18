"""Static visual selectors and private, provenance-checked oracle normalization."""
import copy

PROFILE = "visual-ui-v1"
SCHEMA = "native-visual-ui/1"


def action_schema(common):
    control = common.object_schema("Exact control identity from the selected frame.",
        required=["owner", "generation", "sequence", "retired", "protection_generation"],
        properties={"owner": common.string(), "generation": common.integer(),
                    "sequence": common.integer(), "retired": {"type": "boolean"},
                    "protection_generation": common.integer()})
    # The initial owner is an empty native token, not a model-selected owner.
    control["properties"]["owner"] = {"type": "string"}
    actuator = common.object_schema("Exact frame actuator identity, never auto-refreshed.",
        required=["actor", "control", "active_action_id"],
        properties={"actor": common.ENTITY_REF, "control": control,
                    "active_action_id": {"anyOf": [common.string(), {"type": "null"}]}})
    return common.object_schema(
        "A gesture on the latest actual PNG. One gesture consumes its frame. Right-click a visible "
        "bag item to use/equip it; middle-click the visible map to set a waypoint through its "
        "ordinary HUD binding; left-click an actual dialogue choice; interact continues an "
        "on-screen statement or uses the normal visible context prompt. Drag from x/y to "
        "to_x/to_y; scroll at x/y. Hover then request a fresh frame "
        "for native tooltips. Optional modifiers=['shift'] or ['ctrl'] on left_click use the stock "
        "HUD's individual or automatic quantity request; no combined modifiers or other modified "
        "gestures. Omitted/empty modifiers preserve ordinary clicking. No item IDs, NPC IDs, or extracted text. Unsupported widgets do not "
        "mutate gameplay. A dispatch/completion receipt is not dialogue progress or task success.",
        required=["type", "action_id", "frame_id", "ui_state_id", "expected_session_id", "actuator", "gesture"],
        properties={"type": {"type": "string", "enum": ["visual_input"]},
                    "action_id": common.string(128), "frame_id": common.string(),
                    "ui_state_id": common.string(), "actuator": actuator,
                    "expected_session_id": common.string(),
                    "gesture": {"type": "string", "enum": [
                        "hover", "left_click", "right_click", "middle_click", "interact",
                        "drag", "scroll_up", "scroll_down", "context_trade"]},
                    "x": {"type": "number", "minimum": 0},
                    "y": {"type": "number", "minimum": 0},
                    "to_x": {"type": "number", "minimum": 0},
                    "to_y": {"type": "number", "minimum": 0},
                    "modifiers": {"type": "array", "minItems": 0, "maxItems": 1,
                                  "items": {"type": "string", "enum": ["shift", "ctrl"]}}})


def validate(action):
    if action.get("modifiers") and action["gesture"] != "left_click":
        raise ValueError("visual modifiers are supported only on left_click")
    pointer = action["gesture"] not in {"interact", "context_trade"}
    if ("x" in action) != pointer or ("y" in action) != pointer:
        raise ValueError("pointer gestures need x/y; interact accepts neither")
    if ("to_x" in action) != (action["gesture"] == "drag") or (
            "to_y" in action) != (action["gesture"] == "drag"):
        raise ValueError("only drag needs both destination coordinates")


def private_receipt(request, response, actor, monotonic):
    row = {"request": request, "response": response, "actor": actor,
           "wall_monotonic": monotonic}
    if request.get("args", {}).get("action", {}).get("type") != "visual_input":
        return row
    result = response.get("result") or {}
    resolution = result.get("visual_resolution")
    if resolution is None:
        return row
    action = resolution.get("action")
    if (resolution.get("source") != "native-hud-hit-test-v1"
            or resolution.get("original_args") != request["args"]
            or not isinstance(action, dict)
            or action.get("type") not in {
                "use_item", "equip", "dialogue_action", "context_interact", "pickup",
                "craft", "trade_action", "social_action", "unlock_skill",
                "configure_auxiliary_ability", "inventory_action", "map_marker"}
            or action.get("action_id") != request["args"]["action"]["action_id"]
            or action.get("action_id") != result.get("action_id")
            or action.get("type") != result.get("action_type")
            or response.get("id") != request["id"]):
        raise ValueError("native visual resolution differs from its exact original request")
    canonical = copy.deepcopy(request)
    canonical["args"]["action"] = action
    row.update(request=canonical, public_request=request,
               resolution_source="actual native HUD hit-test; evaluator-private only")
    return row


def intent_fence_matches(row, opening):
    """Revalidate a normalized HUD receipt, not just its descriptive marker.

    Native submit/finish checks the latest, single-use frame and exact current
    actuator before emitting visual_resolution. Retain that native trust
    boundary: rederive the canonical request with the same normalizer, validate
    the original public gesture, and bind its frame actuator to this opening.
    This does not infer freshness from a frame's spelling or accept a public
    semantic action as image intent.
    """
    from scripts import rpg_sdk_agent as common

    try:
        public = row["public_request"]
        if public["cmd"] != "step" or not isinstance(public["id"], str) or not public["id"]:
            return False
        common.validate_request("step", public["args"], renderer=True)
        action = public["args"]["action"]
        if (action["type"] != "visual_input"
                or action["expected_session_id"] != opening["session_id"]
                or action["frame_id"] != action["ui_state_id"]
                or action["actuator"]["actor"] != opening["control_actor"]
                or action["actuator"]["active_action_id"] is not None):
            return False
        normalized = private_receipt(public, row["response"], row["actor"],
                                     row.get("wall_monotonic"))
        return ("public_request" in normalized
                and normalized["request"] == row["request"]
                and normalized["resolution_source"] == row.get("resolution_source"))
    except (KeyError, TypeError, AttributeError, ValueError):
        return False
