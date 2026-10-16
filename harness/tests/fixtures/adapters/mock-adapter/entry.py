import asyncio
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone
import time

sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[3])
from scripts.rpg_sdk_agent import NativeClient, Evidence
from scripts.rpg_benchmark import output_paths
from scripts.headless_action_validation.run import process_start_ticks


async def main():
    out = output_paths.reserve_output(Path(sys.argv[2]), Path(sys.argv[2]).parent)
    point = {"monotonic": time.monotonic(), "wall": datetime.now(timezone.utc).isoformat()}
    (out / "harness-entry.json").write_text(json.dumps(point))
    evidence = Evidence(out / "native-client.ndjson")
    client = NativeClient(Path(sys.argv[1]), evidence)
    entry = {"status": "idle", "cleanup_errors": [], "synthetic": True}
    try:
        await client.connect()
        await client.request("observe", {})
    except (RuntimeError, OSError, TimeoutError) as error:
        entry.update(status="error", error=str(error))
        raise
    finally:
        await client.close()
        evidence.close()
        (out / "receipt.json").write_text(json.dumps({
            "entry": entry, "cleanup_complete": True,
            "owned_processes": [{"pid": os.getpid(), "start_ticks": process_start_ticks(os.getpid())}],
        }))


asyncio.run(main())
