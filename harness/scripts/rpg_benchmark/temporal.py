"""Private scorer-boundary sequence contracts; no render-loop synchronization."""


def event_window(opening, final, events):
    domain = opening.get("sequence_domain")
    if domain is None:
        # Historical headless observations retain their original inclusive tick
        # predicates. Do not reinterpret private server journal sequences.
        return events
    if (domain != {"kind": "client-update-counter", "session_id": opening["session_id"]}
            or final.get("sequence_domain") != domain
            or final["tick"] < opening["tick"]
            or type(opening.get("event_cursor")) is not int
            or type(final.get("event_cursor")) is not int
            or final["event_cursor"] < opening["event_cursor"]):
        raise ValueError("incomparable opening/final native sequence domains")
    selected = []
    previous = None
    for row in events:
        if row.get("session_id") != opening["session_id"]:
            continue
        # ready_event() reserves integer seq=0 for transport startup and has no
        # ledger domain. Keep its raw audit envelope, but never treat that marker
        # as a record_event() sequence or manufacture a domain for it. A genuine
        # domain-bearing event (including a malformed "ready") still validates.
        if (row.get("type") == "event" and row.get("event") == "ready"
                and type(row.get("seq")) is int and row["seq"] == 0
                and "sequence_domain" not in row):
            continue
        # Renderer frame/transport notices without seq are not ledger records.
        if "seq" not in row:
            continue
        if row.get("sequence_domain") != domain or type(row["seq"]) is not int:
            raise ValueError("native event has an unidentified sequence domain")
        if previous is not None and row["seq"] <= previous:
            raise ValueError("native event sequence is not strictly ordered")
        previous = row["seq"]
        if opening["event_cursor"] < row["seq"] <= final["event_cursor"]:
            if not opening["tick"] <= row["tick"] <= final["tick"]:
                raise ValueError("native event cursor and client update interval disagree")
            selected.append(row)
    return selected


def append_sample(samples, final):
    """Each received renderer observation is distinct, even at the same update.

    A host-received sample sequence identifies observations, not native physics
    updates or effect ordering. Legacy samples retain their original tick gate.
    """
    identity = final.get("sample_identity")
    if identity is not None:
        previous = samples[-1].get("sample_identity")
        if (identity["kind"] != "host-received-observation-sequence"
                or previous is None or identity["session_id"] != previous["session_id"]
                or identity["sequence"] <= previous["sequence"]):
            raise ValueError("renderer observation sample sequence is not comparable")
        samples.append(final)
    elif final["tick"] > samples[-1]["tick"]:
        samples.append(final)
