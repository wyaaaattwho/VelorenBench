import copy
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.rpg_benchmark import combat_support as family, native_outcomes as oracle, providers, renderer, renderer_support, run, suite
from .test_rpg_b3_upgrade import approved_b3_loadout


def event(name, data, sequence=1):
    return {"event": name, "data": data, "sequence": sequence}


def receipt(kind="attack_primary", detail=None, **args):
    action = {"type": kind, "action_id": kind, **args}
    return {"actor": 1, "request": {"id": kind, "cmd": "step", "args": {"action": action,
                "control_fence": {"generation": 0, "expected_session_id": "s",
                                  "actor": {"uid": 1, "lifecycle": "a"}}}},
            "response": {"ok": True, "session_id": "s", "result": {
                "status": "completed", "action_type": kind, "action_id": kind, "detail": detail or {}}}}


def positive(case="T06"):
    opening = {"session_id": "s", "self_identity": {"uid": 1}, "control_actor": {"uid": 1, "lifecycle": "a"},
               "survival": {"health": {"current": 100, "maximum": 100}}}
    source = {"uid": 1, "server_entity": "Entity(1, Generation(1))"}
    target = {"uid": 2, "server_entity": "Entity(2, Generation(1))"}
    fixture = {"case": case, "opening": opening, "actor_binding": source, "targets": [{"binding": target}],
               "private_opening_sequence": 0, "native_nonce": "native-1", "variant": "sword",
               "fixture_version": "test"}
    applied = {"changed": True, "before": 10, "after": 0, "requested_amount": -10, "instance": 7,
               "change_time": 4, "contributor": {"Solo": 1}, "contributor_entity": source["server_entity"],
               "target_uid": 2, "target_entity": target["server_entity"], "cause": {"Attack": "Projectile"}}
    ability = {"tool": "Sword", "input": {"Ability": 0}, "ability": {"ability": {"MainWeaponAux": 3}}}
    queued = {"source_uid": 1, "source_entity": source["server_entity"], "target_uid": 2,
              "target_entity": target["server_entity"], "instance": 7, "requested_amount": -10,
              "server_time": 4, "ability": ability}
    journal = [event("opened", {"nonce": "native-1"}, 0),
               event("attack_health_effect_queued", queued, 1),
               event("health_effect_applied", applied, 2),
               event("native_entity_death", {"uid": 2, "entity": target["server_entity"],
                                            "cause": {"by": {"Solo": 99}}}, 3)]
    final = copy.deepcopy(opening)
    receipts = [receipt()]
    if case == "T08":
        other = {"uid": 3, "server_entity": "Entity(3, Generation(1))"}
        fixture["targets"].append({"binding": other})
        for name, value in (("health_effect_applied", {**applied, "target_uid": 3,
                                                      "target_entity": other["server_entity"]}),
                            ("native_entity_death", {"uid": 3, "entity": other["server_entity"]})):
            journal.append(event(name, value, len(journal)))
    if case == "T07":
        skill = {"Sword": "Cascade"}
        receipts.extend([
            receipt("unlock_skill", {"skill": skill, "skill_unlock_confirmed": True, "effect": "observed",
                                    "before_level": 0, "after_level": 1, "before_points": 4,
                                    "after_points": 3, "expected_cost": 1}, skill=skill),
            receipt("configure_auxiliary_ability", {"ability_configuration_confirmed": True,
                    "effect": "observed", "ability": {"MainWeapon": 3}, "observed": {"MainWeapon": 3}},
                    slot=0, ability={"MainWeapon": 3})])
        control = {"owner": "model", "generation": 1, "sequence": 1,
                   "actor": opening["control_actor"], "expected_session_id": "s", "auxiliary": 0}
        receipts.append({"actor": 1, "request": {"cmd": "set_control_state", "id": "aux", "args": control},
                         "response": {"ok": True, "session_id": "s", "result": {
                             "status": "applied", "session_id": "s",
                             **{key: control[key] for key in ("owner", "generation", "sequence", "actor")}}}})
        final["skill_tree"] = {"groups": [{"group": {"Weapon": "Sword"}, "skills": [
            {"skill": skill, "learned_level": 1}]}]}
    if case == "T09":
        ally = {"uid": 3, "server_entity": "Entity(3, Generation(1))"}
        fixture["ally"] = {"binding": ally, "threshold_hp": 61}
        heal = {**applied, "target_uid": 3, "target_entity": ally["server_entity"],
                "before": 60, "after": 61, "requested_amount": 1,
                "contributor": {"Group": {"entity_uid": 1, "group": 5}},
                "contributor_group": 5, "target_group": 5, "cause": {"Buff": "Regeneration"}}
        journal.extend([event("health_effect_applied", heal, 4),
                        event("combat_entity_bindings", {"bindings": [
                            {**ally, "health": 61, "dead": False}]}, 5)])
        receipts.append(receipt("attack_secondary"))
    return fixture, final, receipts, journal


