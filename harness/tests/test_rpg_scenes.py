import asyncio
import copy
import json
import math
import tempfile
import unittest
from pathlib import Path

from scripts.rpg_benchmark import combat, crafting, evidence, navigation, oracles, pins, run, scene, setup, suite
from scripts import rpg_sdk_agent as sdk
from unittest.mock import patch
from scripts.rpg_benchmark.native import NativeConfig
from tests.test_rpg_benchmark import observation


def craft_receipt(recipe, output, delta, identifier="a"):
    return {
        "actor": 42,
        "request": {"cmd": "step", "args": {"action": {
            "type": "craft", "recipe": recipe, "action_id": identifier}}},
        "response": {"ok": True, "session_id": "s", "result": {
            "status": "completed", "action_id": identifier, "action_type": "craft",
            "detail": {"craft_update_observed": True, "output_definition_id": output,
                       "output_definition_delta": delta.get(output, 0), "inventory_delta": delta}}},
    }


class AuthoredVoxelTests(unittest.IsolatedAsyncioTestCase):
    def pose(self, z, *, grounded=True, speed=0):
        return {"position": [18709.5, 10801.5, z], "velocity": [0, 0, speed],
                "on_ground": grounded, "tick": 10, "session_id": "admin",
                "self_identity": {"uid": 1}}

    def terrain(self, z):
        return {"status": "complete", "support_candidates": [{
            "occupiable": True, "standing_position": [18709.5, 10801.5, z + 1],
            "support": {"position": [18709, 10801, z], "solid": True, "kind": "Rock"}}]}

    async def test_floor_authoring_rejects_original_falling_pose_and_settles_before_next_voxel(self):
        from unittest.mock import AsyncMock, Mock
        point = [18709, 10801, 410]
        checks = []
        async def wait(actor, predicate, reason, timeout):
            checks.append(reason)
            if len(checks) == 1:
                self.assertFalse(predicate(self.pose(409.8809814453125, grounded=False, speed=-11.6)))
                self.assertFalse(predicate(self.pose(410.01, grounded=False)))
                value = self.pose(410.9, grounded=False, speed=-1)
            else:
                self.assertFalse(predicate(self.pose(410)))
                self.assertFalse(predicate(self.pose(411, grounded=False)))
                self.assertFalse(predicate(self.pose(411, speed=-1)))
                value = self.pose(411)
            self.assertTrue(predicate(value))
            return value
        admin = Mock()
        with patch.object(setup, "goto", new=AsyncMock()) as goto, patch.object(
                setup, "wait_observation", side_effect=wait), patch.object(
                setup, "action", new=AsyncMock(return_value={"action_id": "block"})) as action, patch.object(
                setup, "terrain", new=AsyncMock(return_value=self.terrain(410))):
            result = await setup.rock(admin, point)
        self.assertEqual(action.await_args_list[0].kwargs["args"], ["18709.5", "10801.5", "410.95"])
        self.assertEqual(action.await_args_list[1].kwargs["basis"]["position"][2], 410.9)
        self.assertEqual(action.await_args_list[1].kwargs["name"], "make_block")
        goto.assert_awaited_once_with(admin, admin, [18709.5, 10801.5, 411], settle=False)
        self.assertEqual(result["settled_on_top"]["position"][2], 411)

    async def test_upper_voxel_uses_grounded_lower_support_and_exact_new_top(self):
        from unittest.mock import AsyncMock, Mock
        observations = []
        async def wait(actor, predicate, reason, timeout):
            value = self.pose(411 if not observations else 412)
            self.assertFalse(predicate(self.pose(value["position"][2], grounded=False)))
            self.assertTrue(predicate(value))
            observations.append(value)
            return value
        with patch.object(setup, "goto", new=AsyncMock()) as goto, patch.object(
                setup, "wait_observation", side_effect=wait), patch.object(
                setup, "action", new=AsyncMock(return_value={"action_id": "block"})) as action, patch.object(
                setup, "terrain", new=AsyncMock(return_value=self.terrain(411))):
            await setup.rock(Mock(), [18709, 10801, 411], base_voxel=False)
        self.assertEqual(action.await_args_list[0].kwargs["args"], ["18709.5", "10801.5", "411"])
        self.assertEqual(goto.await_args_list[0].args[2], [18709.5, 10801.5, 412.5])

    async def test_wrong_native_support_cannot_admit_authored_voxel(self):
        from unittest.mock import AsyncMock, Mock
        wrong = self.terrain(409)
        with patch.object(setup, "goto", new=AsyncMock()), patch.object(
                setup, "wait_observation", new=AsyncMock(side_effect=[self.pose(410.9), self.pose(411)])), patch.object(
                setup, "action", new=AsyncMock(return_value={"action_id": "block"})), patch.object(
                setup, "terrain", new=AsyncMock(return_value=wrong)):
            with self.assertRaisesRegex(RuntimeError, "exact native support"):
                await setup.rock(Mock(), [18709, 10801, 410])

    def test_n1_design_and_full_native_voxel_gate_are_not_weakened(self):
        config = navigation.layouts.scene("wall_side_detour")
        required = [navigation.xyz(point) for point in config["solid_voxels_y_up"]]
        self.assertEqual(len(required), 249)
        self.assertEqual(navigation.xyz(config["start_y_up"]), [18701.5, 10806.5, 411])
        self.assertEqual(navigation.xyz(config["goal_y_up"]), [18714.5, 10806.5, 411])
        with self.assertRaisesRegex(RuntimeError, "solid voxel readback incomplete"):
            navigation.verify_voxels([], required)

    async def test_checked_native_basis_reuses_exact_fence_without_extra_observation(self):
        from unittest.mock import AsyncMock, Mock
        actor = Mock(request=AsyncMock(return_value={"status": "completed", "action_id": "a"}))
        basis = {"continuous_control": {"generation": 7}, "session_id": "admin",
                 "control_actor": {"uid": 1}}
        with patch.object(setup, "uuid4", return_value=Mock(hex="a")):
            await setup.action(actor, "server_command", basis=basis,
                               name="make_block", args=["Rock"])
        actor.request.assert_awaited_once_with("step", {
            "control_fence": {"generation": 7, "expected_session_id": "admin", "actor": {"uid": 1}},
            "action": {"type": "server_command", "action_id": "a",
                       "name": "make_block", "args": ["Rock"]}}, origin="setup")


