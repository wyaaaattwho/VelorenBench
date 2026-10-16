from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import signal
import time
import traceback
from pathlib import Path
from uuid import uuid4
from datetime import datetime, timezone

from scripts.rpg_benchmark import lifecycle, runtime
from scripts.rpg_benchmark import evidence, pins, scene, suite, setup, navigation, navigation_tasks
from scripts.rpg_benchmark.oracles import target_validity
from scripts.rpg_benchmark import output_paths
from scripts.rpg_benchmark import external
from scripts.rpg_benchmark import compatibility, results, launching
from scripts.rpg_benchmark.timing import AttemptTiming
from scripts.rpg_benchmark.native import (
    EpisodeBoundary, Journal, NativeActionFailure, NativeBridge, NativeConfig,
    NativeRefusal, SocketEndpoint, private_server, stop_owned,
)


def reserve_output(path: Path, root: Path | None = None) -> Path:
    root = pins.ROOT if root is None else root
    if root is None:
        raise ValueError("an explicit runtime output root is required")
    return output_paths.reserve_output(path, pins.ROOT if root is None else root)


def output_locations(path: Path, root: Path | None = None,
                     sdk_root: Path | None = None, *, check_sdk: bool = False) -> tuple[Path, Path, Path]:
    root = pins.ROOT if root is None else root
    if root is None:
        raise ValueError("an explicit runtime output root is required")
    root = output_paths.validate_root(pins.ROOT if root is None else root)
    output_paths.validate_output(path, root)
    sdk_root = output_paths.validate_root(root / "sdk-adapter" if sdk_root is None else sdk_root)
    sdk_out = sdk_root / "episodes" / path.name
    if check_sdk:
        output_paths.validate_output(sdk_out, sdk_root)
        if path == sdk_out or path.is_relative_to(sdk_out):
            raise ValueError("run output must not equal or descend from the SDK output")
    return root, sdk_root, sdk_out


def sdk_command(python: str, script: str, socket_path: Path, task: str,
                out: Path, root: Path) -> list[str]:
    return [python, "-I", "-m", "scripts.rpg_sdk_agent", "--episode-socket", str(socket_path), "--task", task,
            "--model", "gpt-6-luna", "--out", str(out), "--output-root", str(root)]


def agent_command(out: Path, actor: str, python: str, source: str, *,
                  task: str = scene.TASK, gameplay_mode: str = "economy",
                  task_id: str = "m0-food", config: NativeConfig | None = None) -> list[str]:
    native = config or NativeConfig()
    return [
        python, str(Path(source) / "tools/run_veloren_agent.py"),
        "--name", actor, "--run-id", actor, "--out-dir", str(out / "agent"),
        "--world-dir", str(out / "agent-world"), "--task-id", task_id,
        "--task", task, "--runtime-mode", "async", "--gameplay-mode", gameplay_mode,
        "--model", "gpt-6-luna", "--reasoning-effort", "none", "--no-wall-limit",
        "--veloren-root", str(native.root),
        "--bridge", str(Path(python).parent / "rpg-episode-bridge"),
        "--assets", str(native.assets),
    ]


def log_supplies(fixture: dict, observation: dict, journal: Journal, previous: dict) -> None:
    supplies = setup.supplies(fixture)
    nearby = {row["uid"]: row for row in observation["nearby_entities"]}
    clock = observation["survival"]["buff_clock"]["now"]
    for supply in supplies:
        visible = supply["uid"] in nearby
        expiry_window = clock >= supply["expiry_game_time_estimate"]
        state = (visible, expiry_window)
        if previous.get(supply["uid"]) != state:
            journal.write(
                origin="judge", event="supply_lifetime_observation", uid=supply["uid"],
                item=supply["item"], visible_in_local_view=visible,
                native_lifetime_game_seconds=supply["expires_after_game_seconds"],
                estimated_expiry_window_reached=expiry_window,
                estimated_game_time=clock, expiry_game_time_estimate=supply["expiry_game_time_estimate"],
                replenished=False,
                limit="client-clock estimate; local absence can be distance/unload/collection/expiry, not an ECS deletion proof")
            previous[supply["uid"]] = state


def measurement_validity(record: dict, fixture: dict, observation: dict,
                         receipts: list[dict], score: dict, journal: Journal) -> dict:
    validity = target_validity(fixture, observation, receipts, setup.supplies(fixture))
    previous = record.get("target_validity")
    if previous is None or [
            (row["uid"], row["status"], row["valid"]) for row in previous["required_targets"]] != [
            (row["uid"], row["status"], row["valid"]) for row in validity["required_targets"]]:
        journal.write(origin="judge", event="required_target_validity", **validity)
    record["target_validity"] = validity
    navigation = score.get("parts", {}).get("travel", score)
    if "arrival" in navigation:
        metric = record.setdefault("persistent_arrival", {
            "ever": False, "first_elapsed_s": None,
            "ever_with_confirmed_target_condition": False,
            "ever_with_uncertain_target_condition": False,
            "ever_with_unavailable_target_condition": False})
        metric["final"] = navigation["arrival"]
        if navigation["arrival"]:
            if not metric["ever"]:
                metric["first_elapsed_s"] = observation["_elapsed_s"]
            metric["ever"] = True
            metric["ever_with_confirmed_target_condition"] |= validity["confirmed_available"]
            metric["ever_with_uncertain_target_condition"] |= validity["uncertain"]
            metric["ever_with_unavailable_target_condition"] |= not validity["valid"]
    return validity