def score(values):
    fixture, final, receipts, journal = values
    return family.score(fixture, final, receipts, [], [], journal=journal)


def test_preserves_all_twenty_definitions_and_legacy_semantic_files():
    base = json.loads(Path(__file__).with_name("fixtures").joinpath("crafting-recovery-scenes.json").read_text())
    current = json.loads(Path(suite.__file__).with_name("scenes.json").read_text())
    expected = {row["id"]: row for row in base["scenes"]}
    actual = {row["id"]: row for row in current["scenes"]}
    assert len(expected) == 20 and len(actual) == 29
    assert set(actual) - set(expected) == family.TASK_IDS | {"T01", "T02", "T10", "T11", "T12"}
    assert all(actual[key] == approved_b3_loadout(value) for key, value in expected.items())
    assert actual["T06"]["variants"] == ["sword", "bow", "staff", "sceptre"]


def test_t08_v2_discloses_optional_finite_stock_staff_without_changing_enemies():
    task = suite.load(Path(suite.__file__).with_name("scenes.json"))["T08"]
    assert task["fixture_version"] == "t08-melee-ranged-cover-v2"
    assert task["default_variant"] == "sword"
    assert task["initial_items"] == {"common.items.consumable.potion_minor": 2,
                                     "common.items.weapons.staff.starter_staff": 1}
    assert "equipping or using it is optional" in task["task"]
    assert [enemy["asset"] for enemy in task["enemies"]] == [
        "common.entity.benchmark.male_green_legoom", "common.entity.dungeon.haniwa.archer"]
    assert task["enemies"][1]["initial_hp"] == 25


@pytest.mark.parametrize("case", sorted(family.TASK_IDS))
def test_all_goals_and_wrong_actor_unknown_receipt_no_effect_stale_generation(case):
    values = positive(case)
    assert score(values)["success"]
    for mutate in ("actor", "session", "unknown", "missing_receipt", "noeffect",
                   "wrongtarget", "stale", "missing_death", "dead", "nonce"):
        fixture, final, receipts, journal = copy.deepcopy(values)
        if mutate == "actor":
            for row in receipts:
                row["actor"] = 99
        elif mutate == "session":
            final["session_id"] = "other"
        elif mutate == "unknown":
            for row in receipts:
                row["response"]["result"]["status"] = "unknown"
        elif mutate == "missing_receipt":
            receipts.clear()
        elif mutate == "noeffect":
            for row in journal:
                if row["event"] == "health_effect_applied":
                    row["data"]["changed"] = False
        elif mutate == "wrongtarget":
            for row in journal:
                if row["event"] == "health_effect_applied":
                    row["data"]["target_uid"] = 999
        elif mutate == "stale":
            fixture["actor_binding"]["server_entity"] = "Entity(1, Generation(2))"
        elif mutate == "missing_death":
            journal[:] = [row for row in journal if row["event"] != "native_entity_death"]
        elif mutate == "dead":
            final["survival"]["health"]["current"] = 0
        else:
            with pytest.raises(ValueError, match="process identity"):
                family.score({**fixture, "native_nonce": "other"}, final, receipts, [], [], journal=journal)
            continue
        assert not score((fixture, final, receipts, journal))["success"], (case, mutate)


