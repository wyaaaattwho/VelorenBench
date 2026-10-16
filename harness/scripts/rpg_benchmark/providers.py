"""Explicit provider capabilities; a route is not a global model default."""
from . import capabilities

SCHEMA = "rpg-responses-provider/1"
LUNA = {
    "schema": SCHEMA, "id": "existing-luna", "tool_calls": True,
    "observation_modes": ["structured", "image", "hybrid"],
    "image_transport": "image-blocks-v1",
    "model_profiles": [{"model": "gpt-6-luna", "reasoning_efforts": ["none"]}],
    "default_model": "gpt-6-luna", "default_effort": "none",
}


def manifest(provider: dict, mode: str) -> dict:
    if (not isinstance(provider, dict) or set(provider) != set(LUNA)
            or provider["schema"] != SCHEMA or not isinstance(provider["id"], str)
            or not provider["id"] or provider["tool_calls"] is not True
            or not isinstance(provider["model_profiles"], list) or not provider["model_profiles"]
            or not isinstance(provider["observation_modes"], list)
            or not set(provider["observation_modes"]) <= {"structured", "image", "hybrid"}):
        raise ValueError("invalid explicit Responses provider capabilities")
    for profile in provider["model_profiles"]:
        if (not isinstance(profile, dict) or set(profile) != {"model", "reasoning_efforts"}
                or not isinstance(profile["model"], str) or not profile["model"]
                or not isinstance(profile["reasoning_efforts"], list) or not profile["reasoning_efforts"]
                or any(not isinstance(effort, str) or not effort for effort in profile["reasoning_efforts"])):
            raise ValueError("invalid provider model/effort profile")
    profile = "visual-motor-v1" if mode == "image" else "ordinary-renderer-native-v1"
    value = {"schema": capabilities.SCHEMA, "tool_track": "ordinary-renderer-public",
             "capabilities": {
                 "native_protocol": "0.2", "integer_transport": "strict", "asynchronous_receipts": True,
                 **{key: provider[key] for key in ("model_profiles", "observation_modes", "image_transport")},
                 "action_profiles": ["visual-motor-v1", "visual-ui-v1", "ordinary-renderer-native-v1"],
                 "action_profiles_by_observation_mode": {
                     "structured": ["ordinary-renderer-native-v1"],
                     "image": ["visual-motor-v1", "visual-ui-v1"],
                     "hybrid": ["ordinary-renderer-native-v1"]}},
             "default_configuration": {"model": provider["default_model"],
                                       "reasoning_effort": provider["default_effort"],
                                       "observation_mode": mode, "action_profile": profile}}
    capabilities.negotiate(value, {})
    return value