def measurement_stop(record: dict, definition: dict, validity: dict, *,
                     entry_exited: bool, horizon_reached: bool) -> bool:
    score = record["score"]
    if score["success"] or score["dead"]:
        record["stop_reason"] = "native_goal_success" if score["success"] else "player_death"
        record["classification"] = "capability_success" if score["success"] else "agent_failure"
    elif not validity["valid"] and (
            definition.get("timed_interaction_policy") != "persistent-arrival"
            or record.get("persistent_arrival", {}).get("ever") is True):
        record["stop_reason"] = "required_target_unavailable"
        record["classification"] = "target_unavailable"
    elif score.get("measurement_unknown_reasons") and score.get("parts", {}).get("encounter_resolved"):
        record["stop_reason"] = "native_effect_provenance_unknown"
        record["classification"] = "native_effect_unknown"
    elif entry_exited:
        record["stop_reason"] = "normal_entry_exited"
        record["classification"] = ("interface_entry_exited" if record.get("mode") == "interface-smoke"
                                    else "entry_exited_without_goal")
    elif horizon_reached:
        record["stop_reason"] = "evaluation_horizon"
        record["classification"] = "evaluation_horizon_without_success"
    else:
        return False
    if "persistent_arrival" in record:
        record["navigation_outcome"] = (
            "arrival_observed" if record["persistent_arrival"]["ever"]
            else record["stop_reason"] + "_without_arrival")
    return True


async def stop_agent(process: asyncio.subprocess.Process, start: int, record: dict) -> None:
    if process.returncode is None:
        lifecycle.assert_owned(process.pid, start)
        record["stop_signal"] = {
            "signal": "SIGINT", "requested_monotonic": time.monotonic(), "sent": False}
        process.send_signal(signal.SIGINT)
        record["stop_signal"]["sent"] = True
        try:
            await asyncio.wait_for(process.wait(), 45)
        except TimeoutError:
            record["graceful_stop_timeout"] = True
    await stop_owned(process, start, record)


def bridge_cleanup(bridge: NativeBridge) -> dict:
    if bridge.process is None:
        return {**bridge.cleanup, "started": False, "stopped": True,
                "reason": "native bridge process was never started"}
    return bridge.cleanup


def record_failure(record, timing, error, *, source="runtime", phase=None):
    phase = record.get("phase", "pre_entry") if phase is None else phase
    post_terminal = "task_terminal" in record
    interrupted = isinstance(error, (KeyboardInterrupt, asyncio.CancelledError))
    if interrupted:
        record.setdefault("interruption", {
            "stage": ("post_terminal" if post_terminal else
                      "measurement" if "harness_entry" in timing.points else "pre_entry"),
            "phase": phase, "observed_monotonic": time.monotonic()})
        record.setdefault("pre_entry_failure", "harness_entry" not in timing.points)
    results.error(record, error, source=source, phase=phase)
    record.setdefault("error", results.exception_detail(error))
    record.update(success=False, capability_claim=False)
    if not post_terminal:
        record["classification"] = (
            "operator_interrupted" if interrupted else
            "mechanism_failure" if (record.get("mode") == "mechanism" and phase == "measurement"
                                    and isinstance(error, (NativeRefusal, NativeActionFailure))) else
            "fixture_invalid" if phase == "fixture_setup" else "infrastructure_error")
        if isinstance(error, NativeRefusal):
            record["native_refusal"] = {"command": error.command, "error": error.native_error}
        record.setdefault("stop_reason", "operator_interrupted" if interrupted else
                          "pre_entry_error" if "harness_entry" not in timing.points else "runtime_error")
        results.terminal(record, timing)


async def finish_owned(awaitable, record, timing):
    task = asyncio.ensure_future(awaitable)
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if task.cancelled():
                raise
            record_failure(record, timing, error, phase="cleanup")


