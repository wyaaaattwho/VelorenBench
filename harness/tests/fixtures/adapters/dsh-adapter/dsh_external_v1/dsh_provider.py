from __future__ import annotations

import asyncio
import re
import time

from aiohttp import ClientError, ClientSession, ClientTimeout, web
from dsh_external_v1.dsh_credentials import _get_oauth_token

MODEL = "gpt-6-luna"
EFFORT = "none"
TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
PUBLIC_HEADERS = {
    "User-Agent": "GitHubCopilotChat/0.35.0",
    "Editor-Version": "vscode/1.107.0",
    "Editor-Plugin-Version": "copilot-chat/0.35.0",
    "Copilot-Integration-Id": "vscode-chat",
}


def aligned_request(body: dict) -> dict:
    if not isinstance(body, dict):
        raise ValueError("Expected a Responses request object")
    if body.get("model") != MODEL:
        raise ValueError("Only the exact admitted gpt-6-luna model is supported")
    reasoning = body.get("reasoning", {})
    if not isinstance(reasoning, dict) or reasoning.get("effort", EFFORT) != EFFORT:
        raise ValueError("Only explicit none reasoning is admitted")
    if not isinstance(body.get("input"), list):
        raise ValueError("Expected the official Responses input array")
    return {**body, "reasoning": {**reasoning, "effort": EFFORT}}


class CopilotProtocolProxy:
    """Correct wire-only off omission; never plans, enables models, or lists them."""

    def __init__(self, evidence, local_key: str, *, upstream: str | None = None, allowed_tools=None):
        self.evidence, self.local_key, self.upstream = evidence, local_key, upstream
        self.allowed_tools = allowed_tools
        self.runner = None
        self.session = None
        self.access = None
        self.expires = 0
        self.base_url = None
        self.requests = set()

    async def start(self) -> str:
        self.session = ClientSession(timeout=ClientTimeout(total=None, sock_connect=30),
                                     trust_env=True)
        app = web.Application(client_max_size=0)
        app.router.add_post("/responses", self._response)
        self.runner = web.AppRunner(app, shutdown_timeout=1)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    async def _credential(self) -> tuple[str, str]:
        if self.upstream is not None:
            return "offline-mock-only", self.upstream
        if time.time() >= self.expires:
            oauth = _get_oauth_token(allow_interactive_auth=False)
            async with self.session.get(
                    TOKEN_URL, headers={**PUBLIC_HEADERS, "Accept": "application/json",
                                        "Authorization": f"token {oauth}"}) as response:
                if response.status != 200:
                    raise RuntimeError(f"Copilot credential exchange HTTP {response.status}")
                result = await response.json()
            access, expiry = result.get("token"), result.get("expires_at")
            if not isinstance(access, str) or not access or type(expiry) not in (int, float):
                raise RuntimeError("Invalid Copilot credential exchange fields")
            match = re.search(r"proxy-ep=([^;]+)", access)
            host = match.group(1).replace("proxy.", "api.", 1) if match else "api.individual.githubcopilot.com"
            if not re.fullmatch(r"api\.(?:individual|business|enterprise)\.githubcopilot\.com", host):
                raise RuntimeError("Copilot token endpoint is outside configured official account routes")
            self.access, self.expires, self.base_url = access, expiry - 300, f"https://{host}"
        return self.access, self.base_url

    async def _response(self, request: web.Request) -> web.StreamResponse:
        task = asyncio.current_task()
        self.requests.add(task)
        try:
            if request.headers.get("Authorization") != f"Bearer {self.local_key}":
                raise web.HTTPForbidden(text="local provider authorization rejected")
            try:
                body = aligned_request(await request.json())
                if self.allowed_tools is not None and {tool.get("name") for tool in body.get("tools", [])} != self.allowed_tools:
                    raise ValueError("Official dsh must expose exactly the unchanged native player tools")
            except (ValueError, TypeError) as error:
                self.evidence.write("provider_rejected", error=type(error).__name__)
                raise web.HTTPBadRequest(text=str(error)) from error
            self.evidence.write("provider_request", model=body["model"],
                                reasoning_effort=body["reasoning"]["effort"],
                                offline_mock=self.upstream is not None,
                                tool_names=[tool.get("name") for tool in body.get("tools", [])])
            access, base = await self._credential()
            headers = {**PUBLIC_HEADERS, "Authorization": f"Bearer {access}",
                       "Content-Type": "application/json",
                       "X-Initiator": request.headers.get("X-Initiator", "user"),
                       "Openai-Intent": "conversation-edits"}
            async with self.session.post(base + "/responses", json=body, headers=headers) as upstream:
                self.evidence.write("provider_response", status=upstream.status,
                                    runtime_model_metadata="unknown")
                response = web.StreamResponse(
                    status=upstream.status, headers={"Content-Type": upstream.headers.get(
                        "Content-Type", "application/json")})
                await response.prepare(request)
                async for chunk in upstream.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
                return response
        except (ClientError, OSError, RuntimeError, ValueError, asyncio.TimeoutError) as error:
            self.evidence.write("provider_error", error_type=type(error).__name__,
                                message="provider transport/credential failure; details withheld to protect credentials")
            raise web.HTTPBadGateway(text="provider transport/credential failure") from None
        finally:
            self.requests.discard(task)

    async def close(self) -> None:
        for task in list(self.requests):
            task.cancel()
        await asyncio.gather(*self.requests, return_exceptions=True)
        if self.runner is not None:
            await self.runner.cleanup()
        if self.session is not None:
            await self.session.close()
        self.access = None
