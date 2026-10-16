import asyncio
import json
from types import SimpleNamespace

import pytest

from scripts.rpg_benchmark import native


@pytest.mark.parametrize("departed,missing", [(False, False), (True, False), (False, True)])
def test_original_frame_receipt_precedes_native_retirement(tmp_path, monkeypatch, departed, missing):
    async def check():
        journal = native.Journal(tmp_path / "native.ndjson")
        bridge = native.NativeBridge(tmp_path, "frame-test", 0, tmp_path, journal)
        stdout = asyncio.StreamReader()
        written = asyncio.Event()
        order = []

        def respond(request, result):
            order.append("receipt:" + request["cmd"])
            stdout.feed_data((json.dumps({"type": "response", "protocol_version": "0.2",
                                         "session_id": "frame-test", "id": request["id"],
                                         "ok": True, "result": result}) + "\n").encode())

        class Input:
            def write(self, payload):
                request = json.loads(payload)
                order.append("request:" + request["cmd"])
                if request["cmd"] == "frame":
                    written.set()
                    if not missing:
                        asyncio.get_running_loop().call_later(
                            .02, lambda: respond(request, {"frame_id": "original"})
                            if bridge.process.returncode is None else None)
                else:
                    respond(request, {"closed": True})

            async def drain(self):
                await asyncio.sleep(0)

        async def stop(process, start, record):
            order.append("retire")
            process.returncode = -15
            stdout.feed_eof()
            record.update(stopped=True)

        monkeypatch.setattr(native, "stop_owned", stop)
        wait_for = asyncio.wait_for

        async def compressed_wait(awaitable, timeout):
            return await wait_for(awaitable, .05 if timeout == 60 else timeout)

        monkeypatch.setattr(asyncio, "wait_for", compressed_wait)
        bridge.process = SimpleNamespace(stdin=Input(), stdout=stdout, returncode=None)
        bridge.start_ticks = 1
        bridge.ready = asyncio.get_running_loop().create_future()
        bridge.ready.set_result({"protocol_version": "0.2", "session_id": "frame-test", "uid": 3})
        bridge.reader_task = asyncio.create_task(bridge._read())
        frame = asyncio.create_task(bridge.envelope(
            {"id": "original-frame", "cmd": "frame", "args": {}}, origin="agent", timeout=None))
        await written.wait()
        if departed:
            frame.cancel()
            with pytest.raises(asyncio.CancelledError):
                await frame
        try:
            if missing:
                with pytest.raises(TimeoutError):
                    await bridge.close()
                with pytest.raises(RuntimeError, match="without terminal receipts"):
                    await frame
                assert bridge.failed and "request:close" not in order and "retire" in order
                assert bridge.error_observations[-1]["effect_status"] == "unknown"
                assert not bridge.effect_observations
                return
            await bridge.close()
            if not departed:
                assert (await frame)["result"] == {"frame_id": "original"}
            assert order.index("receipt:frame") < order.index("request:close") < order.index("retire")
            assert not bridge.failed and not bridge.pending
            assert len([row for row in order if row == "request:frame"]) == 1
        finally:
            if not frame.done():
                await asyncio.gather(frame, return_exceptions=True)
            journal.close()

    asyncio.run(check())