def collect_entry(args, out, record, timing, adapter, actor, fixture, sdk_out):
    record["entry_evidence_collection"] = {"attempted": True, "status": "collecting"}
    entry_evidence = (adapter.collect() if adapter is not None else
                      evidence.select(out / "agent") if args.harness == "gameagent"
                      else evidence.select_sdk(sdk_out))
    lifecycle.write(out / "entry-evidence.json", entry_evidence)
    record["entry_evidence_collection"].update(
        status="partial" if entry_evidence.get("collection_errors") else "complete",
        errors=entry_evidence.get("collection_errors", []))
    record["runtime_model_metadata"] = entry_evidence["runtime_model_metadata"]
    if record.get("renderer"):
        record["model_image_transport"] = entry_evidence.get("image_transport")
    classification = record["classification"]
    if args.harness == "gameagent":
        record["provider_errors_excluding_cancellation"] = (
            entry_evidence["provider_errors_excluding_cancellation"])
        record["model_success_evidence"] = evidence.gameagent_evaluation(
            entry_evidence, stop_reason=record["stop_reason"],
            stop_signal=record["cleanup"]["agent"].get("stop_signal", {}),
            stop_requested_at=record.get("evaluator_stop_requested_at"))
    if adapter is not None or args.harness == "copilot-sdk":
        facts = entry_evidence if adapter is not None else entry_evidence["common_facts"]
        classification, record["model_success_evidence"] = external.adjudicate(
            facts, actor.measured_receipts, fixture["opening"], record.get("score", {}),
            native_classification=record["task_terminal"]["classification"],
            stop_reason=record["stop_reason"],
            stop_signal=record["cleanup"]["agent"].get("stop_signal", {}),
            terminal_monotonic=timing.points["terminal"]["monotonic"],
            unknown_effects=record["unknown_effects"])
    elif not entry_evidence["entry_receipt_present"]:
        classification = "infrastructure_error"
    elif entry_evidence["provider_errors_excluding_cancellation"]:
        classification = "provider_error"
    elif record["stop_reason"] == "normal_entry_exited":
        error = entry_evidence["entry"].get("primary_error") or {}
        classification = ("infrastructure_error" if error.get("type") == "VelorenBridgeError"
                          else "agent_entry_error")
    if args.harness == "gameagent" and record.get("score", {}).get("success"):
        record["model_success_evidence"].update(evidence.gameagent_success(
            entry_evidence, fixture["opening"], actor.measured_receipts, record["score"],
            stop_reason=record["stop_reason"],
            stop_requested_at=record.get("evaluator_stop_requested_at")))
    entry = entry_evidence.get("entry")
    record["harness_cleanup_complete"] = bool(
        entry is not None and not entry.get("cleanup_errors")
        and (adapter is None or external.retired(entry_evidence)))
    unknown = record.get("score", {}).get("measurement_unknown_reasons", [])
    if unknown:
        proof = record.setdefault("model_success_evidence", {})
        evaluation = proof.setdefault("evaluation_validity", {"status": "unknown", "reasons": []})
        if evaluation["status"] != "invalid":
            evaluation["status"] = "unknown"
        evaluation["reasons"] = list(dict.fromkeys([*evaluation["reasons"], *unknown]))
        proof["accepted"] = False
        classification = "native_effect_unknown"
    if "error" not in record:
        record["classification"] = classification
        if args.mode == "interface-smoke" and entry and entry["status"] == "idle":
            record["classification"] = "interface_engineering_completed"
    if entry_evidence.get("collection_errors"):
        raise ValueError("entry evidence is partial; see entry-evidence.json collection_errors")
    if not record["harness_cleanup_complete"]:
        raise RuntimeError("normal-entry cleanup or owned-process retirement is unconfirmed")


async def episode(args: argparse.Namespace) -> dict:
    timing = AttemptTiming()
    preflight = pins.load(args.pins, args.pins_sha256, backend=getattr(args, "backend", "headless"))
    if args.scene in {f"T{i:02}" for i in range(1, 13)} and getattr(args, "backend", "headless") != "renderer":
        raise ValueError("new family headless combinations are unqualified; use the explicit renderer matrix")
    if args.harness == "responses" and getattr(args, "backend", "headless") != "renderer":
        raise ValueError("Responses multimodal entry requires --backend renderer")
    if args.mode == "interface-smoke" and args.harness not in {"responses", "external"}:
        raise ValueError("interface-smoke requires an explicit engineering adapter")
    if args.mode == "task-engineering" and (
            args.harness not in {"responses", "external"} or getattr(args, "backend", "headless") != "renderer"):
        raise ValueError("task-engineering requires a declared renderer adapter")
    if getattr(args, "backend", "headless") == "renderer":
        from .renderer_support import admit_selection
        admit_selection(args, preflight)
    if getattr(args, "variant", None) is not None and args.scene != "T06":
        raise ValueError("--variant is only available for the single multi-weapon task T06")
    # Setup-only mechanism selections retain no-allocation semantics.
    if args.mode == "mechanism":
        definition = suite.load(Path(preflight["dependencies"]["scene_manifest"]["path"]))[args.scene]
        if definition.get("mechanism_mode") == "setup-only":
            raise ValueError("scene reachability is setup only; use --mode setup or capability")
    configured_root = Path(preflight["output_root"])
    if getattr(args, "output_root", None) not in (None, configured_root):
        raise ValueError("--output-root must match the validated runtime configuration")
    args.output_root = configured_root
    protected = [Path(p) for p in preflight["protected_paths"]]
    runtime.exclude_outputs(configured_root, protected)
    if getattr(args, "sdk_output_root", None) is not None:
        runtime.exclude_outputs(args.sdk_output_root, protected)
    lifecycle.protection_snapshot()
    output_locations(
        args.out, getattr(args, "output_root", None), getattr(args, "sdk_output_root", None),
        check_sdk=args.mode == "capability" and args.harness == "copilot-sdk")
    root = getattr(args, "output_root", None) or pins.ROOT
    out = reserve_output(args.out, root)
    record = {}
    try:
        record = await _episode(args, timing, out)
    except BaseException as error:
        timing.mark("terminal")
        record = getattr(timing, "record", record)
        record.update({
            "success": False, "capability_claim": False,
            "mode": args.mode, "scene": args.scene, "terrain": args.terrain, "harness": args.harness,
            "cleanup_complete": record.get("cleanup_complete", "fixture_setup_start" not in timing.points),
            "process_cleanup_complete": record.get(
                "process_cleanup_complete", "fixture_setup_start" not in timing.points),
            "pre_entry_failure": "harness_entry" not in timing.points,
        })
        record_failure(record, timing, error, source="entry")
    finally:
        timing.mark("terminal")
        timing.mark("cleanup_end")
        reason = record.get("stop_reason", "pre_entry_error")
        if (args.mode == "capability" and "harness_entry" not in timing.points
                and "stop_reason" not in record):
            reason = "pre_entry_harness_error"
        try:
            record["timing"] = timing.finish(reason)
        except ValueError as error:
            results.error(record, error, source="timing", phase="finalization")
            record.update(success=False, capability_claim=False, classification="infrastructure_error")
            record.setdefault("error", results.exception_detail(error))
            record["timing"] = {"points": timing.points, "task_elapsed_seconds": None,
                                "task_duration_null_reason": "invalid_lifecycle_chronology",
                                "terminal_reason": reason,
                                "timing_error": str(error),
                                "setup_elapsed_seconds": (
                                    timing.points.get("setup_qualified", timing.points["terminal"])["monotonic"]
                                    - timing.points["attempt_start"]["monotonic"]),
                                "attempt_elapsed_seconds": (
                                    timing.points["cleanup_end"]["monotonic"] - timing.points["attempt_start"]["monotonic"]),
                                "cleanup_elapsed_seconds": (
                                    timing.points["cleanup_end"]["monotonic"] - timing.points["terminal"]["monotonic"])}
        lifecycle.write(out / "timing.json", record["timing"])
        record.setdefault("artifact_sha256", {})["timing.json"] = lifecycle.sha(out / "timing.json")
        if hasattr(timing, "record") or "error" in record:
            results.finalize(record, timing)
            navigation_tasks.finalize_environment(record)
        lifecycle.write(out / "result.json", record)
    return record


