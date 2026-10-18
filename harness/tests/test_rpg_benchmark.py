import asyncio
import copy
import json
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from scripts.rpg_benchmark import evidence, native, oracles, pins, run, scene, setup


def observation(output=0):
    return {
        "session_id": "s", "self_identity": {"uid": 42},
        "survival": {"health": {"current": 100}},
        "inventory": {"items": [
            {"item": {"id": name, "amount": amount}}
            for name, amount in {**scene.INPUTS, scene.OUTPUT: output}.items()
            if amount
        ]},
    }


class OracleTests(unittest.TestCase):
    def setUp(self):
        self.opening = observation()
        self.final = observation(1)
        self.final["inventory"]["items"] = [
            {"item": {"id": {"Simple": scene.OUTPUT}, "amount": 1}}]
        self.receipt = {
            "actor": 42,
            "request": {"cmd": "step", "args": {"action": {
                "type": "craft", "recipe": scene.RECIPE, "action_id": "a"}}},
            "response": {"ok": True, "session_id": "s", "result": {
                "status": "completed", "action_type": "craft", "action_id": "a",
                "detail": {"craft_update_observed": True, "output_definition_id": scene.OUTPUT,
                           "output_definition_delta": 1,
                           "inventory_delta": {name: -amount for name, amount in scene.INPUTS.items()}}}},
        }

    def test_requires_native_receipt_consumption_and_retention(self):
        self.assertTrue(scene.score(self.opening, self.final, [self.receipt])["success"])
        self.assertFalse(scene.score(self.opening, self.final, [])["success"])
        self.assertFalse(scene.score(self.opening, self.opening, [self.receipt])["success"])
        for key, value in (("craft_update_observed", False), ("output_definition_delta", 0),
                           ("output_definition_id", "wrong"), ("inventory_delta", {})):
            receipt = copy.deepcopy(self.receipt)
            receipt["response"]["result"]["detail"][key] = value
            self.assertFalse(scene.score(self.opening, self.final, [receipt])["success"])

    def test_identity_death_setup_products_and_uncorrelated_actions_fail(self):
        for key, value in (("session_id", "other"), ("self_identity", {"uid": 43}),
                           ("survival", {"dead": True})):
            final = copy.deepcopy(self.final)
            final[key] = value
            self.assertFalse(scene.score(self.opening, final, [self.receipt])["success"])
        self.assertFalse(scene.score(observation(1), self.final, [self.receipt])["success"])
        receipt = copy.deepcopy(self.receipt)
        receipt["response"]["result"]["action_id"] = "unrelated"
        self.assertFalse(scene.score(self.opening, self.final, [receipt])["success"])
        final = copy.deepcopy(self.final)
        final.pop("survival")
        self.assertFalse(scene.score(self.opening, final, [self.receipt])["success"])


