"""Native bootstrap and local footing eligibility precede resident commitment."""
import asyncio
import json

import pytest

from scripts.rpg_benchmark import navigation_tasks as tasks
from .test_resident_placement import ResidentWorld


class EligibleWorld(ResidentWorld):
    def __init__(self, tmp_path):
        self.bootstrap = False
        self.bootstrap_samples = 0
        self.airborne_first = False
        self.no_local = False
        self.after_commit_wrong = False
        self.commands = []
        super().__init__(tmp_path)

    def bind(self):
        entries = [
            {"rtsim_uid": 2203, "uid": self.native_uid, "civilised": True,
             "body": "Humanoid", "alive": True, "on_ground": True,
             "server_position": {"x": 26696 + self.walk_offset, "y": 4790.4, "z": 144}},
            {"rtsim_uid": 103, "uid": 35, "civilised": True,
             "body": "Humanoid", "alive": True, "on_ground": False,
             "server_position": {"x": 26696, "y": 4790.4, "z": 248.545}},
        ]
        if self.no_local:
            entries[0]["server_position"] = {"x": 27000, "y": 4912, "z": 144}
        if self.after_commit_wrong and self.player.process is not None:
            entries[0]["rtsim_uid"] = 9999
        self.entries = entries
        (self.admin.out / "private-native-oracle.ndjson").write_text("".join(
            json.dumps(row) + "\n" for row in [
                {"event": "rtsim_population", "sequence": self.bootstrap_samples, "data": {
                    "clock": "server RTSim tick/time-of-day; not a client-update counter",
                    "rtsim_tick": 32 if self.bootstrap else 31,
                    "settlement_bootstrap": {
                        "last_maintenance_tick": 32 if self.bootstrap else None,
                        "eligible_pending": 0 if self.bootstrap else 1,
                        "allocated_deficit": 0, "ready": self.bootstrap}}},
                {"event": "loaded_actor_bindings", "data": {"bindings": entries}},
            ]))

    def observe(self, actor):
        self.bootstrap_samples += 1
        if self.bootstrap_samples >= 3:
            self.bootstrap = True
        self.bind()
        return super().observe(actor)

    async def command(self, admin, name, *args):
        self.commands.append((name, args))
        if name == "site":
            assert self.bootstrap, "resident-dependent destination loaded before actual native bootstrap"
        if name == "rtsim_npc":
            assert args == ("civilised,humanoid", 1), "retain broad nearest count1 before eligibility"
            uid = 103 if self.airborne_first else 2203
            admin.native_events.append({"event": "chat", "data": {
                "kind": "command_info", "text": f"-- NPCs matching [{uid}] -- name ({uid}),"}})
            return {}
        return await super().command(admin, name, *args)


def test_bootstrap_precedes_loading_resident_destination_then_original_puzzle(tmp_path, monkeypatch):
    world = EligibleWorld(tmp_path)
    world.install(monkeypatch)
    fixture = asyncio.run(tasks.setup_facility(world.admin, world.player, {"task": "original puzzle"}))
    proof = fixture["native_population_readiness"]["data"]["settlement_bootstrap"]
    assert proof["last_maintenance_tick"] == 32 and proof["eligible_pending"] == 0
    assert fixture["goal"] == tasks.FACILITY["goal"]
    assert fixture["native_resident_discovery"]["rtsim_uid"] == 2203
    assert len(world.references) == 1


def test_airborne_first_candidate_is_not_committed_or_teleported_before_eligibility(tmp_path, monkeypatch):
    world = EligibleWorld(tmp_path)
    world.airborne_first = True
    world.walk_on_load = True
    world.install(monkeypatch)
    fixture = asyncio.run(tasks.setup_facility(world.admin, world.player, {"task": "original puzzle"}))
    assert fixture["native_resident_discovery"]["first_candidate_rtsim_uid"] == 103
    assert fixture["native_resident_discovery"]["rtsim_uid"] == 2203
    assert fixture["native_resident_discovery"]["eligibility"]["binding"]["on_ground"] is True
    assert not any(name == "rtsim_tp" and args == (103,) for name, args in world.commands)
    assert fixture["resident"]["uid"] == 78
    assert fixture["resident_placements"][0]["rtsim_uid"] == 2203
    assert fixture["resident_placements"][1]["rtsim_uid"] == 2203