class CraftInitializationTests(unittest.IsolatedAsyncioTestCase):
    def clean_actor(self):
        row = observation()
        row.update(position=[18704, 10800, 382], nearby_entities=[], tick=10)
        row["survival"].update(buffs=[], buff_clock={"now": 3})
        return row

    def test_initial_state_does_not_heal_or_accept_burning(self):
        row = self.clean_actor()
        setup.require_uninjured_start(row)
        row["survival"]["health"]["current"] = 99
        with self.assertRaisesRegex(RuntimeError, "100 HP"):
            setup.require_uninjured_start(row)
        self.assertEqual(row["survival"]["health"]["current"], 99)
        row["survival"]["health"]["current"] = 100
        row["survival"]["buffs"] = [{"kind": "Burning", "active": True}]
        with self.assertRaisesRegex(RuntimeError, "residual buffs"):
            setup.require_uninjured_start(row)

    async def test_initial_spawn_isolation_records_native_setup_without_player_protection(self):
        from unittest.mock import AsyncMock, Mock
        row = self.clean_actor()
        actor = Mock(request=AsyncMock(return_value=row))
        admin = Mock(request=AsyncMock(return_value=row))
        with patch.object(setup, "command", new=AsyncMock(return_value={"action_id": "isolate"})) as command:
            result = await setup.isolate_initial_spawn(admin, actor)
        command.assert_awaited_once_with(admin, "kill_npcs", 96)
        self.assertFalse(result["player_health_reset"])
        self.assertFalse(result["player_buff_added"])
        self.assertEqual(result["expected_health"], 100)

    async def test_initial_isolation_rejects_remote_admin_or_injured_actor_without_retry(self):
        from unittest.mock import AsyncMock, Mock
        row = self.clean_actor()
        author = copy.deepcopy(row)
        author["position"][0] += 100
        actor = Mock(request=AsyncMock(return_value=row))
        admin = Mock(request=AsyncMock(return_value=author))
        with patch.object(setup, "command", new=AsyncMock()) as command:
            with self.assertRaisesRegex(RuntimeError, "not near"):
                await setup.isolate_initial_spawn(admin, actor)
            command.assert_not_awaited()
            row["survival"]["health"]["current"] = 99
            with self.assertRaisesRegex(RuntimeError, "100 HP"):
                await setup.isolate_initial_spawn(admin, actor)
            command.assert_not_awaited()

    async def test_c2_qualifies_before_learning_and_preserves_task_supplies_and_distractors(self):
        from unittest.mock import AsyncMock, Mock
        order = []
        row = self.clean_actor()
        admin = Mock()
        actor = Mock(request=AsyncMock(side_effect=lambda cmd, *args: (
            {"output": scene.OUTPUT} if cmd == "goal_recipes" else row)))
        async def isolate(*args):
            order.append("isolate")
            return {"expected_health": 100}
        async def clean(*args):
            order.append("cleanup")
            return {}
        async def site(*args):
            order.append("site")
            return {"start": [18959.5, 10800.5, 352]}
        async def learn(*args):
            self.assertEqual(order, ["isolate", "cleanup", "site"])
            order.append("learn")
            return {"status": "completed"}
        async def drop(admin, point, item, amount, terrain, *, required):
            return {"item": item, "amount": amount, "required": required}
        definition = {"id": "C2", "placements": {}, "task": "unchanged macro goal"}
        with patch.object(setup, "isolate_initial_spawn", side_effect=isolate), patch.object(
                setup, "clean_inventory", side_effect=clean), patch.object(
                setup, "site", side_effect=site), patch.object(
                setup, "learn", side_effect=learn), patch.object(
                setup, "gift", new=AsyncMock()) as gift, patch.object(
                setup, "drop_supply", side_effect=drop), patch.object(setup, "goto", new=AsyncMock()):
            fixture = await crafting.setup(admin, actor, definition, "natural")
        self.assertEqual(fixture["task"], definition["task"])
        self.assertEqual([(entry.args[2], entry.args[3]) for entry in gift.await_args_list],
                         [("common.items.crafting_ing.twigs", 1), ("common.items.food.mushroom", 1)])
        self.assertEqual([(supply["item"], supply["required"]) for supply in fixture["supplies"]], [
            ("common.items.food.mushroom", True), ("common.items.food.apple", False),
            ("common.items.crafting_ing.hide.tough_hide", False)])

    async def test_c2_shared_opening_gate_rejects_residual_burning(self):
        from unittest.mock import AsyncMock, Mock
        row = self.clean_actor()
        row["survival"]["buffs"] = [{"kind": "Burning", "active": True}]
        actor = Mock(request=AsyncMock(return_value=row))
        fixture = {"opening": row, "initialization": {"expected_health": 100}, "supplies": []}
        with self.assertRaisesRegex(RuntimeError, "residual buffs"):
            await setup.validate_opening(Mock(), actor, fixture)

    async def test_c2_rejects_relocation_injury_before_recipe_group_use(self):
        from unittest.mock import AsyncMock, Mock
        row = self.clean_actor()
        row["survival"]["buffs"] = [{"kind": "Burning", "active": True}]
        actor = Mock(request=AsyncMock(return_value=row))
        with patch.object(setup, "isolate_initial_spawn", new=AsyncMock()), patch.object(
                setup, "clean_inventory", new=AsyncMock()), patch.object(
                setup, "site", new=AsyncMock()), patch.object(setup, "learn", new=AsyncMock()) as learn:
            with self.assertRaisesRegex(RuntimeError, "residual buffs"):
                await crafting.setup(Mock(), actor, {"id": "C2", "placements": {}}, "natural")
            learn.assert_not_awaited()

    async def test_c1_retained_natural_setup_never_uses_new_initialization(self):
        from unittest.mock import AsyncMock, Mock
        row = self.clean_actor()
        with patch.object(scene, "setup", new=AsyncMock(return_value={"opening": row})) as original, patch.object(
                setup, "isolate_initial_spawn", new=AsyncMock()) as isolate:
            admin, actor = Mock(), Mock()
            fixture = await crafting.setup(admin, actor, {"id": "C1"}, "natural")
        original.assert_awaited_once_with(admin, actor)
        isolate.assert_not_awaited()
        self.assertEqual(fixture["task"], scene.TASK)


