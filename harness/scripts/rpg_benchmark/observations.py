"""Versioned renderer observations and fail-closed public visibility."""
from __future__ import annotations

import hashlib
import json
import struct
import zlib
from pathlib import Path

from .player_types import Image

SCHEMA = "rpg-observation/1"
IMAGE_PROFILE = "image-receipts-v1"
TIMING = "voxygen-variable-dt-client-update-v1"
IMAGE_ACTIONS = frozenset({
    "move_forward", "move_backward", "move_left", "move_right", "move_vector",
    "jump", "sit", "stop", "toggle_wield", "visual_input",
})
RECEIPT_FIELDS = {
    "action_id", "action_type", "status", "started_tick", "finished_tick",
    "duplicate", "accepted", "closed", "session_id", "protocol_version",
    "owner", "generation", "sequence", "retired", "released", "actor",
    "accepted_tick", "applied_tick", "gameplay_success",
}
ENVELOPE_FIELDS = {
    "type", "protocol_version", "session_id", "tick", "id", "ok", "event",
    "seq", "uid", "character", "observation_schema_version",
}


def selected(value: dict, fields: set[str]) -> dict:
    return {key: value[key] for key in fields if key in value}


def project_image(value: dict, *, frame: bool = False) -> dict:
    """Only the correlated frame command may admit the renderer's frame builder."""
    out = selected(value, ENVELOPE_FIELDS)
    out["visibility_profile"] = IMAGE_PROFILE
    if "error" in value:
        error = value["error"]
        out["error"] = None if error is None else selected(error, {"code"})
    for field in ("result", "data"):
        if field in value:
            payload = value[field]
            out[field] = (None if payload is None else payload if frame
                          else selected(payload, RECEIPT_FIELDS))
    return out


def image_allowed(request: dict) -> bool:
    command, args = request.get("cmd"), request.get("args", {})
    if command in {"frame", "ui", "ping", "close", "release_control_state"}:
        return True
    if command == "step":
        return args.get("action", {}).get("type") in IMAGE_ACTIONS
    if command == "set_control_state":
        return all(args.get(key) is None for key in ("target", "destination", "movement_intent"))
    return False


def verify_png(data: bytes, width: int, height: int) -> None:
    if (type(width) is not int or type(height) is not int or width <= 0 or height <= 0
            or data[:8] != b"\x89PNG\r\n\x1a\n"):
        raise ValueError("invalid PNG identity/dimensions")
    offset, compressed, header, ended = 8, bytearray(), None, False
    while offset < len(data):
        if offset + 12 > len(data):
            raise ValueError("truncated PNG chunk")
        size = struct.unpack_from(">I", data, offset)[0]
        kind, payload = data[offset + 4:offset + 8], data[offset + 8:offset + 8 + size]
        end = offset + size + 12
        if end > len(data) or zlib.crc32(kind + payload) != struct.unpack_from(">I", data, end - 4)[0]:
            raise ValueError("PNG chunk length/CRC mismatch")
        if header is None and kind != b"IHDR":
            raise ValueError("PNG must start with IHDR")
        if kind == b"IHDR":
            if header is not None or size != 13:
                raise ValueError("invalid PNG IHDR")
            header = struct.unpack(">IIBBBBB", payload)
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            if size or end != len(data):
                raise ValueError("invalid PNG ending")
            ended = True
        offset = end
    if not ended or header is None:
        raise ValueError("incomplete PNG")
    w, h, depth, color, compression, filtering, interlace = header
    if (w, h) != (width, height) or depth != 8 or color not in (2, 6) or any(
            (compression, filtering, interlace)):
        raise ValueError("PNG dimensions/encoding differ from qualified RGB/RGBA8 renderer")
    stride = 1 + width * (3 if color == 2 else 4)
    decoder = zlib.decompressobj()
    pixels = decoder.decompress(compressed, stride * height + 1)
    if (len(pixels) != stride * height or not decoder.eof or decoder.unused_data
            or decoder.unconsumed_tail or any(pixels[i] > 4 for i in range(0, len(pixels), stride))):
        raise ValueError("invalid PNG pixel stream")


def read_frame(frame: dict, directory: Path, session: str, actor: dict,
               episode: str, sequence: int, previous_begin: int | None) -> tuple[dict, Image]:
    if (frame["frame_schema_version"] != "1.0" or frame["session_id"] != session
            or frame["actor"] != actor or frame["native_tick"] is not None
            or frame["correlation"]["action_effect_ordering"] != "unconfirmed"):
        raise ValueError("renderer frame identity/timing changed")
    identity = frame["image"]
    path = Path(identity["path"])
    if (not path.is_absolute() or path.parent != directory
            or any(p.is_symlink() for p in (path, *path.parents))
            or identity["mime_type"] != "image/png"):
        raise ValueError("frame attachment escaped its owned recording directory")
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if len(data) != identity["byte_length"] or digest != identity["sha256"]:
        raise ValueError("frame PNG byte identity mismatch")
    verify_png(data, identity["width"], identity["height"])
    begin = int(frame["capture_host"]["begin_ns"])
    complete = int(frame["capture_host"]["readback_complete_ns"])
    if complete < begin or (previous_begin is not None and begin < previous_begin):
        raise ValueError("invalid frame capture chronology")
    public_frame = json.loads(json.dumps(frame))
    public_frame["image"].pop("path")
    public_frame["image"].pop("model_attachment_delivered")
    envelope = {
        "schema": SCHEMA, "episode_id": episode, "session_id": session, "actor": actor,
        "observation_sequence": sequence, "frame": public_frame,
        "timing": {"profile": TIMING, "native_tick": None,
                   "headless_timing_equivalent": False, "structured_frame_pairing": "not-synchronized",
                   "capture_duration_ns": str(complete - begin),
                   "capture_begin_interval_ns": None if previous_begin is None else str(begin - previous_begin)},
        "model_image": {"original_sha256": digest, "transformed_sha256": digest,
                        "transformation": "identity", "mime_type": "image/png",
                        "width": identity["width"], "height": identity["height"],
                        "byte_length": len(data), "delivery": "not-yet-submitted"},
    }
    return envelope, Image(data, "image/png", digest, identity["width"], identity["height"])
