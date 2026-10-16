"""One process launch boundary; legacy command construction lives only here."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from . import capabilities, external


def builtin_python(args, selected):
    config = json.loads(args.adapter_config)
    if not isinstance(config, dict) or set(config) - {"python"}:
        raise ValueError("Legacy adapters accept only an explicit isolated python; use external /3 for new integrations")
    if args.harness == "gameagent" and "python" not in config:
        raise ValueError("GameAgent requires adapter-config.python for its independent dependency environment")
    return config.get("python", selected["dependencies"]["python"]["path"])


async def preflight_builtin(args, selected, source):
    """Import the actual entry in its own interpreter, without entering a world/model."""
    configuration = {
        "observation_mode": args.observation_mode or "structured",
        "action_profile": args.action_profile or "raw-native-v2"}
    capabilities.admit_backend(configuration, {"backends": ["headless"]}, args.backend)
    python = Path(builtin_python(args, selected))
    if not python.is_absolute() or not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("Legacy adapter requires an executable absolute interpreter path")
    if args.harness == "gameagent":
        bridge = python.parent / "rpg-episode-bridge"
        if not bridge.is_file() or not os.access(bridge, os.X_OK):
            raise ValueError(f"Install this benchmark in the GameAgent environment; missing entrypoint: {bridge}")
        command = [str(python), "-I", "-B", "-m", "scripts.rpg_benchmark.legacy_preflight",
                   "--source", str(source)]
    else:
        command = [str(python), "-I", "-c",
                   "import importlib.metadata; import copilot; import scripts.rpg_sdk_agent; "
                   "assert importlib.metadata.version('github-copilot-sdk') == '1.0.11'"]
    process = await asyncio.create_subprocess_exec(
        *command, env=external.clean_environment(), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 60)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise ValueError("Agent dependency preflight failed before native world creation. "
                         "Install the Agent's declared dependencies in its selected interpreter. "
                         + output.decode(errors="replace").strip())
    return {"command": command, "exit_code": 0, "python": str(python)}


def plan(args, selected, adapter, *, out, actor, source, task, definition,
         native, socket, sdk_out, sdk_root):
    if adapter is not None:
        return adapter.launch(
            task=task, socket=socket, output=out / "external", output_root=out,
            python=selected["dependencies"]["python"]["path"],
            common_interface=selected["dependencies"]["sdk"]["path"])
    # This is the single deprecated built-in migration boundary. New integrations
    # use the same explicit Launch value, never source-file executable bits.
    from .run import agent_command, sdk_command
    python = builtin_python(args, selected)
    if args.harness == "gameagent":
        command = agent_command(out, actor, python, source, task=task,
                                task_id=f"m1-{args.scene}",
                                gameplay_mode=definition["gameplay_mode"], config=native)
        cwd = Path(source)
    else:
        command = sdk_command(python, selected["dependencies"]["sdk"]["path"],
                              socket, task, sdk_out, sdk_root)
        cwd = out
    return external.Launch(tuple(command), cwd, {"RPG_EPISODE_SOCKET": str(socket)})
