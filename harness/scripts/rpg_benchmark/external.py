"""One pinned external adapter seam; benchmark remains the evaluator and owner."""
from dataclasses import dataclass
import importlib.util
import json
import os
from pathlib import Path
import sys

from scripts.rpg_benchmark import lifecycle
from scripts.rpg_benchmark import capabilities, results

CONTRACT = "rpg-external-adapter/1"
EVIDENCE = "rpg-external-evidence/1"
PLAYER = "rpg-sdk-native-contract/2"


@dataclass(frozen=True)
class Launch:
    command: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    entry_stamp: str = "harness-entry.json"


def verify_freeze(path, digest, seen=None):
    from .runtime import local_path
    seen = set() if seen is None else seen
    if not path.is_absolute() or path in seen:
        raise ValueError("Invalid or cyclic adapter freeze path")
    seen.add(path)
    if lifecycle.sha(path) != digest:
        raise ValueError("External adapter freeze changed")
    freeze = json.loads(path.read_text())
    root = local_path(path.parent, freeze["root"])
    if not root.is_dir():
        raise ValueError("External adapter freeze root must be a directory")
    for name, checksum in freeze["files"].items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or lifecycle.sha(root / relative) != checksum:
            raise ValueError(f"External adapter frozen input changed: {name}")
    for parent in freeze.get("parents", []):
        verify_freeze(local_path(path.parent, parent["path"]), parent["sha256"], seen)