class SourcePinsTests(unittest.TestCase):
    def make_source(self, root, name, *, declared=True):
        source = root / name / "source/GameAgent"
        entry = source / "tools/run_veloren_agent.py"
        entry.parent.mkdir(parents=True)
        entry.write_text(f"# {name}\n")
        manifest = root / name / "manifest.json"
        data = {"files": pins.inventory(source)}
        if declared:
            data["source"] = str(source)
        manifest.write_text(json.dumps(data))
        return source, manifest

    def test_arbitrary_baseline_and_disjoint_candidate_load_with_independent_digests(self):
        from .test_portability import fixture_config, save_fixture, load_fixture
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, content = fixture_config(root)
            baseline, first = self.make_source(root, "baseline")
            candidate, second = self.make_source(root, "disjoint-candidate")
            sources = {"baseline": pins.source_profile(first),
                       "candidate": pins.source_profile(second)}
            config["sources"] = {name: data["root"] for name, data in sources.items()}
            content["sources"] = {name: pins.inventory(Path(data["root"])) for name, data in sources.items()}
            save_fixture(root, config, content)
            selected = load_fixture(root)
            self.assertEqual(selected["sources"]["baseline"]["root"], str(baseline))
            self.assertEqual(selected["sources"]["candidate"]["root"], str(candidate))
            self.assertNotEqual(sources["baseline"]["manifest_sha256"],
                                sources["candidate"]["manifest_sha256"])
            command = run.agent_command(root / "episode", "actor", str(pins.PYTHON),
                                        selected["sources"]["candidate"]["root"])
            self.assertEqual(command[1], str(candidate / "tools/run_veloren_agent.py"))
            (candidate / "tools/run_veloren_agent.py").write_text("# changed\n")
            with self.assertRaisesRegex(ValueError, "inventory changed"):
                load_fixture(root)

    def test_manifest_without_root_requires_explicit_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, manifest = self.make_source(Path(tmp), "copied", declared=False)
            with self.assertRaisesRegex(ValueError, "source root required"):
                pins.source_profile(manifest)
            self.assertEqual(pins.source_profile(manifest, source)["root"], str(source))

    def test_declared_root_mismatch_and_manifest_changes_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, manifest = self.make_source(root, "first")
            other, _ = self.make_source(root, "other")
            with self.assertRaisesRegex(ValueError, "disagrees"):
                pins.source_profile(manifest, other)
            digest = pins.lifecycle.sha(manifest)
            manifest.write_text(manifest.read_text() + "\n")
            with self.assertRaisesRegex(ValueError, "manifest changed"):
                pins.source_profile(manifest, source, digest)

    def test_missing_extra_and_changed_source_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for condition in ("missing", "extra", "changed"):
                with self.subTest(condition=condition):
                    source, manifest = self.make_source(root, condition)
                    entry = source / "tools/run_veloren_agent.py"
                    if condition == "missing":
                        entry.unlink()
                    elif condition == "extra":
                        (source / "extra.py").write_text("# extra\n")
                    else:
                        entry.write_text("# modified after manifest creation\n")
                    with self.assertRaisesRegex(ValueError, "inventory changed"):
                        pins.source_profile(manifest)

    def test_source_and_manifest_symlinks_and_traversal_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, manifest = self.make_source(root, "first", declared=False)
            linked_manifest = root / "linked.json"
            linked_manifest.symlink_to(manifest)
            with self.assertRaisesRegex(ValueError, "manifest path"):
                pins.source_profile(linked_manifest, source)
            linked_source = root / "linked-source"
            linked_source.symlink_to(source, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                pins.source_profile(manifest, linked_source)
            with self.assertRaisesRegex(ValueError, "source root"):
                pins.source_profile(manifest, source / "tools/..")
            data = json.loads(manifest.read_text())
            data["files"]["../escape.py"] = "0" * 64
            manifest.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "inventory entry"):
                pins.source_profile(manifest, source)

    def test_invalid_candidate_is_rejected_before_creating_binding(self):
        from scripts.rpg_benchmark import runtime
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline_source, baseline = self.make_source(root, "baseline")
            source, candidate = self.make_source(root, "candidate")
            (source / "extra.py").write_text("# extra\n")
            legacy = root / "legacy.json"
            legacy.write_text(json.dumps({
                "schema": 2, "seeds": [130626853],
                "dependencies": {**{key: {"path": "/unused", "sha256": "0" * 64}
                                   for key in ("server", "bridge", "build_receipt")},
                                 "scene_manifest": {"sha256": pins.lifecycle.sha(
                                     Path(suite.__file__).with_name("scenes.json"))}},
                "native": {"server_source_head_current": "mock", "native_source_head_current": "mock"},
                "assets": {"root": "/unused/assets", "files": {}}, "engine_root": "/unused",
                "sources": {name: {"root": str(path), "manifest": str(manifest),
                                  "manifest_sha256": pins.lifecycle.sha(manifest)}
                            for name, path, manifest in (("baseline", baseline_source, baseline),
                                                         ("candidate", source, candidate))}}))
            destination = root / "bundle"
            with self.assertRaisesRegex(ValueError, "inventory changed"):
                runtime.migrate(legacy, pins.lifecycle.sha(legacy), destination, root,
                                with_gameagent=True)
            self.assertFalse(destination.exists())

    def test_portable_install_has_no_implicit_historical_source_or_output_root(self):
        self.assertIsNone(pins.ROOT)
        self.assertFalse(hasattr(pins, "SOURCE"))
        self.assertFalse(hasattr(pins, "MANIFEST"))

    def test_content_preserves_arbitrary_source_inventories_separate_from_locations(self):
        from .test_portability import fixture_config, save_fixture, load_fixture
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, content = fixture_config(root)
            _, baseline = self.make_source(root, "baseline", declared=False)
            candidate_source, candidate = self.make_source(root, "candidate", declared=False)
            baseline_source = root / "baseline/source/GameAgent"
            for name, source, manifest in (("baseline", baseline_source, baseline),
                                           ("candidate", candidate_source, candidate)):
                profile = pins.source_profile(manifest, source)
                config["sources"][name] = profile["root"]
                content["sources"][name] = json.loads(manifest.read_text())["files"]
            save_fixture(root, config, content)
            selected = load_fixture(root)
            self.assertEqual(selected["sources"]["baseline"]["root"], str(baseline_source))
            self.assertEqual(selected["sources"]["candidate"]["root"], str(candidate_source))
            moved = root / "moved-source"
            import shutil
            shutil.copytree(candidate_source, moved)
            content_sha = config["content"]["sha256"]
            config["sources"]["candidate"] = str(moved)
            save_fixture(root, config, content)
            self.assertEqual(config["content"]["sha256"], content_sha)
            self.assertEqual(load_fixture(root)["sources"]["candidate"]["root"], str(moved))


class SceneTests(unittest.TestCase):
    def test_manifest_has_all_tracks_and_no_capability_solver(self):
        values = suite.load(Path(suite.__file__).with_name("scenes.json"))
        self.assertEqual(set(values), {*(f"C{i}" for i in range(1, 5)),
                                      *(f"B{i}" for i in range(1, 4)),
                                      *(f"N{i}" for i in range(1, 8)), "X1", "C3U", "X2",
                                      "T01", "T02", "T03", "T04", "T05", "T06", "T07", "T08", "T09",
                                      "T10", "T11", "T12"})
        for identifier in ("B1", "B2", "B3", "C1", "C2", "C3", "C4"):
            self.assertEqual(values[identifier]["terrains"], ["flat", "natural"])
        self.assertEqual(values["B3"]["body"]["BipedLarge"]["body_type"], "Male")
        self.assertEqual(values["B3"]["gameplay_mode"], "work-state")
        self.assertNotIn("twigs", values["C3"]["task"])

    def test_source_mode_and_derivative_assets_reach_real_entry(self):
        native = NativeConfig(assets=Path("/isolated/assets"), seed=123)
        command = run.agent_command(Path("/episode"), "actor", "/python", "/source",
                                    task="Fight and survive.", gameplay_mode="work-state", config=native)
        self.assertEqual(command[command.index("--gameplay-mode") + 1], "work-state")
        self.assertEqual(command[command.index("--assets") + 1], "/isolated/assets")
        self.assertNotIn("--brain-direct-only", command)

    def test_c4_requires_actual_intermediate_and_final_transactions(self):
        opening = observation()
        opening["inventory"]["items"] = [{"item": {"id": crafting.HIDE, "amount": 5}}]
        final = observation()
        final["inventory"]["items"] = [{"item": {"id": crafting.CHEST, "amount": 1}}]
        fixture = {"opening": opening, "case": "C4"}
        first = craft_receipt("simple_leather", crafting.LEATHER,
                              {crafting.HIDE: -5, crafting.LEATHER: 5})
        last = craft_receipt("rawhide_chest", crafting.CHEST,
                             {crafting.LEATHER: -5, crafting.CHEST: 1}, "b")
        self.assertTrue(crafting.score(fixture, final, [first, last], [], [])["success"])
        self.assertFalse(crafting.score(fixture, final, [last], [], [])["success"])
        wrong = copy.deepcopy(first)
        wrong["response"]["result"]["detail"]["inventory_delta"][crafting.HIDE] = -4
        self.assertFalse(crafting.score(fixture, final, [wrong, last], [], [])["success"])
        wrong["actor"] = 99
        self.assertFalse(crafting.score(fixture, final, [wrong, last], [], [])["success"])

    def test_combat_disappearance_is_not_defeat_and_damage_is_uid_attributed(self):
        opening = observation()
        opening["tick"] = 10
        target = {"uid": 7, "control_target": {"uid": 7, "lifecycle": "enemy"},
                  "health": {"current": 10, "maximum": 10, "dead": False}}
        fixture = {"opening": opening, "target": target}
        final = observation()
        final["tick"] = 12
        final["nearby_entities"] = []
        event = {"session_id": "s", "tick": 11, "data": {
            "source": "server_outcome_health_change", "amount": -10,
            "target_uid": 7, "contributor_uid": 42, "cause": "Attack(Melee)"}}
        self.assertFalse(combat.score(fixture, final, [], [event], [])["success"])
        dead = copy.deepcopy(target)
        dead["health"].update(current=0, dead=True)
        final["nearby_entities"] = [dead]
        self.assertTrue(combat.score(fixture, final, [], [event], [])["success"])
        other = copy.deepcopy(event)
        other["data"]["contributor_uid"] = 99
        self.assertFalse(combat.score(fixture, final, [], [event, other], [])["success"])
        event["data"]["contributor_uid"] = 99
        self.assertFalse(combat.score(fixture, final, [], [event], [])["success"])
        event["data"]["contributor_uid"] = 42
        final["survival"]["health"]["current"] = 0
        self.assertFalse(combat.score(fixture, final, [], [event], [])["success"])

    def test_navigation_vertical_settlement_and_interaction_are_distinct(self):
        opening = observation()
        opening.update(position=[0, 0, 1], tick=1)
        fixture = {"opening": opening, "goal": [512, 0, 1], "supplies": []}
        samples = []
        for tick, elapsed in ((2, 1.0), (3, 1.3)):
            row = copy.deepcopy(opening)
            row.update(position=[512, 0, 1], velocity=[0, 0, 0],
                       tick=tick, _elapsed_s=elapsed)
            samples.append(row)
        self.assertTrue(navigation.score(fixture, samples[-1], [], [], samples)["success"])
        samples[-1]["position"][2] = 3
        self.assertFalse(navigation.score(fixture, samples[-1], [], [], samples)["success"])
        samples[-1]["position"][2] = 1
        fixture["supplies"] = [{"uid": 10}]
        score = navigation.score(fixture, samples[-1], [], [], samples)
        self.assertTrue(score["arrival"])
        self.assertFalse(score["interaction"])
        self.assertFalse(score["success"])

    def test_sdk_evidence_requires_sdk_events_not_gameagent_final(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "result.json").write_text(json.dumps({"status": "interrupted", "provider_usage": []}))
            (root / "events.ndjson").write_text(
                json.dumps({"kind": "sdk_event", "event": {"type": "assistant.usage"}}) + "\n")
            value = evidence.select_sdk(root)
            self.assertTrue(value["entry_receipt_present"])
            self.assertEqual(value["actual_sdk_model_events"], 1)
            self.assertFalse(value["provider_error"])

    def test_exact_native_pickup_feedback_not_inventory_proxy(self):
        row = {
            "actor": 42, "request": {"args": {"action": {"type": "pickup", "uid": 9, "action_id": "a"}}},
            "response": {"ok": True, "session_id": "s", "result": {
                "action_id": "a", "action_type": "pickup", "status": "completed", "detail": {
                    "uid": 9, "operational_success": True, "pickup_feedback": {
                        "server_event": "Collected", "operational_collection_justified": True,
                        "candidate_request_count": 1, "collected_item": {
                            "id": {"Simple": "common.items.food.mushroom"}, "amount": 1}}}}},
        }
        self.assertEqual(oracles.pickup_actions(observation(), [row], 9, "common.items.food.mushroom"), ["a"])
        row["response"]["result"]["detail"]["pickup_feedback"]["candidate_request_count"] = 2
        self.assertEqual(oracles.pickup_actions(observation(), [row], 9, "common.items.food.mushroom"), [])

    def test_sdk_authentication_reads_only_explicit_existing_cache_or_environment(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sdk.os.environ, {}, clear=True):
            root = Path(tmp)
            cache = root / "oauth.json"
            cache.write_text('{"oauth_token": "unit-auth-token"}')
            self.assertEqual(sdk.authentication_token(cache), "unit-auth-token")
            self.assertEqual(list(root.iterdir()), [cache])
            cache.write_text("{}")
            with self.assertRaises(ValueError):
                sdk.authentication_token(cache)
            with self.assertRaises(FileNotFoundError):
                sdk.authentication_token(root / "missing")
            self.assertIsNone(sdk.authentication_token(None))

    def test_supply_expiry_is_logged_as_estimate_not_automatic_replenishment(self):
        from unittest.mock import Mock
        journal = Mock()
        fixture = {"supplies": [{"uid": 9, "item": "mushroom",
                                "expiry_game_time_estimate": 300,
                                "expires_after_game_seconds": 300}]}
        state = {}
        current = {"nearby_entities": [], "survival": {"buff_clock": {"now": 301}}}
        run.log_supplies(fixture, current, journal, state)
        data = journal.write.call_args.kwargs
        self.assertTrue(data["estimated_expiry_window_reached"])
        self.assertFalse(data["visible_in_local_view"])
        self.assertFalse(data["replenished"])
        run.log_supplies(fixture, current, journal, state)
        self.assertEqual(journal.write.call_count, 1)


