"""Replay the actual renderer transport/ledger boundary, without private state."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts.rpg_benchmark import suite, temporal


@pytest.fixture
def trace():
    return json.loads((Path(__file__).parent / "fixtures" /
                       "renderer-ready-ledger-trace.json").read_text())


def test_real_pre_entry_window_excludes_transport_not_audit(trace, monkeypatch):
    before = deepcopy(trace)
    assert len(trace["events"]) == 21
    assert "sequence_domain" not in trace["events"][0]
    # The actual opening/final cursor is 18; event 19 happened after final.
    assert temporal.event_window(trace["opening"], trace["final"], trace["events"]) == []
    received = []
    monkeypatch.setattr(suite.MODULES["combat"], "score",
                        lambda fixture, final, receipts, events, samples: received.extend(events))
    suite.score({"kind": "combat"}, {"opening": trace["opening"]},
                trace["final"], [], trace["events"], [])
    assert received == []
    assert trace == before  # Raw ready/audit/source envelopes must not be rewritten.


def test_real_ledger_projection_keeps_order_identity_and_exact_source(trace):
    before = deepcopy(trace)
    opening = dict(trace["opening"], tick=0, event_cursor=0)
    final = dict(trace["final"], tick=15477, event_cursor=19)
    selected = temporal.event_window(opening, final, trace["events"])
    assert [event["seq"] for event in selected] == list(range(1, 20))
    assert all(actual is raw for actual, raw in zip(selected, trace["events"][2:]))
    assert selected[0]["data"]["source"] == "server_outcome_health_change"
    assert trace == before


@pytest.mark.parametrize("domain", [
    None,
    {"kind": "server-tick", "session_id": "rpg47f731ee8c"},
    {"kind": "client-update-counter", "session_id": "another-session"},
])
def test_real_ledger_domain_is_checked_even_outside_window(trace, domain):
    event = trace["events"][2]  # seq=1 is outside the actual measured interval.
    if domain is None:
        del event["sequence_domain"]
    else:
        event["sequence_domain"] = domain
    with pytest.raises(ValueError, match="unidentified sequence domain"):
        temporal.event_window(trace["opening"], trace["final"], trace["events"])


@pytest.mark.parametrize("changes", [
    {"seq": True},  # bool must not impersonate the reserved integer marker.
    {"seq": 1},
    {"event": "combat_health_change"},
    {"type": "response"},
    {"sequence_domain": {"kind": "server-tick", "session_id": "rpg47f731ee8c"}},
    {"sequence_domain": None},
])
def test_ready_exception_does_not_hide_invalid_ledger_envelopes(trace, changes):
    trace["events"][0].update(changes)
    with pytest.raises(ValueError, match="unidentified sequence domain"):
        temporal.event_window(trace["opening"], trace["final"], trace["events"])


@pytest.mark.parametrize("seq", [1, 0])
def test_real_ledger_duplicate_or_reversed_order_still_fails(trace, seq):
    trace["events"][3]["seq"] = seq
    with pytest.raises(ValueError, match="not strictly ordered"):
        temporal.event_window(trace["opening"], trace["final"], trace["events"])


def test_actual_post_final_event_cannot_be_admitted_before_its_update(trace):
    # Cursor 19 paired with the real final tick (15476) is inconsistent.
    final = dict(trace["final"], event_cursor=19)
    with pytest.raises(ValueError, match="cursor and client update interval disagree"):
        temporal.event_window(trace["opening"], final, trace["events"])


@pytest.mark.parametrize("changes", [
    {"event_cursor": 17},
    {"tick": 15350},
    {"sequence_domain": {"kind": "server-tick", "session_id": "rpg47f731ee8c"}},
])
def test_actual_window_keeps_comparability_guards(trace, changes):
    with pytest.raises(ValueError, match="incomparable opening/final"):
        temporal.event_window(trace["opening"], dict(trace["final"], **changes), trace["events"])
