import json
import os
from pathlib import Path

from scripts.rpg_benchmark.external import Launch, EVIDENCE

ROOT = Path(__file__).resolve().parent
DEPENDENCIES = ROOT.parents[2] / "deepseek-harness-baseline-20261015"


def build_launch(context):
    config = context["config"]
    if set(config) - {"mock_provider"}:
        raise ValueError("Unknown dsh adapter option")
    mock = config.get("mock_provider")
    if context["engineering"] != (mock is not None):
        raise ValueError("Dsh engineering requires local mock; live mode forbids provider override")
    if mock is not None:
        from urllib.parse import urlsplit
        url = urlsplit(mock)
        if (url.scheme != "http" or url.hostname != "127.0.0.1" or url.port is None
                or url.path or url.query or url.fragment or url.username or url.password):
            raise ValueError("Mock provider must be an explicit loopback origin")
    common = Path(context["common_interface"])
    harness = common.parents[1]
    env = {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join(map(str, [
            ROOT, harness, DEPENDENCIES / "venv/lib/python3.13/site-packages"])),
        "RPG_COMMON_INTERFACE": str(common),
        "RPG_EXTERNAL_ADMISSION": context["admission"] or "",
        "RPG_EXTERNAL_PINS_SHA": context["pins_sha256"] or "",
        "RPG_EXTERNAL_ADAPTER_SHA": context["adapter_sha256"],
        "RPG_DSH_RUNTIME": str(DEPENDENCIES / "runtime"),
    }
    command = [context["python"], "-m", "dsh_external_v1.entry",
               "--episode-socket", context["episode_socket"], "--task", context["task"],
               "--out", context["output"], "--output-root", context["output_root"]]
    if mock is not None:
        command += ["--mock-provider", mock]
    return Launch(tuple(command), ROOT, env)


def collect_evidence(context):
    out = Path(context["output"])
    entry = json.loads((out / "entry.json").read_text())
    rows = [json.loads(line) for line in (out / "events.ndjson").read_text().splitlines()]
    requests = [{"model": row["model"], "effort": row["reasoning_effort"],
                 "synthetic": row["offline_mock"], "wall_monotonic": row["wall_monotonic"]}
                for row in rows if row["kind"] == "provider_request"]
    calls = []
    submissions = []
    for row in rows:
        if row["kind"] == "dsh_jsonrpc" and row["envelope"].get("method") == "session.event":
            event = row["envelope"]["params"]["event"]
            if event["type"] == "tool/call":
                data = event["data"]
                calls.append({"id": data["callId"], "name": data["name"],
                              "arguments": json.loads(data["arguments"])})
        if (row["kind"] == "tool_result" and row["payload"].get("state") == "submitted"
                or row["kind"] == "tool_native_submission"):
            payload = row["payload"] if row["kind"] == "tool_result" else row
            submissions.append({"call_id": row["tool_call_id"], "request": payload["request"]})
    errors = [row for row in rows if row["kind"] in {"provider_error", "provider_rejected"}
              or row["kind"] == "provider_response" and row["status"] >= 400]
    return {
        "schema": EVIDENCE, "adapter_id": context["adapter_id"],
        "player_interface": context["player_interface"], "entry": entry,
        "entry_receipt_present": any(row["kind"] == "dsh_entry_ready" for row in rows),
        "model_requests": requests, "tool_calls": calls, "submissions": submissions,
        "provider_errors": errors, "owned_processes": entry["runtime_cleanup"].get("owned_processes", []),
        "error_observations": [
            {"source": ("transport" if row["kind"] == "transport_error" else
                        "native_refusal" if row.get("error", {}).get("type") == "NativeError" else
                        "native_client_request" if row["kind"] == "request_error" else "adapter"), **row}
            for row in rows if row["kind"] in {
                "transport_error", "request_error", "broker_error", "dsh_mapping_error"}],
        "cleanup_complete": not entry["cleanup_errors"] and entry["runtime_cleanup"].get("stopped") is True,
    }
