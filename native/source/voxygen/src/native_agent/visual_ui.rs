//! Frame-bound ordinary HUD gestures. No public inventory or dialogue lookup.
use client::{Client, agent_protocol::{self as protocol, BridgeState, Request}};
use common::{comp, rtsim, uid::Uid};
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Gesture {
    #[serde(rename = "type")]
    kind: String,
    action_id: String,
    frame_id: String,
    ui_state_id: String,
    expected_session_id: String,
    actuator: Value,
    gesture: String,
    x: Option<f64>,
    y: Option<f64>,
}

struct Frame {
    id: String,
    state: String,
    actuator: Value,
    size: [u32; 2],
}

struct Pending {
    request: Request,
    state: String,
    actuator: Value,
}

#[derive(Default)]
pub struct VisualUi {
    frame: Option<Frame>,
    pending: Option<Pending>,
}

fn state_key(client: &Client, hud: &crate::hud::Hud, talk: Option<specs::Entity>) -> String {
    let state = format!("{:?}|{}|{talk:?}", client.current::<comp::Inventory>(),
                        hud.native_state_key());
    hex::encode(Sha256::digest(state.as_bytes()))
}

fn validate(gesture: &Gesture, frame: &Frame, current_state: &str,
            actuator: &Value) -> Result<(), &'static str> {
    if gesture.kind != "visual_input" || gesture.action_id.is_empty() || gesture.action_id.len() > 128 {
        return Err("invalid_visual_action");
    }
    if gesture.frame_id != frame.id || gesture.ui_state_id != frame.id || frame.state != current_state {
        return Err("stale_visual_frame");
    }
    if gesture.actuator != frame.actuator || gesture.actuator != *actuator {
        return Err("stale_visual_actuator");
    }
    match (gesture.gesture.as_str(), gesture.x, gesture.y) {
        ("hover" | "left_click" | "right_click", Some(x), Some(y))
            if x.is_finite() && y.is_finite() && x >= 0.0 && y >= 0.0
                && x < frame.size[0] as f64 && y < frame.size[1] as f64 => Ok(()),
        ("interact", None, None) => Ok(()),
        _ => Err("unsupported_visual_selector"),
    }
}

impl VisualUi {
    pub fn capture(&mut self, client: &Client, hud: &crate::hud::Hud, talk: Option<specs::Entity>,
                   id: &str, actuator: Value, size: [u32; 2]) -> Value {
        self.frame = Some(Frame { id: id.into(), state: state_key(client, hud, talk), actuator, size });
        json!({
            "schema": "native-visual-ui/1", "ui_state_id": id,
            "coordinates": "PNG pixels, top-left origin; stock native HUD, no overlay",
            "gestures": ["hover", "left_click", "right_click", "interact"],
            "right_click": "native inventory item use/equip",
            "left_click": "native dialogue choice only",
            "interact": "native visible Talk prompt or statement continuation",
            "freshness": "latest frame only, consumed by one gesture; changed UI/actor/owner rejected",
            "unsupported": ["crafting", "trade", "skills", "map targeting", "drag", "unequip"],
        })
    }

    pub fn submit(&mut self, client: &mut Client, hud: &mut crate::hud::Hud, state: &mut BridgeState,
                  request: Request, talk: Option<specs::Entity>) -> Result<(), (Request, &'static str)> {
        let gesture: Gesture = match serde_json::from_value(request.args["action"].clone()) {
            Ok(gesture) => gesture,
            Err(_) => return Err((request, "invalid_visual_action")),
        };
        if gesture.expected_session_id != state.session_id {
            return Err((request, "stale_session"));
        }
        let duplicate = match protocol::visual_duplicate(state, &gesture.action_id, &request.args) {
            Ok(duplicate) => duplicate,
            Err(code) => return Err((request, code)),
        };
        if duplicate {
            if let Some(response) = protocol::visual_replay(state, &request) {
                protocol::emit(&response);
                return Ok(());
            }
            return Err((request, "visual_action_pending_effect_unknown"));
        }
        if self.pending.is_some() { return Err((request, "visual_ui_busy")); }
        let Some(frame) = &self.frame else { return Err((request, "visual_frame_required")); };
        if let Err(code) = validate(&gesture, frame, &state_key(client, hud, talk),
                                    &protocol::actuator_identity(client, state)) {
            return Err((request, code));
        }
        if let Err(code) = protocol::visual_fence(client, state, &request.args) {
            return Err((request, code));
        }
        if gesture.gesture != "interact" && !hud.native_pointer_available() {
            return Err((request, "unsupported_visual_view"));
        }
        if gesture.gesture == "interact" {
            let event = hud.native_statement_ack();
            let action = if let Some(event) = event {
                resolve_event(client, event)
            } else if hud.current_dialogue().is_none() {
                talk.and_then(|entity| client.state().read_storage::<Uid>().get(entity).copied())
                    .and_then(|uid| {
                        let observation = protocol::observation(client, state);
                        let target = observation["nearby_entities"].as_array()?.iter()
                            .find(|row| row["uid"] == json!(uid.0.get()))?;
                        Some(json!({"type":"context_interact", "operation":"talk",
                                    "target":target["control_target"]}))
                    })
            } else { None };
            let Some(mut action) = action else { return Err((request, "no_visible_interact_prompt")); };
            action["action_id"] = json!(gesture.action_id);
            protocol::reserve_visual_submission(state, &request);
            self.frame = None;
            protocol::dispatch_visual_action(client, state, request, action);
        } else {
            let size = frame.size;
            let pending_state = frame.state.clone();
            let pending_actuator = frame.actuator.clone();
            protocol::reserve_visual_submission(state, &request);
            self.frame = None;
            hud.native_pointer(gesture.x.unwrap(), gesture.y.unwrap(), size,
                               &gesture.gesture);
            self.pending = Some(Pending { request, state: pending_state, actuator: pending_actuator });
        }
        Ok(())
    }

