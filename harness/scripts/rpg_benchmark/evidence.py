"""Read-only selection of production entry evidence; no agent runtime imports."""
from __future__ import annotations

import json
import hashlib
import math
from collections import Counter
from pathlib import Path


def select(root: Path) -> dict:
    counts: Counter = Counter()
    choices: Counter = Counter()
    brain_models, jev_models = set(), set()
    overlapping = 0
    errors, cancellations = [], 0
    error_observations = []
    cancellation_events, jev_choices, jev_effects, jev_controls = [], [], [], []
    requests = {}
    current_choice = handoff = None
    events = root / "events.ndjson"
    if events.is_file():
        with events.open() as stream:
            for line in stream:
                row = json.loads(line)
                kind = row.get("record_type")
                counts[kind] += 1
                if kind == "provider_model_receipt":
                    model = (row.get("usage") or {}).get("sdk_model")
                    if isinstance(model, str):
                        brain_models.add(model)
                    choices.update(tool["name"] for tool in row.get("tool_calls", []))
                elif kind == "async_jev_request":
                    requests[row["decision_id"]] = row
                elif kind == "async_jev_receipt":
                    model = ((row.get("outcome") or {}).get("receipt") or {}).get("model")
                    if isinstance(model, str):
                        jev_models.add(model)
                    overlapping += row.get("brain_pending") is True
                    request = requests.get(row.get("decision_id"))
                    proof = observed_jev_choice(request, row)
                    current_choice = proof
                    if proof is not None:
                        jev_choices.append(proof)
                    if (row.get("outcome") or {}).get("status") == "provider_error":
                        errors.append({"phase": "jev_provider", **((row.get("outcome") or {}).get("failure") or {})})
                        error_observations.append({
                            "source": "provider", "phase": "jev_provider",
                            "host_recorded_at": row.get("host_recorded_at"),
                            "wall_monotonic": None, "error": errors[-1],
                            **{key: row[key] for key in ("occurrence", "category", "caused_by", "wall_monotonic")
                               if key in row}})
                elif kind == "async_stale_choice":
                    current_choice = None
                elif kind == "async_effect_handoff":
                    handoff = None
                    if (current_choice is not None and row.get("owner") == "jev"
                            and row.get("epoch") == current_choice["epoch"]
                            and row.get("tool") == current_choice["tool"]):
                        handoff = {**current_choice, "invocation_id": row["invocation_id"]}
                elif kind == "public_action_dispatch" and handoff is not None and row.get("forwarded") is True:
                    action = row["action"]
                    parameters = action.get("parameters") or {}
                    if (isinstance(action.get("correlation_id"), str) and all(
                            parameters[key] == value for key, value in handoff["binding_arguments"].items()
                            if key in parameters)):
                        jev_effects.append({**handoff, "action_id": action["correlation_id"],
                                            "action_name": action["name"],
                                            "dispatch_sequence": row["host_event_sequence"]})
                elif kind == "async_effect" and row.get("source") == "jev":
                    proof = next((value for value in jev_choices
                                  if value["decision_id"] == row.get("decision_id")), None)
                    result = row.get("result") or {}
                    if (proof is not None and row["binding"]["tool"] == proof["tool"]
                            and result.get("status") == "applied"):
                        jev_controls.append({**proof, "control_receipt": result})
                elif kind == "provider_model_error":
                    error = row.get("error") or {}
                    if error.get("type") == "CancelledError":
                        cancellations += 1
                        cancellation_events.append(dict(row))
                    else:
                        errors.append(error)
                        error_observations.append({
                            "source": "provider", "phase": "brain_provider",
                            "host_recorded_at": row.get("host_recorded_at"),
                            "wall_monotonic": None, "error": error,
                            **{key: row[key] for key in ("occurrence", "category", "caused_by", "wall_monotonic")
                               if key in row}})
    final_path = root / "final.json"
    final = json.loads(final_path.read_text()) if final_path.is_file() else None
    return {
        "events_path": str(events), "entry_receipt_present": final is not None,
        "record_counts": dict(counts), "brain_tool_choices": dict(choices),
        "runtime_model_metadata": {
            "brain_sdk_usage_models": sorted(brain_models),
            "jev_response_models": sorted(jev_models),
            "ultimate_served_brain_identity": "unknown",
        },
        "jev_receipts_while_brain_pending": overlapping,
        "provider_errors_excluding_cancellation": errors,
        "error_observations": error_observations,
        "provider_cancellation_receipts": cancellations,
        "provider_cancellation_events": cancellation_events,
        "observed_jev_choices": jev_choices,
        "jev_effect_dispatches": jev_effects,
        "jev_control_effects": jev_controls,
        "entry": {
            "status": final.get("status"), "primary_error": final.get("primary_error"),
            "cleanup_errors": final.get("cleanup_errors"),
            "runtime_mode": final.get("runtime_mode"),
            "brain_direct_only": final.get("brain_direct_only"),
            "used": final.get("used"),
        } if final is not None else None,
        "source_provenance_caveat": (
            "The production receipt's Git HEAD is the ancestor control repository, "
            "not this manifest-only selected Python source's unknown commit."),
    }


