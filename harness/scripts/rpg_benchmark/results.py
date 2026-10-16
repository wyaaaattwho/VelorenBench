"""Independent native outcome, measurement validity, errors and cleanup."""
from __future__ import annotations

import math
import time

from scripts.rpg_benchmark.native import exception_detail

SCHEMA = "rpg-run-result/3"
NATIVE_REASONS = {"native_goal_success", "player_death", "evaluation_horizon",
                  "required_target_unavailable"}


def terminal(record, timing):
    timing.mark("terminal")
    if "task_terminal" in record:
        return
    score = record.get("score", {})
    reason = record.get("stop_reason", "unobserved")
    observed_stop = (reason == "normal_entry_exited" and record.get("final_state_observed") is True
                     and score.get("success") is False and score.get("dead") is False)
    record["task_terminal"] = {
        "outcome": ("success" if score.get("success") else "death" if score.get("dead")
                    else "horizon" if reason == "evaluation_horizon"
                    else "stopped_without_goal" if observed_stop
                    else reason if reason in NATIVE_REASONS else "unknown"),
        "native_success": score.get("success") is True,
        "native_death": score.get("dead") is True,
        "native_outcome": ("success" if score.get("success") else "death" if score.get("dead")
                           else "not_terminal" if reason in NATIVE_REASONS or observed_stop else "unknown"),
        "reason": reason, "observed_at": timing.points["terminal"],
        "classification": record.get("classification", "infrastructure_error"),
        "native_terminal_observed": bool(score.get("success") or score.get("dead")),
        "terminal_observed": reason in NATIVE_REASONS,
        "final_state_observed": record.get("final_state_observed") is True,
    }


def error(record, error, *, source, phase, observed_monotonic=None):
    record.setdefault("error_observations", []).append({
        "source": source, "phase": phase,
        "wall_monotonic": time.monotonic() if observed_monotonic is None else observed_monotonic,
        "error": exception_detail(error),
    })


def observations(facts, terminal_monotonic=None, stop_signal=None):
    values = list(facts.get("error_observations", []))
    values.extend({"source": "provider", **row} for row in facts.get("provider_errors", []))
    entry = facts.get("entry") or {}
    if (entry.get("status") in {"error", "failed", "cancelled", "interrupted"}
            or entry.get("error") or entry.get("primary_error")):
        detail = entry.get("error") or entry.get("primary_error")
        stamp = (entry.get("terminal") or {}).get("monotonic")
        if stamp is None:
            matching = [row.get("wall_monotonic") for row in values
                        if row.get("source") == "adapter" and row.get("error") == detail]
            if matching and all(type(value) in (int, float) and math.isfinite(value) for value in matching):
                stamp = min(matching)
        values.append({"source": "entry", "error": detail, "wall_monotonic": stamp})
    values.extend({"source": "cleanup", "phase": "cleanup", "error": row}
                  for row in entry.get("cleanup_errors", []))
    normalized = []
    for value in values:
        value = dict(value)
        detail = value.get("error")
        value["error_detail"] = (detail if isinstance(detail, dict) else {
            "type": value.get("error_type", "unknown"),
            "message": detail if isinstance(detail, str) else value.get("message"),
        })
        stamp = value.get("wall_monotonic")
        cutoff = (stop_signal or {}).get("requested_monotonic")
        evaluator_cancel = (
            value["error_detail"].get("type") == "CancelledError"
            and (stop_signal or {}).get("sent") is True
            and (stop_signal or {}).get("signal") == "SIGINT"
            and type(stamp) in (int, float) and math.isfinite(stamp)
            and type(cutoff) in (int, float) and math.isfinite(cutoff)
            and terminal_monotonic is not None and stamp >= cutoff >= terminal_monotonic)
        if evaluator_cancel:
            value["evaluator_terminal_cancellation"] = True
        if type(stamp) not in (int, float) or not math.isfinite(stamp):
            value["wall_monotonic"] = None
            value["ordering"] = "unknown"
        elif terminal_monotonic is None:
            value["ordering"] = "unknown"
        else:
            value["ordering"] = "post_terminal" if stamp > terminal_monotonic else "pre_terminal"
        value.setdefault("phase", value["ordering"])
        normalized.append(value)
    return normalized


def validity(facts, errors, *, native_terminal=False, unknown_effects=()):
    reasons, unknown = [], []
    if not facts.get("entry_receipt_present") or not facts.get("model_requests"):
        reasons.append("missing_entry_or_model_evidence")
    for row in errors:
        if row["source"] == "native_refusal":
            continue
        if row.get("evaluator_terminal_cancellation") is True:
            continue
        detail = row["error_detail"]
        # A timed reset after a known terminal is an observation, not its cause.
        benign_reset = (native_terminal and row["ordering"] == "post_terminal"
                        and detail.get("type") in {
                            "ClientConnectionResetError", "ConnectionResetError", "BrokenPipeError"}
                        and row["source"] in {"transport", "provider"})
        if benign_reset:
            continue
        target = unknown if row["ordering"] == "unknown" and row["source"] != "cleanup" else reasons
        target.append(row["source"] + "_error")
    if unknown_effects:
        unknown.append("unknown_mutation_effect")
    return {"status": "invalid" if reasons else "unknown" if unknown else "valid",
            "reasons": list(dict.fromkeys(reasons + unknown))}


def finalize(record, timing):
    terminal(record, timing)
    outcome = record["task_terminal"]
    if outcome["terminal_observed"]:
        record["classification"] = outcome["classification"]
    record["result_schema"] = SCHEMA
    record.setdefault("error_observations", [])
    record.setdefault("unknown_effects", [])
    proof = record.get("model_success_evidence", {})
    evaluation = dict(proof.get("evaluation_validity", {"status": "valid", "reasons": []}))
    evaluation["reasons"] = list(evaluation["reasons"])
    local = observations({"error_observations": record["error_observations"]},
                         outcome["observed_at"]["monotonic"])
    record["error_observations"] = local + proof.get("error_observations", [])
    if local:
        evaluation["status"] = "invalid"
        evaluation["reasons"].append("runtime_or_boundary_error")
    if record["unknown_effects"]:
        if evaluation["status"] != "invalid":
            evaluation["status"] = "unknown"
        evaluation["reasons"].append("unknown_mutation_effect")
    if record.get("cleanup_complete") is not True:
        evaluation["status"] = "invalid"
        evaluation["reasons"].append("cleanup_incomplete")
    if record.get("mode") == "capability" and not proof:
        evaluation["status"] = "invalid"
        evaluation["reasons"].append("measurement_evidence_unavailable")
    elif (record.get("mode") == "capability" and outcome["native_success"]
          and not proof.get("accepted") and evaluation["status"] == "valid"):
        evaluation["status"] = "invalid"
        evaluation["reasons"].append("native_goal_lacks_accepted_model_evidence")
    if outcome["outcome"] == "unknown" and evaluation["status"] == "valid":
        evaluation["status"] = "unknown"
        evaluation["reasons"].append("native_terminal_unobserved")
    synthetic = record.get("external_engineering") is True or proof.get("synthetic") is True
    if synthetic:
        evaluation["reasons"].append("mocked_engineering_evidence")
    evaluation["accepted"] = bool(
        outcome["native_success"] and evaluation["status"] == "valid"
        and proof.get("accepted") is True and not synthetic)
    evaluation["reasons"] = list(dict.fromkeys(evaluation["reasons"]))
    record["evaluation_validity"] = evaluation
    if record.get("mode") == "capability":
        record["success"] = evaluation["accepted"]
        record["capability_claim"] = evaluation["accepted"]
    return record
