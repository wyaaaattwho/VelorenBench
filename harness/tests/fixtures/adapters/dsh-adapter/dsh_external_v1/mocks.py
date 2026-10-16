"""Local protocol fixtures only, never model ability measurements."""
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from aiohttp import web


class NativeFixture:
    def __init__(self, *, refusal=False, delay=.01):
        self.path = Path("/tmp") / f"dsh-common-mock-{uuid4().hex}.sock"
        self.refusal, self.delay = refusal, delay
        self.requests = []
        self.writers = set()

    async def start(self):
        self.server = await asyncio.start_unix_server(self.connection, str(self.path))

    async def connection(self, reader, writer):
        self.writers.add(writer)
        writer.write((json.dumps({"event": "ready", "session_id": "mock-native", "uid": 42,
                                  "protocol_version": "0.2"}) + "\n").encode())
        await writer.drain()

        async def dispatch(row):
            self.requests.append(row)
            if row["cmd"] == "step":
                await asyncio.sleep(self.delay)
            response = {"type": "response", "id": row["id"], "session_id": "mock-native",
                        "protocol_version": "0.2", "ok": not (self.refusal and row["cmd"] == "step")}
            if response["ok"]:
                response["result"] = {"status": "completed", "tick": len(self.requests),
                                      "player_visible": True}
            else:
                response["error"] = {"code": "stale_control_fence", "message": "Synthetic native refusal"}
            writer.write((json.dumps(response) + "\n").encode())
            await writer.drain()
        try:
            async with asyncio.TaskGroup() as group:
                while line := await reader.readline():
                    group.create_task(dispatch(json.loads(line)))
        finally:
            writer.close()
            await writer.wait_closed()
            self.writers.discard(writer)

    async def close(self):
        self.server.close()
        await self.server.wait_closed()
        for writer in tuple(self.writers):
            writer.close()
        await asyncio.sleep(.02)
        self.path.unlink(missing_ok=True)


class Provider:
    def __init__(self, *, failure=False):
        self.failure = failure
        self.requests = []

    async def start(self):
        app = web.Application()
        app.router.add_post("/responses", self.response)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        return f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    async def response(self, request):
        body = await request.json()
        self.requests.append(body)
        if self.failure:
            return web.json_response({"error": {"message": "Synthetic provider failure"}}, status=402)
        n = len(self.requests)
        if n <= 3:
            names = ("game_observe", "game_step", "game_result")
            if n == 1:
                args = {}
            elif n == 2:
                args = {"action": {"type": "jump", "action_id": "offline-jump-once"},
                        "duration_ticks": 1, "timeout_ticks": 2}
            else:
                outputs = [row for row in body["input"] if row.get("type") == "function_call_output"]
                args = {"request_id": json.loads(outputs[-1]["output"])["request_id"], "wait": True}
            item = {"type": "function_call", "id": f"fc_{n}", "call_id": f"call_{n}",
                    "name": names[n - 1], "arguments": json.dumps(args), "status": "completed"}
        else:
            item = {"type": "message", "id": "msg_4", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": "Synthetic protocol check complete.",
                                 "annotations": []}]}
        response = {"id": f"resp_{n}", "object": "response", "created_at": 0,
                    "status": "completed", "model": "gpt-6-luna", "output": [item],
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                              "input_tokens_details": {"cached_tokens": 0},
                              "output_tokens_details": {"reasoning_tokens": 0}}}
        events = [
            {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
            {"type": "response.output_item.added", "output_index": 0, "item": item},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": response},
        ]
        return web.Response(text="".join(f"data: {json.dumps(row)}\n\n" for row in events),
                            content_type="text/event-stream")

    async def close(self):
        await self.runner.cleanup()