def observed_jev_choice(request: dict | None, response: dict) -> dict | None:
    outcome = response.get("outcome") or {}
    receipt = outcome.get("receipt") or {}
    if request is None or outcome.get("status") != "accepted":
        return None
    public = request["request"]
    canonical = json.dumps(public, sort_keys=True, separators=(",", ":"), allow_nan=False)
    probabilities = receipt.get("probabilities")
    choice = receipt.get("choice")
    if (hashlib.sha256(canonical.encode()).hexdigest() != request.get("request_sha256")
            or request.get("epoch") != response.get("epoch")
            or response.get("executive_mode") != "jev" or response.get("jev_execution") != "enabled"
            or receipt.get("model") != "jev-1.13.0"
            or any(type(receipt.get(key)) is not int or receipt[key] <= 0
                   for key in ("input_tokens", "output_tokens"))
            or type(receipt.get("confidence")) not in (int, float)
            or not math.isfinite(receipt["confidence"]) or not 0 <= receipt["confidence"] <= 1
            or type(receipt.get("elapsed_seconds")) not in (int, float)
            or not math.isfinite(receipt["elapsed_seconds"]) or receipt["elapsed_seconds"] < 0
            or not isinstance(probabilities, dict) or set(probabilities) != set(public["criteria"])
            or any(type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1
                   for value in probabilities.values())
            or abs(sum(probabilities.values()) - 1) > .001
            or choice not in probabilities or probabilities[choice] + 1e-9 < max(probabilities.values())
            or choice not in request["choices"]):
        return None
    binding = request["choices"][choice]
    if not isinstance(binding.get("arguments_json"), str):
        return None
    arguments = json.loads(binding["arguments_json"])
    if not isinstance(arguments, dict):
        return None
    return {"decision_id": response["decision_id"], "epoch": response["epoch"],
            "model": receipt["model"], "choice": choice, "tool": binding["tool"],
            "binding_arguments": arguments,
            "brain_pending": response.get("brain_pending") is True}