class Adapter:
    def __init__(self, selected, identifier, config, *, engineering=False, admission=None,
                 pins_sha=None, requested=None):
        declaration = selected.get("external_adapters", {}).get(identifier)
        if declaration is None:
            raise ValueError(f"External adapter is not registered in selected pins: {identifier}")
        path = Path(declaration["path"])
        if lifecycle.sha(path) != declaration["sha256"]:
            raise ValueError("External adapter declaration changed")
        self.manifest = json.loads(path.read_text())
        self.id, self.config, self.engineering = identifier, config, engineering
        self.admission = admission
        self.pins_sha = pins_sha
        self.declaration_sha = declaration["sha256"]
        if self.manifest.get("schema") not in {CONTRACT, capabilities.SCHEMA}:
            raise ValueError("Unsupported external adapter declaration version")
        for key, expected in {"id": identifier, "player_interface": PLAYER}.items():
            if self.manifest.get(key) != expected:
                raise ValueError(f"Invalid external adapter declaration: {key}")
        self.configuration, self.capabilities, self.legacy = capabilities.negotiate(
            self.manifest, requested or {})
        from .runtime import local_path
        implementation = local_path(path.parent, self.manifest["implementation"])
        if not implementation.is_absolute() or lifecycle.sha(implementation) != self.manifest["implementation_sha256"]:
            raise ValueError("External adapter implementation changed")
        freeze_path = local_path(path.parent, self.manifest["freeze"]["path"])
        verify_freeze(freeze_path, self.manifest["freeze"]["sha256"])
        if not engineering:
            if admission is None:
                raise ValueError("Live external model entry requires A0 experiment admission")
            approved = json.loads(Path(admission).read_text())
            expected = {
                "schema": "rpg-external-admission/1", "approved_by": "A0",
                "pins_sha256": pins_sha, "adapter_id": identifier,
                "adapter_sha256": declaration["sha256"], "model": self.configuration["model"],
                "reasoning_effort": self.configuration["reasoning_effort"], "model_calls_admitted": True,
            }
            if not self.legacy:
                expected["configuration"] = self.configuration
            if any(approved.get(key) != value for key, value in expected.items()):
                raise ValueError("A0 admission does not match exact external configuration")
        module_name = "_rpg_external_" + declaration["sha256"]
        spec = importlib.util.spec_from_file_location(module_name, implementation)
        if spec is None or spec.loader is None:
            raise ValueError("Invalid external adapter module")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        if not callable(getattr(module, "build_launch", None)) or not callable(getattr(module, "collect_evidence", None)):
            raise ValueError("Adapter must export build_launch and collect_evidence")
        self.module = module
        self.context = None

    def launch(self, *, task, socket, output, output_root, python, common_interface):
        self.context = {
            "schema": "rpg-external-context/2", "adapter_id": self.id,
            "player_interface": PLAYER, "model": self.configuration["model"],
            "reasoning_effort": self.configuration["reasoning_effort"],
            "configuration": self.configuration, "capabilities": self.capabilities,
            "legacy_contract_compatibility": self.legacy,
            "task": task, "episode_socket": str(socket), "output": str(output),
            "output_root": str(output_root), "python": python,
            "common_interface": common_interface, "config": self.config,
            "engineering": self.engineering,
            "admission": str(self.admission) if self.admission is not None else None,
            "pins_sha256": self.pins_sha, "adapter_sha256": self.declaration_sha,
        }
        plan = self.module.build_launch(self.context)
        if (not isinstance(plan, Launch) or not plan.command
                or any(not isinstance(value, str) or not value for value in plan.command)
                or not Path(plan.command[0]).is_absolute()
                or not plan.cwd.is_absolute() or not plan.cwd.is_dir()
                or not isinstance(plan.env, dict)
                or any(not isinstance(key, str) or not isinstance(value, str) for key, value in plan.env.items())):
            raise ValueError("Invalid external launch contract")
        self.plan = plan
        self._inside(plan.entry_stamp)
        return plan

    def _inside(self, name):
        root = Path(self.context["output"])
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("External evidence path escapes private output")
        path = root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("External evidence symlink escapes private output")
        return path

    def observe_entry(self, timing):
        path = self._inside(self.plan.entry_stamp)
        if path.exists():
            timing.mark("harness_entry", json.loads(path.read_text()))

    def collect(self):
        result = self.module.collect_evidence(self.context)
        if not isinstance(result, dict):
            raise ValueError("External evidence must be an object")
        required = {"schema": EVIDENCE, "adapter_id": self.id, "player_interface": PLAYER}
        if any(result.get(key) != value for key, value in required.items()):
            raise ValueError("External evidence identity/contract mismatch")
        for key in ("model_requests", "tool_calls", "submissions", "provider_errors", "owned_processes"):
            if not isinstance(result.get(key), list):
                raise ValueError(f"External evidence missing list: {key}")
        entry = result.get("entry")
        if (not isinstance(entry, dict) or entry.get("status") not in {"idle", "error", "cancelled"}
                or not isinstance(entry.get("cleanup_errors"), list)
                or type(result.get("entry_receipt_present")) is not bool):
            raise ValueError("Invalid external terminal/entry evidence")
        for row in result["owned_processes"]:
            if any(type(row.get(key)) is not int or row[key] <= 0 for key in ("pid", "start_ticks")):
                raise ValueError("Invalid external owned process identity")
        for request in result["model_requests"]:
            if (request.get("model") != self.configuration["model"]
                    or request.get("effort") != self.configuration["reasoning_effort"]):
                raise ValueError("External backbone/effort evidence mismatch")
            if request.get("synthetic") is not self.engineering:
                raise ValueError("External evidence synthetic/live mode mismatch")
        result["runtime_model_metadata"] = {"brain": "unknown", "jev": "not-used"}
        return result


