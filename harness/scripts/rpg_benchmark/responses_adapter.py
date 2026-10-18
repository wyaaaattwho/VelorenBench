"""Normal runner adapter with finite multimodal negotiation and common evidence."""
from __future__ import annotations

import json
import math
from pathlib import Path
import time
import sys
import asyncio

from . import capabilities, lifecycle, providers, results
from .external import Launch


class Adapter:
    def __init__(self, args):
        self.id, self.legacy = "responses-image-blocks", False
        config = json.loads(args.adapter_config)
        if not isinstance(config, dict) or set(config) - {
                "mock_provider", "credential_loader", "credential_loader_sha256", "transport_probe",
                "route", "provider", "endpoint", "api_key_env"}:
            raise ValueError("invalid Responses adapter configuration")
        self.mock, self.config = config.get("mock_provider"), config
        self.engineering = self.mock is not None
        route = config.get("route")
        if not self.mock and route not in {"existing-luna", "standard-responses"}:
            raise ValueError("select loopback engineering or an explicit existing-luna/standard-responses route")
        if route == "existing-luna" and not (
                config.get("credential_loader") and config.get("credential_loader_sha256")):
            raise ValueError("existing-luna requires the explicitly pinned credential loader")
        if route == "standard-responses":
            if "credential_loader" in config or "credential_loader_sha256" in config:
                raise ValueError("standard-responses cannot select an account credential loader")
            from .provider_route import StandardResponsesRoute
            StandardResponsesRoute(config.get("endpoint", ""), config.get("api_key_env", ""))
            if "provider" not in config:
                raise ValueError("standard-responses requires explicit provider capabilities/defaults")
        if self.mock and ("credential_loader" in config or "credential_loader_sha256" in config):
            raise ValueError("mock provider and live credential route are mutually exclusive")
        mode = args.observation_mode or "structured"
        if route == "existing-luna" and "provider" in config:
            raise ValueError("existing live route capabilities cannot be overridden")
        if self.mock and "route" in config:
            raise ValueError("mock origin cannot select a live route")
        if self.mock and "provider" not in config:
            raise ValueError("loopback mock requires explicit provider capabilities and defaults")
        self.provider = providers.LUNA if route == "existing-luna" else config["provider"]
        self.manifest = providers.manifest(self.provider, mode)
        self.configuration, self.capabilities, _ = capabilities.negotiate(self.manifest, {
            key: getattr(args, key, None) for key in (
                "model", "reasoning_effort", "observation_mode", "action_profile")})
        self.frames = args.recordings
        self.context = None

    async def preflight(self, *, backend, frames=None, detail_storage=None):
        capabilities.admit_backend(self.configuration, self.capabilities, backend)
        from . import startup, runtime
        construction = startup.context(self.configuration, backend, frames, detail_storage)
        # The bundled stdlib-only entry is installed with the evaluator.
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-m", "scripts.rpg_benchmark.responses_entry", "--startup-check",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate(json.dumps(construction).encode())
        if process.returncode:
            raise ValueError("Bundled adapter is not installed in the evaluator interpreter: "
                             + stderr.decode(errors="replace"))
        receipt = json.loads(stdout)
        startup.validate_receipt(receipt, construction)
        if receipt.get("player_package_files") != runtime.package_files():
            raise ValueError("bundled adapter installed content differs from candidate")
        self.frames = frames
        return {"command": [sys.executable, "-I", "-m",
                            "scripts.rpg_benchmark.responses_entry", "--startup-check"],
                "exit_code": 0, "receipt": receipt}

    def launch(self, *, task, socket, output, output_root, python, common_interface):
        self.context = {"output": str(output), "task": task, "configuration": self.configuration,
                        "public_context_only": True,
                        "frames": str(self.frames) if self.frames is not None else None}
        self.output = output
        command = (python, "-I", "-B", "-m", "scripts.rpg_benchmark.responses_entry",
                   "--episode-socket", str(socket), "--task", task, "--out", str(output),
                   "--output-root", str(output_root),
                   "--episode-id", output.parent.name,
                   "--observation-mode", self.configuration["observation_mode"],
                   "--configuration", json.dumps(self.configuration),
                   "--provider-declaration", json.dumps(self.provider))
        if self.frames is not None:
            command += ("--frames", str(self.frames))
        if self.mock:
            command += ("--mock-provider", self.mock)
        elif self.config.get("route") == "existing-luna":
            command += ("--credential-loader", self.config["credential_loader"],
                        "--credential-loader-sha256", self.config["credential_loader_sha256"])
        else:
            command += ("--endpoint", self.config["endpoint"], "--api-key-env", self.config["api_key_env"])
        if self.config.get("transport_probe") is True:
            command += ("--transport-probe",)
        return Launch(command, output_root, {})

    def observe_entry(self, timing):
        path = self.output / "harness-entry.json"
        if path.exists():
            point = json.loads(path.read_text())
            launch = timing.points.get("agent_launched")
            if (launch is not None and isinstance(point, dict)
                    and type(point.get("monotonic")) in (int, float)
                    and point["monotonic"] < launch["monotonic"]):
                raise ValueError("harness entry precedes the owned player launch")
            terminal = timing.points.get("terminal")
            late = (terminal is not None and isinstance(point, dict)
                    and type(point.get("monotonic")) in (int, float)
                    and point["monotonic"] > terminal["monotonic"])
            timing.mark("late_harness_entry" if late else "harness_entry", point)

    def collect(self):
        collection_errors, rows = [], []

        def issue(error, path, line=None):
            collection_errors.append({
                "source": "evidence", "phase": "collection", "wall_monotonic": time.monotonic(),
                "path": str(path), "line": line, "error": results.exception_detail(error)})

        entry_path, events_path = self.output / "entry.json", self.output / "events.ndjson"
        entry_complete = False
        try:
            entry = json.loads(entry_path.read_text())
            terminal = entry.get("terminal") if isinstance(entry, dict) else None
            if (not isinstance(entry, dict) or entry.get("status") not in {"idle", "error", "cancelled"}
                    or not isinstance(entry.get("cleanup_errors"), list)
                    or not isinstance(terminal, dict)
                    or type(terminal.get("monotonic")) not in (int, float)
                    or not math.isfinite(terminal["monotonic"])):
                raise ValueError("invalid or incomplete Responses terminal evidence")
            entry_complete = True
        except (OSError, ValueError) as error:
            issue(error, entry_path)
            entry = {"status": "incomplete", "cleanup_errors": []}
        try:
            lines = events_path.read_bytes().splitlines()
        except OSError as error:
            issue(error, events_path)
            lines = []
        for number, line in enumerate(lines, 1):
            try:
                row = json.loads(line)
                if not isinstance(row, dict) or not isinstance(row.get("kind"), str):
                    raise ValueError("event must be an object with a kind")
                kind = row["kind"]
                if kind == "provider_request" and (
                        row.get("model") != self.configuration["model"]
                        or row.get("effort") != self.configuration["reasoning_effort"]
                        or row.get("synthetic") is not self.engineering):
                    raise ValueError("provider evidence differs from negotiated runtime configuration")
                if kind == "model_tool_call" and (
                        not isinstance(row.get("id"), str) or not isinstance(row.get("name"), str)
                        or not isinstance(row.get("arguments"), dict)):
                    raise ValueError("invalid observed model tool call")
                if kind == "tool_native_submission" and (
                        not isinstance(row.get("tool_call_id"), str)
                        or not isinstance(row.get("request"), dict)):
                    raise ValueError("invalid native submission evidence")
                if kind == "provider_response" and not isinstance(row.get("images"), list):
                    raise ValueError("invalid accepted response image evidence")
                rows.append(row)
            except ValueError as error:
                issue(error, events_path, number)
        return {
            "entry": entry, "entry_receipt_present": any(row["kind"] == "entry_ready" for row in rows),
            "model_requests": [row for row in rows if row["kind"] == "provider_request"],
            "tool_calls": [row for row in rows if row["kind"] == "model_tool_call"],
            "submissions": [{"call_id": row["tool_call_id"], "request": row["request"]}
                            for row in rows if row["kind"] == "tool_native_submission"],
            "provider_errors": [row for row in rows if row["kind"] == "provider_error"],
            "image_transport": {
                "schema": "rpg-model-image-delivery/1",
                "transport": "Responses-input_image-data-url-real-bytes",
                "accepted": [row for row in rows if row["kind"] == "provider_response" and row["images"]],
                "model_attachment_delivered": any(
                    row["kind"] == "provider_response" and row["images"] for row in rows),
                "actual_backend": "unknown", "vision_ability_claim": False,
            },
            "owned_processes": [], "cleanup_complete": entry_complete and not entry["cleanup_errors"],
            "collection_errors": collection_errors,
            "runtime_model_metadata": {"brain": "unknown", "jev": "not-used"},
            "error_observations": [
                {"source": "entry" if row["kind"] == "entry_error" else "transport", **row}
                for row in rows if row["kind"] in {"entry_error", "transport_error"}] + collection_errors,
        }