def test_non_final_delayed_and_projectile_damage_not_restricted_to_attack_last_blow():
    values = positive()
    values[3][2]["data"]["cause"] = {"Buff": "Bleeding"}
    assert score(values)["success"]
    assert score(values)["last_blow_required"] is False


@pytest.mark.parametrize("impact_tool", [None, "Bow", "Staff"])
def test_configured_effect_uses_original_queue_when_caster_changes_before_application(impact_tool):
    values = positive("T07")
    queued, applied = values[3][1], values[3][2]
    applied["data"]["cause"] = {"Attack": "Melee"}
    applied["data"]["server_time"] = 8
    applied["data"]["contributor_ability_at_effect"] = (
        None if impact_tool is None else
        {"tool": impact_tool, "input": "Primary", "ability": {"ability": "ToolPrimary"}})
    result = score(values)
    assert result["success"]
    assert result["targets"][0]["original_attack_joins"][0]["status"] == "causally_attributed"
    assert result["build_or_support"]["uses"][0]["original_attack"] is queued
    assert result["build_or_support"]["uses"][0]["effect"] is applied


def test_delayed_bow_projectile_keeps_original_weapon_after_caster_switches_to_sword():
    values = positive()
    values[0]["variant"] = "bow"
    queued, applied = values[3][1], values[3][2]
    applied["data"]["contributor_ability_at_effect"] = copy.deepcopy(queued["data"]["ability"])
    queued["data"]["ability"] = {
        "tool": "Bow", "input": "Primary", "ability": {"ability": "ToolPrimary"}}
    applied["data"]["server_time"] = 8
    result = score(values)
    assert result["success"]
    joined = result["targets"][0]["original_attack_joins"][0]
    assert joined["status"] == "causally_attributed"
    assert joined["queue_event"]["data"]["ability"]["tool"] == "Bow"
    assert result["last_blow_required"] is False


def test_current_cascade_cannot_relabel_a_different_originating_ability():
    values = positive("T07")
    queued, applied = values[3][1], values[3][2]
    applied["data"]["contributor_ability_at_effect"] = copy.deepcopy(queued["data"]["ability"])
    queued["data"]["ability"] = {
        "tool": "Bow", "input": "Primary", "ability": {"ability": "ToolPrimary"}}
    result = score(values)
    assert not result["success"] and not result["parts"]["configured_learned_effect"]
    assert result["targets"][0]["original_attack_joins"][0]["status"] == "causally_attributed"
    assert not result["measurement_unknown_reasons"]


@pytest.mark.parametrize("origin,status", [
    ("missing", "unknown"), ("multiple", "ambiguous"),
    ("none", "ability_unknown"), ("known", "causally_attributed")])
def test_original_queue_status_contract_never_falls_back_to_current_caster(origin, status):
    values = positive("T07")
    journal = values[3]
    applied = journal[2]
    applied["data"]["contributor_ability_at_effect"] = copy.deepcopy(journal[1]["data"]["ability"])
    if origin == "missing":
        journal.pop(1)
    elif origin == "multiple":
        journal.insert(2, copy.deepcopy(journal[1]))
    elif origin == "none":
        journal[1]["data"]["ability"] = None
    for sequence, row in enumerate(journal):
        row["sequence"] = sequence
    assert oracle.attack_join(journal, applied)["status"] == status
    result = score(values)
    assert result["parts"]["configured_learned_effect"] is (origin == "known")
    assert bool(result["measurement_unknown_reasons"]) is (origin != "known")


