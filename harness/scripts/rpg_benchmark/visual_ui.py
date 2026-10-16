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
        "bag item to use/equip it; left-click an actual dialogue choice; interact continues an "
        "on-screen statement or uses the normal Talk prompt. Hover then request a fresh frame "
        "for native tooltips. No item IDs, NPC IDs, or extracted text. Unsupported widgets do not "
        "mutate gameplay. A dispatch/completion receipt is not dialogue progress or task success.",
        required=["type", "action_id", "frame_id", "ui_state_id", "expected_session_id", "actuator", "gesture"],
        properties={"type": {"type": "string", "enum": ["visual_input"]},
                    "action_id": common.string(128), "frame_id": common.string(),
                    "ui_state_id": common.string(), "actuator": actuator,
                    "expected_session_id": common.string(),
                    "gesture": {"type": "string", "enum": [
                        "hover", "left_click", "right_click", "interact"]},
                    "x": {"type": "number", "minimum": 0},
                    "y": {"type": "number", "minimum": 0}})


def validate(action):
    pointer = action["gesture"] != "interact"
    if ("x" in action) != pointer or ("y" in action) != pointer:
        raise ValueError("pointer gestures need x/y; interact accepts neither")


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
            or action.get("type") not in {"use_item", "dialogue_action", "context_interact"}
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
