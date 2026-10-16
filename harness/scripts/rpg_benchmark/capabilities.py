"""Finite versioned adapter declarations, negotiated before any launch."""
SCHEMA = "rpg-external-adapter/2"
CONFIGURATION = "rpg-adapter-configuration/1"
ACTION_PROFILE = "raw-native-v2"


def negotiate(manifest, requested):
    legacy = manifest["schema"] == "rpg-external-adapter/1"
    if legacy:
        capabilities = {
            "observation_modes": ["structured"], "action_profiles": [ACTION_PROFILE],
            "model_profiles": [{"model": manifest["model"],
                                "reasoning_efforts": [manifest["reasoning_effort"]]}],
            "native_protocol": "0.2", "integer_transport": "strict",
            "asynchronous_receipts": True,
        }
        defaults = {"model": manifest["model"], "reasoning_effort": manifest["reasoning_effort"],
                    "observation_mode": "structured", "action_profile": ACTION_PROFILE}
    else:
        if manifest["schema"] != SCHEMA:
            raise ValueError("Unsupported adapter contract version")
        capabilities = manifest["capabilities"]
        defaults = manifest["default_configuration"]
    if (capabilities.get("native_protocol") != "0.2"
            or capabilities.get("integer_transport") != "strict"
            or capabilities.get("asynchronous_receipts") is not True):
        raise ValueError("Adapter lacks required native/strict-integer/asynchronous capabilities")
    fields = {"model", "reasoning_effort", "observation_mode", "action_profile"}
    if set(defaults) != fields or set(requested) - fields:
        raise ValueError("Invalid adapter configuration fields")
    selection = {**defaults, **{key: value for key, value in requested.items() if value is not None}}
    if selection["observation_mode"] not in capabilities["observation_modes"]:
        raise ValueError("Unsupported observation mode")
    mapping = capabilities.get("action_profiles_by_observation_mode")
    if "action_profiles_by_observation_mode" in capabilities:
        if (not isinstance(mapping, dict)
                or any(not isinstance(profiles, list) or not profiles
                       or any(not isinstance(profile, str) or not profile for profile in profiles)
                       for profiles in mapping.values())
                or selection["action_profile"] not in mapping.get(selection["observation_mode"], [])):
            raise ValueError("Unsupported observation mode/action profile mapping")
    elif capabilities["observation_modes"] != ["structured"]:
        raise ValueError("Multimodal adapters require an observation mode/action profile mapping")
    if (selection["observation_mode"] in {"image", "hybrid"}
            and capabilities.get("image_transport") != "image-blocks-v1"):
        raise ValueError("Unsupported observation mode: real image-block transport required")
    if (selection["action_profile"] not in capabilities["action_profiles"]
            or selection["action_profile"] not in {
                ACTION_PROFILE, "visual-motor-v1", "visual-ui-v1", "ordinary-renderer-native-v1"}):
        raise ValueError("Unsupported action profile")
    if not any(profile["model"] == selection["model"]
               and selection["reasoning_effort"] in profile["reasoning_efforts"]
               for profile in capabilities["model_profiles"]):
        raise ValueError("Unsupported model/reasoning-effort profile")
    if any(not isinstance(value, str) or not value for value in selection.values()):
        raise ValueError("Adapter configuration values must be nonempty strings")
    return {"schema": CONFIGURATION, **selection}, capabilities, legacy
