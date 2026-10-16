//! Optional observation-bound commit checks for the normal shared executor.
use super::*;

pub(super) fn validate(
    expected: &Value, session: &str, actor: &Value, inventory: &Value, nearby: &[Value],
) -> Result<(), &'static str> {
    let fields = expected.as_object().ok_or("invalid_action_preconditions")?;
    if fields.keys().any(|key| !matches!(key.as_str(), "session_id" | "actor" | "slot" | "inventory" | "target"))
        || expected.get("session_id").and_then(Value::as_str) != Some(session)
    {
        return Err("action_session_changed");
    }
    if expected.get("actor") != Some(actor) || actor.is_null() {
        return Err("action_actor_changed");
    }
    if let Some(slot) = expected.get("slot") {
        let reference = slot.get("reference").and_then(Value::as_object)
            .ok_or("invalid_action_preconditions")?;
        if reference.is_empty() || reference.keys().any(|key|
            !matches!(key.as_str(), "slot" | "loadout_idx" | "slot_idx" | "equip_slot"))
        {
            return Err("invalid_action_preconditions");
        }
        let actual = if let Some(name) = reference.get("equip_slot").and_then(Value::as_str) {
            inventory.get("equipment").and_then(|equipment| equipment.get(name)).cloned()
                .unwrap_or(Value::Null)
        } else {
            inventory.get("items").and_then(Value::as_array).and_then(|items| items.iter().find(|row|
                reference.iter().all(|(key, value)| row.get(key) == Some(value))))
                .and_then(|row| row.get("item")).cloned().unwrap_or(Value::Null)
        };
        if slot.get("item") != Some(&actual) { return Err("action_slot_changed"); }
    }
    if expected.get("inventory").is_some_and(|value| value != inventory) {
        return Err("action_inventory_changed");
    }
    if let Some(target) = expected.get("target") {
        if target.is_null() || !nearby.iter().any(|row| row.get("control_target") == Some(target)) {
            return Err("action_target_changed");
        }
    }
    Ok(())
}

pub(super) fn check(client: &Client, state: &BridgeState, expected: &Value) -> Result<(), &'static str> {
    let inventory = client.current::<comp::Inventory>();
    let nearby = client.position().map_or_else(Vec::new, |position| nearby_entities(client, state, position));
    validate(expected, &state.session_id, &continuous_control::actor_ref(client, state),
             &inventory_json(inventory.as_ref()), &nearby)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn old_actor_slot_amount_and_target_are_rejected_before_dispatch() {
        let actor = json!({"uid": 1, "lifecycle": "a"});
        let target = json!({"uid": 2, "lifecycle": "t"});
        let inventory = json!({"items": [{"slot": 3, "item": {"id": "potion", "amount": 2}}]});
        let expected = json!({"session_id": "s", "actor": actor,
            "slot": {"reference": {"slot": 3}, "item": inventory["items"][0]["item"]},
            "target": target});
        let nearby = vec![json!({"control_target": target})];
        assert!(validate(&expected, "s", &actor, &inventory, &nearby).is_ok());
        assert_eq!(validate(&expected, "s", &json!({"uid": 1, "lifecycle": "new"}), &inventory, &nearby),
                   Err("action_actor_changed"));
        let mut changed = inventory.clone();
        changed["items"][0]["item"]["amount"] = json!(1);
        assert_eq!(validate(&expected, "s", &actor, &changed, &nearby), Err("action_slot_changed"));
        assert_eq!(validate(&expected, "s", &actor, &inventory, &[]), Err("action_target_changed"));
        assert_eq!(validate(&expected, "other", &actor, &inventory, &nearby), Err("action_session_changed"));
        let craft = json!({"session_id": "s", "actor": actor, "inventory": inventory});
        assert_eq!(validate(&craft, "s", &actor, &changed, &nearby), Err("action_inventory_changed"));
    }
}
