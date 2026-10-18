"""Semantic authoring boundary around the unchanged outcome/transaction scorers."""
from . import npc_contracts

LEGACY_TEMPLATES = {"T10": "resident_exchange", "T11": "message_contract", "T12": "slay_contract"}
TEMPLATES = {
    "resident_exchange": {"legacy_case": "T10", "requires": ["movement", "dialogue", "trade"],
                          "runtime": {"resident_fixture": "resident-smoke-v1"}},
    "message_contract": {"legacy_case": "T11", "requires": ["movement", "dialogue", "map"],
                         "runtime": {"small_world": True, "resident_fixture": "outcomes-message-v1"}},
    "slay_contract": {"legacy_case": "T12", "requires": ["movement", "dialogue", "combat", "map"],
                      "runtime": {"small_world": True, "resident_fixture": "outcomes-slay-v1"}},
}
setup = npc_contracts.setup
score = npc_contracts.score
mechanism = npc_contracts.mechanism
for _spec in TEMPLATES.values():
    _spec["mechanism"] = False