def test_no_eligible_local_resident_is_explicit_not_global_substitution(tmp_path, monkeypatch):
    world = EligibleWorld(tmp_path)
    world.airborne_first = world.no_local = True
    world.install(monkeypatch)
    with pytest.raises(RuntimeError, match="grounded local resident eligibility"):
        asyncio.run(tasks.setup_facility(world.admin, world.player, {"task": "original puzzle"}))
    assert not world.references and world.player.process is None
    assert not any(name == "rtsim_tp" for name, _ in world.commands)


def test_identity_after_eligibility_is_not_replaced(tmp_path, monkeypatch):
    world = EligibleWorld(tmp_path)
    world.after_commit_wrong = True
    world.install(monkeypatch)
    with pytest.raises(RuntimeError, match="current existing native resident binding"):
        asyncio.run(tasks.setup_facility(world.admin, world.player, {"task": "original puzzle"}))
    assert len(world.references) == 1
    assert all(entry["uid"] == 54 for entry in world.references)


@pytest.mark.parametrize("metadata", [
    {"profession": "Adventurer(2)", "home": None, "action": "travel_to_site"},
    {"profession": "Merchant", "home": "a different settlement", "action": "travel_to_site"},
    {"profession": "Captain", "home": None, "riding": None, "action": "walking"},
])
def test_valid_local_moving_and_home_alternatives_are_not_filtered(tmp_path, monkeypatch, metadata):
    world = EligibleWorld(tmp_path)
    world.install(monkeypatch)
    original = world.bind

    def bind():
        original()
        path = world.admin.out / "private-native-oracle.ndjson"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[-1]["data"]["bindings"][0].update(metadata, velocity=[3, 1, 0])
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    world.bind = bind
    fixture = asyncio.run(tasks.setup_facility(world.admin, world.player, {"task": "original puzzle"}))
    assert fixture["native_resident_discovery"]["rtsim_uid"] == 2203
    assert len(world.references) == 1


@pytest.mark.parametrize("change", [
    {"ready": False}, {"last_maintenance_tick": None}, {"eligible_pending": 1},
    {"allocated_deficit": 1},
])
def test_incomplete_native_bootstrap_cannot_load_destination(tmp_path, monkeypatch, change):
    world = EligibleWorld(tmp_path)
    world.install(monkeypatch)
    original = world.bind

    def bind():
        original()
        path = world.admin.out / "private-native-oracle.ndjson"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[0]["data"]["settlement_bootstrap"].update(change)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    world.bind = bind
    with pytest.raises(RuntimeError, match="settlement population bootstrap"):
        asyncio.run(tasks.setup_facility(world.admin, world.player, {"task": "original puzzle"}))
    assert not world.commands and not world.references


@pytest.mark.parametrize("change", [
    {"civilised": False}, {"body": "BirdLarge"}, {"alive": False}, {"on_ground": None},
    {"rtsim_uid": None}, {"server_position": None},
    {"server_position": [26696,4790.4,float("nan")]},
    {"server_position": {"x": 26696, "y": 4790.4, "z": float("nan")}},
    {"server_position": [26696,4790.4,144]}, {"server_position": {"x": 26696, "y": 4790.4}},
])
def test_precommit_eligibility_rejects_invalid_native_bindings_without_teleport(tmp_path, monkeypatch, change):
    world = EligibleWorld(tmp_path)
    world.install(monkeypatch)
    original = world.bind

    def bind():
        original()
        path = world.admin.out / "private-native-oracle.ndjson"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[-1]["data"]["bindings"][0].update(change)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    world.bind = bind
    with pytest.raises(RuntimeError, match="grounded local resident eligibility"):
        asyncio.run(tasks.setup_facility(world.admin, world.player, {"task": "original puzzle"}))
    assert not world.references and not any(name == "rtsim_tp" for name, _ in world.commands)


def test_foreign_bootstrap_clock_is_not_compared_to_client_observations(tmp_path, monkeypatch):
    world = EligibleWorld(tmp_path)
    world.install(monkeypatch)
    original = world.bind

    def bind():
        original()
        path = world.admin.out / "private-native-oracle.ndjson"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[0]["data"]["clock"] = "client-update-counter"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    world.bind = bind
    with pytest.raises(RuntimeError, match="settlement population bootstrap"):
        asyncio.run(tasks.setup_facility(world.admin, world.player, {"task": "original puzzle"}))
    assert not world.commands