@pytest.mark.parametrize("case", ["T06", "T08", "T09"])
def test_secondary_effects_without_original_queue_keep_only_native_contribution_evidence(case):
    values = positive(case)
    journal = values[3]
    journal[:] = [row for row in journal if row["event"] != "attack_health_effect_queued"]
    for sequence, row in enumerate(journal):
        row["sequence"] = sequence
        if row["event"] == "health_effect_applied":
            row["data"]["contributor_ability_at_effect"] = None
            if row["data"]["requested_amount"] < 0:
                row["data"]["cause"] = {"Buff": "Burning"}
            assert oracle.attack_join(journal, row)["status"] == "unknown"
    result = score(values)
    assert result["success"] and not result["measurement_unknown_reasons"]
    assert all(row["native_death"] for row in result["targets"])
    if case == "T09":
        assert result["parts"]["actual_group_healing"]
    else:
        assert result["parts"]["declared_player_contribution"]


def test_group_healing_uses_individual_application_not_net_health_gain():
    values = positive("T09")
    journal = values[3]
    heal = journal[4]["data"]
    heal.update(after=62, requested_amount=2, contributor_ability_at_effect=None)
    damage = {**heal, "before": 62, "after": 61, "requested_amount": -1,
              "contributor": {"Solo": 99}, "contributor_group": None,
              "contributor_entity": "Entity(99, Generation(1))", "cause": {"Attack": "Melee"}}
    journal.insert(5, event("health_effect_applied", damage))
    for sequence, row in enumerate(journal):
        row["sequence"] = sequence
    result = score(values)
    assert result["success"]
    assert result["build_or_support"]["healing_applications"] == [journal[4]]
    assert heal["after"] - heal["before"] == 2
    assert result["build_or_support"]["ally_state"]["health"] - heal["before"] == 1
    assert oracle.attack_join(journal, journal[4])["status"] == "unknown"


def test_protection_cancellation_after_actual_native_damage_is_still_contribution():
    values = positive()
    values[2][0]["response"]["result"]["status"] = "cancelled"
    values[2][0]["response"]["result"]["detail"] = {
        "code": "action_cancelled", "reason": "native_protection_interrupted"}
    assert score(values)["success"]
    values[3][2]["data"]["changed"] = False
    assert not score(values)["success"]


@pytest.mark.parametrize("change", ["queue_missing", "ambiguous", "change_time", "instance",
                                  "source_entity", "target_entity", "amount", "ability_unknown"])
def test_configured_ability_requires_original_native_attack_causality(change):
    values = positive("T07")
    journal = values[3]
    queued = journal[1]["data"]
    if change == "queue_missing":
        journal.pop(1)
    elif change == "ambiguous":
        journal.insert(2, copy.deepcopy(journal[1]))
    elif change == "ability_unknown":
        queued["ability"] = None
    else:
        key = {"change_time": "server_time", "amount": "requested_amount"}.get(change, change)
        queued[key] = 999
    assert not score(values)["success"]


@pytest.mark.parametrize("index", [1, 2])
def test_t07_requires_real_learning_cost_and_configuration(index):
    values = positive("T07")
    values[2][index]["response"]["result"]["detail"] = {"submitted_to_server": True}
    assert not score(values)["success"]


@pytest.mark.parametrize("state", ["knowledge_reset", "slot_empty", "slot_different"])
def test_prior_world_or_submitted_configuration_does_not_replace_actual_readback(state):
    values = positive("T07")
    if state == "knowledge_reset":
        values[1]["skill_tree"]["groups"][0]["skills"][0]["learned_level"] = 0
    else:
        values[2][2]["response"]["result"]["detail"]["observed"] = (
            None if state == "slot_empty" else {"MainWeapon": 4})
    result = score(values)
    assert not result["success"] and not result["parts"]["configured_learned_effect"]
    if state != "knowledge_reset":
        assert not result["parts"]["configured"]


