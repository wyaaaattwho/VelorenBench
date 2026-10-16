import copy
import unittest
from pathlib import Path

from scripts.rpg_benchmark import crafting, mixed, scene, suite
from scripts.rpg_benchmark import setup
from unittest.mock import AsyncMock, Mock, patch
from tests.test_rpg_benchmark import observation
from tests.test_rpg_scenes import craft_receipt


class CompletionTests(unittest.TestCase):
    def test_unknown_query_is_not_craftability_or_model_ignorance(self):
        opening = observation()
        opening["known_recipes"] = {"items": []}
        query = {"items": [{"recipe": scene.RECIPE, "character_known": False, "satisfied": None}]}
        result = crafting.discovery_evidence(opening, query, crafting.DEFAULT_RECIPE)
        self.assertFalse(result["model_ignorance_claim"])
        for change in ({"character_known": True}, {"satisfied": True}):
            invalid = copy.deepcopy(query)
            invalid["items"][0].update(change)
            with self.assertRaises(RuntimeError):
                crafting.discovery_evidence(opening, invalid, crafting.DEFAULT_RECIPE)
        opening["known_recipes"]["items"] = [{"recipe": scene.RECIPE}]
        with self.assertRaises(RuntimeError):
            crafting.discovery_evidence(opening, query, crafting.DEFAULT_RECIPE)

    def test_alternate_recipe_requires_exact_own_effect_not_original_output(self):
        output = "common.items.food.apple_stick"
        apple = "common.items.food.apple"
        twig = "common.items.crafting_ing.twigs"
        before, after = observation(), observation()
        before["inventory"]["items"] = [{"item": {"id": item, "amount": 1}} for item in (apple, twig)]
        after["inventory"]["items"] = [{"item": {"id": output, "amount": 1}}]
        receipt = craft_receipt("apples_stick", output, {apple: -1, twig: -1, output: 1})
        self.assertTrue(scene.score(before, after, [receipt], recipe="apples_stick",
                                    output=output, inputs={apple: 1, twig: 1})["success"])
        self.assertFalse(scene.score(before, after, [receipt])["success"])
        self.assertFalse(scene.score(before, after, [], recipe="apples_stick",
                                     output=output, inputs={apple: 1, twig: 1})["success"])

    def test_heldout_differences_are_explicit_and_original_goals_retained(self):
        values = suite.load(Path(suite.__file__).with_name("scenes.json"))
        x2 = values["X2"]
        condition = x2["held_out_condition"]
        self.assertFalse(condition["seed_difference"])
        self.assertNotEqual(condition["development_recipe"], condition["recipe"])
        self.assertNotEqual(x2["rendezvous_offset"], [80, 32])
        self.assertNotIn("move_to", x2["task"])
        self.assertNotIn("apples_stick", x2["task"])
        self.assertEqual(values["C2"]["task"], "Make and keep one mushroom stick for food (common.items.food.mushroom_stick). You know the food recipes, but have only partial supplies. Find what you need in the environment using normal observation and keep the product.")

    def test_unlock_needs_own_correlated_native_group_consumption(self):
        receipt = {"actor": 42, "request": {"args": {"action": {
            "type": "use_item", "action_id": "unlock"}}},
            "response": {"ok": True, "session_id": "s", "result": {
                "action_type": "use_item", "action_id": "unlock", "status": "completed",
                "detail": {"item_definition_id": scene.GROUP, "definition_count_delta": -1}}}}
        self.assertEqual(crafting.unlock_actions(observation(), [receipt]), ["unlock"])
        for key, value in (("actor", 43),):
            changed = copy.deepcopy(receipt)
            changed[key] = value
            self.assertEqual(crafting.unlock_actions(observation(), [changed]), [])
        receipt["response"]["result"]["detail"]["definition_count_delta"] = 0
        self.assertEqual(crafting.unlock_actions(observation(), [receipt]), [])

    def test_mixed_supply_adequacy_uses_native_inputs_not_just_recipe_name(self):
        craft = {"recipe_spec": crafting.DEFAULT_RECIPE, "opening": observation(),
                 "supplies": [], "goal_recipe_setup_evidence": {"items": [{
                     "recipe": scene.RECIPE, "character_known": True, "station": None,
                     "inputs": [{"kind": "item", "item": key, "amount": value}
                                for key, value in scene.INPUTS.items()]}]}}
        mixed.resource_adequacy(craft)
        craft["opening"]["inventory"]["items"] = []
        with self.assertRaisesRegex(RuntimeError, "inadequate"):
            mixed.resource_adequacy(craft)


