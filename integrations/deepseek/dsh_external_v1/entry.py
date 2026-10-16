import argparse
import asyncio
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
import signal
from uuid import uuid4

from scripts.rpg_benchmark import output_paths
from dsh_external_v1.dsh_agent import EFFORT, MODEL, PROVIDER, OfficialRuntime, prepare_profile
from dsh_external_v1.dsh_native_protocol import Evidence, NativeClient, common, error_record
from dsh_external_v1.dsh_provider import CopilotProtocolProxy
from dsh_external_v1.dsh_tools import PlayerTools, ToolBroker
from dsh_external_v1.dsh_timing import stamp


async def run(args):
    out = output_paths.reserve_output(args.out, args.output_root)
    evidence = Evidence(out / "events.ndjson")
    runtime = OfficialRuntime(out, evidence, args.runtime)
    record = {"schema": "dsh-external-entry/1", "harness": "official-deepseek-harness",
              "status": "starting", "cleanup_errors": [], "runtime_model_metadata": "unknown"}
    try:
        if args.mock_provider is None:
            approved = json.loads(Path(os.environ["RPG_EXTERNAL_ADMISSION"]).read_text())
            expected = {
                "schema": "rpg-external-admission/1", "approved_by": "A0",
                "pins_sha256": os.environ["RPG_EXTERNAL_PINS_SHA"],
                "adapter_id": "deepseek-harness",
                "adapter_sha256": os.environ["RPG_EXTERNAL_ADAPTER_SHA"],
                "model": MODEL, "reasoning_effort": "none", "model_calls_admitted": True,
            }
            if any(approved.get(key) != value for key, value in expected.items()):
                raise ValueError("Live dsh entry lacks exact central A0 experiment admission")
        async with AsyncExitStack() as stack:
            def retire(callback):
                async def close():
                    try:
                        await callback()
                    except (RuntimeError, OSError, TimeoutError) as error:
                        record["cleanup_errors"].append(error_record(error))
                        evidence.write("cleanup_error", error=error_record(error))
                        raise
                stack.push_async_callback(close)
                stack.callback(lambda: record.setdefault("terminal", stamp()))

            native = NativeClient(args.episode_socket, evidence)
            retire(native.close)
            await native.connect()
            configuration = json.loads(args.configuration)
            if configuration["model"] != MODEL or configuration["reasoning_effort"] != "none":
                raise ValueError("This dsh profile declares Luna/none; select an explicit provider profile for other models")
            tools = PlayerTools(native, configuration, frames=args.frames, episode=args.episode_id)
            broker = ToolBroker(tools, Path("/tmp") / f"dsh-external-{uuid4().hex}.sock")
            retire(broker.close)
            await broker.start()
            key, session = uuid4().hex, uuid4().hex
            proxy = CopilotProtocolProxy(
                evidence, key, upstream=args.mock_provider, allowed_tools=set(tools.handlers))
            retire(proxy.close)
            url = await proxy.start()
            prepare_profile(out / "home", broker.path, url, session_id=session)
            # Retire the actual Harness before its provider/tool transports.
            # Register before start so partial process creation is owned too.
            retire(runtime.close)
            await runtime.start(out / "home", key)
            record["initialize_receipt"] = await runtime.rpc("initialize", {
                "cwd": str(out / "work"), "provider": PROVIDER, "model": MODEL, "reasoningEffort": EFFORT})
            record["dsh_session_id"] = session
            evidence.write("dsh_entry_ready", **record)
            await runtime.rpc("session/prompt", {
                "sessionId": session, "contentBlocks": [{"type": "text", "text": args.task}]})
            await runtime.rpc("session/wait", {"sessionId": session})
            record["terminal"] = stamp()
            record.update(status="idle", finish_reason=runtime.finished_turn(session))
    except BaseException as error:
        record.setdefault("terminal", stamp())
        record.update(status="cancelled" if isinstance(error, asyncio.CancelledError) else "error",
                      error=error_record(error))
        evidence.write("dsh_entry_error", **record)
    finally:
        record.update(runtime_cleanup=runtime.cleanup, cleanup_end=stamp())
        (out / "entry.json").write_text(json.dumps(record, indent=2) + "\n")
        evidence.close()
    return record


async def signals(args):
    task = asyncio.create_task(run(args))
    loop = asyncio.get_running_loop()
    stopped = False

    def stop():
        nonlocal stopped
        if not stopped:
            stopped = True
            task.cancel()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop)
    return await task


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--episode-socket", required=True, type=Path)
    parser.add_argument("--task", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--mock-provider")
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--configuration", required=True)
    parser.add_argument("--frames", type=Path)
    parser.add_argument("--episode-id", required=True)
    args = parser.parse_args()
    record = asyncio.run(signals(args))
    raise SystemExit(0 if record["status"] == "idle" and not record["cleanup_errors"] else 1)


if __name__ == "__main__":
    main()