def test_primary_native_ability_enum_is_not_an_auxiliary_build_effect():
    values = positive("T07")
    values[3][1]["data"]["ability"]["ability"]["ability"] = "ToolPrimary"
    assert not score(values)["success"]
    values = positive("T07")
    values[2][1]["response"]["result"]["detail"]["after_points"] = 4
    assert not score(values)["success"]


@pytest.mark.parametrize("field,value", [
    ("target_group", 6), ("contributor_group", None), ("contributor", {"Solo": 1}),
    ("cause", {"Buff": "Potion"}), ("target_uid", 1)])
def test_t09_requires_individual_other_group_healing_not_potion_or_net_hp(field, value):
    values = positive("T09")
    values[3][4]["data"][field] = value
    assert not score(values)["success"]


def test_t09_threshold_survival_and_setup_cutoff_are_separate():
    for condition in ("threshold", "dead", "setup"):
        values = positive("T09")
        if condition == "setup":
            values[0]["private_opening_sequence"] = 4
        else:
            state = values[3][-1]["data"]["bindings"][0]
            state["health" if condition == "threshold" else "dead"] = 60 if condition == "threshold" else True
        assert not score(values)["success"]


def test_private_reader_rejects_cross_run_version_sequence_and_partial_final(tmp_path):
    path = tmp_path / "private.ndjson"
    row = {"event": "opened", "data": {"run_id": "r"}, "schema": "rpg-native-oracle",
           "version": "1.1", "private": True, "sequence": 0}
    path.write_text(json.dumps(row) + "\n")
    assert len(oracle.read(path, "r", complete=True)) == 1
    for field, value in (("version", "1.0"), ("private", False), ("sequence", 1)):
        path.write_text(json.dumps({**row, field: value}) + "\n")
        with pytest.raises(ValueError):
            oracle.read(path, "r")
    path.write_text(json.dumps(row) + "\n{")
    assert len(oracle.read(path, "r")) == 1
    with pytest.raises(ValueError):
        oracle.read(path, "r", complete=True)
    with pytest.raises(ValueError):
        oracle.read(path, "wrong")


