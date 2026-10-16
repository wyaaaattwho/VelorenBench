"""The public tool seam shared by external processes and bundled transports.

The evaluator owns the backend, projection boundary, fixture and termination.
Adapters own model calls. Neither tool selection nor media delivery grants access
to the private evaluator pipe.
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from scripts import rpg_sdk_agent as common
from .player_types import Result
from .renderer_tools import RendererTools

CONTRACT = "rpg-player-tools/1"


def tools_for(native, evidence, configuration: dict, *, frames: Path | None, episode: str):
    mode = configuration["observation_mode"]
    if "renderer" in native.ready:
        if frames is None:
            raise ValueError("Renderer tools require the owned frame directory")
        tools = RendererTools(native, evidence, mode, frames, episode,
                              configuration["action_profile"])
        native.request_validator = tools.validate_request
        return tools
    if mode != "structured" or configuration["action_profile"] != "raw-native-v2":
        raise ValueError("Headless tools support only structured/raw-native-v2")
    return common.GameTools(native, evidence)


def content_blocks(result: Result) -> list[dict]:
    """Broker content blocks, containing bytes, never local image paths.

    A Harness with an attachment store must admit these bytes into its official
    image-reference vocabulary, not pass the broker block directly as history.
    """
    blocks = [{"type": "text", "text": result.text_result_for_llm}]
    for image in result.images:
        if hashlib.sha256(image.data).hexdigest() != image.sha256:
            raise ValueError("Model attachment bytes changed")
        blocks.append({"type": "image", "data": base64.b64encode(image.data).decode("ascii"),
                       "mimeType": image.mime_type})
    return blocks
