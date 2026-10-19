"""B3 loadout regression checks; all native effects are mocked."""
import copy
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from scripts.rpg_benchmark import combat, combat_support, suite

STARTER = "common.items.weapons.sword.starter"
FROST = "common.items.weapons.sword.frost-1"
POTION = "common.items.consumable.potion_minor"


def approved_b3_loadout(original):
    """Apply only the approved delta to an unchanged historical scene copy."""
    expected = copy.deepcopy(original)
    if original["id"] == "B3":
        assert "mainhand" not in original
        assert original["consumables"] == {POTION: 2}
        assert original["loadout_provenance"] == (
            "exact-body derivative of common.entity.wild.aggressive.ogre; native FromBody Male Ogre "
            "selects common.items.npc_weapons.hammer.ogre_hammer before NPC construction")
        expected["mainhand"] = FROST
        expected["consumables"] = {POTION: 3}
        expected["loadout_provenance"] += "; player equipped with " + FROST
    return expected


def catalog():
    return suite.load(Path(suite.__file__).with_name("scenes.json"))


def item(name, amount=1):
    return {"id": name, "amount": amount, "durability": {"lost": 0}}


class B3LoadoutTests(unittest.IsolatedAsyncioTestCase):
    def test_preservation_delta_keeps_historical_fixtures_and_other_fields_strict(self):
        import json
        path = Path(__file__).with_name("fixtures") / "release041-scenes.json"
        historical = json.loads(path.read_text())["scenes"]
        unchanged = copy.deepcopy(historical)
        for original in historical:
            expected = approved_b3_loadout(original)
            self.assertEqual(original, unchanged[historical.index(original)])
            self.assertIsNot(expected, original)
            if original["id"] != "B3":
                self.assertEqual(expected, original)
                continue
            self.assertEqual({key: value for key, value in expected.items()
                              if key not in {"mainhand", "consumables", "loadout_provenance"}},
                             {key: value for key, value in original.items()
                              if key not in {"consumables", "loadout_provenance"}})
            for mutation in ({"mainhand": FROST}, {"consumables": {POTION: 3}},
                             {"loadout_provenance": "changed historical provenance"}):
                with self.subTest(mutation=mutation), self.assertRaises(AssertionError):
                    approved_b3_loadout({**original, **mutation})

    def test_catalog_changes_only_b3_loadout(self):
        definitions = catalog()
        self.assertEqual(definitions["B3"]["mainhand"], FROST)
        self.assertEqual(definitions["B3"]["consumables"], {POTION: 3})
        for task in ("B1", "B2"):
            self.assertNotIn("mainhand", definitions[task])
            self.assertEqual(definitions[task]["consumables"], {POTION: 2})

    def test_optional_mainhand_validation(self):
        definition = catalog()["B3"]
        for mainhand in (None, True, 1, [], {}, "", "common.entity.ogre",
                         "common.items.consumable.potion_minor"):
            with self.subTest(mainhand=mainhand), self.assertRaisesRegex(ValueError, "mainhand"):
                combat.validate_definition({**definition, "mainhand": mainhand})
        for mainhand in (STARTER, FROST):
            combat.validate_definition({**definition, "mainhand": mainhand})
        definition.pop("mainhand")
        combat.validate_definition(definition)

    async def setup_case(self, definition, *, opening=None):
        mainhand = definition.get("mainhand", STARTER)
        equipment = {**definition["player_equipment"], "active_mainhand": mainhand}
        target = {"uid": 2, "body_identity": definition["body"],
                  "health": {"current": 320, "dead": False}}
        if opening is None:
            opening = {
                "survival": {"health": {"current": 100}, "buffs": [],
                             "equipment": {key: item(name) for key, name in equipment.items()}},
                "skill_tree": {"groups": [{"available_points": 0, "unlockable_now": []}]},
                "inventory": {"items": [{"item": item(name, amount)}
                                        for name, amount in definition["consumables"].items()]},
                "nearby_entities": [target],
            }
        actor = Mock(request=AsyncMock(side_effect=[{"nearby_entities": []}, opening]))
        admin, order, cleanups = Mock(), [], []

        async def clean(*args):
            order.append("clean")
            result = {"removed": []}
            cleanups.append(result)
            return result

        async def site(*args):
            order.append("site")
            return {"start": [0, 0, 0]}

        async def equip(*args):
            order.append("equip")
            self.assertEqual(args, (admin, actor, mainhand))

        async def gift(*args):
            order.append("gift")
            self.assertEqual(args, (admin, actor, POTION, definition["consumables"][POTION]))

        async def wait(actor, predicate, reason):
            row = {"nearby_entities": [target]}
            self.assertTrue(predicate(row))
            self.assertFalse(predicate({"nearby_entities": []}))
            return row

        with ExitStack() as stack:
            stack.enter_context(patch.object(combat.native, "isolate_initial_spawn", new=AsyncMock()))
            stack.enter_context(patch.object(combat.native, "clean_inventory", side_effect=clean))
            stack.enter_context(patch.object(combat.native, "site", side_effect=site))
            stack.enter_context(patch.object(combat_support, "equip", side_effect=equip))
            stack.enter_context(patch.object(combat.native, "gift", side_effect=gift))
            stack.enter_context(patch.object(combat.native, "goto", new=AsyncMock()))
            stack.enter_context(patch.object(combat.native, "command", new=AsyncMock()))
            stack.enter_context(patch.object(combat.native, "wait_observation", side_effect=wait))
            fixture = await combat.setup(admin, actor, definition, "flat")
        return fixture, order, cleanups

    async def test_upgrade_equips_then_cleans_before_gifting_potions(self):
        definition = catalog()["B3"]
        original_equipment = copy.deepcopy(definition["player_equipment"])
        fixture, order, cleanups = await self.setup_case(definition)
        self.assertEqual(order, ["clean", "site", "equip", "clean", "gift"])
        self.assertIs(fixture["cleanup_supplies"], cleanups[0])
        self.assertIs(cleanups[0]["replaced_weapon_inventory"], cleanups[1])
        self.assertEqual(definition["player_equipment"], original_equipment)

    async def test_legacy_and_explicit_starter_do_not_replace_weapon_or_clean_twice(self):
        for task in ("B1", "B2"):
            for explicit in (False, True):
                with self.subTest(task=task, explicit=explicit):
                    definition = catalog()[task]
                    if explicit:
                        definition["mainhand"] = STARTER
                    _, order, cleanups = await self.setup_case(definition)
                    self.assertEqual(order, ["clean", "site", "gift"])
                    self.assertNotIn("replaced_weapon_inventory", cleanups[0])

    async def test_opening_gates_still_fail_closed(self):
        definition = catalog()["B3"]
        fixture, _, _ = await self.setup_case(definition)
        for problem, message in (
                ("weapon", "declared mainhand"),
                ("potions", "consumable inventory"),
                ("leftover-starter", "consumable inventory"),
                ("armor", "equipped loadout"),
                ("buffs", "residual setup buffs"),
                ("skills", "fixed cleared skill"),
                ("health", "fixed 100 HP"),
                ("durability", "fully repaired")):
            row = copy.deepcopy(fixture["opening"])
            if problem == "weapon":
                row["survival"]["equipment"]["active_mainhand"] = item(STARTER)
            elif problem == "potions":
                row["inventory"]["items"][0]["item"]["amount"] = 2
            elif problem == "leftover-starter":
                row["inventory"]["items"].append({"item": item(STARTER)})
            elif problem == "armor":
                row["survival"]["equipment"].pop("chest")
            elif problem == "buffs":
                row["survival"]["buffs"] = ["Regeneration"]
            elif problem == "skills":
                row["skill_tree"]["groups"][0]["available_points"] = 1
            elif problem == "health":
                row["survival"]["health"]["current"] = 99
            else:
                row["survival"]["equipment"]["active_mainhand"]["durability"]["lost"] = 1
            with self.subTest(problem=problem), self.assertRaisesRegex(RuntimeError, message):
                await self.setup_case(definition, opening=row)

    async def test_equip_helper_uses_native_slot_and_waits_for_declared_weapon(self):
        for existing in (STARTER, FROST):
            with self.subTest(existing=existing):
                opening = {"survival": {"equipment": {"active_mainhand": item(existing)}}}
                actor = Mock(request=AsyncMock(return_value=opening))
                admin = Mock()
                bag = {"inventory": {"items": [{"slot": 7, "item": item(FROST)}]}}

                async def wait(actor, predicate, reason):
                    self.assertFalse(predicate({"survival": {"equipment": {
                        "active_mainhand": item(STARTER)}}}))
                    self.assertTrue(predicate({"survival": {"equipment": {
                        "active_mainhand": item(FROST)}}}))

                with patch.object(combat_support.native, "gift", new=AsyncMock(return_value=bag)) as gift, \
                        patch.object(combat_support.native, "action", new=AsyncMock()) as action, \
                        patch.object(combat_support.native, "wait_observation", side_effect=wait) as waited:
                    await combat_support.equip(admin, actor, FROST)
                if existing == STARTER:
                    gift.assert_awaited_once_with(admin, actor, FROST, 1)
                    action.assert_awaited_once_with(actor, "equip", equip_slot="active_mainhand", slot=7)
                else:
                    gift.assert_not_awaited()
                    action.assert_not_awaited()
                waited.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