def gameagent_success(selected: dict, opening: dict, receipts: list[dict], score: dict,
                      *, stop_reason: str, stop_requested_at: str,
                      stop_signal=None, terminal_monotonic=None) -> dict:
    from . import results
    entry = selected.get("entry") or {}
    normal = (selected["entry_receipt_present"] and entry.get("runtime_mode") == "async"
              and entry.get("brain_direct_only") is False)
    errors = selected["provider_errors_excluding_cancellation"]
    primary = entry.get("primary_error") or {}
    cancellations = selected["provider_cancellation_events"]
    normalized = results.observations({"error_observations": [
        {"source": "provider", **row} for row in cancellations]}, terminal_monotonic, stop_signal)
    causal_stop = (stop_reason == "native_goal_success" and bool(normalized)
                   and all(row.get("evaluator_terminal_cancellation") for row in normalized))
    if primary.get("type") not in (None, "CancelledError"):
        normal = False
    if (cancellations or primary.get("type") == "CancelledError") and not causal_stop:
        normal = False
    completed = set()
    for row in receipts:
        request, response = row["request"], row["response"]
        result = response.get("result") or {}
        action = request.get("args", {}).get("action") or {}
        if (row["actor"] == opening["self_identity"]["uid"]
                and response.get("session_id") == opening["session_id"] and response.get("ok") is True
                and result.get("status") == "completed" and isinstance(action.get("action_id"), str)
                and result.get("action_id") == action["action_id"]):
            completed.add(action["action_id"])
    attributed = set(score.get("attributed_action_ids", []))
    attributed.update(row["action_id"] for row in score.get("final_effects", []))
    if attributed:
        completed.intersection_update(attributed)
    jev = [row for row in selected["jev_effect_dispatches"] if row["action_id"] in completed]
    controls = []
    if not attributed:
        for row in receipts:
            request, response = row["request"], row["response"]
            args = request.get("args") or {}
            result = response.get("result") or {}
            if (request["cmd"] != "set_control_state" or response.get("ok") is not True
                    or result.get("status") != "applied" or row["actor"] != opening["self_identity"]["uid"]
                    or not isinstance(args.get("owner"), str)
                    or any(type(args.get(key)) is not int for key in ("generation", "sequence"))
                    or not isinstance(args.get("actor"), dict)
                    or args["actor"].get("uid") != opening["self_identity"]["uid"]
                    or response.get("session_id") != opening["session_id"]
                    or result.get("session_id") != opening["session_id"]
                    or args.get("expected_session_id") != opening["session_id"]
                    or any(result.get(key) != args.get(key) for key in ("owner", "generation", "sequence", "actor"))
                    or not (any(args.get("movement", [])) or args.get("movement_intent") == "destination"
                            or args.get("primary") is True or args.get("secondary") is True
                            or args.get("melee") is not None)):
                continue
            controls.extend(value for value in selected["jev_control_effects"] if all(
                value["control_receipt"].get(key) == result.get(key)
                for key in ("owner", "generation", "sequence", "actor", "session_id", "applied_tick")))
    brain = (selected["record_counts"].get("provider_model_request", 0) > 0
             and selected["record_counts"].get("provider_model_receipt", 0) > 0
             and "gpt-6-luna" in selected["runtime_model_metadata"]["brain_sdk_usage_models"])
    return {"accepted": normal and not errors and (brain or bool(jev) or bool(controls)),
            "brain_receipt_evidenced": brain, "correlated_jev_effects": jev,
            "correlated_jev_controls": controls,
            "evaluator_terminal_cancellation": bool(cancellations) and causal_stop,
            "normal_async_entry": normal, "genuine_provider_errors": errors}


def gameagent_evaluation(selected, *, stop_reason, stop_signal, stop_requested_at,
                         terminal_monotonic=None):
    """Preserve legacy success proof; independently qualify negative measurements."""
    from scripts.rpg_benchmark import results

    entry = dict(selected.get("entry") or {})
    counts = selected["record_counts"]
    cancellations = selected.get("provider_cancellation_events", [])
    terminal = terminal_monotonic if terminal_monotonic is not None else stop_signal.get("terminal_monotonic")
    normalized = results.observations({"error_observations": [
        {"source": "provider", **row} for row in cancellations]}, terminal, stop_signal)
    causal_cancel = (stop_reason in results.NATIVE_REASONS and bool(normalized)
                     and all(row.get("evaluator_terminal_cancellation") for row in normalized))
    if (entry.get("primary_error") or {}).get("type") == "CancelledError" and causal_cancel:
        entry.update(status="idle", primary_error=None)
    facts = {
        "entry": entry, "entry_receipt_present": selected["entry_receipt_present"],
        "model_requests": [{}] if (
            counts.get("provider_model_request", 0) or counts.get("async_jev_request", 0)) else [],
        "error_observations": selected.get("error_observations") or [
            {"source": "provider", "error": error, "wall_monotonic": None}
            for error in selected["provider_errors_excluding_cancellation"]],
    }
    errors = results.observations(facts, terminal, stop_signal)
    errors.extend(normalized)
    return {"accepted": False, "evaluation_validity": results.validity(facts, errors),
            "error_observations": errors}


def select_sdk(root: Path) -> dict:
    events = root / "events.ndjson"
    kinds: Counter = Counter()
    sdk_types: Counter = Counter()
    adapter_errors, transport_errors = [], []
    rows = []
    if events.is_file():
        for line in events.read_text().splitlines():
            row = json.loads(line)
            rows.append(row)
            kinds[row.get("kind")] += 1
            if row.get("kind") == "sdk_event":
                sdk_types[row["event"]["type"]] += 1
            elif row.get("kind") in ("adapter_error", "transport_error"):
                errors = adapter_errors if row["kind"] == "adapter_error" else transport_errors
                errors.append(dict(row))
    result = root / "result.json"
    entry = json.loads(result.read_text()) if result.is_file() else None
    return {
        "entry_receipt_present": entry is not None,
        "entry": entry, "record_counts": dict(kinds), "sdk_event_counts": dict(sdk_types),
        "actual_sdk_model_events": sum(sdk_types[kind] for kind in (
            "assistant.usage", "assistant.message", "model.call_start")),
        "runtime_model_metadata": {
            "provider_usage": entry.get("provider_usage", []) if entry else [],
            "ultimate_served_identity": "unknown",
        },
        "provider_error": sdk_types["session.error"] > 0,
        "adapter_error_events": adapter_errors, "transport_errors": transport_errors,
        "tool_track": "raw-native-reference",
        "common_facts": sdk_facts(rows, entry),
    }