def test_private_reader_accepts_native_rows_without_repeated_pid_or_nonce(tmp_path):
    path = tmp_path / "private.ndjson"
    metadata = {"schema": "rpg-native-oracle", "version": "1.1", "private": True}
    rows = [
        {**metadata, "event": "opened", "data": {"run_id": "r", "pid": 7, "nonce": "n"}, "sequence": 0},
        {**metadata, "event": "combat_entity_bindings", "data": {"bindings": []}, "sequence": 1},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert oracle.read(path, "r", complete=True) == rows
    assert "pid" not in rows[1]["data"] and "nonce" not in rows[1]["data"]


def test_private_reader_rejects_second_open_even_with_same_header_identity(tmp_path):
    path = tmp_path / "private.ndjson"
    opened = {"schema": "rpg-native-oracle", "version": "1.1", "private": True,
              "event": "opened", "data": {"run_id": "r", "pid": 7, "nonce": "n"}, "sequence": 0}
    path.write_text(json.dumps(opened) + "\n" + json.dumps({**opened, "sequence": 1}) + "\n")
    with pytest.raises(ValueError, match="multiple native processes"):
        oracle.read(path, "r", complete=True)


def test_cover_requires_real_native_contact_at_authored_geometry_with_exact_owner():
    row = event("projectile_solid_contact", {"owner_uid": 1, "position": {"x": 5, "y": 0, "z": 2}}, 4)
    cover = [{"voxel": [5, 0, 2]}]
    assert family.cover_contact(row, cover, 1, 3)
    assert not family.cover_contact(row, cover, 2, 3)
    assert not family.cover_contact(row, cover, 1, 4)
    assert not family.cover_contact(row, [{"voxel": [15, 0, 2]}], 1, 3)
    row["data"]["position"]["x"] = 4.8
    assert not family.cover_contact(row, cover, 1, 3)


def test_unknown_configured_effect_is_not_a_clean_negative_and_stale_control_is_rejected():
    values = positive("T07")
    values[3].pop(1)
    result = score(values)
    assert not result["success"] and result["measurement_unknown_reasons"]
    for field in ("actor", "owner", "generation", "sequence"):
        values = positive("T07")
        values[2][-1]["response"]["result"][field] = "stale"
        assert not score(values)["success"]


@pytest.mark.parametrize("case", sorted(family.TASK_IDS))
@pytest.mark.parametrize("backend,mode", [("headless", "structured"), ("renderer", "image")])
def test_unsupported_modes_fail_before_allocating_any_episode(case, backend, mode, monkeypatch, tmp_path, tmp_path_factory):
    from .test_admission_closeout import selected_native
    selected = selected_native(tmp_path_factory.mktemp("TEST-ONLY-selected"))
    monkeypatch.setattr(run.pins, "load", lambda *args, **kwargs: selected)
    args = SimpleNamespace(scene=case, backend=backend, observation_mode=mode,
                           pins=tmp_path / "input.json", pins_sha256="0" * 64,
                           mode="task-engineering", harness="responses", terrain="natural",
                           out=tmp_path / "unallocated", recordings=None,
                           action_profile="raw-native-v2" if backend == "headless" else "visual-motor-v1",
                           adapter_config=json.dumps({"mock_provider": "http://127.0.0.1:1234", "provider": providers.LUNA}))
    with pytest.raises(ValueError, match="unqualified|unsupported"):
        asyncio.run(run.episode(args))
    assert not args.out.exists()


def test_ambiguous_effect_closes_as_unknown_not_a_clean_agent_failure():
    record = {"score": {"success": False, "dead": False, "parts": {"encounter_resolved": True},
                        "measurement_unknown_reasons": ["configured_ability_effect_provenance_unknown"]}}
    assert run.measurement_stop(record, {}, {"valid": True}, entry_exited=False, horizon_reached=False)
    assert record["classification"] == "native_effect_unknown"


def test_downing_is_not_death_and_recovery_is_not_a_hidden_failure():
    values = positive()
    downed = copy.deepcopy(values[1])
    downed["survival"]["health"].update(current=0, dead=False, downed=True)
    observed = family.score(values[0], downed, values[2], [], [], journal=values[3])
    assert not observed["dead"] and not observed["parts"]["player_alive"]
    recovered = family.score(values[0], values[1], values[2], [], [downed], journal=values[3])
    assert recovered["success"]
    values[3].append(event("native_entity_death", {
        "uid": 1, "entity": values[0]["actor_binding"]["server_entity"]}, 4))
    assert score(values)["dead"]


@pytest.mark.parametrize("code", [0, 1])
def test_display_probe_can_exit_before_fingerprint_without_being_lost(code, monkeypatch):
    class Probe:
        pid, returncode = 42, code

        async def wait(self):
            return self.returncode

    async def create(*args, **kwargs):
        return Probe()

    def missing(pid):
        raise FileNotFoundError(f"/proc/{pid}/stat")

    entries = []
    journal = SimpleNamespace(write=lambda **row: entries.append(row))
    monkeypatch.setattr(renderer.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(renderer.lifecycle, "process_start_ticks", missing)
    if code:
        with pytest.raises(RuntimeError, match="not responsive"):
            asyncio.run(renderer.check_display(":123", journal))
    else:
        asyncio.run(renderer.check_display(":123", journal))
    assert entries[-1]["cleanup"]["exit_code"] == code
    assert entries[-1]["cleanup"]["stopped"] is True


def test_renderer_partial_startup_keeps_display_cleanup_evidence():
    display = {"pid": 42, "start_ticks": 7, "stopped": True}
    result = run.bridge_cleanup(SimpleNamespace(process=None, cleanup={"display": display}))
    assert result["started"] is False and result["stopped"] is True
    assert result["display"] == display


@pytest.mark.parametrize("mode", ["structured", "hybrid"])
def test_t08_native_qualified_modes_are_admitted_without_allocating(mode, tmp_path, tmp_path_factory, monkeypatch):
    from .test_admission_closeout import selected_native, record_for, install_records
    args = SimpleNamespace(scene="T08", observation_mode=mode, mode="capability",
                           harness="responses", terrain="natural", out=tmp_path / "unallocated",
                           recordings=tmp_path / "outputs/recordings/test")
    assert renderer_support.historical_policy()["renderer"]["T08/natural"][mode]["scored_evaluation"]
    assert not renderer_support.support_matrix()["renderer"]["T08/natural"][mode]["scored_evaluation"]
    selected = selected_native(tmp_path_factory.mktemp("TEST-ONLY-selected"))
    with pytest.raises(ValueError, match="unqualified"):
        renderer_support.admit_selection(args, selected)
    definition = suite.load(Path(selected["dependencies"]["scene_manifest"]["path"]))["T08"]
    install_records(tmp_path_factory.mktemp("TEST-ONLY-qualified"), monkeypatch, [
        record_for(definition, selected, terrain="natural", mode=mode)])
    assert renderer_support.admit_selection(args, selected)["scored_evaluation"]
    assert not args.out.exists() and not args.recordings.exists()


@pytest.mark.parametrize("backend,mode", [("headless", "structured"), ("headless", "hybrid"), ("renderer", "image")])
def test_t08_unqualified_backends_and_image_still_fail_before_allocation(backend, mode, monkeypatch, tmp_path, tmp_path_factory):
    from .test_admission_closeout import selected_native
    selected = selected_native(tmp_path_factory.mktemp("TEST-ONLY-selected"))
    monkeypatch.setattr(run.pins, "load", lambda *args, **kwargs: selected)
    args = SimpleNamespace(scene="T08", backend=backend, observation_mode=mode,
                           pins=tmp_path / "input.json", pins_sha256="0" * 64,
                           mode="capability", harness="responses", terrain="natural",
                           out=tmp_path / "unallocated", recordings=None,
                           action_profile="raw-native-v2" if backend == "headless" else "visual-motor-v1",
                           adapter_config=json.dumps({"mock_provider": "http://127.0.0.1:1234", "provider": providers.LUNA}))
    with pytest.raises(ValueError, match="unqualified|unsupported renderer"):
        asyncio.run(run.episode(args))
    assert not args.out.exists()


@pytest.mark.parametrize("condition", [None, "combo", "group", "health", "generation", "public_group"])
def test_support_opening_requires_exact_native_resources_and_actual_initial_group_injury(condition):
    fixture, _, _, journal = positive("T09")
    fixture["actor_binding"]["group"] = 5
    ally = fixture["ally"]
    ally["initial_hp"] = 60
    ally["public"] = {"control_target": {"uid": 3, "lifecycle": "ally(1)"}}
    fixture["opening"]["nearby_entities"] = [
        {"uid": 3, **ally["public"], "same_native_group": True, "health": {"current": 60}}]
    state = {**ally["binding"], "group": 5, "health": 60, "dead": False}
    combo = {"uid": 1, "entity": fixture["actor_binding"]["server_entity"], "after": 4}
    journal.extend([event("combat_entity_bindings", {"bindings": [state]}, 6),
                    event("combo_effect_applied", combo, 7)])
    if condition == "combo":
        combo["after"] = 8
    elif condition == "group":
        state["group"] = 6
    elif condition == "health":
        state["health"] = 59
    elif condition == "generation":
        state["server_entity"] += "-stale"
    elif condition == "public_group":
        fixture["opening"]["nearby_entities"][0]["same_native_group"] = False
    if condition is None:
        family.validate_support_opening(fixture, journal)
    else:
        with pytest.raises(RuntimeError, match="support opening"):
            family.validate_support_opening(fixture, journal)