class ExplicitAuthoringTests(unittest.IsolatedAsyncioTestCase):
    async def test_heldout_setup_component_needs_real_consumption_and_retention(self):
        spec = {"key": "apples_stick", "output": "common.items.food.apple_stick",
                "inputs": {"common.items.food.apple": 1, "common.items.crafting_ing.twigs": 1}}
        before, after = observation(), observation()
        before["inventory"]["items"] = [
            {"item": {"id": item, "amount": amount}} for item, amount in spec["inputs"].items()]
        after["inventory"]["items"] = [{"item": {"id": spec["output"], "amount": 1}}]
        detail = {"craft_update_observed": True, "output_definition_id": spec["output"],
                  "output_definition_delta": 1, "inventory_delta": {item: -amount
                                                                       for item, amount in spec["inputs"].items()}}
        admin = Mock(request=AsyncMock(side_effect=[before, after]))
        with patch.object(setup, "learn", new=AsyncMock()), patch.object(
                setup, "gift", new=AsyncMock()), patch.object(
                setup, "action", new=AsyncMock(return_value={"detail": detail})):
            result = await mixed.qualify_food_recipe(admin, spec)
        self.assertFalse(result["capability_claim"])
        admin.request = AsyncMock(side_effect=[before, before])
        with patch.object(setup, "learn", new=AsyncMock()), patch.object(
                setup, "gift", new=AsyncMock()), patch.object(
                setup, "action", new=AsyncMock(return_value={"detail": detail})):
            with self.assertRaisesRegex(RuntimeError, "actual input/output/retention"):
                await mixed.qualify_food_recipe(admin, spec)

    async def test_submission_is_not_confirmation_and_privilege_is_admin_only(self):
        admin = Mock()
        pose = {"position": [1.5, 2.5, 4], "velocity": [0, 0, 0], "on_ground": True,
                "tick": 3, "session_id": "setup", "self_identity": {"uid": 1}}
        terrain = {"support_candidates": [{
            "occupiable": True, "support": {"position": [1, 2, 3], "kind": "Rock", "solid": True}}]}
        async def wait(actor, predicate, reason):
            self.assertIs(actor, admin)
            self.assertTrue(predicate({"character_controls": {"can_build": {"enabled": True}}}))
        with patch.object(setup, "command", new=AsyncMock()) as command, patch.object(
                setup, "wait_observation", side_effect=wait), patch.object(
                setup, "goto", new=AsyncMock()), patch.object(
                setup, "action", new=AsyncMock(return_value={"submitted_to_server": True})) as action, patch.object(
                setup, "ready_observation", new=AsyncMock(return_value=pose)), patch.object(
                setup, "terrain", new=AsyncMock(return_value=terrain)):
            result = await setup.author_voxels(admin, [[1, 2, 3]])
        self.assertEqual(len(result), 1)
        self.assertEqual(action.await_args.args, (admin, "build_block"))
        self.assertEqual(action.await_args.kwargs["position"], [1, 2, 3])
        self.assertEqual([entry.args[1] for entry in command.await_args_list],
                         ["area_add", "permit_build", "build"])
        terrain["support_candidates"][0]["support"]["position"] = [1, 2, 2]
        with patch.object(setup, "command", new=AsyncMock()), patch.object(
                setup, "wait_observation", new=AsyncMock()), patch.object(
                setup, "goto", new=AsyncMock()), patch.object(
                setup, "action", new=AsyncMock(return_value={"submitted_to_server": True})), patch.object(
                setup, "ready_observation", new=AsyncMock(return_value=pose)), patch.object(
                setup, "terrain", new=AsyncMock(return_value=terrain)):
            with self.assertRaisesRegex(RuntimeError, "exact native Rock readback"):
                await setup.author_voxels(admin, [[1, 2, 3]])


if __name__ == "__main__":
    unittest.main()
