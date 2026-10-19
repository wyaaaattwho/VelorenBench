"""Read-only key/manifest verification; not evidence of native gameplay."""
import copy
import hashlib
import json
from pathlib import Path
import unittest

from scripts.rpg_benchmark import combat, qualification, suite, task_templates


class B3QualificationTests(unittest.TestCase):
    def setUp(self):
        self.definitions = suite.load(Path(suite.__file__).with_name("scenes.json"))
        self.document = qualification.load()

    def test_all_29_tasks_have_default_keys_for_each_terrain_and_mode(self):
        self.assertEqual(len(self.definitions), 29)
        for definition in self.definitions.values():
            dependencies = suite.setup_dependencies(definition, self.definitions)
            for terrain in definition["terrains"]:
                for mode in ("structured", "hybrid", "image"):
                    with self.subTest(task=definition["id"], terrain=terrain, mode=mode):
                        profile = ("visual-ui-v1" if mode == "image"
                                   else "ordinary-renderer-native-v1")
                        native = self.document["records"][0]["key"]["native_identity"]
                        key = qualification.key(definition, terrain, "renderer", mode,
                                                profile, native, dependencies)
                        self.assertIsNotNone(qualification.matching(key, self.document))

    def test_all_125_records_match_current_code_content_and_dependencies(self):
        self.assertEqual(len(self.document["records"]), 125)
        code = qualification.code_identity()
        for record in self.document["records"]:
            key = record["key"]
            with self.subTest(task=key["task_id"], terrain=key["terrain"],
                              mode=key["observation_mode"], profile=key["action_profile"]):
                self.assertEqual(key["harness_code_sha256"], code)
                definition = self.definitions[key["task_id"]]
                candidates = []
                for variant in definition.get("variants", [None]):
                    configured = copy.deepcopy(definition)
                    suite.runtime_options(configured, renderer=True, variant=variant)
                    candidates.append(configured)
                configured = next(row for row in candidates
                                  if task_templates.identity(row) == key["task_content_sha256"])
                expected = qualification.key(
                    configured, key["terrain"], key["backend"], key["observation_mode"],
                    key["action_profile"], key["native_identity"],
                    suite.setup_dependencies(configured, self.definitions))
                self.assertEqual(key, expected)

    def test_single_creature_accepts_optional_mainhand_without_changing_defaults(self):
        self.assertEqual(combat.PARAMETERS, ("mainhand",))
        for task in ("B1", "B2", "B3"):
            definition = copy.deepcopy(self.definitions[task])
            definition["mainhand"] = "common.items.weapons.sword.frost-1"
            configured = task_templates.configure(combat, definition)
            self.assertEqual(configured["mainhand"], definition["mainhand"])
            definition["mainhand"] = "common.items.consumable.potion_minor"
            with self.assertRaisesRegex(ValueError, "mainhand"):
                task_templates.configure(combat, definition)
        dependencies = suite.setup_dependencies(self.definitions["X1"], self.definitions)
        self.assertEqual(dependencies["encounter"]["id"], "B1")

    def test_historical_qualification_retains_its_schema_and_hash(self):
        path = Path(qualification.__file__).with_name("qualification.json")
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                         "913bbd1c02bd60f967ea55e4720757dd9006e5530c2a9e6fcb846f09b6666430")
        historical = json.loads(path.read_text())
        self.assertEqual(historical["schema"], "rpg-release-qualification/1")
        self.assertNotIn("records", historical)
        with self.assertRaisesRegex(ValueError, "invalid qualification document"):
            qualification.validate(historical)


if __name__ == "__main__":
    unittest.main()
