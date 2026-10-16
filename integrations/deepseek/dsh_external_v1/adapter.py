import json
import os
import hashlib
import importlib.util
from pathlib import Path

from scripts.rpg_benchmark.external import Launch, EVIDENCE

ROOT = Path(__file__).resolve().parent


def validate_config(context):
    config = context["config"]
    if set(config) - {"mock_provider", "runtime"} or "runtime" not in config:
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
    runtime = Path(config["runtime"])
    if not runtime.is_absolute() or not (runtime / "node_modules/.bin/dsh").is_file():
        raise ValueError("Select the installed official dsh runtime, not an adapter alias")
    if "runtime" in context and str(runtime) != context["runtime"]["preflight"][-1]:
        raise ValueError("Launch runtime differs from the preflight-bound runtime")


def build_launch(context):
    validate_config(context)
    config = context["config"]
    mock = config.get("mock_provider")
    common = Path(context["common_interface"])
    env = {
        "PYTHONDONTWRITEBYTECODE": "1",
        "RPG_COMMON_INTERFACE_SHA256": hashlib.sha256(common.read_bytes()).hexdigest(),
        "RPG_EXTERNAL_ADMISSION": context["admission"] or "",
        "RPG_EXTERNAL_PINS_SHA": context["pins_sha256"] or "",
        "RPG_EXTERNAL_ADAPTER_SHA": context["adapter_sha256"],
    }
    command = [context["python"], "-I", "-m", "dsh_external_v1.entry",
               "--episode-socket", context["episode_socket"], "--task", context["task"],
               "--out", context["output"], "--output-root", context["output_root"],
               "--runtime", config["runtime"], "--episode-id", context["episode_id"],
               "--configuration", json.dumps(context["configuration"])]
    if context["frames"] is not None:
        command += ["--frames", context["frames"]]
    if mock is not None:
        command += ["--mock-provider", mock]
    return Launch(tuple(command), ROOT, env)


def collect_evidence(context):
    out = Path(context["output"])
    entry = json.loads((out / "entry.json").read_text())
    rows = [json.loads(line) for line in (out / "events.ndjson").read_text().splitlines()]
    translated = [row for row in rows if row["kind"] == "dsh_common_tool_mapping"
                  and "native_arguments" in row]
    translations = {row["dsh_call_id"]: row for row in translated}
    if len(translations) != len(translated):
        raise ValueError("Duplicate integer-argument translation for one model call")
    if translations:
        # Load the same frozen, stdlib/core-only codec without importing the adapter venv.
        spec = importlib.util.spec_from_file_location("velorenbench_dsh_schema", ROOT / "dsh_schema.py")
        if spec is None or spec.loader is None:
            raise ValueError("Frozen argument codec is unavailable")
        codec = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(codec)
    requests = [{**row, "effort": row["reasoning_effort"],
                 "synthetic": row["offline_mock"]} for row in rows if row["kind"] == "provider_request"]
    calls = []
    submissions = []
    for row in rows:
        if row["kind"] == "dsh_jsonrpc" and row["envelope"].get("method") == "session.event":
            event = row["envelope"]["params"]["event"]
            if event["type"] == "tool/call":
                data = event["data"]
                wire = json.loads(data["arguments"])
                call = {"id": data["callId"], "name": data["name"], "arguments": wire}
                translation = translations.get(data["callId"])
                if translation is not None:
                    if translation["tool_name"] != data["name"] or translation["wire_arguments"] != wire:
                        raise ValueError("Integer translation differs from observed model call")
                    arguments = codec.native_arguments(translation["argument_schema"], wire)
                    if arguments != translation["native_arguments"]:
                        raise ValueError("Recorded integer translation differs from the frozen codec")
                    call.update(arguments=arguments, wire_arguments=wire,
                                argument_transport="schema-scoped-decimal-integer")
                calls.append(call)
        if row["kind"] == "tool_result" and row["payload"].get("state") == "submitted" and "request" in row["payload"]:
            submissions.append({"call_id": row["tool_call_id"], "request": row["payload"]["request"]})
        if row["kind"] == "tool_native_submission":
            submissions.append({"call_id": row["tool_call_id"], "request": row["request"]})
    errors = [row for row in rows if row["kind"] in {"provider_error", "provider_rejected"}
              or row["kind"] == "provider_response" and row["status"] >= 400]
    return {
        "schema": EVIDENCE, "adapter_id": context["adapter_id"],
        "player_interface": context["player_interface"], "entry": entry,
        "entry_receipt_present": any(row["kind"] == "dsh_entry_ready" for row in rows),
        "model_requests": requests, "tool_calls": calls, "submissions": submissions,
        "provider_errors": errors, "owned_processes": entry["runtime_cleanup"].get("owned_processes", []),
        "error_observations": [{"source": "transport", **row} for row in rows
                               if row["kind"] == "dsh_transport_error"],
        "image_transport": {
            "schema": "rpg-model-image-delivery/1",
            "transport": "official-dsh-attachment-pi-ai-responses",
            "admitted": [row for row in rows if row["kind"] == "dsh_media_admitted"],
            "accepted": [row for row in rows if row["kind"] == "provider_response"
                         and row.get("stream_complete") and row.get("images")],
            "model_attachment_delivered": any(row["kind"] == "provider_response"
                                             and row.get("stream_complete") and row.get("images")
                                             for row in rows),
            "actual_backend": "unknown", "vision_ability_claim": False,
        },
        "cleanup_complete": not entry["cleanup_errors"] and entry["runtime_cleanup"].get("stopped") is True,
    }