class EvidenceTests(unittest.TestCase):
    def test_runtime_metadata_is_observed_not_inferred_and_cancellation_separate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rows = [
                {"record_type": "provider_model_request"},
                {"record_type": "provider_model_receipt", "usage": {"sdk_model": "observed-brain"},
                 "tool_calls": [{"name": "CraftRecipeSkill"}]},
                {"record_type": "async_jev_receipt", "brain_pending": True,
                 "outcome": {"receipt": {"model": "observed-jev"}}},
                {"record_type": "provider_model_error", "error": {"type": "CancelledError"}},
            ]
            (root / "events.ndjson").write_text("".join(json.dumps(row) + "\n" for row in rows))
            selected = evidence.select(root)
            self.assertEqual(selected["runtime_model_metadata"]["brain_sdk_usage_models"], ["observed-brain"])
            self.assertEqual(selected["runtime_model_metadata"]["jev_response_models"], ["observed-jev"])
            self.assertEqual(selected["jev_receipts_while_brain_pending"], 1)
            self.assertEqual(selected["provider_cancellation_receipts"], 1)
            self.assertEqual(selected["provider_errors_excluding_cancellation"], [])
            self.assertFalse(selected["entry_receipt_present"])

    def test_sdk_normal_idle_without_goal_is_not_provider_error_or_success(self):
        selected = {"entry": {"status": "idle", "cleanup_errors": []},
                    "provider_error": False, "actual_sdk_model_events": 34}
        self.assertEqual(evidence.sdk_classification(
            selected, "provider_or_agent_error", stop_reason="normal_entry_exited",
            native_success=False), "agent_idle_without_goal")
        for reason, classification in (("player_death", "agent_failure"),
                                       ("evaluation_horizon", "evaluation_horizon_without_success"),
                                       ("native_goal_success", "capability_success")):
            self.assertEqual(evidence.sdk_classification(
                selected, classification, stop_reason=reason,
                native_success=reason == "native_goal_success"), classification)
        selected["actual_sdk_model_events"] = 0
        self.assertEqual(evidence.sdk_classification(
            selected, "provider_or_agent_error", stop_reason="normal_entry_exited",
            native_success=False), "provider_or_agent_error")

    def test_sdk_genuine_provider_or_entry_error_overrides_idle_label(self):
        selected = {"entry": {"status": "idle", "cleanup_errors": []},
                    "provider_error": True, "actual_sdk_model_events": 34}
        self.assertEqual(evidence.sdk_classification(
            selected, "capability_success", stop_reason="native_goal_success",
            native_success=True), "provider_error")
        selected["provider_error"] = False
        selected["entry"]["status"] = "failed"
        self.assertEqual(evidence.sdk_classification(
            selected, "provider_or_agent_error", stop_reason="normal_entry_exited",
            native_success=False), "agent_entry_error")
        selected["entry"].update(status="idle", error={"type": "RuntimeError"})
        self.assertEqual(evidence.sdk_classification(
            selected, "provider_or_agent_error", stop_reason="normal_entry_exited",
            native_success=False), "agent_entry_error")


class SdkTerminalAdjudicationTests(unittest.TestCase):
    signal = {"signal": "SIGINT", "requested_monotonic": 100.0, "sent": True,
              "terminal_monotonic": 99.0, "stop_id": "observed-sdk-stop"}

    def selected(self):
        return {
            "entry": {"status": "interrupted", "error": {"type": "CancelledError"},
                      "cleanup_errors": []},
            "provider_error": False, "actual_sdk_model_events": 3,
            "adapter_error_events": [
                {"wall_monotonic": 100.1, "error": {"type": "CancelledError"},
                 "caused_by": {"kind": "evaluator_stop", "stop_id": "observed-sdk-stop",
                               "signal": "SIGINT"}}],
            "transport_errors": [],
        }

    def classify(self, selected, reason="native_goal_success", classification="capability_success",
                 *, stop_signal=signal, exit_code=130):
        return evidence.sdk_classification(
            selected, classification, stop_reason=reason,
            native_success=reason == "native_goal_success",
            stop_signal=stop_signal, process_exit_code=exit_code)

    def test_post_goal_cancellation_preserves_native_success_and_raw_entry(self):
        selected = self.selected()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "result.json").write_text(json.dumps(selected["entry"]))
            rows = [
                {"kind": "sdk_event", "event": {"type": "assistant.usage"}},
                {"kind": "adapter_error", **selected["adapter_error_events"][0]},
            ]
            (root / "events.ndjson").write_text("".join(json.dumps(row) + "\n" for row in rows))
            selected = evidence.select_sdk(root)
        before = copy.deepcopy(selected)
        self.assertEqual(self.classify(selected), "capability_success")
        self.assertTrue(evidence.sdk_terminal_cancellation(
            selected, stop_reason="native_goal_success", native_success=True,
            stop_signal=self.signal, process_exit_code=130))
        self.assertEqual(selected, before)

    def test_death_horizon_and_unavailable_goal_preserve_negative_native_outcomes(self):
        for reason, classification in (
                ("player_death", "agent_failure"),
                ("evaluation_horizon", "evaluation_horizon_without_success"),
                ("required_target_unavailable", "target_unavailable")):
            with self.subTest(reason=reason):
                self.assertEqual(self.classify(self.selected(), reason, classification), classification)

    def test_unproven_pre_entry_or_earlier_cancellation_is_not_forgiven(self):
        cases = [
            ("no_signal", {}, {"stop_signal": None}),
            ("signal_not_sent", {}, {"stop_signal": {**self.signal, "sent": False}}),
            ("unexpected_exit", {}, {"exit_code": 1}),
            ("no_model_entry", {"actual_sdk_model_events": 0}, {}),
            ("earlier_cancellation", {"adapter_error_events": [
                {"wall_monotonic": 99.9, "error": {"type": "CancelledError"}}]}, {}),
            ("missing_cancellation_evidence", {"adapter_error_events": []}, {}),
            ("invalid_clock", {"adapter_error_events": [
                {"wall_monotonic": float("nan"), "error": {"type": "CancelledError"}}]}, {}),
            ("failed_before_entry", {"entry": {
                "status": "failed", "error": {"type": "FileNotFoundError"}, "cleanup_errors": []}}, {}),
        ]
        for name, changes, options in cases:
            with self.subTest(case=name):
                selected = {**self.selected(), **changes}
                self.assertEqual(self.classify(selected, **options), "agent_entry_error")
        self.assertEqual(self.classify(
            self.selected(), "normal_entry_exited", "provider_or_agent_error"), "agent_entry_error")

    def test_genuine_provider_transport_cleanup_and_malformed_errors_remain_visible(self):
        for changes, expected in (
                ({"provider_error": True}, "provider_error"),
                ({"transport_errors": [{"error": {"type": "ProtocolError"}}]}, "infrastructure_error"),
                ({"entry": {"status": "failed", "error": {"type": "JSONDecodeError"},
                            "cleanup_errors": []}}, "agent_entry_error"),
                ({"entry": {**self.selected()["entry"],
                            "cleanup_errors": [{"type": "RuntimeError"}]}}, "infrastructure_error")):
            with self.subTest(changes=changes):
                self.assertEqual(self.classify({**self.selected(), **changes}), expected)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "events.ndjson").write_text('{"kind":"sdk_event","event":\n')
            with self.assertRaises(json.JSONDecodeError):
                evidence.select_sdk(root)


class AgentStopSignalTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_actual_signal_to_live_owned_entry_records_causal_stop(self):
        process = Mock(pid=123, returncode=None, wait=AsyncMock())
        record = {}
        with patch.object(run.lifecycle, "process_start_ticks", return_value=456), \
                patch.object(run, "stop_owned", new=AsyncMock()):
            await run.stop_agent(process, 456, record)
        process.send_signal.assert_called_once_with(run.signal.SIGINT)
        self.assertTrue(record["stop_signal"]["sent"])
        self.assertGreater(record["stop_signal"]["requested_monotonic"], 0)
        process = Mock(pid=123, returncode=1)
        record = {}
        with patch.object(run, "stop_owned", new=AsyncMock()):
            await run.stop_agent(process, 456, record)
        process.send_signal.assert_not_called()
        self.assertNotIn("stop_signal", record)


class TerminalEvidenceTests(unittest.TestCase):
    signal = {"sent": True, "signal": "SIGINT", "requested_monotonic": 100,
              "stop_id": "observed-legacy-stop"}

    def selected(self, root, *, model="jev-1.13.0", cancellation="2026-10-13T19:00:02+00:00",
                 provider_error=False, occurred_monotonic=101):
        public = {"criteria": {"chosen": "one tool"}, "state": {}}
        rows = [
            {"record_type": "async_jev_request", "decision_id": "d", "epoch": 0,
             "request": public, "request_sha256": hashlib.sha256(json.dumps(
                 public, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
             "choices": {"chosen": {"tool": "CraftRecipeSkill",
                                    "arguments_json": '{"recipe":"mushroom_stick","amount":1}'}}},
            {"record_type": "async_jev_receipt", "decision_id": "d", "epoch": 0,
             "executive_mode": "jev", "jev_execution": "enabled", "brain_pending": True,
             "outcome": {"status": "accepted", "receipt": {
                 "model": model, "choice": "chosen", "probabilities": {"chosen": 1.0},
                 "input_tokens": 10, "output_tokens": 3, "confidence": .9, "elapsed_seconds": .3}}},
            {"record_type": "async_effect_handoff", "owner": "jev", "epoch": 0,
             "tool": "CraftRecipeSkill", "invocation_id": "i"},
            {"record_type": "public_action_dispatch", "forwarded": True,
             "host_event_sequence": 4, "action": {"name": "craft", "correlation_id": "native-a",
                                                "parameters": {"recipe": "mushroom_stick", "amount": 1}}},
            {"record_type": "provider_model_error", "host_recorded_at": cancellation,
             "error": {"type": "CancelledError"},
             "occurrence": {"clock": "host-monotonic", "value": occurred_monotonic, "source": "provider"},
             "caused_by": {"kind": "evaluator_stop", "stop_id": "observed-legacy-stop", "signal": "SIGINT"}},
        ]
        if provider_error:
            rows.insert(4, {"record_type": "provider_model_error",
                            "host_recorded_at": "2026-10-13T19:00:00+00:00",
                            "error": {"type": "ProviderHTTPError"}})
        (root / "events.ndjson").write_text("".join(json.dumps(row) + "\n" for row in rows))
        (root / "final.json").write_text(json.dumps({
            "runtime_mode": "async", "brain_direct_only": False, "status": "cancelled",
            "primary_error": {"type": "CancelledError"}, "cleanup_errors": [],
            "used": {"jev_effects": 0}}))
        return evidence.select(root)

    def receipt(self):
        return {"actor": 42, "request": {"cmd": "step", "args": {"action": {
            "type": "craft", "action_id": "native-a"}}},
            "response": {"ok": True, "session_id": "s", "result": {
                "status": "completed", "action_id": "native-a"}}}

    def proof(self, selected, receipts, *, reason="native_goal_success"):
        return evidence.gameagent_success(
            selected, observation(), receipts, {"attributed_action_ids": ["native-a"]},
            stop_signal=self.signal, terminal_monotonic=99,
            stop_reason=reason, stop_requested_at="2026-10-13T19:00:01+00:00")

    def test_correlated_jev_success_survives_evaluator_cancellation_without_brain_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            selected = self.selected(Path(temp))
            self.assertEqual(selected["record_counts"].get("provider_model_receipt", 0), 0)
            proof = self.proof(selected, [self.receipt()])
            self.assertTrue(proof["accepted"])
            self.assertTrue(proof["evaluator_terminal_cancellation"])
            self.assertEqual(proof["correlated_jev_effects"][0]["action_id"], "native-a")

    def test_mock_profile_uncorrelated_effect_and_preterminal_cancel_do_not_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            selected = self.selected(root, model="local-mock-chooser")
            self.assertFalse(self.proof(selected, [self.receipt()])["accepted"])
            selected = self.selected(root)
            self.assertFalse(self.proof(selected, [])["accepted"])
            wrong = self.receipt()
            wrong["actor"] = 99
            self.assertFalse(self.proof(selected, [wrong])["accepted"])
            selected["observed_jev_choices"] = []
            selected["jev_effect_dispatches"] = []
            selected["entry"]["used"] = {"provider_decisions": 99, "jev_decisions": 99}
            self.assertFalse(self.proof(selected, [self.receipt()])["accepted"])
            selected = self.selected(root, cancellation="2026-10-13T19:00:00+00:00", occurred_monotonic=99)
            self.assertFalse(self.proof(selected, [self.receipt()])["accepted"])
            selected = self.selected(root)
            self.assertFalse(self.proof(selected, [self.receipt()], reason="evaluation_horizon")["accepted"])

    def test_genuine_provider_errors_are_not_erased_by_native_or_jev_success(self):
        with tempfile.TemporaryDirectory() as temp:
            selected = self.selected(Path(temp), provider_error=True)
            proof = self.proof(selected, [self.receipt()])
            self.assertFalse(proof["accepted"])
            self.assertEqual(proof["genuine_provider_errors"], [{"type": "ProviderHTTPError"}])

    def test_real_jev_control_requires_matching_non_neutral_native_ownership(self):
        with tempfile.TemporaryDirectory() as temp:
            selected = self.selected(Path(temp))
            selected["observed_jev_choices"][0]["tool"] = "SetMotorIntentSkill"
            control = {"owner": "jev:d", "generation": 1, "sequence": 1,
                       "actor": {"uid": 42}, "session_id": "s", "applied_tick": 2, "status": "applied"}
            selected["jev_control_effects"] = [{
                **selected["observed_jev_choices"][0], "control_receipt": control}]
            selected["jev_effect_dispatches"] = []
            receipt = {"actor": 42, "request": {"cmd": "set_control_state", "args": {
                "owner": "jev:d", "generation": 1, "sequence": 1, "actor": {"uid": 42},
                "expected_session_id": "s", "movement": [1, 0]}},
                "response": {"ok": True, "session_id": "s", "result": control}}
            proof = evidence.gameagent_success(
                selected, observation(), [receipt], {"arrival": True},
                stop_signal=self.signal, terminal_monotonic=99,
                stop_reason="native_goal_success", stop_requested_at="2026-10-13T19:00:01+00:00")
            self.assertTrue(proof["accepted"])
            receipt["request"]["args"]["movement"] = [0, 0]
            proof = evidence.gameagent_success(
                selected, observation(), [receipt], {"arrival": True},
                stop_signal=self.signal, terminal_monotonic=99,
                stop_reason="native_goal_success", stop_requested_at="2026-10-13T19:00:01+00:00")
            self.assertFalse(proof["accepted"])


def validity_observation(clock=10):
    row = observation()
    row.update(tick=10, position=[0, 0, 0], nearby_entities=[], _elapsed_s=1)
    row["survival"]["buff_clock"] = {"now": clock}
    return row


def required_supply():
    return {"uid": 9, "item": "common.items.food.apple", "amount": 1, "required": True,
            "entity": {"position": [512, 0, 0]}, "expiry_game_time_estimate": 300}


class OpeningValidityTests(unittest.IsolatedAsyncioTestCase):
    async def test_dead_unknown_and_invalid_health_are_rejected(self):
        for current in (0, -1, None, True, "100", float("nan")):
            with self.subTest(current=current):
                row = observation()
                row["survival"]["health"]["current"] = current
                with self.assertRaisesRegex(ValueError, "opening actor"):
                    setup.require_living(row)
        row = observation()
        row["survival"]["dead"] = True
        with self.assertRaises(ValueError):
            setup.require_living(row)
        setup.require_living(observation())

    async def test_remote_required_uid_is_checked_after_setup_with_native_timestamps(self):
        from unittest.mock import AsyncMock
        actor, admin = Mock(), Mock()
        opening, final = validity_observation(), validity_observation(12)
        final["tick"] = 12
        actor.request = AsyncMock(side_effect=[opening, final])
        supply = required_supply()
        witness = validity_observation(11)
        witness["tick"] = 11
        witness["session_id"] = "admin"
        witness["nearby_entities"] = [{"uid": 9, "pickup": {
            "id": {"Simple": supply["item"]}, "amount": 1}}]
        fixture = {"opening": opening, "supplies": [supply]}
        with patch.object(setup, "goto", new=AsyncMock()) as relocation, patch.object(
                setup, "wait_observation", new=AsyncMock(return_value=witness)):
            await setup.validate_opening(admin, actor, fixture)
        relocation.assert_awaited_once_with(admin, admin, supply["entity"]["position"], settle=False)
        self.assertEqual(fixture["opening"]["position"], opening["position"])
        self.assertEqual(supply["opening_uid_check"]["native_tick"], 11)
        self.assertEqual(supply["opening_uid_check"]["simulation_time_observed"], 11)
        self.assertTrue(fixture["opening_validation"]["alive"])
        self.assertEqual(fixture["opening"]["tick"], 12)

    async def test_dead_opening_stops_before_supply_or_agent_admission(self):
        from unittest.mock import AsyncMock
        actor = Mock(request=AsyncMock(return_value=validity_observation()))
        actor.request.return_value["survival"]["health"]["current"] = 0
        fixture = {"opening": observation(), "supplies": [required_supply()]}
        with patch.object(setup, "goto", new=AsyncMock()) as relocation:
            with self.assertRaisesRegex(ValueError, "opening actor"):
                await setup.validate_opening(Mock(), actor, fixture)
        relocation.assert_not_awaited()
        self.assertEqual(fixture["opening_admission_observation"]["survival"]["health"]["current"], 0)


class SupplyValidityTests(unittest.TestCase):
    def test_local_absence_is_not_expiry_and_clock_estimate_does_not_grant_success(self):
        supply = required_supply()
        fixture = {"opening": validity_observation(), "supplies": [supply]}
        row = validity_observation(299)
        status = oracles.target_validity(fixture, row, [], [supply])
        self.assertTrue(status["valid"])
        self.assertEqual(status["required_targets"][0]["status"], "not_visible_unconfirmed")
        row["survival"]["buff_clock"]["now"] = 300
        status = oracles.target_validity(fixture, row, [], [supply])
        self.assertTrue(status["valid"])
        self.assertTrue(status["uncertain"])
        self.assertFalse(status["confirmed_available"])
        self.assertEqual(status["required_targets"][0]["status"], "expiry_possible")
        self.assertTrue(status["required_targets"][0]["expiry_possible"])
        row["nearby_entities"] = [{"uid": 9, "pickup": {"id": supply["item"], "amount": 1}}]
        self.assertTrue(oracles.target_validity(fixture, row, [], [supply])["valid"])

    def test_own_collection_keeps_condition_valid_after_consumption_and_expiry(self):
        supply = required_supply()
        fixture = {"opening": validity_observation()}
        receipt = {"actor": 42, "request": {"args": {"action": {
            "type": "pickup", "uid": 9, "action_id": "p"}}},
            "response": {"ok": True, "session_id": "s", "result": {
                "status": "completed", "action_type": "pickup", "action_id": "p", "detail": {
                    "uid": 9, "operational_success": True, "pickup_feedback": {
                        "server_event": "Collected", "operational_collection_justified": True,
                        "candidate_request_count": 1,
                        "collected_item": {"id": supply["item"], "amount": 1}}}}}}
        status = oracles.target_validity(fixture, validity_observation(500), [receipt], [supply])
        self.assertTrue(status["valid"])
        self.assertTrue(status["confirmed_available"])
        self.assertFalse(status["uncertain"])
        self.assertEqual(status["required_targets"][0]["status"], "collected_by_actor")
        receipt["actor"] = 99
        unrelated = oracles.target_validity(fixture, validity_observation(500), [receipt], [supply])
        self.assertTrue(unrelated["valid"])
        self.assertFalse(unrelated["confirmed_available"])
        self.assertTrue(unrelated["uncertain"])

    def test_native_expected_uid_refusal_is_unavailability_not_wrong_uid_failure(self):
        supply = required_supply()
        fixture = {"opening": validity_observation()}
        receipt = {"actor": 42, "request": {"args": {"action": {
            "type": "pickup", "uid": 9, "action_id": "p"}}},
            "response": {"ok": False, "session_id": "s", "error": {
                "code": "invalid_action", "message": "uid 9 not found"}}}
        status = oracles.target_validity(fixture, validity_observation(), [receipt], [supply])
        self.assertFalse(status["valid"])
        self.assertEqual(status["required_targets"][0]["status"], "native_target_unavailable")
        receipt["request"]["args"]["action"]["uid"] = 8
        self.assertTrue(oracles.target_validity(fixture, validity_observation(), [receipt], [supply])["valid"])
        supply["required"] = False
        self.assertTrue(oracles.target_validity(fixture, validity_observation(500), [], [supply])["valid"])

    def test_persistent_arrival_keeps_prior_opportunity_and_later_negative_evidence(self):
        supply = required_supply()
        fixture = {"opening": validity_observation(), "supplies": [supply]}
        record, journal = {}, Mock()
        opening = validity_observation(10)
        opening["_elapsed_s"] = 2
        opening["nearby_entities"] = [{"uid": 9, "pickup": {"id": supply["item"], "amount": 1}}]
        run.measurement_validity(record, fixture, opening, [], {"arrival": True, "success": False}, journal)
        late = validity_observation(400)
        late["_elapsed_s"] = 400
        run.measurement_validity(record, fixture, late, [], {"arrival": False, "success": False}, journal)
        self.assertEqual(record["persistent_arrival"], {
            "ever": True, "first_elapsed_s": 2, "ever_with_confirmed_target_condition": True,
            "ever_with_uncertain_target_condition": False,
            "ever_with_unavailable_target_condition": False, "final": False})
        self.assertTrue(record["target_validity"]["valid"])
        self.assertTrue(record["target_validity"]["uncertain"])

    def test_remote_expiry_estimate_does_not_stop_any_scene_or_confirm_arrival_opportunity(self):
        supply = required_supply()
        fixture = {"opening": validity_observation(), "supplies": [supply]}
        record, journal = {}, Mock()
        current = validity_observation(600)
        current["position"] = [0, 0, 0]
        self.assertEqual(supply["entity"]["position"][0] - current["position"][0], 512)
        validity = run.measurement_validity(
            record, fixture, current, [], {"arrival": False, "success": False}, journal)
        self.assertFalse(record["persistent_arrival"]["ever"])
        record["score"] = {"success": False, "dead": False}
        for definition in ({}, {"timed_interaction_policy": "persistent-arrival"}):
            self.assertFalse(run.measurement_stop(
                record, definition, validity, entry_exited=False, horizon_reached=False))
        current["position"] = list(supply["entity"]["position"])
        current["_elapsed_s"] = 601
        run.measurement_validity(
            record, fixture, current, [], {"arrival": True, "success": False}, journal)
        self.assertTrue(record["persistent_arrival"]["ever_with_uncertain_target_condition"])
        self.assertFalse(record["persistent_arrival"]["ever_with_confirmed_target_condition"])

    def test_navigation_failure_and_horizon_are_not_censored_by_target_qualification(self):
        for validity in ({"valid": True, "uncertain": True}, {"valid": False, "uncertain": False}):
            for dead, expected in ((True, "agent_failure"), (False, "evaluation_horizon_without_success")):
                with self.subTest(validity=validity, dead=dead):
                    record = {"score": {"success": False, "dead": dead},
                              "persistent_arrival": {"ever": False, "final": False}}
                    self.assertTrue(run.measurement_stop(
                        record, {"timed_interaction_policy": "persistent-arrival"}, validity,
                        entry_exited=False, horizon_reached=True))
                    self.assertEqual(record["classification"], expected)
                    self.assertTrue(record["navigation_outcome"].endswith("_without_arrival"))

    def test_confirmed_native_pickup_unavailability_is_not_called_expiry(self):
        record = {"score": {"success": False, "dead": False},
                  "persistent_arrival": {"ever": True}}
        self.assertTrue(run.measurement_stop(
            record, {"timed_interaction_policy": "persistent-arrival"}, {"valid": False},
            entry_exited=False, horizon_reached=False))
        self.assertEqual(record["classification"], "target_unavailable")
        self.assertEqual(record["navigation_outcome"], "arrival_observed")


class ConfigTests(unittest.TestCase):
    def test_normal_entry_real_providers_no_quotas_or_direct_only(self):
        command = run.agent_command(Path("/episode"), "actor", "/python", "/source")
        self.assertEqual(command[1], "/source/tools/run_veloren_agent.py")
        self.assertIn("--no-wall-limit", command)
        self.assertIn("--veloren-root", command)
        self.assertIn("--bridge", command)
        self.assertIn("--assets", command)
        self.assertEqual(command[command.index("--model") + 1], "gpt-6-luna")
        for option in ("--brain-direct-only", "--provider-decisions", "--public-actions",
                       "--sdk-model-requests", "--jev-navigation"):
            self.assertNotIn(option, command)

    def test_output_is_immutable_and_symlink_refused(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(pins, "ROOT", Path(temp)):
            path = Path(temp) / "episode"
            run.reserve_output(path)
            with self.assertRaises(FileExistsError):
                run.reserve_output(path)
            link = Path(temp) / "link"
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                run.reserve_output(link / "new")

    def test_admin_channel_forbidden_normal_tools_allowed(self):
        self.assertFalse(native.allowed({"cmd": "step", "args": {"action": {"type": "server_command"}}}))
        self.assertFalse(native.allowed({"cmd": "step", "args": {"action": {"type": "chat", "message": "/sudo"}}}))
        for command in native.READ_COMMANDS | native.CONTROL_COMMANDS - {"step"}:
            self.assertTrue(native.allowed({"cmd": command, "args": {}}))
        self.assertTrue(native.allowed({"cmd": "step", "args": {"action": {"type": "craft"}}}))

    def test_manifest_detects_extra_or_changed_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "a").write_text("a")
            files = pins.inventory(root)
            pins.admit_tree(root, files)
            (root / "a").write_text("changed")
            with self.assertRaises(ValueError):
                pins.admit_tree(root, files)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_client_preserves_large_native_observation(self):
        payload = json.dumps({"result": "x" * 200000}).encode() + b"\n"
        with tempfile.TemporaryDirectory() as temp:
            endpoint = native.SocketEndpoint(Path(temp))
            self.addCleanup(endpoint.close)
            path = endpoint.path
            async def connected(reader, writer):
                writer.write(payload)
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            server = await asyncio.start_unix_server(connected, path=str(path))
            process = await asyncio.create_subprocess_exec(
                str(pins.PYTHON), str(Path(native.__file__).with_name("bridge_client.py")),
                env={**os.environ, "RPG_EPISODE_SOCKET": str(path)},
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
            server.close()
            await server.wait_closed()
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(stdout, payload)

    async def test_does_not_signal_reused_pid(self):
        process = Mock(pid=123, returncode=None)
        with patch.object(native.lifecycle, "process_start_ticks", return_value=8):
            with self.assertRaises(RuntimeError):
                await native.stop_owned(process, 7, {})
        process.send_signal.assert_not_called()
        process.kill.assert_not_called()

    async def test_owned_stop_receipt(self):
        process = Mock(pid=123, returncode=None)
        async def wait():
            process.returncode = -15
        process.wait = wait
        record = {}
        with patch.object(native.lifecycle, "process_start_ticks", return_value=7):
            await native.stop_owned(process, 7, record)
        self.assertTrue(record["stopped"])

    async def test_observation_does_not_wait_for_action_receipt(self):
        class Bridge:
            def __init__(self):
                self.ready = asyncio.get_running_loop().create_future()
                self.ready.set_result({"event": "ready", "uid": 42, "session_id": "s", "protocol_version": "1.0"})
                self.started = asyncio.Event()
                self.finish = asyncio.Event()
                self.journal = Mock()
            async def envelope(self, request, **kwargs):
                if request["cmd"] == "step":
                    self.started.set()
                    await self.finish.wait()
                return {"id": request["id"], "type": "response", "ok": True, "result": request["cmd"]}
        with tempfile.TemporaryDirectory() as temp:
            bridge = Bridge()
            endpoint = native.SocketEndpoint(Path(temp))
            self.addCleanup(endpoint.close)
            boundary = native.EpisodeBoundary(bridge, endpoint.path)
            await boundary.start()
            reader, writer = await asyncio.open_unix_connection(boundary.path)
            await reader.readline()
            writer.write(b'{"id":"action","cmd":"step","args":{"action":{"type":"move"}}}\n')
            await writer.drain()
            await bridge.started.wait()
            writer.write(b'{"id":"observation","cmd":"observe","args":{}}\n')
            await writer.drain()
            response = json.loads(await asyncio.wait_for(reader.readline(), 1))
            self.assertEqual(response["id"], "observation")
            bridge.finish.set()
            self.assertEqual(json.loads(await reader.readline())["id"], "action")
            writer.close()
            await writer.wait_closed()
            await boundary.close()
            self.assertFalse(boundary.path.exists())


if __name__ == "__main__":
    unittest.main()