async def _episode(args: argparse.Namespace, timing: AttemptTiming, out: Path) -> dict:
    selected = pins.load(args.pins, args.pins_sha256, backend=getattr(args, "backend", "headless"))
    adapter = None
    renderer_selected = getattr(args, "backend", "headless") == "renderer"
    if args.harness == "responses":
        if args.mode not in {"setup", "mechanism"}:
            from .responses_adapter import Adapter
            adapter = Adapter(args)
    elif args.harness == "external":
        if args.mode not in {"capability", "interface-smoke", "task-engineering"}:
            raise ValueError("External entries use capability or engineering modes")
        if getattr(args, "compatibility", None) == "legacy-e244" and args.evaluation_horizon is None:
            args.evaluation_horizon = compatibility.E244_HORIZONS[args.scene]
        config = json.loads(getattr(args, "adapter_config", "{}"))
        if not isinstance(config, dict):
            raise ValueError("Adapter configuration must be a JSON object")
        adapter = external.Adapter(
            selected, args.adapter, config,
            engineering=getattr(args, "external_engineering", False),
            admission=getattr(args, "external_admission", None), pins_sha=args.pins_sha256,
            requested={key: getattr(args, key, None) for key in (
                "model", "reasoning_effort", "observation_mode", "action_profile")})
    elif (getattr(args, "observation_mode", None) not in {None, "structured"}
          or getattr(args, "action_profile", None) not in {None, "raw-native-v2"}
          or getattr(args, "model", None) not in {None, compatibility.LEGACY_MODEL}
          or getattr(args, "reasoning_effort", None) not in {None, compatibility.LEGACY_EFFORT}):
        raise ValueError("Legacy built-in entry does not support the requested configuration")
    definitions = suite.load(Path(selected["dependencies"]["scene_manifest"]["path"]))
    definition = definitions[args.scene]
    if args.terrain not in definition["terrains"] or args.seed not in selected["seeds"]:
        raise ValueError("scene/terrain/seed selection not admitted by frozen manifest")
    if args.mode == "mechanism" and definition.get("mechanism_mode") == "setup-only":
        raise ValueError("scene reachability is setup only; use --mode setup or capability")
    source = None
    if args.harness == "gameagent" and args.mode == "capability":
        if args.source not in selected["sources"]:
            raise ValueError("BuiltinGameAgent requires an explicitly bound external source profile")
        source = selected["sources"][args.source]["root"]
    preflight_receipt = None
    if adapter is not None:
        preflight_receipt = await adapter.preflight(
            backend=args.backend, frames=getattr(args, "recordings", None))
    elif args.mode == "capability":
        preflight_receipt = await launching.preflight_builtin(args, selected, source)
    config = NativeConfig(
        server=Path(selected["dependencies"]["server"]["path"]),
        bridge=Path(selected["dependencies"]["bridge"]["path"]),
        assets=Path(selected["assets"]["root"]), root=Path(selected["engine_root"]),
        seed=definition.get("native_world_seed", args.seed),
        small_world=renderer_selected and args.scene in {"C1", "T11", "T12"},
        private_oracle=selected.get("native_interactions") is not None,
        resident_fixture={"T10": "resident-smoke-v1", "T11": "outcomes-message-v1",
                          "T12": "outcomes-slay-v1"}.get(args.scene))
    output_root = output_paths.validate_root(getattr(args, "output_root", None) or pins.ROOT)
    sdk_root = output_paths.validate_root(getattr(args, "sdk_output_root", None) or output_root / "sdk-adapter")
    sdk_out = sdk_root / "episodes" / out.name
    token = uuid4().hex[:10]
    admin_name, actor_name = f"setup{token}", f"rpg{token}"
    record = {
        "schema": 2, "mode": args.mode, "scene": args.scene, "terrain": args.terrain,
        "output_root": str(output_root), "sdk_output_root": str(sdk_root),
        "harness": args.harness, "source_profile": args.source, "source": source,
        "scene_qualification": definition.get("qualification", "native per-episode fixture validation; capability tier not calibrated"),
        "tool_track": (adapter.manifest["tool_track"] if adapter is not None else
                       "gameagent-composites" if args.harness == "gameagent" else "raw-native-reference"),
        "classification": "infrastructure_error", "success": False,
        "username": admin_name, "actor": actor_name, "phase": "startup",
        "pins_path": str(args.pins), "pins_sha256": args.pins_sha256,
        "selected_benchmark_seed": args.seed,
        "native_backend_identity": selected["native_backend_identity"],
        "source_commit": "unknown", "control_commit": "dirty-working-tree",
        "profile": {"brain": "gpt-6-luna",
                    "reasoning_effort": "none" if args.harness == "gameagent" else "omitted SDK default",
                    "jev": "existing real adapter" if args.harness == "gameagent" else "not-used"},
        "served_model": {"brain": "unknown", "jev": "unknown"},
        "evaluation_horizon_seconds": args.evaluation_horizon,
        "evaluation_horizon_policy": "explicit evaluator horizon, not a model request/action/retry quota",
        "content_sha256": selected["content_sha256"],
        "shared_before": lifecycle.protection_snapshot(),
        "cleanup": {},
        "agent_preflight": preflight_receipt,
    }
    if adapter is not None:
        record["external_adapter"] = adapter.id
        record["external_engineering"] = adapter.engineering
        record["profile"] = {"brain": adapter.configuration["model"],
                             "reasoning_effort": adapter.configuration["reasoning_effort"], "jev": "not-used"}
        record["adapter_configuration"] = adapter.configuration
        record["adapter_capabilities"] = adapter.capabilities
        record["legacy_contract_compatibility"] = adapter.legacy
    if renderer_selected:
        record["renderer"] = {
            **selected["renderer"], "selected_for_episode": True,
            "status": "episode-selected", "native_execution": "not-yet-started",
            "backend_observation_mode": "hybrid-trusted-private-pipe",
            "public_observation_mode": args.observation_mode or "structured",
            "effective_action_profile": adapter.configuration["action_profile"] if adapter else "setup-private",
            "fixture_profile": f"matching-source-{'small' if config.small_world else 'default'}-world-{args.scene}-{args.terrain}-v2",
            "native_tick": None, "action_effect_ordering": "unconfirmed",
            "structured_frame_pairing": "not-synchronized"}
        record["renderer"].pop("model_attachment_delivered")
        record["renderer"]["native_local_attachment_only"] = True
        if args.mode == "interface-smoke":
            record["engineering_scope"] = "interface-transport-not-scored-ability"
        elif args.mode == "task-engineering":
            record["engineering_scope"] = "ordinary-model-driven-task-integration-not-formal-ability"
        elif args.mode == "mechanism":
            record["engineering_scope"] = "synthetic-native-mechanism-not-model-ability"
    record["compatibility_policy"] = getattr(args, "compatibility", None)
    timing.record = record
    if renderer_selected:
        from .renderer import RendererJournal
        journal = RendererJournal(out / "native.ndjson")
    else:
        journal = Journal(out / "native.ndjson")
    socket_owner = None
    admin = actor = boundary = player = None
    player_log = None
    samples = []
    fixture = None
    supply_states = {}
    try:
        timing.mark("fixture_setup_start")
        async with private_server(out, record, config) as (port, userdata):
            admin = NativeBridge(out, admin_name, port, userdata, journal, config)
            if renderer_selected:
                from .renderer import RendererBridge
                args.recordings.mkdir(parents=True, exist_ok=False)
                actor = RendererBridge(out, actor_name, port, userdata, journal, config,
                                       renderer=selected["renderer"], frames=args.recordings)
            else:
                actor = NativeBridge(out, actor_name, port, userdata, journal, config)
            actor.family_companions = []
            try:
                await admin.start()
                deferred_actor = definition.get("layout") == "named_site_long" or args.scene == "T01"
                if not deferred_actor:
                    await actor.start()
                    if renderer_selected:
                        record["renderer"]["native_execution"] = "started-same-ordinary-actor"
                record["phase"] = "fixture_setup"
                await setup.command(admin, "buff", "invulnerability", 1, 3600)
                protected = await setup.wait_observation(admin, lambda row: any(
                    buff.get("kind") == "Invulnerability" for buff in row["survival"]["buffs"]),
                    "setup administrator-only invulnerability")
                record["setup_admin_protection"] = protected["survival"]["buffs"]
                if definition.get("layout") == "named_site_long":
                    fixture = await navigation.setup_named_site(admin, actor, definition, start_actor=True)
                else:
                    if args.scene == "T06":
                        definition = {**definition, "selected_variant": getattr(args, "variant", None) or "sword"}
                    fixture = await suite.setup(admin, actor, definition, args.terrain, definitions)
                    if renderer_selected and deferred_actor:
                        record["renderer"]["native_execution"] = "started-same-ordinary-actor"
                if renderer_selected:
                    from .renderer_tasks import public_task
                    fixture["task"] = public_task(definition, fixture, args.observation_mode or "structured")
                try:
                    await setup.validate_opening(admin, actor, fixture)
                finally:
                    if renderer_selected:
                        from .renderer import temporal_artifact
                        persisted_fixture = temporal_artifact(fixture)
                        persisted_fixture["renderer_fixture_provenance"] = {
                            "profile": record["renderer"]["fixture_profile"],
                            "timing_profile": "voxygen-variable-dt-client-update-v1",
                            "native_tick": None, "historical_headless_equivalence": False}
                        if "placement" in persisted_fixture:
                            persisted_fixture["placement"]["condition"] = (
                                f"{args.scene} preserved fixture in the matching-source renderer world "
                                "ordinary renderer; not historical native-spawn equivalence")
                        lifecycle.write(out / "fixture.json", persisted_fixture)
                    else:
                        lifecycle.write(out / "fixture.json", fixture)
                if args.scene != "T02":
                    await admin.close()
                    record["cleanup"]["admin"] = admin.cleanup
                timing.mark("setup_qualified")
                opening = fixture["opening"]
                if "timed_resource_condition" in definition:
                    record["timed_resource_condition"] = definition["timed_resource_condition"]
                start = time.monotonic()
                opening["_elapsed_s"] = 0
                samples.append(opening)
                log_supplies(fixture, opening, journal, supply_states)
                record["actor_identity"] = opening["self_identity"]
                record["native_session_id"] = opening["session_id"]
                record["fixture_verified"] = True
                record["phase"] = "measurement"
                journal.write(origin="measurement", event="start", tick=opening["tick"],
                              actor=record["actor_identity"], mode=args.mode)
                if args.mode == "setup":
                    final = await actor.request("observe", origin="judge")
                    log_supplies(fixture, final, journal, supply_states)
                    final["_elapsed_s"] = time.monotonic() - start
                    measurement_validity(record, fixture, final, [], {}, journal)
                    record["classification"] = "fixture_verified_only"
                    record["stop_reason"] = "setup_only"
                elif args.mode == "mechanism":
                    await suite.mechanism(definition, actor, fixture)
                    final = await actor.request("observe", origin="judge")
                    log_supplies(fixture, final, journal, supply_states)
                    final["_elapsed_s"] = time.monotonic() - start
                    samples.append(final)
                    await asyncio.sleep(.3)
                    final = await actor.request("observe", origin="judge")
                    final["_elapsed_s"] = time.monotonic() - start
                    samples.append(final)
                    record["score"] = suite.score(definition, fixture, final, actor.measured_receipts,
                                                   actor.native_events, samples)
                    record["final_state_observed"] = True
                    measurement_validity(record, fixture, final, actor.measured_receipts, record["score"], journal)
                    if definition["kind"] == "combat":
                        record["mechanism_effect_verified"] = bool(record["score"]["enemy_damage_events"])
                    else:
                        record["mechanism_effect_verified"] = record["score"]["success"]
                    record["stop_reason"] = "mechanism_completed"
                    record["classification"] = ("mechanism_pass" if record["mechanism_effect_verified"]
                                                else "mechanism_failure")
                else:
                    socket_owner = SocketEndpoint(out)
                    socket_path = socket_owner.path
                    if renderer_selected:
                        from .renderer import RendererBoundary
                        boundary = RendererBoundary(actor, socket_path, args.observation_mode or "structured")
                        if args.scene == "T02":
                            boundary.public_observer = lambda request, response: navigation_tasks.public_witness(
                                fixture, request, response)
                    else:
                        boundary = EpisodeBoundary(actor, socket_path)
                    await boundary.start()
                    plan = launching.plan(
                        args, selected, adapter, out=out, actor=actor_name, source=source,
                        task=fixture["task"], definition=definition, native=config,
                        socket=socket_path, sdk_out=sdk_out, sdk_root=sdk_root)
                    command = list(plan.command)
                    if args.harness == "copilot-sdk":
                        record["sdk_output"] = str(sdk_out)
                    if adapter is not None:
                        lifecycle.write(out / "external-context.json", adapter.context)
                    lifecycle.write(out / "agent-command.json", command)
                    environment = external.clean_environment()
                    environment.update(plan.env)
                    timing.mark("agent_launch_start")
                    player_log = (out / "agent-console.log").open("x")
                    player = await asyncio.create_subprocess_exec(
                        *command, cwd=plan.cwd, env=environment,
                        stdout=player_log, stderr=asyncio.subprocess.STDOUT)
                    player_start = lifecycle.process_start_ticks(player.pid)
                    record["agent_pid"] = player.pid
                    record["agent_start_ticks"] = player_start
                    timing.mark("agent_launched")
                    if adapter is None:
                        timing.mark("harness_entry", timing.points["agent_launched"])
                    timing.mark("evaluator_start")
                    start = timing.points["evaluator_start"]["monotonic"]
                    while True:
                        if adapter is not None:
                            adapter.observe_entry(timing)
                        # An entry stop needs a sample taken after its last possible submission.
                        entry_exited = player.returncode is not None
                        final = await actor.request("observe", origin="judge")
                        if definition["kind"] == "navigation_tasks":
                            await navigation_tasks.update_event(admin, actor, fixture, final)
                        final["_elapsed_s"] = time.monotonic() - start
                        if final["tick"] > samples[-1]["tick"]:
                            samples.append(final)
                        record["score"] = suite.score(definition, fixture, final, actor.measured_receipts,
                                                       actor.native_events, samples)
                        record["final_state_observed"] = True
                        validity = measurement_validity(
                            record, fixture, final, actor.measured_receipts, record["score"], journal)
                        if measurement_stop(
                                record, definition, validity, entry_exited=entry_exited,
                                horizon_reached=args.evaluation_horizon is not None
                                and time.monotonic() - start >= args.evaluation_horizon):
                            break
                        await asyncio.sleep(.25)
                    record["measurement_wall_seconds"] = time.monotonic() - start
                    results.terminal(record, timing)
                    record["phase"] = "teardown"
                    if record["score"]["success"]:
                        timing.mark("native_success", timing.points["terminal"])
                    record["evaluator_stop_requested_at"] = datetime.now(timezone.utc).isoformat()
                    journal.write(origin="judge", event="evaluator_agent_stop_requested",
                                  stop_reason=record["stop_reason"],
                                  host_recorded_at=record["evaluator_stop_requested_at"])
                lifecycle.write(out / "final-observation.json", final)
                record.setdefault("measurement_wall_seconds", time.monotonic() - start)
                record["success"] = record["classification"] in (
                    "capability_success", "mechanism_pass", "fixture_verified_only",
                    "interface_engineering_completed")
                record["capability_claim"] = args.mode == "capability" and record["success"]
                if adapter is not None and adapter.engineering and args.mode != "interface-smoke":
                    record.update(success=False, capability_claim=False)
                lifecycle.write(out / "trajectory.json", samples)
                lifecycle.write(out / "native-events.json", actor.native_events)
                record["phase"] = "measured"
                results.terminal(record, timing)
            except BaseException as error:
                record_failure(record, timing, error)
            finally:
                try:
                    if player is not None:
                        record.setdefault("evaluator_stop_requested_at", datetime.now(timezone.utc).isoformat())
                        await finish_owned(stop_agent(
                            player, player_start, record["cleanup"].setdefault("agent", {})), record, timing)
                        timing.mark("agent_final_stop")
                except BaseException as error:
                    record_failure(record, timing, error, phase="cleanup")
                finally:
                    for resource, operation in ((player_log, "close"), (boundary, "begin_close")):
                        try:
                            if resource is not None:
                                getattr(resource, operation)()
                        except BaseException as error:
                            record_failure(record, timing, error, phase="cleanup")
                    companions = [(f"companion-{i}", bridge) for i, bridge in enumerate(
                        getattr(actor, "family_companions", []))]
                    for role, bridge in (*companions, ("actor", actor), ("admin", admin)):
                        try:
                            if bridge is not None:
                                await finish_owned(bridge.close(), record, timing)
                        except BaseException as error:
                            record_failure(record, timing, error, phase="cleanup")
                        finally:
                            if bridge is not None:
                                record["cleanup"][role] = bridge_cleanup(bridge)
                    if boundary is not None:
                        try:
                            await finish_owned(boundary.close(), record, timing)
                        except BaseException as error:
                            record_failure(record, timing, error, phase="cleanup")
                        finally:
                            record["cleanup"]["boundary"] = {
                                "socket_removed": not boundary.path.exists(),
                                "errors": boundary.errors, "error_details": boundary.error_details,
                                "proxy_peer": boundary.peer}
                if boundary is not None and boundary.errors:
                    record_failure(record, timing, RuntimeError(
                        f"native boundary errors: {boundary.errors}"), phase="cleanup")
    except BaseException as error:
        record_failure(record, timing, error)
    finally:
        if fixture is not None and "event_state" in fixture:
            record["world_event"] = fixture["event_state"]
            lifecycle.write(out / "private-world-event.json", fixture["event_state"])
        if actor is not None:
            lifecycle.write(out / "native-events.json", actor.native_events)
            lifecycle.write(out / "native-receipts.json", actor.measured_receipts)
            record["unknown_effects"] = [row for row in actor.effect_observations.values()
                                         if row["effect_status"] == "unknown"]
            record.setdefault("error_observations", []).extend(actor.error_observations)
        if boundary is not None:
            record.setdefault("error_observations", []).extend(
                {"source": "boundary", "phase": row["phase"],
                 "wall_monotonic": row["wall_monotonic"], "error": row}
                for row in boundary.error_details)
        if samples and not (out / "trajectory.json").exists():
            lifecycle.write(out / "trajectory.json", samples)
        if player is not None:
            if adapter is not None:
                try:
                    adapter.observe_entry(timing)
                except BaseException as error:
                    record_failure(record, timing, error, source="evidence", phase="entry_timestamp")
            try:
                if record["cleanup"].get("agent", {}).get("stopped") is not True:
                    raise RuntimeError("cannot collect evidence before owned player retirement")
                collect_entry(args, out, record, timing, adapter, actor, fixture, sdk_out)
            except BaseException as error:
                record.setdefault("entry_evidence_collection", {"attempted": False})["error"] = (
                    results.exception_detail(error))
                if record["entry_evidence_collection"].get("status") == "collecting":
                    record["entry_evidence_collection"]["status"] = "failed"
                record.setdefault("harness_cleanup_complete", False)
                record_failure(record, timing, error, source="evidence", phase="finalization")
        journal.close()
        if socket_owner is not None:
            socket_owner.close()
        record["shared_after"] = lifecycle.protection_snapshot()
        record["shared_preserved"] = record["shared_before"] == record["shared_after"]
        record["process_cleanup_complete"] = (
            record.get("server_stopped") is True and record["shared_preserved"]
            and all(record["cleanup"].get(name, {}).get("stopped") is True
                    for name in ("actor", "admin"))
            and all(value.get("stopped") is True for name, value in record["cleanup"].items()
                    if name.startswith("companion-"))
            and (not renderer_selected or record["cleanup"].get("actor", {}).get("started") is False
                 or record["cleanup"].get("actor", {}).get(
                "display", {}).get("stopped") is True)
            and (args.mode not in {"capability", "interface-smoke", "task-engineering"} or player is None
                 or (record["cleanup"].get("agent", {}).get("stopped") is True
                     and record["cleanup"].get("boundary", {}).get("socket_removed") is True
                     and (not boundary.peer or boundary.peer.get("retired") is True)))
        )
        record["harness_evidence_complete"] = (
            player is None or record.get("entry_evidence_collection", {}).get("status") == "complete")
        record["cleanup_complete"] = (
            record["process_cleanup_complete"]
            and record.get("harness_cleanup_complete", True)
            and (boundary is None or not boundary.errors))
        if not record["cleanup_complete"]:
            record.update(success=False, capability_claim=False)
        timing.mark("terminal")
        timing.mark("cleanup_end")
        record["artifact_sha256"] = {
            str(path.relative_to(out)): lifecycle.sha(path)
            for path in sorted(out.glob("*")) if path.is_file()
        }
        if (record.get("entry_evidence_collection", {}).get("status") == "complete"
                and record["cleanup_complete"] and "error" not in record):
            record["success"] = record["classification"] in (
                "capability_success", "interface_engineering_completed")
            record["capability_claim"] = args.mode == "capability" and record["success"]
            if adapter is not None and adapter.engineering and args.mode != "interface-smoke":
                record.update(success=False, capability_claim=False)
        lifecycle.write(out / "result.json", record)
    return record


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Frozen concrete native M1 scenes through real agent harnesses.")
    result.add_argument("--config", "--pins", dest="pins", required=True, type=Path,
                        help="Explicit rpg-runtime/1 local binding, not historical pins.")
    result.add_argument("--config-sha256", "--pins-sha256", dest="pins_sha256", required=True)
    result.add_argument("--out", required=True, type=Path)
    result.add_argument("--output-root", type=Path,
                        help="Must match runtime config output_root; normally omit this override.")
    result.add_argument("--sdk-output-root", type=Path,
                        help="SDK allowlist root; default is OUTPUT_ROOT/sdk-adapter.")
    result.add_argument("--mode", required=True, choices=("capability", "mechanism", "setup", "interface-smoke", "task-engineering"))
    result.add_argument("--scene", default="C1")
    result.add_argument("--variant", choices=("sword", "bow", "staff", "sceptre"),
                        help="T06 configured weapon condition; one task, not four task definitions.")
    result.add_argument("--terrain", choices=("flat", "natural"), default="natural")
    result.add_argument("--seed", type=int, default=lifecycle.SEED)
    result.add_argument("--source", choices=("baseline", "candidate"), default="baseline")
    result.add_argument("--harness", choices=("gameagent", "copilot-sdk", "external", "responses"), default="gameagent")
    result.add_argument("--backend", choices=("headless", "renderer"), default="headless")
    result.add_argument("--recordings", type=Path,
                        help="New outputs/recordings/RUN-ID directory; required for renderer.")
    result.add_argument("--adapter", help="External adapter ID registered in frozen pins.")
    result.add_argument("--adapter-config", default="{}", help="Adapter-owned JSON configuration; no evaluator fields.")
    result.add_argument("--model")
    result.add_argument("--reasoning-effort")
    result.add_argument("--observation-mode", choices=("structured", "image", "hybrid"))
    result.add_argument("--action-profile")
    result.add_argument("--compatibility", choices=("legacy-e244",),
                        help="Opt into historical scene horizons when no horizon is specified.")
    result.add_argument("--external-engineering", action="store_true",
                        help="Synthetic external integration check, never capability evidence.")
    result.add_argument("--external-admission", type=Path, help="A0 exact-configuration live experiment admission.")
    result.add_argument("--evaluation-horizon", type=float,
                        help="Explicit evaluator wall horizon; omitted stops on success/death/entry error.")
    return result


def main() -> None:
    args = parser().parse_args()
    if args.evaluation_horizon is not None and (
            not math.isfinite(args.evaluation_horizon) or args.evaluation_horizon <= 0):
        raise ValueError("evaluation horizon must be finite and positive")
    record = asyncio.run(episode(args))
    print(json.dumps({key: record.get(key) for key in (
        "classification", "success", "capability_claim", "cleanup_complete", "error")}))
    raise SystemExit(0 if record["success"] else 1)


if __name__ == "__main__":
    main()