def adjudicate(facts, receipts, opening, score, *, native_classification, stop_reason, stop_signal,
               terminal_monotonic=None, unknown_effects=()):
    """Native scoring plus explicit observed model/intent/receipt joins, never self-success."""
    entry = facts["entry"]
    evaluator_cancel = (
        entry.get("status") == "cancelled" and stop_reason != "normal_entry_exited"
        and stop_signal.get("sent") is True)
    errors = results.observations(facts, terminal_monotonic, stop_signal)
    evaluation = results.validity(facts, errors, native_terminal=stop_reason in results.NATIVE_REASONS,
                                  unknown_effects=unknown_effects)
    calls = {row["id"]: row for row in facts["tool_calls"]}
    joined = []
    for submission in facts["submissions"]:
        call = calls.get(submission["call_id"])
        request = submission["request"]
        if call is None:
            raise ValueError("Native submission has no observed model tool call")
        if call.get("name") is not None and call["name"] != "game_" + request["cmd"]:
            raise ValueError("Observed model tool differs from native submission")
        from scripts.rpg_sdk_agent import validate_request
        validate_request(request["cmd"], request["args"], renderer=(
            request.get("args", {}).get("action", {}).get("type") == "visual_input"))
        action = request.get("args", {}).get("action", {})
        if call["arguments"] != request["args"]:
            # The existing native contract may assign an omitted action ID.
            observed = json.loads(json.dumps(request["args"]))
            observed.get("action", {}).pop("action_id", None)
            if call["arguments"] != observed:
                raise ValueError("Observed model intent differs from native submission")
        for receipt in receipts:
            response = receipt["response"]
            if response.get("ok") is not True:
                continue
            result = response.get("result")
            if not isinstance(result, dict):
                raise ValueError("successful native mutation lacks a receipt object")
            args = request["args"]
            control = request["cmd"] == "set_control_state"
            if request["cmd"] not in {"step", "set_control_state"}:
                continue
            if control and (
                    args["actor"] != opening.get("control_actor", opening["self_identity"])
                    or args["expected_session_id"] != opening["session_id"]
                    or result.get("session_id") != opening["session_id"]
                    or any(type(result.get(key)) is not int for key in ("generation", "sequence", "applied_tick"))
                    or any(result.get(key) != args[key] for key in ("owner", "generation", "sequence", "actor"))
                    or not (any(args["movement"]) or args.get("movement_intent") is not None
                            or args["primary"] or args.get("secondary") or args.get("jump")
                            or args.get("melee") is not None)):
                continue
            if (receipt.get("public_request", receipt["request"]) == request
                    and receipt["actor"] == opening["self_identity"]["uid"]
                    and response.get("id") == request["id"]
                    and response.get("session_id") == opening["session_id"]
                    and response.get("ok") is True
                    and result.get("status") == ("applied" if control else "completed")
                    and (control or (isinstance(action.get("action_id"), str)
                                     and result.get("action_id") == action["action_id"]))):
                joined.append({"call_id": submission["call_id"], "request_id": request["id"],
                               "command": request["cmd"], "action_id": action.get("action_id"),
                               "owner": args.get("owner"), "generation": args.get("generation"),
                               "sequence": args.get("sequence"), "native_ok": True})
    synthetic = any(row["synthetic"] for row in facts["model_requests"])
    if score.get("success") and not joined:
        evaluation["status"] = "invalid"
        evaluation["reasons"].append("native_goal_lacks_model_effect_join")
    accepted = bool(score.get("success") and joined and not synthetic and evaluation["status"] == "valid")
    classification = native_classification
    if stop_reason not in results.NATIVE_REASONS:
        if not facts.get("entry_receipt_present") or not facts["model_requests"]:
            classification = "infrastructure_error"
        elif facts["provider_errors"]:
            classification = "provider_error"
        elif entry.get("status") == "error" or (entry.get("status") == "cancelled" and not evaluator_cancel):
            classification = "agent_entry_error"
        elif entry.get("status") == "idle":
            classification = "model_stopped_without_goal"
    return classification, {"accepted": accepted, "joins": joined, "synthetic": synthetic,
                            "evaluation_validity": evaluation, "error_observations": errors,
                            "evaluator_terminal_cancellation": evaluator_cancel}


def retired(facts):
    for row in facts["owned_processes"]:
        pid, start = row["pid"], row["start_ticks"]
        try:
            current = lifecycle.process_start_ticks(pid)
        except FileNotFoundError:
            continue
        if current == start:
            return False
    return facts.get("cleanup_complete") is True
