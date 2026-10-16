"""Source-only adapter regression with deterministic synthetic stdio peers."""
import asyncio
import json
import os
from pathlib import Path
import sys
import unittest

from dsh_external_v1.dsh_agent import OfficialRuntime, identity_gone, start_ticks
from dsh_external_v1.dsh_native_protocol import Evidence

ROOT = Path(__file__).resolve().parent
OUTPUT = Path(os.environ["DSH_RPC_TEST_OUTPUT"])


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(.001)


class RpcLifecycle(unittest.IsolatedAsyncioTestCase):
    async def make_runtime(self, scenario):
        out = OUTPUT / self.id().split(".")[-1] / scenario
        out.mkdir(parents=True, exist_ok=False)
        evidence = Evidence(out / "events.ndjson")
        runtime = OfficialRuntime(out, evidence, Path("/synthetic-stdio-only"))
        runtime.stderr = (out / "stderr.log").open("x")
        runtime.process = await asyncio.create_subprocess_exec(
            sys.executable, "-B", str(ROOT / "stdio_peer.py"), scenario,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=runtime.stderr)
        runtime.owned[runtime.process.pid] = start_ticks(runtime.process.pid)
        runtime.reader = asyncio.create_task(runtime._read())

        async def cleanup():
            await runtime.close()
            evidence.close()
            (out / "cleanup.json").write_text(json.dumps(runtime.cleanup, indent=2) + "\n")
            self.assertTrue(runtime.cleanup["stopped"])
            self.assertTrue(all(identity_gone(pid, ticks) for pid, ticks in runtime.owned.items()))
            self.assertTrue(all(row["identity_gone_or_reaped"]
                                for row in runtime.cleanup["owned_processes"]))
        self.addAsyncCleanup(cleanup)
        return runtime, out

    def events(self, out, kind=None):
        rows = [json.loads(line) for line in (out / "events.ndjson").read_text().splitlines()]
        return [row for row in rows if kind is None or row["kind"] == kind]

    async def cancelled_wait(self, runtime, out):
        task = asyncio.create_task(runtime.rpc("session/wait", {"sessionId": "synthetic"}))
        await until(lambda: any(row.get("envelope", {}).get("method") == "test.wait_accepted"
                                for row in self.events(out)))
        task.cancel("synthetic evaluator horizon / SIGINT")
        with self.assertRaisesRegex(asyncio.CancelledError, "synthetic evaluator horizon / SIGINT"):
            await task
        self.assertFalse(runtime.pending)
        return task

    async def late_reply(self, scenario):
        runtime, out = await self.make_runtime(scenario)
        caller = await self.cancelled_wait(runtime, out)
        await runtime.close()
        self.assertIsNone(runtime.transport_failure)
        self.assertEqual(runtime.cleanup["exit_code"], 0)
        self.assertTrue(caller.cancelled(), "Late receipt must never resurrect caller/action")
        self.assertFalse(runtime.pending)
        self.assertFalse(runtime.turns, "A wait response is not a turn or native action")
        self.assertFalse(self.events(out, "dsh_transport_error"))
        self.assertFalse(self.events(out, "runtime_shutdown_error"))
        requests = self.events(out, "dsh_rpc_request")
        self.assertEqual([row["method"] for row in requests], ["session/wait", "shutdown"])
        self.assertNotEqual(requests[0]["request_id"], requests[1]["request_id"])
        cancellations = self.events(out, "dsh_rpc_cancelled")
        self.assertEqual(len(cancellations), 1)
        self.assertEqual(cancellations[0]["request_id"], requests[0]["request_id"])
        self.assertEqual(cancellations[0]["cause"], {
            "type": "CancelledError", "message": "synthetic evaluator horizon / SIGINT"})
        late = self.events(out, "dsh_rpc_late_cancelled_response")
        self.assertEqual(len(late), 1)
        self.assertEqual(late[0]["request_id"], requests[0]["request_id"])
        self.assertEqual(late[0]["method"], "session/wait")
        responses = self.events(out, "dsh_rpc_response")
        self.assertEqual({row["request_id"] for row in responses},
                         {row["request_id"] for row in requests})
        shutdown = [row for row in responses if row["method"] == "shutdown"]
        self.assertEqual(len(shutdown), 1)
        self.assertEqual(shutdown[0]["disposition"], "active")
        self.assertEqual(len(self.events(out, "dsh_jsonrpc")), 3)

    async def test_late_cancelled_wait_before_shutdown(self):
        await self.late_reply("wait-first")

    async def test_late_cancelled_wait_after_shutdown(self):
        await self.late_reply("shutdown-first")

    async def protocol_failure(self, scenario, message):
        runtime, out = await self.make_runtime(scenario)
        await self.cancelled_wait(runtime, out)
        runtime.closing = True
        shutdown = asyncio.create_task(runtime.rpc("shutdown", {}))
        await asyncio.wait_for(runtime.reader, 3)
        self.assertIsInstance(runtime.transport_failure, RuntimeError)
        self.assertIn(message, str(runtime.transport_failure))
        self.assertEqual(len(self.events(out, "dsh_transport_error")), 1)
        result = (await asyncio.gather(shutdown, return_exceptions=True))[0]
        if scenario not in {"duplicate-active"}:
            self.assertIsInstance(result, RuntimeError)
        with self.assertRaises(RuntimeError):
            await runtime.rpc("session/wait", {})
        await runtime.process.wait()
        return runtime, out

    async def test_actually_unknown_id_remains_fatal(self):
        await self.protocol_failure("unknown", "unknown/duplicate")

    async def test_duplicate_cancelled_id_remains_fatal(self):
        _, out = await self.protocol_failure("duplicate-cancelled", "unknown/duplicate")
        self.assertEqual(len(self.events(out, "dsh_rpc_late_cancelled_response")), 1)

    async def test_duplicate_active_id_remains_fatal(self):
        await self.protocol_failure("duplicate-active", "unknown/duplicate")

    async def test_cancelled_request_error_is_not_swallowed(self):
        await self.protocol_failure("cancelled-error", "synthetic provider failure")

    async def test_active_rpc_error_still_reaches_caller(self):
        runtime, out = await self.make_runtime("active-error")
        await self.cancelled_wait(runtime, out)
        runtime.closing = True
        with self.assertRaisesRegex(RuntimeError, "synthetic shutdown failure"):
            await runtime.rpc("shutdown", {})
        await asyncio.wait_for(runtime.reader, 3)
        self.assertIsInstance(runtime.transport_failure, RuntimeError)

    async def test_unexpected_pipe_closure_remains_fatal(self):
        await self.protocol_failure("pipe-closure", "stdout closed")

    async def test_future_cancelled_before_rpc_coroutine_resumes(self):
        # Force the scheduling window: cancellation propagates to the awaited
        # future, then the reader runs before rpc's except/finally.
        runtime = OfficialRuntime(OUTPUT, None, Path("/synthetic"))
        rows = []
        runtime.evidence = type("Evidence", (), {"write": lambda _, kind, **row:
                                                rows.append({"kind": kind, **row})})()
        reader = asyncio.StreamReader()
        written = []

        class Writer:
            def write(self, data):
                written.append(json.loads(data))

            async def drain(self):
                pass

        runtime.process = type("Process", (), {"stdin": Writer(), "stdout": reader})()
        caller = asyncio.create_task(runtime.rpc("session/wait", {}))
        await until(lambda: bool(written))
        transport = asyncio.create_task(runtime._read())
        # Wake the reader before waking the cancelled rpc task.
        reader.feed_data((json.dumps({"id": written[0]["id"], "result": {}}) + "\n").encode())
        caller.cancel("queued cancellation cause")
        await asyncio.sleep(0)
        with self.assertRaisesRegex(asyncio.CancelledError, "queued cancellation cause"):
            await caller
        self.assertIsNone(runtime.transport_failure)
        self.assertEqual(len([row for row in rows if row["kind"] ==
                              "dsh_rpc_late_cancelled_response"]), 1)
        self.assertEqual([row["cause"]["message"] for row in rows
                          if row["kind"] == "dsh_rpc_cancelled"], ["queued cancellation cause"])
        transport.cancel()
        await asyncio.gather(transport, return_exceptions=True)

    async def test_cancellation_during_drain_is_owned(self):
        out = OUTPUT / self.id().split(".")[-1]
        out.mkdir()
        evidence = Evidence(out / "events.ndjson")
        self.addCleanup(evidence.close)
        runtime = OfficialRuntime(out, evidence, Path("/synthetic"))
        reader = asyncio.StreamReader()
        written = []
        blocked = asyncio.Event()

        class Writer:
            def write(self, data):
                written.append(json.loads(data))

            async def drain(self):
                await blocked.wait()

        runtime.process = type("Process", (), {"stdin": Writer(), "stdout": reader})()
        caller = asyncio.create_task(runtime.rpc("session/wait", {}))
        await until(lambda: bool(written))
        caller.cancel("cancel during drain")
        with self.assertRaises(asyncio.CancelledError):
            await caller
        reader.feed_data((json.dumps({"id": written[0]["id"], "result": {}}) + "\n").encode())
        transport = asyncio.create_task(runtime._read())
        self.addAsyncCleanup(self.stop_reader, transport)
        await asyncio.sleep(0)
        self.assertIsNone(runtime.transport_failure)
        self.assertEqual(len(self.events(out, "dsh_rpc_late_cancelled_response")), 1)

    async def test_closing_without_shutdown_receipt_does_not_hide_eof(self):
        runtime = OfficialRuntime(OUTPUT, None, Path("/synthetic"))
        rows = []
        runtime.evidence = type("Evidence", (), {"write": lambda _, kind, **row:
                                                rows.append({"kind": kind, **row})})()
        reader = asyncio.StreamReader()
        runtime.process = type("Process", (), {"stdout": reader})()
        runtime.closing = True
        reader.feed_eof()
        await runtime._read()
        self.assertIsInstance(runtime.transport_failure, RuntimeError)
        self.assertEqual(len([row for row in rows if row["kind"] == "dsh_transport_error"]), 1)

    async def stop_reader(self, task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
