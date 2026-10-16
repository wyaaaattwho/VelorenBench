"""Explicit image-block Responses transport, independent of a provider SDK."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from .player_types import Invocation, Result
from . import capabilities, providers


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise RuntimeError("provider redirect refused")


def content(result: Result, call_id: str) -> list[dict]:
    items = [{"type": "function_call_output", "call_id": call_id,
              "output": result.text_result_for_llm}]
    if result.images:
        blocks = [{"type": "input_text", "text": f"Image attachments for tool call {call_id}."}]
        for image in result.images:
            if hashlib.sha256(image.data).hexdigest() != image.sha256:
                raise ValueError("model attachment bytes changed")
            blocks.append({"type": "input_image", "detail": "high",
                           "image_url": f"data:{image.mime_type};base64,"
                           + base64.b64encode(image.data).decode("ascii")})
        items.append({"role": "user", "content": blocks})
    return items


def image_identities(items: list[dict], *, cache: dict | None = None) -> list[dict]:
    identities = []
    for item in items:
        # Responses admits images in user content AND function-call output
        # blocks. Official pi-ai uses the latter; count history on the real wire.
        blocks = item.get("output") if item.get("type") == "function_call_output" else item.get("content")
        for block in blocks if isinstance(blocks, list) else []:
            if block.get("type") == "input_image":
                url = block["image_url"]
                identity = cache.get(url) if cache is not None else None
                if identity is None:
                    header, encoded = url.split(",", 1)
                    data = base64.b64decode(encoded, validate=True)
                    identity = {"sha256": hashlib.sha256(data).hexdigest(),
                                "byte_length": len(data), "media_type": header.split(";")[0][5:],
                                "block_type": "input_image"}
                    if cache is not None:
                        cache[url] = identity
                identities.append(dict(identity))
    return identities


class ResponsesProvider:
    def __init__(self, evidence, *, declaration: dict, configuration: dict,
                 mock_url: str | None = None, route=None):
        if (mock_url is None) == (route is None):
            raise ValueError("select exactly one mock origin or explicitly bound existing route")
        if mock_url is not None:
            url = urlsplit(mock_url)
            if (url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port
                    or url.path or url.query or url.fragment or url.username or url.password):
                raise ValueError("mock provider must be an explicit loopback origin")
        self.evidence, self.mock_url, self.route = evidence, mock_url, route
        selected, _, _ = capabilities.negotiate(
            providers.manifest(declaration, configuration["observation_mode"]),
            {key: configuration[key] for key in ("model", "reasoning_effort", "observation_mode", "action_profile")})
        if selected != configuration:
            raise ValueError("provider configuration differs from negotiated selection")
        self.configuration = selected
        self.response_ids = set()
        self.image_identity_cache = {}

    def body(self, messages, **options):
        return {"model": self.configuration["model"],
                "reasoning": {"effort": self.configuration["reasoning_effort"]},
                "input": messages, "stream": False, **options}

    @staticmethod
    def accepted(value: object, body: dict) -> dict:
        if (not isinstance(value, dict) or value.get("status") != "completed"
                or not isinstance(value.get("id"), str) or not value["id"]
                or not isinstance(value.get("output"), list) or not value["output"]
                or value.get("error") is not None or value.get("incomplete_details") is not None):
            raise RuntimeError("provider lacks an accepted completed Responses envelope")
        names = {tool["name"] for tool in body.get("tools", [])}
        calls, useful = set(), False
        for item in value["output"]:
            if not isinstance(item, dict):
                raise RuntimeError("provider output item must be an object")
            kind = item.get("type")
            if kind == "function_call":
                call_id = item.get("call_id")
                if (item.get("status") != "completed" or item.get("name") not in names
                        or not isinstance(call_id, str) or not call_id or call_id in calls
                        or not isinstance(item.get("arguments"), str)):
                    raise RuntimeError("provider tool call is unaccepted or lacks unique declared identity")
                if not isinstance(json.loads(item["arguments"]), dict):
                    raise RuntimeError("provider tool arguments must be an object")
                calls.add(call_id)
                useful = True
            elif kind == "message":
                if (item.get("status") != "completed" or item.get("role") != "assistant"
                        or not isinstance(item.get("content"), list) or not item["content"]
                        or any(not isinstance(block, dict) or block.get("type") not in {"output_text", "refusal"}
                               or not isinstance(block.get("text" if block.get("type") == "output_text" else "refusal"), str)
                               for block in item["content"])):
                    raise RuntimeError("provider assistant message is not accepted")
                useful = True
            elif kind != "reasoning":
                raise RuntimeError("provider output type is unsupported")
        if not useful:
            raise RuntimeError("provider response has no accepted message or tool call")
        return value

    def _post(self, body: dict) -> dict:
        if self.mock_url is not None:
            endpoint, headers = self.mock_url + "/responses", {"Content-Type": "application/json"}
        else:
            endpoint, headers = self.route()
        try:
            with build_opener(NoRedirect).open(
                    Request(endpoint, data=json.dumps(body, allow_nan=False).encode(), headers=headers),
                    timeout=120) as response:
                if response.status != 200:
                    raise RuntimeError("provider did not return HTTP 200")
                value = json.load(response)
        except HTTPError as error:
            # Provider error bodies/headers may echo authentication or private input.
            raise RuntimeError(f"provider HTTP {error.code}; response body withheld") from None
        return value

    async def send(self, body: dict) -> dict:
        if (body.get("model") != self.configuration["model"]
                or body.get("reasoning") != {"effort": self.configuration["reasoning_effort"]}):
            raise ValueError("transport model/effort differs from negotiated configuration")
        request_id = uuid4().hex
        identities = image_identities(body["input"], cache=self.image_identity_cache)
        self.evidence.write("provider_request", request_id=request_id, model=body["model"],
                            effort=body["reasoning"]["effort"], synthetic=self.mock_url is not None,
                            images=identities, actual_image_blocks=len(identities))
        try:
            response = self.accepted(await asyncio.to_thread(self._post, body), body)
            if response["id"] in self.response_ids:
                raise RuntimeError("provider response identity was reused")
            self.response_ids.add(response["id"])
        except (OSError, ValueError, RuntimeError) as error:
            self.evidence.write("provider_error", request_id=request_id, error_type=type(error).__name__,
                                message="provider request failed; private details withheld",
                                images=identities, effect_status="transport-outcome-unknown")
            raise RuntimeError("provider request failed; consult private transport evidence") from None
        self.evidence.write("provider_response", request_id=request_id, response_id=response["id"],
                            status="completed", images=identities,
                            delivery="provider-accepted-response-not-proof-of-vision-reasoning",
                            provider_reported_model=response.get("model", "unknown"),
                            runtime_model_metadata="unknown")
        return response


async def play(provider: ResponsesProvider, tools, task: str, evidence, *, session_id: str) -> dict:
    handlers = {tool.name: tool.handler for tool in tools.tools}
    definitions = [{"type": "function", "name": tool.name, "description": tool.description,
                    "parameters": tool.parameters, "strict": False} for tool in tools.tools]
    initial = [
        {"role": "system", "content": (
            "Pursue the user's macro goal using only the declared tools. Choose your own actions. "
            "A submitted/applied receipt is not task success. Preserve request and control identities; "
            "never resubmit an uncertain mutation. No filesystem, shell, private judge or admin tool "
            "is available. Durations count variable-dt client updates, not fixed-60Hz/server ticks. "
            "The frame and any structured observation are not synchronized.")},
        {"role": "user", "content": task},
    ]
    contract = await handlers["game_contract"](Invocation(session_id, "initial-contract", "game_contract", {}))
    initial += [{"role": "user", "content": contract.text_result_for_llm}]
    mode = provider.configuration["observation_mode"]
    if mode != "image" and "game_observe" in handlers:
        observation = await handlers["game_observe"](
            Invocation(session_id, "initial-observe", "game_observe", {}))
        if observation.result_type != "success":
            raise RuntimeError("initial public structured observation failed")
        initial.append({"role": "user", "content": observation.text_result_for_llm})
    if mode in {"image", "hybrid"}:
        frame = await handlers["game_frame"](Invocation(session_id, "initial-frame", "game_frame", {}))
        if frame.result_type != "success" or not frame.images:
            raise RuntimeError("initial public frame failed")
        blocks = content(frame, "initial-frame")[1]["content"]
        blocks[0]["text"] = frame.text_result_for_llm
        initial.append({"role": "user", "content": blocks})
    messages, completed = list(initial), {}
    while True:
        response = await provider.send(provider.body(messages, tools=definitions))
        outputs = response["output"]
        messages.extend(outputs)
        calls = [item for item in outputs if item.get("type") == "function_call"]
        if not calls:
            evidence.write("model_stop", response_id=response.get("id"))
            return {"finish_reason": "provider-completed", "runtime_model_metadata": "unknown"}
        for call in calls:
            name, call_id = call.get("name"), call.get("call_id")
            if name not in handlers or not isinstance(call_id, str) or not call_id:
                raise ValueError("provider selected an undeclared tool or lacks call identity")
            args = json.loads(call["arguments"])
            if call_id in completed:
                previous_name, previous_args, result = completed[call_id]
                if (name, args) != (previous_name, previous_args):
                    raise ValueError("provider reused a call identity with different intent")
                evidence.write("model_duplicate_call", id=call_id, name=name, resubmitted=False)
                messages.extend(content(result, call_id))
                continue
            evidence.write("model_tool_call", id=call_id, name=name, arguments=args,
                           provider_response_id=response["id"])
            result = await handlers[name](Invocation(session_id, call_id, name, args))
            completed[call_id] = name, args, result
            messages.extend(content(result, call_id))
