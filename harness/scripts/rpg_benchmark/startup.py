"""Provider-independent startup construction input (no world or model calls).

A /2 preflight must construct its actual player/media/detail components using
these values. Importing an entrypoint or echoing this input is not construction.
Headless frames=None is intentional; detail storage is a separate requirement.
"""
from pathlib import Path

SCHEMA = "rpg-adapter-startup/1"
PREFLIGHT = "rpg-adapter-preflight/2"


def context(configuration, backend, frames, detail_storage):
    mode = configuration["observation_mode"]
    if backend not in {"headless", "renderer"}:
        raise ValueError("unknown startup backend")
    if backend == "headless" and (mode != "structured" or frames is not None):
        raise ValueError("headless startup must use structured mode and frames=None")
    if backend == "renderer" and frames is None:
        raise ValueError("renderer startup requires owned frame storage")
    for value in (frames, detail_storage):
        if value is not None and not Path(value).is_absolute():
            raise ValueError("startup storage must be absolute")
    if detail_storage is None:
        raise ValueError("detail storage is independent of frames and is required")
    return {"schema": SCHEMA, "configuration": configuration, "backend": backend,
            "frames": str(frames) if frames is not None else None,
            "detail_storage": str(detail_storage), "model_calls": False, "native_worlds": False}


def validate_receipt(receipt, expected):
    if (receipt.get("schema") != PREFLIGHT
            or receipt.get("startup") != expected
            or receipt.get("construction_checked") is not True):
        raise ValueError("adapter must exercise actual startup construction with the exact "
                         "backend/frames/detail storage; import-only /1 preflight is insufficient")


async def probe_player(selected):
    """Construct actual common client/tools/storage without connect/inference.

    Readiness is a constructor fixture, explicitly not a native session, PNG,
    transport delivery or task qualification.
    """
    from scripts import rpg_sdk_agent as common
    from .player import tools_for
    expected = context(selected["configuration"], selected["backend"],
                       selected["frames"], selected["detail_storage"])
    if selected != expected:
        raise ValueError("startup context changed")
    detail = Path(selected["detail_storage"])
    detail.mkdir(parents=True, exist_ok=False)
    evidence = common.Evidence(detail / "construction.ndjson")
    try:
        native = common.NativeClient(detail / "not-connected.sock", evidence)
        native.ready = {"session_id": "construction-only", "protocol_version": "0.2", "uid": 1}
        if selected["backend"] == "renderer":
            native.ready["renderer"] = {
                "actor": {}, "control": {}, "visual_ui": {"schema": "native-visual-ui/1"}}
        tools = tools_for(native, evidence, selected["configuration"],
                          frames=Path(selected["frames"]) if selected["frames"] is not None else None,
                          episode="construction-only")
        if not tools.tools:
            raise ValueError("actual player construction yielded no tools")
        native.failure.cancel()
    finally:
        evidence.close()
    return {"schema": PREFLIGHT, "startup": selected, "construction_checked": True}
