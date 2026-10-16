//! Model-selected downed-to-dead request. Sending is never completion.
use serde_json::{Map, Value, json};

fn bound(snapshot: &Value) -> bool {
    snapshot["session_id"].as_str().is_some_and(|s| !s.is_empty())
        && snapshot["self_identity"]["uid"].as_u64().is_some_and(|uid| uid > 0)
        && snapshot["self_identity"]["entity_id"].as_u64().is_some()
        && snapshot["tick"].as_u64().is_some()
}

pub(super) fn begin(
    action: &Map<String, Value>, before: &Value, send: impl FnOnce(),
) -> Result<(), &'static str> {
    if action.keys().any(|key| !matches!(key.as_str(), "type" | "action_id")) {
        return Err("give_up does not accept parameters");
    }
    if !bound(before) {
        return Err("give_up requires current self identity and session");
    }
    // Match Client::give_up's native health + Crawl prerequisite, not HP/prose.
    if before["health"]["dead"] != false || before["health"]["downed"] != true
        || before["character_state"] != "Crawl"
    {
        return Err("give_up requires an observed alive downed character in Crawl");
    }
    send();
    Ok(())
}

pub(super) fn outcome(before: &Value, after: &Value, expired: bool) -> Option<(&'static str, Value)> {
    let failure = |status, code| Some((status, json!({
        "code": code, "sent": true, "observed_dead": null,
        "effect_outcome": "unknown", "recovery": "not_established",
    })));
    if !bound(before) || !bound(after) {
        return failure("failed", "give_up_actor_unavailable_or_removed");
    }
    if before["session_id"] != after["session_id"] || before["self_identity"] != after["self_identity"] {
        return failure("failed", "give_up_identity_changed");
    }
    if after["tick"].as_u64() > before["tick"].as_u64() {
        if after["health"]["dead"] == true {
            return Some(("completed", json!({
                "code": "give_up_observed_dead", "sent": true, "observed_dead": true,
                "transition": "same_actor_downed_to_dead", "recovery": "not_respawned",
                "causality": "observed_after_request_not_exclusive_cause",
            })));
        }
        if after["health"]["dead"] == false && after["health"]["downed"] == false {
            return failure("failed", "give_up_no_longer_downed");
        }
    }
    if expired {
        failure("timeout", "give_up_death_not_observed")
    } else {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample(tick: u64) -> Value {
        json!({"session_id": "session", "tick": tick,
            "self_identity": {"uid": 3, "entity_id": 1, "native_character_id": 17},
            "health": {"dead": false, "downed": true}, "character_state": "Crawl"})
    }

    #[test]
    fn parser_and_native_precondition_never_send_on_invalid_input() {
        let action = json!({"type": "give_up", "action_id": "g1"});
        let mut sent = 0;
        assert!(begin(action.as_object().unwrap(), &sample(1), || sent += 1).is_ok());
        assert_eq!(sent, 1);
        for key in ["force", "uid", "timeout", "duration_ticks", "dead"] {
            let mut bad = action.clone();
            bad[key] = Value::Null;
            assert!(begin(bad.as_object().unwrap(), &sample(1), || panic!("sent")).is_err());
        }
        for (path, value) in [
            ("/health/dead", json!(true)), ("/health/downed", json!(false)),
            ("/health/downed", json!("true")), ("/character_state", json!("Idle")),
            ("/session_id", Value::Null), ("/self_identity/uid", Value::Null),
        ] {
            let mut bad = sample(1);
            *bad.pointer_mut(path).unwrap() = value;
            assert!(begin(action.as_object().unwrap(), &bad, || panic!("sent")).is_err());
        }
    }

    #[test]
    fn lifecycle_requires_fresh_same_actor_death_not_send_or_crawl() {
        let before = sample(1);
        assert!(outcome(&before, &sample(2), false).is_none());
        let mut dead = sample(2);
        dead["health"]["dead"] = json!(true);
        dead["health"]["downed"] = json!(false);
        let (status, detail) = outcome(&before, &dead, false).unwrap();
        assert_eq!(status, "completed");
        assert_eq!(detail["observed_dead"], true);
        dead["tick"] = json!(1);
        assert!(outcome(&before, &dead, false).is_none());
        assert_eq!(outcome(&before, &dead, true).unwrap().0, "timeout");
    }

    #[test]
    fn revive_timeout_actor_removal_and_identity_changes_are_not_success() {
        let before = sample(1);
        assert_eq!(outcome(&before, &sample(2), true).unwrap().0, "timeout");
        let mut revived = sample(2);
        revived["health"]["downed"] = json!(false);
        assert_eq!(outcome(&before, &revived, false).unwrap().1["code"], "give_up_no_longer_downed");
        for path in ["/self_identity/uid", "/self_identity/native_character_id", "/session_id"] {
            let mut changed = sample(2);
            *changed.pointer_mut(path).unwrap() = json!(99);
            changed["health"]["dead"] = json!(true);
            assert_eq!(outcome(&before, &changed, false).unwrap().0, "failed");
        }
        assert_eq!(outcome(&before, &Value::Null, false).unwrap().1["code"],
                   "give_up_actor_unavailable_or_removed");
    }
}
