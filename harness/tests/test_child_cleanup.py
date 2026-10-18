import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from scripts.rpg_benchmark import capabilities, providers, responses_entry
from scripts.rpg_benchmark.responses_adapter import Adapter


def test_post_terminal_cancellation_during_native_close_is_deferred_and_recorded():
    async def check():
        started, finish = asyncio.Event(), asyncio.Event()
        async def close():
            started.set()
            await finish.wait()
        entry, evidence = {}, Mock()
        task = asyncio.create_task(responses_entry.close_native(SimpleNamespace(close=close), entry, evidence))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        finish.set()
        await task
        assert entry["cleanup_interruptions"][0]["type"] == "CancelledError"
        evidence.write.assert_called_once()
    asyncio.run(check())


def test_native_close_error_is_not_swallowed_by_cancellation_shield():
    async def close():
        raise RuntimeError("concrete close failure")
    with pytest.raises(RuntimeError, match="concrete close failure"):
        asyncio.run(responses_entry.close_native(SimpleNamespace(close=close), {}, Mock()))


@pytest.mark.parametrize("primary", [None, RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("close_error", [None, RuntimeError, OSError, TimeoutError, asyncio.CancelledError])
@pytest.mark.parametrize("interruptions", [1, 2])
def test_child_entry_persists_after_close_cancellation_and_parent_collects_it(
        primary, close_error, interruptions, tmp_path, monkeypatch):
    calls, started, finish = [], asyncio.Event(), asyncio.Event()

    class Native:
        def __init__(self, *args):
            pass

        async def connect(self):
            pass

        async def close(self):
            calls.append("close")
            started.set()
            await finish.wait()
            if close_error:
                raise close_error("concrete close failure")

    monkeypatch.setattr(responses_entry.common, "NativeClient", Native)
    monkeypatch.setattr(responses_entry, "tools_for",
                        lambda *args, **kwargs: SimpleNamespace(validate_request=lambda *args: None))
    monkeypatch.setattr(responses_entry, "play", AsyncMock(
        side_effect=primary("primary entry failure") if primary else None,
        return_value={"finish_reason": "model_stopped"}))
    configuration = capabilities.negotiate(providers.manifest(providers.LUNA, "structured"), {})[0]
    args = SimpleNamespace(
        out=tmp_path / "entry", output_root=tmp_path, episode_socket=tmp_path / "unused.sock",
        frames=tmp_path / "frames", episode_id="child-close",
        mock_provider="http://127.0.0.1:1234", observation_mode="structured",
        configuration=json.dumps(configuration), provider_declaration=json.dumps(providers.LUNA),
        transport_probe=False, task="unchanged public task")

    async def check():
        task = asyncio.create_task(responses_entry.run(args))
        await started.wait()
        for _ in range(interruptions):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finish.set()
        return await task

    entry = asyncio.run(check())
    assert calls == ["close"]
    assert json.loads((args.out / "entry.json").read_text()) == entry
    assert entry["status"] == (
        "idle" if primary is None else "cancelled" if primary is asyncio.CancelledError else "error")
    if primary:
        assert entry["error"]["type"] == primary.__name__
        assert entry["error"]["message"] == "primary entry failure"
    assert len(entry["cleanup_interruptions"]) == interruptions
    assert [row["type"] for row in entry["cleanup_errors"]] == (
        [close_error.__name__] if close_error else [])
    rows = [json.loads(line) for line in (args.out / "events.ndjson").read_text().splitlines()]
    deferred = [row for row in rows if row["kind"] == "cleanup_cancel_deferred"]
    assert len(deferred) == interruptions
    assert all(row["wall_monotonic"] >= entry["terminal"]["monotonic"] for row in deferred)
    adapter_args = SimpleNamespace(
        adapter_config=json.dumps({"mock_provider": args.mock_provider, "provider": providers.LUNA}),
        observation_mode="structured", model=None, reasoning_effort=None, action_profile=None,
        recordings=args.frames)
    adapter = Adapter(adapter_args)
    adapter.output = args.out
    facts = adapter.collect()
    assert facts["entry"] == entry and facts["entry_receipt_present"]
    assert not facts["collection_errors"]
    assert facts["cleanup_complete"] is (close_error is None)