class NamedSiteTests(unittest.TestCase):
    def marker(self):
        return {"name": "Spausalm", "kind": "Town", "map_id": "Town@18688,10784",
                "position": [18688.0, 10784.0, None]}

    def test_named_macro_goal_has_no_coordinates_route_or_query_ritual(self):
        values = suite.load(Path(suite.__file__).with_name("scenes.json"))
        definition = values["N7"]
        self.assertEqual(definition["mechanism_mode"], "setup-only")
        self.assertGreater(definition["minimum_distance_m"], 220)
        text = navigation.named_goal_text(definition["destination_name"])
        self.assertIn("Spausalm", text)
        self.assertFalse(any(character.isdigit() for character in text))
        for token in ("InspectMap", "waypoint", "first", "19088", "18688"):
            self.assertNotIn(token, text)

    def test_named_site_requires_unique_normal_native_label_and_unknown_altitude(self):
        marker = self.marker()
        self.assertEqual(navigation.named_site({"known_sites": [marker]}, "Spausalm", "Town"), marker)
        for rows in ([], [marker, marker]):
            with self.assertRaisesRegex(RuntimeError, "one named"):
                navigation.named_site({"known_sites": rows}, "Spausalm", "Town")
        marker["position"][2] = 350
        with self.assertRaisesRegex(ValueError, "unknown altitude"):
            navigation.named_site({"known_sites": [marker]}, "Spausalm", "Town")

    def test_native_map_observation_agreement_and_road_origin_are_required(self):
        marker = self.marker()
        view = {"schema_version": "1.0", "source": "connected_client_world_data",
                "coordinate_frame": "native_xy", "cell_stride": 1,
                "markers": [{"name": "Spausalm", "kind": "Town", "position": marker["position"][:2]}],
                "coverage_bounds": [18688, 10784, 18752, 10816], "symbol_blocks": [32, 32],
                "rows": ["14"], "legend": {"1": "land", "4": "road"}}
        result = navigation.qualify_named_map(view, marker, [18736, 10800])
        self.assertEqual(result["origin_cell"], [1, 0])
        with self.assertRaisesRegex(RuntimeError, "road/bridge"):
            navigation.qualify_named_map(view, marker, [18700, 10800])
        view["markers"][0]["position"] = [0, 0]
        with self.assertRaisesRegex(RuntimeError, "disagree"):
            navigation.qualify_named_map(view, marker, [18736, 10800])

    def test_setup_road_candidate_is_map_derived_and_requires_native_qualification(self):
        marker = self.marker()
        marker["position"] = [0, 0, None]
        view = {"schema_version": "1.0", "source": "connected_client_world_data",
                "coordinate_frame": "native_xy", "cell_stride": 1,
                "markers": [{"name": "Spausalm", "kind": "Town", "position": [0, 0]}],
                "coverage_bounds": [0, 0, 96, 96], "symbol_blocks": [32, 32],
                "rows": ["411", "441", "144"], "legend": {"1": "land", "4": "road"}}
        points = navigation.map_road_approach(view, marker, [80, 80], 32)
        self.assertEqual(points[0], [80, 80])
        self.assertEqual(points[-1], [16, 16])
        self.assertGreater(len(points), 2)
        view["rows"] = ["411", "111", "114"]
        with self.assertRaisesRegex(RuntimeError, "disconnected"):
            navigation.map_road_approach(view, marker, [80, 80], 32)

    def named_samples(self):
        opening = observation()
        opening.update(position=[19088, 10768, 350], tick=1, velocity=[0, 0, 0], _elapsed_s=0)
        fixture = {"opening": opening, "goal": [18688, 10784, 350], "supplies": [],
                   "named_destination": self.marker(),
                   "arrival_region": {"approach_horizontal_m": 64, "horizontal_m": 32, "vertical_m": 6}}
        rows = [opening]
        for tick, elapsed in ((2, 1.0), (3, 1.3)):
            row = copy.deepcopy(opening)
            row.update(position=[18720, 10784, 356], tick=tick, _elapsed_s=elapsed,
                       on_ground=True, current_site={
                           "location_name": "Spausalm", "loaded_chunk": True,
                           "source": "native_current_site_and_loaded_chunk"})
            rows.append(row)
        return fixture, rows

    def test_named_arrival_exact_region_grounding_and_native_name_not_displacement(self):
        fixture, rows = self.named_samples()
        score = navigation.score(fixture, rows[-1], [], [], rows)
        self.assertTrue(score["success"])
        self.assertTrue(score["named_site_observed"])
        for field, value in (("position", [18720.01, 10784, 356]),
                             ("position", [18720, 10784, 356.01]),
                             ("on_ground", False), ("velocity", [.11, 0, 0])):
            with self.subTest(field=field, value=value):
                changed = copy.deepcopy(rows)
                changed[-1][field] = value
                self.assertFalse(navigation.score(fixture, changed[-1], [], [], changed)["success"])
        for field, value in (("location_name", "Other town"), ("loaded_chunk", False),
                             ("source", "operator-fixture")):
            changed = copy.deepcopy(rows)
            changed[-1]["current_site"][field] = value
            self.assertFalse(navigation.score(fixture, changed[-1], [], [], changed)["success"])
        rows[-1]["position"][0] = 18728
        result = navigation.score(fixture, rows[-1], [], [], rows)
        self.assertTrue(result["approach"])
        self.assertFalse(result["arrival"])

    def test_named_arrival_preserves_pair_identity_freshness_and_survival(self):
        fixture, rows = self.named_samples()
        for field, value in (("tick", 2), ("_elapsed_s", 1.1), ("session_id", "other"),
                             ("self_identity", {"uid": 99})):
            changed = copy.deepcopy(rows)
            changed[-1][field] = value
            self.assertFalse(navigation.score(fixture, changed[-1], [], [], changed)["success"])
        rows[-1]["survival"]["health"]["current"] = 0
        self.assertFalse(navigation.score(fixture, rows[-1], [], [], rows)["success"])

    def test_named_mechanism_is_rejected_before_output_or_native_start(self):
        definitions = suite.load(Path(suite.__file__).with_name("scenes.json"))
        args = run.parser().parse_args([
            "--pins", "/unused", "--pins-sha256", "unused", "--out", "/unused-output",
            "--mode", "mechanism", "--scene", "N7"])
        admitted = {"dependencies": {"scene_manifest": {"path": str(Path(suite.__file__).with_name("scenes.json"))}},
                    "seeds": [args.seed]}
        with patch.object(pins, "load", return_value=admitted), patch.object(run, "reserve_output") as reserve:
            with self.assertRaisesRegex(ValueError, "setup only"):
                asyncio.run(run.episode(args))
        reserve.assert_not_called()
        with self.assertRaisesRegex(ValueError, "no measured whole-task solver"):
            asyncio.run(navigation.mechanism(None, {"named_destination": definitions["N7"]}))


