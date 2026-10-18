"""Public PlayerPort/schema negotiation for stock HUD quantity modifiers."""
import pytest

from scripts import rpg_sdk_agent as common
from scripts.rpg_benchmark import player, visual_ui
from .test_visual_ui import action, native, Evidence


def tools(tmp_path, modifiers=("shift", "ctrl")):
    bridge = native()
    bridge.ready["renderer"]["benchmark_features"] = ["trade", "inventory"]
    bridge.ready["renderer"]["visual_ui"].update(
        gestures=["left_click", "right_click", "drag", "interact"],
        modifiers={"left_click": list(modifiers)})
    return player.tools_for(bridge, Evidence(), {
        "observation_mode": "image", "action_profile": visual_ui.PROFILE},
        frames=tmp_path, episode="generic-quantity")


def request(modifiers=None, gesture="left_click"):
    value = action()
    value["gesture"] = gesture
    if modifiers is not None:
        value["modifiers"] = modifiers
    return {"action": value}


@pytest.mark.parametrize("modifiers", [None, [], ["shift"], ["ctrl"]])
def test_public_playerport_admits_only_native_advertised_quantity_modifiers(tmp_path, modifiers):
    selected = tools(tmp_path)
    selected.validate_request("step", request(modifiers))
    assert selected.contract()["visual_addressing"]["modifiers"] == {"left_click": ["shift", "ctrl"]}
    schema = next(row for row in selected.schemas["step"]["properties"]["action"]["oneOf"]
                  if row["properties"]["type"]["enum"] == ["visual_input"])
    assert schema["properties"]["modifiers"]["maxItems"] == 1
    assert "modifiers" not in schema["required"]


@pytest.mark.parametrize("modifiers", [
    ["shift","ctrl"], ["shift","shift"], ["alt"], ["SHIFT"], [True], [1], "shift",
])
def test_unsupported_modifier_shapes_and_combinations_are_rejected(tmp_path, modifiers):
    with pytest.raises(ValueError):
        tools(tmp_path).validate_request("step", request(modifiers))
    with pytest.raises(ValueError):
        common.validate_request("step", request(modifiers), renderer=True)


def test_explicit_null_modifier_is_not_absent(tmp_path):
    value = request()
    value["action"]["modifiers"] = None
    with pytest.raises(ValueError):
        tools(tmp_path).validate_request("step", value)


@pytest.mark.parametrize("gesture", ["right_click", "drag", "interact", "hover", "scroll_up"])
def test_modifier_on_non_left_click_is_explicitly_refused(tmp_path, gesture):
    value = request(["shift"], gesture)
    if gesture == "drag":
        value["action"].update(to_x=30,to_y=40)
    if gesture == "interact":
        value["action"].pop("x")
        value["action"].pop("y")
    with pytest.raises(ValueError, match="only on left_click"):
        common.validate_request("step", value, renderer=True)


def test_older_native_pin_cannot_advertise_or_silently_ignore_new_modifiers(tmp_path):
    selected = tools(tmp_path, ())
    selected.validate_request("step", request())
    with pytest.raises(ValueError, match="unknown fields"):
        selected.validate_request("step", request(["shift"]))
    assert selected.contract()["visual_addressing"]["modifiers"] == {"left_click": []}
    subset = tools(tmp_path, ("shift",))
    subset.validate_request("step", request(["shift"]))
    with pytest.raises(ValueError):
        subset.validate_request("step", request(["ctrl"]))


def test_invalid_native_capability_is_not_negotiated_as_a_supported_modifier(tmp_path):
    with pytest.raises(ValueError, match="unsupported native visual modifier"):
        tools(tmp_path, ("alt",))


def check_production_ready_negotiation(payload, tmp_path):
    """Native integration regression: consume the real Rust builders' output.

    The Rust production_ready_and_frame_capabilities_reach_public_schema test
    exports this input when VELORENBENCH_VISUAL_CAPABILITY_EVIDENCE is set.
    No native capability is filled in or inferred from frame metadata here.
    Python-only unit tests do not acquire a compiler/runtime dependency.
    """
    import copy
    from types import SimpleNamespace
    from scripts.rpg_benchmark.renderer import RendererBoundary

    event, frame = payload["ready_event"], payload["frame_visual_ui"]

    def negotiated(value):
        bridge = SimpleNamespace(
            ready=SimpleNamespace(result=lambda: {
                "type": "event", "event": "ready", "uid": value["data"]["actor"]["uid"],
                "session_id": value["session_id"], "protocol_version": value["protocol_version"]}),
            renderer_ready=SimpleNamespace(result=lambda: value),
            renderer={"benchmark_features": ["trade", "inventory"]})
        public = RendererBoundary(bridge, tmp_path / "unused.sock", "image").public_ready()
        return player.tools_for(SimpleNamespace(ready=public), Evidence(), {
            "observation_mode": "image", "action_profile": visual_ui.PROFILE},
            frames=tmp_path, episode="production-ready-quantity")

    selected = negotiated(event)
    for modifiers in (["shift"], ["ctrl"], [], None):
        value = request(modifiers)
        value["action"].update(
            frame_id="frame", ui_state_id=frame["ui_state_id"],
            expected_session_id=event["session_id"], actuator=event["data"]["actuator"])
        selected.validate_request("step", value)
    assert selected.contract()["visual_addressing"]["modifiers"] == {"left_click": ["shift", "ctrl"]}
    for field in ("schema", "gestures", "modifiers"):
        assert event["data"]["visual_ui"][field] == frame[field]
    assert "ui_state_id" not in event["data"]["visual_ui"]
    assert not {"state", "inventory", "dialogue", "position", "actor"} & set(frame)

    # A newer frame must not upgrade an old pin's unadvertised ready surface.
    old = copy.deepcopy(event)
    old["data"]["visual_ui"].pop("modifiers")
    legacy = negotiated(old)
    legacy.validate_request("step", request())
    with pytest.raises(ValueError, match="unknown fields"):
        legacy.validate_request("step", request(["shift"]))
    assert legacy.contract()["visual_addressing"]["modifiers"] == {"left_click": []}
