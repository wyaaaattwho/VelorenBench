import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from scripts.rpg_benchmark import native
from scripts.rpg_sdk_agent import Evidence, NativeClient


class PipeFixture:
    """Real NativeBridge reader/envelope with synthetic native stdout/stdin."""
    def __init__(self, out, *, delay=.04, missing_on_close=False, fault=None):
        self.journal = native.Journal(out / "native.ndjson")
        self.delay, self.missing_on_close, self.fault = delay, missing_on_close, fault
        self.requests, self.active = [], {}
        self.started = asyncio.Event()
        self.stdout = asyncio.StreamReader()
        fixture = self

        class CompressedBridge(native.NativeBridge):
            async def envelope(self, request, *, origin, timeout=60):
                return await super().envelope(
                    request, origin=origin, timeout=.01 if timeout is not None else None)

        self.bridge = CompressedBridge(out, "synthetic", 0, out, self.journal)
        self.bridge.ready = asyncio.get_running_loop().create_future()
        self.bridge.ready.set_result({"event": "ready", "uid": 42,
                                      "session_id": "synthetic", "protocol_version": "0.2"})

        class Input:
            def write(self, payload):
                request = json.loads(payload)
                fixture.requests.append(request)
                if request["cmd"] == "step":
                    fixture.active[request["id"]] = request
                    fixture.started.set()
                    if fixture.fault:
                        fixture.stdout.feed_data(b'{"event":"fatal","detail":"synthetic native fault"}\n')
                    else:
                        asyncio.get_running_loop().call_later(
                            fixture.delay, fixture.finish, request["id"], "completed")
                elif request["cmd"] == "close":
                    for identifier in list(fixture.active):
                        if not fixture.missing_on_close:
                            fixture.finish(identifier, "cancelled")
                    fixture.respond(request, {"closed": True})
                    fixture.stdout.feed_eof()
                else:
                    fixture.respond(request, {"tick": 1, "current_action": (
                        {"status": "running"} if fixture.active else None)})

            async def drain(self):
                await asyncio.sleep(0)

        self.bridge.process = SimpleNamespace(stdin=Input(), stdout=self.stdout)
        self.bridge.reader_task = asyncio.create_task(self.bridge._read())
        self.endpoint = native.SocketEndpoint(out)
        self.boundary = native.EpisodeBoundary(self.bridge, self.endpoint.path)

    def respond(self, request, result):
        self.stdout.feed_data((json.dumps({
            "type": "response", "id": request["id"], "protocol_version": "0.2",
            "session_id": "synthetic", "ok": True, "result": result}) + "\n").encode())

    def finish(self, identifier, status):
        request = self.active.pop(identifier, None)
        if request is not None:
            self.respond(request, {"status": status, "action_id": request["args"]["action"]["action_id"]})

    async def start(self):
        await self.boundary.start()

    async def client(self):
        reader, writer = await asyncio.open_unix_connection(self.boundary.path)
        await reader.readline()
        return reader, writer

    async def shutdown(self):
        self.boundary.begin_close()
        self.bridge.closing = True
        if not self.bridge.reader_task.done():
            await self.bridge.request("close", origin="cleanup")
        await self.bridge.reader_task
        await asyncio.wait_for(self.boundary.close(), 1)
        self.endpoint.close()
        self.journal.close()


def step(identifier="step"):
    return {"id": identifier, "protocol_version": "0.2", "cmd": "step", "args": {
        "action": {"type": "move_to", "position": [19039.5, 341, 10832.5],
                   "action_id": identifier, "speed": .8, "tolerance": 2},
        "duration_ticks": 3600, "timeout_ticks": 36000}}


async def send(writer, request):
    writer.write((json.dumps(request) + "\n").encode())
    await writer.drain()


def test_original_failure_regression_long_action_survives_bridge_watchdog_and_reads(tmp_path):
    async def check():
        fixture = PipeFixture(tmp_path)
        await fixture.start()
        reader, writer = await fixture.client()
        try:
            await send(writer, step())
            await fixture.started.wait()
            await asyncio.sleep(.02)
            assert fixture.bridge.pending and not fixture.boundary.errors
            await send(writer, {"id": "observe", "cmd": "observe", "args": {}})
            observation = json.loads(await reader.readline())
            assert observation["id"] == "observe" and observation["result"]["current_action"]["status"] == "running"
            receipt = json.loads(await reader.readline())
            assert receipt["id"] == "step" and receipt["result"]["status"] == "completed"
            assert len(fixture.bridge.measured_receipts) == 1
        finally:
            writer.close()
            await writer.wait_closed()
            await fixture.shutdown()
        assert not fixture.boundary.errors
    asyncio.run(check())