class NamedSiteFootingTests(unittest.IsolatedAsyncioTestCase):
    async def test_ground_level_wood_needs_actual_grounding_and_nearby_non_canopy_support(self):
        from unittest.mock import AsyncMock
        point = [18896.205, 10767.845, 361.0]
        observed = {"on_ground": True, "position": point}
        wood = {"standing_position": [point[0] - .65, point[1], 361.0], "occupiable": True,
                "support": {"kind": "Wood"}}
        grass = {"standing_position": [point[0], point[1] + .65, 360.0], "occupiable": True,
                 "support": {"kind": "Grass"}}
        center = {"status": "complete", "support_candidates": []}
        readback = {"status": "complete", "support_candidates": [wood, grass]}
        actor = AsyncMock()
        actor.request.return_value = observed
        with patch.object(setup, "terrain", new=AsyncMock(side_effect=[center, *([readback] * 8)])):
            with self.assertRaisesRegex(RuntimeError, "lacks physical/geometry footing"):
                await setup.qualify(actor, point)
        with patch.object(setup, "terrain", new=AsyncMock(side_effect=[center, readback])):
            result = await setup.qualify(actor, point, allow_ground_level_wood=True)
        self.assertEqual(result["wood_support"], wood)
        self.assertEqual(result["nearby_ground_support"], grass)
        observed["on_ground"] = False
        with patch.object(setup, "terrain", new=AsyncMock(return_value=center)):
            with self.assertRaisesRegex(RuntimeError, "lacks physical/geometry footing"):
                await setup.qualify(actor, point, allow_ground_level_wood=True)
        observed["on_ground"] = True
        grass["standing_position"][2] = 350
        with patch.object(setup, "terrain", new=AsyncMock(side_effect=[center, *([readback] * 8)])):
            with self.assertRaisesRegex(RuntimeError, "lacks physical/geometry footing"):
                await setup.qualify(actor, point, allow_ground_level_wood=True)


class ContinuousWalkDeadlineTests(unittest.IsolatedAsyncioTestCase):
    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

    class Actor:
        def __init__(self, clock, completion_time, *, failure=None, release_status="released"):
            self.clock = clock
            self.completion_time = completion_time
            self.failure = failure
            self.release_status = release_status
            self.generation = 7
            self.calls = []

        async def request(self, command, arguments=None, *, origin):
            self.calls.append((command, arguments, origin))
            if command == "set_control_state":
                self.generation = arguments["generation"]
                return {"status": "applied"}
            if command == "release_control_state":
                return {"status": self.release_status}
            if command != "observe":
                raise AssertionError(command)
            return {
                "position": [10, 0, 0] if self.clock.now >= self.completion_time else [0, 0, 0],
                "session_id": "test-session", "control_actor": {"uid": 42},
                "continuous_control": {
                    "generation": self.generation, "protection_generation": 3,
                    "local_route_failure": self.failure,
                },
            }

    async def walk(self, origin, completion_time, *, jump, failure=None,
                   release_status="released", error=None):
        clock = self.Clock()
        actor = self.Actor(clock, completion_time, failure=failure, release_status=release_status)

        async def sleep(seconds):
            self.assertEqual(seconds, .1)
            clock.now += jump

        with patch.object(setup, "time", clock), patch.object(setup.asyncio, "sleep", new=sleep):
            if error is None:
                result = await setup.continuous_walk(actor, [10, 0, 0], origin=origin)
                self.assertEqual(result["achieved"]["position"], [10, 0, 0])
                self.assertEqual(result["primitive"], "native-continuous-loaded-astar-destination")
            else:
                with self.assertRaisesRegex(*error):
                    await setup.continuous_walk(actor, [10, 0, 0], origin=origin)
        admission = next(args for cmd, args, _ in actor.calls if cmd == "set_control_state")
        releases = [args for cmd, args, _ in actor.calls if cmd == "release_control_state"]
        self.assertEqual(len(releases), 1)
        self.assertEqual(releases[0], {
            key: admission[key] for key in (
                "owner", "generation", "expected_session_id", "actor", "expected_protection_generation")
        } | {"sequence": 2})
        self.assertEqual(admission["sequence"], 1)
        self.assertTrue(all(call_origin == origin for _, _, call_origin in actor.calls))
        return actor

    async def test_setup_accepts_completion_after_60_before_300_and_releases(self):
        for elapsed in (60, 60.001, 120, 299.999):
            with self.subTest(elapsed=elapsed):
                await self.walk("setup", elapsed, jump=elapsed)

    async def test_setup_times_out_at_and_after_exact_300_boundary_and_releases(self):
        for elapsed in (300, 300.001, 301):
            with self.subTest(elapsed=elapsed):
                await self.walk("setup", elapsed, jump=elapsed, error=(
                    TimeoutError, "exceeded 300-second RPC-equivalent deadline"))

    async def test_setup_stalled_actor_times_out_without_success_fallback(self):
        await self.walk("setup", float("inf"), jump=100, error=(
            TimeoutError, "exceeded 300-second RPC-equivalent deadline"))

    async def test_nonsetup_completion_before_60_is_unchanged(self):
        for origin in ("mechanism", "agent", "judge", "Setup"):
            with self.subTest(origin=origin):
                await self.walk(origin, 59.999, jump=59.999)

    async def test_nonsetup_at_60_and_after_60_is_still_timeout(self):
        for origin in ("mechanism", "agent", "judge", "Setup"):
            for elapsed in (60, 60.001, 120, 299.999):
                with self.subTest(origin=origin, elapsed=elapsed):
                    await self.walk(origin, elapsed, jump=elapsed, error=(
                        TimeoutError, "exceeded 60-second RPC-equivalent deadline"))

    async def test_native_route_failure_still_aborts_and_releases_immediately(self):
        actor = await self.walk("setup", float("inf"), jump=100, failure="blocked",
                                error=(RuntimeError, "native continuous route failed"))
        self.assertEqual(len(actor.calls), 5)

    async def test_release_failure_is_not_hidden_by_setup_completion(self):
        await self.walk("setup", 120, jump=120, release_status="refused",
                        error=(RuntimeError, "native calibration control was not released"))