    pub fn finish(&mut self, client: &mut Client, hud: &crate::hud::Hud, state: &mut BridgeState,
                  events: &mut Vec<crate::hud::Event>, talk: Option<specs::Entity>) {
        let Some(pending) = self.pending.take() else { events.clear(); return; };
        let request = pending.request;
        if pending.state != state_key(client, hud, talk)
            || pending.actuator != protocol::actuator_identity(client, state)
        {
            events.clear();
            protocol::complete_visual_ui(client, state, request, "failed", "visual_state_changed_before_dispatch");
            return;
        }
        let gesture = request.args["action"]["gesture"].as_str().unwrap().to_owned();
        let mut selected = None;
        for event in events.drain(..) {
            let allowed = matches!((&event, gesture.as_str()),
                (crate::hud::Event::UseSlot { .. }, "right_click")
                | (crate::hud::Event::Dialogue(..), "left_click"));
            if allowed && selected.is_none() { selected = resolve_event(client, event); }
        }
        if let Err(code) = protocol::visual_fence(client, state, &request.args) {
            protocol::complete_visual_ui(client, state, request, "failed", code);
        } else if let Some(mut action) = selected {
            action["action_id"] = request.args["action"]["action_id"].clone();
            protocol::dispatch_visual_action(client, state, request, action);
        } else {
            protocol::complete_visual_ui(client, state, request,
                if gesture == "hover" { "completed" } else { "failed" },
                if gesture == "hover" { "pointer_moved" } else { "unsupported_visible_widget" });
        }
    }
}

fn resolve_event(client: &Client, event: crate::hud::Event) -> Option<Value> {
    match event {
        crate::hud::Event::UseSlot { slot: comp::slot::Slot::Inventory(slot), bypass_dialog: false } => {
            let inventory = client.current::<comp::Inventory>()?;
            inventory.get(slot)?;
            if inventory.free_after_equip(slot) < 0 { return None; }
            Some(json!({"type":"use_item", "slot":slot.idx()}))
        },
        crate::hud::Event::Dialogue(sender, dialogue) => {
            let uid = client.state().read_storage::<Uid>().get(sender).copied()?;
            let mut action = json!({"type":"dialogue_action", "sender_uid":uid.0.get(),
                                    "dialogue_id":dialogue.id});
            match dialogue.kind {
                rtsim::DialogueKind::Ack { tag } => {
                    action["operation"] = json!("ack");
                    action["tag"] = json!(tag);
                },
                rtsim::DialogueKind::Response { tag, response_id, .. } => {
                    action["operation"] = json!("respond");
                    action["tag"] = json!(tag);
                    action["response_id"] = json!(response_id);
                },
                _ => return None,
            }
            Some(action)
        },
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn visual_ids_cannot_replay_another_shared_tool_action() {
        let args = json!({"action":{"type":"visual_input","action_id":"shared"}});
        let mut state = BridgeState::new("s", Default::default());
        state.completed.insert("shared".into(), protocol::CachedAction {
            signature: "ordinary".into(), result: json!({}),
        });
        assert_eq!(protocol::visual_duplicate(&state, "shared", &args), Err("action_id_conflict"));
        state.completed.clear();
        assert_eq!(protocol::visual_duplicate(&state, "shared", &args), Ok(false));
        let request = Request { id: json!("r"), protocol_version: Some("0.2".into()),
                                cmd: "step".into(), args: args.clone() };
        protocol::reserve_visual_submission(&mut state, &request);
        assert_eq!(protocol::visual_duplicate(&state, "shared", &args), Ok(true));
        assert_eq!(protocol::visual_duplicate(&state, "shared", &json!({})), Err("action_id_conflict"));
    }
    #[test]
    fn selectors_are_frame_state_actor_owner_and_pixel_bound() {
        let frame = Frame { id:"f".into(), state:"private".into(),
            actuator:json!({"actor":1,"control":{"generation":2}}), size:[960,540] };
        let mut gesture: Gesture = serde_json::from_value(json!({
            "type":"visual_input","action_id":"a","frame_id":"f","ui_state_id":"f",
            "expected_session_id":"s",
            "actuator":frame.actuator,"gesture":"right_click","x":12,"y":20
        })).unwrap();
        assert!(validate(&gesture, &frame, "private", &frame.actuator).is_ok());
        assert_eq!(validate(&gesture, &frame, "changed", &frame.actuator), Err("stale_visual_frame"));
        assert_eq!(validate(&gesture, &frame, "private", &json!({})), Err("stale_visual_actuator"));
        gesture.x = Some(960.0);
        assert_eq!(validate(&gesture, &frame, "private", &frame.actuator), Err("unsupported_visual_selector"));
        gesture.gesture = "hidden_item".into();
        assert!(validate(&gesture, &frame, "private", &frame.actuator).is_err());
    }
}