def sdk_facts(rows, entry):
    """SDK event collector into the same evidence/evaluator contract as external adapters."""
    calls, submissions, requests, errors = [], [], [], []
    for row in rows:
        kind = row.get("kind")
        if kind == "tool_invocation":
            calls.append({"id": row["tool_call_id"], "name": row["tool_name"],
                          "arguments": row["arguments"]})
        elif kind == "tool_native_submission":
            submissions.append({"call_id": row["tool_call_id"], "request": row["request"]})
        elif kind == "tool_result" and row["payload"].get("state") == "submitted":
            submissions.append({"call_id": row["tool_call_id"], "request": row["payload"]["request"]})
        elif kind == "sdk_event" and row["event"]["type"] in {
                "assistant.usage", "assistant.message", "model.call_start"}:
            requests.append({"synthetic": False, "wall_monotonic": row.get("wall_monotonic")})
        elif kind == "sdk_event" and row["event"]["type"] == "session.error":
            errors.append({"wall_monotonic": row.get("wall_monotonic"), "error": row["event"]["data"]})
    return {
        "entry_receipt_present": entry is not None, "entry": entry or {},
        "model_requests": requests, "tool_calls": calls, "submissions": submissions,
        "provider_errors": errors, "error_observations": [
            {"source": ("transport" if row["kind"] == "transport_error" else
                        "native_refusal" if row.get("error", {}).get("type") == "NativeError" else
                        "native_client_request" if row["kind"] == "request_error" else "adapter"), **row}
            for row in rows if row.get("kind") in {"transport_error", "adapter_error", "request_error"}],
    }


def sdk_terminal_cancellation(selected: dict, *, stop_reason: str, native_success: bool,
                              stop_signal: dict | None, process_exit_code: int | None) -> bool:
    entry = selected["entry"]
    terminal = (stop_reason == "native_goal_success" if native_success else stop_reason in (
        "player_death", "evaluation_horizon", "required_target_unavailable"))
    if (not terminal or selected["provider_error"] or selected.get("transport_errors")
            or selected["actual_sdk_model_events"] <= 0
            or entry["status"] != "interrupted" or entry.get("cleanup_errors")
            or (entry.get("error") or {}).get("type") != "CancelledError"
            or not stop_signal or stop_signal.get("sent") is not True
            or stop_signal.get("signal") != "SIGINT"
            or process_exit_code not in (130, 143, -2, -15)):
        return False
    cutoff = stop_signal.get("requested_monotonic")
    errors = selected.get("adapter_error_events", [])
    from . import results
    normalized = results.observations({"error_observations": [
        {"source": "adapter", **row} for row in errors]},
        stop_signal.get("terminal_monotonic"), stop_signal)
    return bool(normalized) and all(row.get("evaluator_terminal_cancellation") for row in normalized)


def sdk_classification(selected: dict, classification: str, *,
                       stop_reason: str, native_success: bool,
                       stop_signal: dict | None = None,
                       process_exit_code: int | None = None) -> str:
    entry = selected["entry"]
    if selected["provider_error"]:
        return "provider_error"
    if selected.get("transport_errors") or entry.get("cleanup_errors"):
        return "infrastructure_error"
    if sdk_terminal_cancellation(
            selected, stop_reason=stop_reason, native_success=native_success,
            stop_signal=stop_signal, process_exit_code=process_exit_code):
        return classification
    if (entry["status"] != "idle" or entry.get("error") is not None
            or process_exit_code not in (None, 0)):
        return "agent_entry_error"
    if (stop_reason == "normal_entry_exited" and not native_success
            and entry["status"] == "idle" and selected["actual_sdk_model_events"] > 0):
        return "agent_idle_without_goal"
    return classification


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Select full-journal M0 model and effect evidence.")
    parser.add_argument("agent_output", type=Path)
    print(json.dumps(select(parser.parse_args().agent_output), indent=2))