class ReliabilityRepairTests(unittest.IsolatedAsyncioTestCase):
    def test_physical_route_endpoint_gate_preserves_exact_boundaries(self):
        row = {"position": [1.5, 0, 1], "on_ground": True}
        self.assertTrue(navigation.at_route_endpoint(row, [0, 0, 0]))
        for point, grounded in (([1.50001, 0, 1], True), ([1.5, 0, 1.00001], True),
                                ([0, 0, 0], False)):
            self.assertFalse(navigation.at_route_endpoint({
                "position": point, "on_ground": grounded}, [0, 0, 0]))

    async def endpoint_after_obstructed_finisher(self, *, dead=False, changed_actor=False,
                                                drift=False, wrong_support=False):
        from unittest.mock import AsyncMock, Mock
        from scripts.rpg_benchmark.native import NativeRefusal
        current = {"position": [0, 0, 0], "on_ground": True, "velocity": [0, 0, 0],
                   "session_id": "admin", "self_identity": {"uid": 1},
                   "survival": {"health": {"current": 100}, "dead": False}}
        admin = Mock(request=AsyncMock(side_effect=lambda *args, **kw: copy.deepcopy(current)))
        controls, moves = [], []
        async def continuous(actor, goal, **kw):
            controls.append(goal)
            if len(controls) == 1:
                raise NativeRefusal("set_control_state", {"code": "control_route_no_path"})
            current.update(position=[15.45, 0, 1], velocity=[2.8, -2.6, 0])
            if dead:
                current["survival"]["health"]["current"] = 0
            if changed_actor:
                current["self_identity"] = {"uid": 2}
            raise setup.ContinuousRouteFailure({"status": "applied"}, {
                **copy.deepcopy(current), "continuous_control": {
                    "local_route": {"blocked_edge": {"cause": "control_motion_obstructed"}}}})
        async def query(actor, point):
            return {"status": "complete", "support_candidates": [{
                "occupiable": True, "standing_position": point, "support": {"kind": "Grass"}}]}
        async def move(actor, kind, **kw):
            moves.append(kw["position"])
            current["position"] = kw["position"]
            if len(moves) == 3:
                current["position"] = [14, 0, 0]
                return {"status": "failed", "detail": {"code": "stalled"}}
            return {"status": "completed"}
        async def settle(*args):
            current["velocity"] = [0, 0, 0]
            if len(controls) == 2 and drift:
                current["position"] = [13, 0, 1]
            return copy.deepcopy(current)
        with patch.object(navigation, "recover_climb", new=AsyncMock()), patch.object(
                setup, "continuous_walk", side_effect=continuous), patch.object(
                setup, "terrain", side_effect=query), patch.object(
                setup, "qualify", new=AsyncMock(side_effect=(
                    RuntimeError("native footing absent") if wrong_support else None),
                    return_value={"status": "complete"})), patch.object(
                setup, "action", side_effect=move), patch.object(
                setup, "wait_observation", side_effect=settle), patch.object(
                setup, "ready_observation", new=AsyncMock(side_effect=lambda *args: copy.deepcopy(current))):
            result = await navigation.recover_route_leg(admin, [16, 0, 0], {"status": "failed"})
        return result

    async def test_finisher_obstruction_refreshes_actual_pose_and_qualifies_native_endpoint(self):
        result = await self.endpoint_after_obstructed_finisher()
        leg = result["attempts"][0]["legs"][-1]
        self.assertEqual(leg["receipt"]["status"], "failed")
        self.assertIn("failure", leg["endpoint_finishing"])
        proof = leg["endpoint_after_failure"]
        self.assertEqual(proof["achieved"]["position"], [15.45, 0, 1])
        self.assertEqual(proof["achieved"]["velocity"], [0, 0, 0])
        self.assertEqual(proof["terrain"]["status"], "complete")
        self.assertEqual(result["achieved"]["position"], [15.45, 0, 1])

    async def test_obstructed_finisher_cannot_admit_dead_or_changed_actor_or_absent_footing(self):
        for kwargs, error in (({"dead": True}, ValueError), ({"changed_actor": True}, RuntimeError),
                              ({"wrong_support": True}, RuntimeError)):
            with self.subTest(kwargs=kwargs), self.assertRaises(error):
                await self.endpoint_after_obstructed_finisher(**kwargs)

    async def test_finisher_near_pose_drifting_outside_gate_is_not_accepted(self):
        with self.assertRaisesRegex(RuntimeError, "could not qualify"):
            await self.endpoint_after_obstructed_finisher(drift=True)

    def test_deferred_actor_not_started_is_not_a_missing_owned_process(self):
        from unittest.mock import Mock
        self.assertEqual(run.bridge_cleanup(Mock(process=None, cleanup={})), {
            "started": False, "stopped": True, "reason": "native bridge process was never started"})
        actual = {"pid": 99, "start_ticks": 123, "stopped": False}
        self.assertIs(run.bridge_cleanup(Mock(process=Mock(), cleanup=actual)), actual)
        self.assertFalse(actual["stopped"])

    async def test_stalled_chaser_after_real_grounded_progress_uses_native_endpoint_finishing(self):
        from unittest.mock import AsyncMock, Mock
        from scripts.rpg_benchmark.native import NativeRefusal
        current = {"position": [0, 0, 0], "on_ground": True, "velocity": [0, 0, 0]}
        admin = Mock(request=AsyncMock(side_effect=lambda *args, **kw: copy.deepcopy(current)))
        continuous_calls = []
        async def continuous(actor, goal, **kw):
            continuous_calls.append((list(current["position"]), goal))
            if len(continuous_calls) == 1:
                raise NativeRefusal("set_control_state", {"code": "control_route_no_path"})
            current["position"] = goal
            return {"achieved": copy.deepcopy(current), "primitive": "native-continuous-loaded-astar-destination"}
        async def query(actor, point):
            return {"status": "complete", "support_candidates": [{
                "occupiable": True, "standing_position": point, "support": {"kind": "Grass"}}]}
        moves = []
        async def move(actor, kind, **kw):
            moves.append(kw["position"])
            current["position"] = kw["position"]
            if len(moves) == 3:
                current["position"] = [14, 0, 0]
                return {"status": "failed", "detail": {"code": "stalled"}}
            return {"status": "completed"}
        with patch.object(navigation, "recover_climb", new=AsyncMock()), patch.object(
                setup, "continuous_walk", side_effect=continuous), patch.object(
                setup, "terrain", side_effect=query), patch.object(
                setup, "action", side_effect=move), patch.object(
                setup, "wait_observation", new=AsyncMock(side_effect=lambda *args: copy.deepcopy(current))), patch.object(
                setup, "ready_observation", new=AsyncMock(side_effect=lambda *args: copy.deepcopy(current))):
            result = await navigation.recover_route_leg(admin, [16, 0, 0], {"status": "failed"})
        leg = result["attempts"][0]["legs"][-1]
        self.assertEqual(leg["receipt"]["status"], "failed")
        self.assertEqual(leg["endpoint_finishing"]["continuous"]["achieved"]["position"], [16, 0, 0])
        self.assertEqual(continuous_calls[-1], ([14, 0, 0], [16, 0, 0]))

    async def test_completed_airborne_receipt_then_grounded_overshoot_is_corrected_not_trusted(self):
        from unittest.mock import AsyncMock, Mock
        drift = {"position": [17.52, -.34, 0], "on_ground": True, "velocity": [.089, 0, 0]}
        endpoint = {**drift, "position": [16, 0, 0], "velocity": [0, 0, 0]}
        first = {"status": "completed", "detail": {"on_ground": False, "remaining_distance": .959}}
        correction = {"status": "completed"}
        admin = Mock()
        with patch.object(setup, "settled_point", new=AsyncMock(return_value=[16, 0, 0])), patch.object(
                setup, "goto", new=AsyncMock()), patch.object(
                setup, "action", new=AsyncMock(side_effect=[first, correction])) as action, patch.object(
                setup, "wait_observation", new=AsyncMock(side_effect=[drift, endpoint])), patch.object(
                setup, "qualify", new=AsyncMock(return_value={})):
            route = await navigation.physical_route(admin, [0, 0, 0], [16, 0, 0], isolate_ambient=False)
        self.assertEqual(action.await_args_list[1].kwargs["speed"], .35)
        self.assertEqual(action.await_args_list[1].kwargs["basis"], drift)
        self.assertEqual(route[0]["receipt"], first)
        self.assertEqual(route[0]["source_backed_alternative"]["post_completion_displacement"], drift)
        self.assertEqual(route[0]["achieved"]["position"], [16, 0, 0])

    async def test_completed_leg_settling_above_target_requires_vertical_correction(self):
        from unittest.mock import AsyncMock, Mock
        displaced = {"position": [15.4, .1, 2], "on_ground": True, "velocity": [0, 0, 0]}
        endpoint = {**displaced, "position": [16, 0, 0]}
        with patch.object(setup, "settled_point", new=AsyncMock(return_value=[16, 0, 0])), patch.object(
                setup, "goto", new=AsyncMock()), patch.object(
                setup, "action", new=AsyncMock(return_value={"status": "completed"})) as action, patch.object(
                setup, "wait_observation", new=AsyncMock(side_effect=[displaced, endpoint])), patch.object(
                setup, "qualify", new=AsyncMock(return_value={})):
            route = await navigation.physical_route(Mock(), [0, 0, 0], [16, 0, 0], isolate_ambient=False)
        self.assertEqual(action.await_count, 2)
        self.assertEqual(route[0]["achieved"]["position"], [16, 0, 0])

    async def test_partial_detour_progress_rebases_next_native_query_window(self):
        from unittest.mock import AsyncMock, Mock
        from scripts.rpg_benchmark.native import NativeRefusal
        current = {"position": [0, 0, 0], "on_ground": True, "velocity": [0, 0, 0]}
        admin = Mock(request=AsyncMock(side_effect=lambda *args, **kw: copy.deepcopy(current)))
        queried = []
        async def query(actor, probe):
            self.assertLessEqual(math.dist(probe[:2], current["position"][:2]), 10)
            queried.append((list(current["position"]), probe))
            return {"status": "complete", "support_candidates": [{
                "occupiable": True, "standing_position": probe, "support": {"kind": "Grass"}}]}
        calls = []
        async def move(actor, kind, **kw):
            calls.append(kw["position"])
            current["position"] = kw["position"]
            if len(calls) == 3:
                current["position"] = [13.5, 1, 0]
                return {"status": "failed"}
            return {"status": "completed"}
        with patch.object(navigation, "recover_climb", new=AsyncMock()), patch.object(
                setup, "continuous_walk", new=AsyncMock(side_effect=NativeRefusal(
                    "set_control_state", {"code": "control_route_no_path"}))), patch.object(
                setup, "terrain", side_effect=query), patch.object(
                setup, "action", side_effect=move), patch.object(
                setup, "wait_observation", new=AsyncMock(side_effect=lambda *args: copy.deepcopy(current))), patch.object(
                setup, "ready_observation", new=AsyncMock(side_effect=lambda *args: copy.deepcopy(current))):
            result = await navigation.recover_route_leg(admin, [16, 0, 0], {"status": "failed"})
        self.assertTrue(result["attempts"][0]["failed"])
        self.assertEqual(result["attempts"][1]["from"]["position"], [13.5, 1, 0])
        self.assertEqual(result["achieved"]["position"], [16, 0, 0])

    async def test_real_action_helper_preserves_character_action_kind_and_fence(self):
        from unittest.mock import AsyncMock, Mock
        basis = {"continuous_control": {"generation": 9}, "session_id": "admin",
                 "control_actor": {"uid": 1}}
        actor = Mock(request=AsyncMock(return_value={"action_id": "cancel", "status": "completed"}))
        with patch.object(setup, "uuid4", return_value=Mock(hex="cancel")):
            await setup.action(actor, "character_action", kind="cancel_climb", basis=basis)
        submitted = actor.request.await_args.args[1]
        self.assertEqual(submitted["action"]["kind"], "cancel_climb")
        self.assertEqual(submitted["action"]["type"], "character_action")
        self.assertEqual(submitted["control_fence"]["generation"], 9)

    async def test_n1_uses_exact_explicit_voxels_not_falling_author_pose(self):
        from unittest.mock import AsyncMock, Mock
        config = navigation.layouts.scene("wall_side_detour")
        expected = [navigation.xyz(point) for point in config["solid_voxels_y_up"]]
        with patch.object(setup, "author_voxels", new=AsyncMock(
                side_effect=RuntimeError("explicit authoring sentinel"))) as author, patch.object(
                setup, "rock", new=AsyncMock()) as rock, patch.object(
                setup, "isolate_initial_spawn", new=AsyncMock()) as isolate:
            with self.assertRaisesRegex(RuntimeError, "explicit authoring sentinel"):
                await navigation.setup(Mock(), Mock(), {"id": "N1", "layout": "wall_side_detour"}, "flat")
        author.assert_awaited_once()
        self.assertEqual(author.await_args.args[1], expected)
        self.assertEqual(len(expected), 249)
        self.assertEqual(len(config["standing_columns"]), 221)
        self.assertIn([18713, 10809, 410], expected)
        rock.assert_not_awaited()
        isolate.assert_not_awaited()

    async def test_accepted_goto_fall_cannot_trigger_legacy_make_block(self):
        from unittest.mock import AsyncMock, Mock
        async def failed_pose(actor, predicate, reason, timeout):
            for z in (410.5044, 409.7958, 381):
                self.assertFalse(predicate({
                    "position": [18713.5, 10809.5, z], "on_ground": z == 381,
                    "velocity": [0, 0, -4.514]}))
            raise RuntimeError("no author pose")
        with patch.object(setup, "wait_observation", side_effect=failed_pose), patch.object(
                setup, "action", new=AsyncMock(return_value={"status": "completed"})) as action:
            with self.assertRaisesRegex(RuntimeError, "no author pose"):
                await setup.rock(Mock(), [18713, 10809, 410])
        self.assertEqual(action.await_count, 1)
        self.assertEqual(action.await_args.kwargs["name"], "goto")

    async def test_explicit_build_ack_without_exact_readback_fails(self):
        from unittest.mock import AsyncMock, Mock
        from functools import partial
        from tests.authoring import Administrator
        admin = Mock()
        state = Administrator(admin)
        async def terrain(*args):
            return state.readback({"status": "no_candidate_in_window",
                "vertical_scan": {"all_cells_loaded": True}, "support_candidates": []},
                [18713, 10809, 410])
        readback = partial(setup.construction_readback, timeout=.001)
        with patch.object(setup, "command", new=state.command), patch.object(
                setup, "construction_readback", new=readback), patch.object(
                setup, "wait_observation", side_effect=state.wait), patch.object(
                setup, "goto", new=AsyncMock()), patch.object(
                setup, "action", new=AsyncMock(return_value={"status": "completed"})), patch.object(
                setup, "ready_observation", new=state.ready), patch.object(
                setup, "terrain", new=terrain):
            with self.assertRaisesRegex(RuntimeError, "exact native Rock readback"):
                await setup.author_voxels(admin, [[18713, 10809, 410]])

    async def test_climb_cancel_is_native_and_grounding_precedes_new_control(self):
        from unittest.mock import AsyncMock, Mock
        before = {"character_state": "Climb", "on_ground": False, "position": [19268.55, 10800.57, 349.67]}
        after = {**before, "character_state": "Idle", "on_ground": True,
                 "velocity": [0, 0, 0], "position": [19268.55, 10800.57, 349]}
        order = []
        async def cancel(*args, **kwargs):
            self.assertEqual(kwargs, {"kind": "cancel_climb", "basis": before})
            order.append("cancel")
            return {"status": "completed"}
        async def grounded(actor, predicate, reason):
            self.assertFalse(predicate({**after, "character_state": "Climb", "on_ground": False}))
            self.assertTrue(predicate(after))
            order.append("grounded")
            return after
        async def continuous(*args, **kwargs):
            self.assertEqual(order, ["cancel", "grounded"])
            raise setup.NativeActionFailure("unexpected", {})
        admin = Mock(request=AsyncMock(return_value=before))
        with patch.object(setup, "action", side_effect=cancel), patch.object(
                setup, "wait_observation", side_effect=grounded), patch.object(
                setup, "continuous_walk", side_effect=continuous), patch.object(
                setup, "goto", new=AsyncMock()) as goto:
            with self.assertRaises(setup.NativeActionFailure):
                await navigation.recover_route_leg(admin, [19279.45, 10800.5, 351],
                                                  {"status": "failed", "detail": {"reason": "stalled"}})
        goto.assert_not_awaited()

    async def test_nested_blocked_edge_is_released_before_detour(self):
        class BlockedActor(ContinuousWalkDeadlineTests.Actor):
            async def request(self, command, arguments=None, *, origin):
                row = await super().request(command, arguments, origin=origin)
                if command == "observe" and self.generation > 7:
                    row["continuous_control"].update(local_route={
                        "blocked_edge": {"cause": "control_motion_obstructed"}},
                        motion_feedback={"status": "control_motion_obstructed"})
                    row["position"] = [0, 0, 0]
                return row
        clock = ContinuousWalkDeadlineTests.Clock()
        actor = BlockedActor(clock, float("inf"))
        with patch.object(setup, "time", clock):
            with self.assertRaises(setup.ContinuousRouteFailure) as caught:
                await setup.continuous_walk(actor, [15.445, 0, 0], origin="setup")
        self.assertIsNone(caught.exception.observation["continuous_control"]["local_route_failure"])
        self.assertEqual(actor.calls[-1][0], "release_control_state")
        self.assertEqual(clock.now, 0)

    async def test_climb_refusal_and_obstruction_use_walked_geometry_alternate(self):
        from unittest.mock import AsyncMock, Mock
        from scripts.rpg_benchmark.native import NativeRefusal
        for error in (
            NativeRefusal("set_control_state", {"code": "control_motion_unsupported"}),
            NativeRefusal("set_control_state", {"code": "control_route_no_path"}),
            NativeRefusal("set_control_state", {"code": "control_route_step_requires_action"}),
            NativeRefusal("set_control_state", {"code": "control_route_vertical_unsupported"}),
            setup.ContinuousRouteFailure({"status": "applied"}, {
                "continuous_control": {"local_route": {"blocked_edge": {
                    "cause": "control_motion_obstructed"}}}}),
        ):
            current = {"position": [0, 0, 0], "on_ground": True}
            admin = Mock(request=AsyncMock(side_effect=lambda *args, **kw: copy.deepcopy(current)))
            def terrain(actor, probe):
                return {"status": "complete", "support_candidates": [{
                    "occupiable": True, "standing_position": [*probe[:2], 0],
                    "support": {"kind": "Grass"}}]}
            async def move(actor, kind, **kw):
                self.assertEqual(kind, "move_to")
                current["position"] = kw["position"]
                return {"status": "completed"}
            failed = {"status": "failed", "detail": {"reason": "stalled"}}
            with patch.object(navigation, "recover_climb", new=AsyncMock()), patch.object(
                    setup, "continuous_walk", new=AsyncMock(side_effect=error)), patch.object(
                    setup, "terrain", new=AsyncMock(side_effect=terrain)), patch.object(
                    setup, "action", side_effect=move), patch.object(
                    setup, "wait_observation", new=AsyncMock(side_effect=lambda *args: copy.deepcopy(current))), patch.object(
                    setup, "ready_observation", new=AsyncMock(side_effect=lambda _: copy.deepcopy(current))), patch.object(
                    setup, "goto", new=AsyncMock()) as goto:
                result = await navigation.recover_route_leg(admin, [16, 0, 0], failed)
            self.assertEqual(result["failed_move_to"], failed)
            self.assertEqual(len(result["attempts"][0]["legs"]), 3)
            self.assertEqual(result["achieved"]["position"], [16, 0, 0])
            goto.assert_not_awaited()

    async def test_no_endpoint_progress_cannot_qualify_alternate(self):
        from unittest.mock import AsyncMock, Mock
        from scripts.rpg_benchmark.native import NativeRefusal
        row = {"position": [0, 0, 0], "on_ground": True, "velocity": [0, 0, 0]}
        admin = Mock(request=AsyncMock(return_value=row))
        async def query(actor, point):
            return {"status": "complete", "support_candidates": [{
                "occupiable": True, "standing_position": point, "support": {"kind": "Grass"}}]}
        with patch.object(navigation, "recover_climb", new=AsyncMock()), patch.object(
                setup, "continuous_walk", new=AsyncMock(side_effect=NativeRefusal(
                    "set_control_state", {"code": "control_motion_obstructed"}))), patch.object(
                setup, "terrain", side_effect=query), patch.object(
                setup, "action", new=AsyncMock(return_value={"status": "completed"})):
            with self.assertRaisesRegex(RuntimeError, "could not qualify"):
                await navigation.recover_route_leg(admin, [16, 0, 0], {"status": "failed"})

    def test_actor_damaged_or_dead_during_route_is_not_protected_admin(self):
        initial = observation()
        initial["survival"]["buffs"] = []
        navigation.monitor_setup_actor(initial, initial)
        for health, buffs in ((92, [{"kind": "Bleeding"}]), (0, [])):
            injured = copy.deepcopy(initial)
            injured["survival"]["health"]["current"] = health
            injured["survival"]["buffs"] = buffs
            with self.assertRaises((RuntimeError, ValueError)):
                navigation.monitor_setup_actor(injured, initial)
        admin = copy.deepcopy(initial)
        admin["self_identity"] = {"uid": 1}
        admin["survival"]["buffs"] = [{"kind": "Invulnerability"}]
        with self.assertRaisesRegex(RuntimeError, "identity changed"):
            navigation.monitor_setup_actor(admin, initial)


if __name__ == "__main__":
    unittest.main()