@pytest.mark.parametrize("client_kind", ["external-common", "copilot-sdk-common", "gameagent-stdio"])
def test_unchanged_external_and_legacy_public_transports(tmp_path, client_kind):
    async def check():
        fixture = PipeFixture(tmp_path, delay=.001)
        await fixture.start()
        process = None
        evidence = None
        try:
            if client_kind == "gameagent-stdio":
                process = await asyncio.create_subprocess_exec(
                    sys.executable, str(Path(native.__file__).with_name("bridge_client.py")),
                    env={**os.environ, "RPG_EPISODE_SOCKET": str(fixture.boundary.path),
                         "PYTHONDONTWRITEBYTECODE": "1"},
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE)
                await process.stdout.readline()
                process.stdin.write((json.dumps(step()) + "\n").encode())
                await process.stdin.drain()
                row = json.loads(await process.stdout.readline())
                assert row["id"] == "step" and row["result"]["status"] == "completed"
                process.stdin.close()
                assert await asyncio.wait_for(process.wait(), 1) == 0
            else:
                evidence = Evidence(tmp_path / "client.ndjson")
                client = NativeClient(fixture.boundary.path, evidence)
                await client.connect()
                try:
                    request, task = client.submit("step", step()["args"])
                    row = await task
                    assert row["id"] == request["id"] and row["result"]["status"] == "completed"
                finally:
                    await client.close()
        finally:
            if process is not None and process.returncode is None:
                process.terminate()
                await process.wait()
            if evidence is not None:
                evidence.close()
            await fixture.shutdown()
        assert not fixture.boundary.errors
    asyncio.run(check())


def test_client_timeout_does_not_destroy_native_receipt_or_connection(tmp_path):
    async def check():
        fixture = PipeFixture(tmp_path, delay=.06)
        evidence = Evidence(tmp_path / "client.ndjson")
        await fixture.start()
        client = NativeClient(fixture.boundary.path, evidence, rpc_timeout=.01)
        await client.connect()
        try:
            request, task = client.submit("step", step()["args"])
            with pytest.raises(TimeoutError):
                await task
            assert not fixture.boundary.errors and request["id"] in fixture.bridge.pending
            response = await client.request("observe", {})
            assert response["result"]["current_action"]["status"] == "running"
            await asyncio.sleep(.07)
            assert len(fixture.bridge.measured_receipts) == 1
            assert not client.failure.done()
            assert request["id"] not in client.abandoned
        finally:
            await client.close()
            await fixture.shutdown()
            evidence.close()
        rows = [json.loads(row) for row in (tmp_path / "client.ndjson").read_text().splitlines()]
        assert any(row["kind"] == "late_native_response" for row in rows)
    asyncio.run(check())


@pytest.mark.parametrize("reset", [False, True])
def test_peer_departure_and_shutdown_finish_known_native_cancellation(tmp_path, reset):
    async def check():
        fixture = PipeFixture(tmp_path, delay=10)
        await fixture.start()
        _, writer = await fixture.client()
        await send(writer, step())
        await fixture.started.wait()
        if reset:
            writer.transport.abort()
        else:
            writer.close()
            await writer.wait_closed()
        await fixture.shutdown()
        assert not fixture.boundary.errors
        receipt = fixture.bridge.measured_receipts[0]
        assert receipt["request"]["id"] == receipt["response"]["id"] == "step"
        assert receipt["response"]["result"]["status"] == "cancelled"
        assert not fixture.bridge.pending
        rows = [json.loads(row) for row in (tmp_path / "native.ndjson").read_text().splitlines()]
        assert any(row.get("event") == "known_response_not_delivered" for row in rows)
    asyncio.run(check())


@pytest.mark.parametrize("missing,fault", [(True, None), (False, "fatal")])
def test_unresolved_mutation_and_true_native_fault_remain_failure_with_nested_provenance(tmp_path, missing, fault):
    async def check():
        fixture = PipeFixture(tmp_path, delay=10, missing_on_close=missing, fault=fault)
        await fixture.start()
        _, writer = await fixture.client()
        await send(writer, step())
        await fixture.started.wait()
        await asyncio.sleep(.01)
        await fixture.shutdown()
        writer.close()
        await writer.wait_closed()
        assert fixture.boundary.errors
        details = fixture.boundary.error_details
        assert details[0]["type"] == "ExceptionGroup"
        assert details[0]["exceptions"][0]["type"] == "RuntimeError"
        assert ("effects unknown" if missing else "native bridge fatal") in details[0]["traceback"]
    asyncio.run(check())


def test_cancelled_dispatch_still_records_late_authoritative_receipt(tmp_path):
    async def check():
        fixture = PipeFixture(tmp_path, delay=.03)
        task = asyncio.create_task(fixture.bridge.envelope(step(), origin="agent", timeout=None))
        await fixture.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert "step" in fixture.bridge.pending
        await asyncio.sleep(.04)
        assert fixture.bridge.measured_receipts[0]["response"]["result"]["status"] == "completed"
        await fixture.start()
        await fixture.shutdown()
    asyncio.run(check())


def test_admin_refusal_ownership_and_malformed_input_not_silenced(tmp_path):
    async def check():
        fixture = PipeFixture(tmp_path)
        await fixture.start()
        reader, writer = await fixture.client()
        await send(writer, {"id": "admin", "cmd": "step", "args": {
            "action": {"type": "server_command", "command": "give"}}})
        row = json.loads(await reader.readline())
        assert row["error"]["code"] == "benchmark_admin_forbidden" and not fixture.requests
        writer.write(b"{malformed}\n")
        await writer.drain()
        assert await reader.readline() == b""
        writer.close()
        await writer.wait_closed()
        await fixture.shutdown()
        assert fixture.boundary.error_details[0]["exceptions"][0]["type"] == "JSONDecodeError"
    asyncio.run(check())
