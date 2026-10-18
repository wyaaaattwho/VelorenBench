"""Installed normal multimodal player adapter; owns no game or judge process."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import time
from uuid import uuid4

from scripts import rpg_sdk_agent as common
from . import lifecycle, output_paths, results
from .player import tools_for
from .responses import ResponsesProvider, play
from .timing import stamp


async def close_native(native, entry, evidence):
    closing = asyncio.create_task(native.close())
    while True:
        try:
            await asyncio.shield(closing)
            return
        except asyncio.CancelledError as error:
            if closing.cancelled():
                raise
            detail = common.error_record(error)
            entry.setdefault("cleanup_interruptions", []).append(detail)
            evidence.write("cleanup_cancel_deferred", error=detail)


async def run(args) -> dict:
    out = output_paths.reserve_output(args.out, args.output_root)
    evidence = common.Evidence(out / "events.ndjson")
    entry = {"schema": "rpg-responses-entry/1", "status": "starting",
             "cleanup_errors": [], "runtime_model_metadata": "unknown"}
    native = common.NativeClient(args.episode_socket, evidence)
    route = None
    try:
        if getattr(args, "endpoint", None) is not None:
            from .provider_route import StandardResponsesRoute
            route = StandardResponsesRoute(args.endpoint, args.api_key_env)
        elif args.mock_provider is None:
            from .luna_route import ExistingLunaRoute
            route = ExistingLunaRoute(args.credential_loader, args.credential_loader_sha256)
        configuration, declaration = json.loads(args.configuration), json.loads(args.provider_declaration)
        if args.observation_mode != configuration["observation_mode"]:
            raise ValueError("entry observation mode differs from negotiated configuration")
        if getattr(args, "credential_loader", None) is not None:
            from .providers import LUNA
            if declaration != LUNA:
                raise ValueError("live route requires its explicitly bound provider capabilities")
        provider = ResponsesProvider(evidence, declaration=declaration, configuration=configuration,
                                     mock_url=args.mock_provider, route=route)
        await native.connect()
        tools = tools_for(native, evidence, configuration, frames=args.frames, episode=args.episode_id)
        lifecycle.write(out / "harness-entry.json", stamp())
        evidence.write("entry_ready", mode=args.observation_mode)
        if args.transport_probe:
            from .player_types import Invocation
            from .responses import content
            result = await tools.handler("frame")(Invocation("transport-probe", "probe-frame", "game_frame", {}))
            if not result.images:
                raise RuntimeError("transport probe lacks verified real frame bytes")
            blocks = content(result, "probe-frame")[1]["content"]
            blocks[0]["text"] = (
                "Briefly describe the visible game screen and any open UI panels. This is an "
                "image-transport engineering check, not a scored ability task. Do not infer hidden state.")
            response = await provider.send(provider.body([{"role": "user", "content": blocks}]))
            lifecycle.write(out / "transport-probe-response.json", common.redact(response))
            entry.update(finish_reason="image-transport-engineering-probe-completed",
                         ultimate_backend="unknown")
        else:
            entry.update(await play(provider, tools, args.task, evidence, session_id=uuid4().hex))
        entry["status"] = "idle"
    except BaseException as error:
        entry.update(status="cancelled" if isinstance(error, asyncio.CancelledError) else "error",
                     error=common.error_record(error))
        detail = results.error_event(error, source="entry", category="adapter_failure")
        if (isinstance(error, asyncio.CancelledError) and error.args
                and isinstance(error.args[0], dict)
                and error.args[0].get("stop_id") == os.environ.get("RPG_EVALUATOR_STOP_ID")
                and error.args[0].get("kind") == "evaluator_stop"):
            detail.update(category="evaluator_cancellation", caused_by=error.args[0])
        entry.update({key: detail[key] for key in ("occurrence", "caused_by", "category") if key in detail})
        evidence.write("entry_error", **detail)
    finally:
        entry["terminal"] = stamp()
        try:
            await close_native(native, entry, evidence)
        except (OSError, RuntimeError, TimeoutError, asyncio.CancelledError) as error:
            entry["cleanup_errors"].append(common.error_record(error))
        lifecycle.write(out / "entry.json", entry)
        if route is not None:
            route.close()
        evidence.close()
    return entry


async def signals(args):
    task = asyncio.create_task(run(args))
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(
            sig, task.cancel, {"kind": "evaluator_stop", "signal": sig.name,
                              "stop_id": os.environ.get("RPG_EVALUATOR_STOP_ID")})
    return await task


def main():
    if sys.argv[1:] == ["--startup-check"]:
        from . import runtime, startup
        receipt = asyncio.run(startup.probe_player(json.loads(sys.stdin.read())))
        print(json.dumps({**receipt, "player_package_files": runtime.package_files()}))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("episode-socket", "out", "output-root"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--frames", type=Path)
    for name in ("task", "episode-id", "observation-mode", "configuration", "provider-declaration"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--mock-provider")
    parser.add_argument("--transport-probe", action="store_true")
    parser.add_argument("--credential-loader", type=Path)
    parser.add_argument("--credential-loader-sha256")
    parser.add_argument("--endpoint")
    parser.add_argument("--api-key-env")
    entry = asyncio.run(signals(parser.parse_args()))
    raise SystemExit(0 if entry["status"] == "idle" and not entry["cleanup_errors"] else 1)


if __name__ == "__main__":
    main()
