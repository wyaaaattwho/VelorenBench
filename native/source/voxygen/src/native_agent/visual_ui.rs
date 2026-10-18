//! Frame-bound ordinary HUD gestures. No public inventory or dialogue lookup.
use client::{Client, agent_protocol::{self as protocol, BridgeState, Request}};
use common::{comp, rtsim, uid::Uid};
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use crate::session::interactable::{Interactable, Interactables, EntityInteraction, BlockInteraction};
use crate::game_input::GameInput;
use common::comp::slot::{Slot, EquipSlot};

pub const GESTURES: &[&str] = &[
    "hover", "left_click", "right_click", "middle_click", "interact", "drag",
    "scroll_up", "scroll_down", "context_trade",
];

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
    to_x: Option<f64>,
    to_y: Option<f64>,
    #[serde(default)]
    modifiers: Vec<String>,
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
    dialogue: String,
    prompt: Option<u64>,
}

#[derive(Default)]
pub struct VisualUi {
    frame: Option<Frame>,
    pending: Option<Pending>,
}

fn gameplay_key(client: &Client, context: &Interactables) -> String {
    format!("{:?}|{:?}|{:?}|{:?}|{:?}|{:?}", client.current::<comp::Inventory>(),
            client.current::<comp::SkillSet>(), client.pending_trade().as_ref().map(|(id,trade,_)| (id,trade)),
            client.invite(), context.input_map.get(&GameInput::Interact).map(|(_, target)| target),
            context.input_map.get(&GameInput::Trade).map(|(_, target)| target))
}

fn state_key(client: &Client, hud: &crate::hud::Hud, context: &Interactables) -> String {
    let state = format!("{}|{}", gameplay_key(client, context), hud.native_state_key());
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
    gesture.modifier_key()?;
    let in_bounds = |x: f64, y: f64| x.is_finite() && y.is_finite() && x >= 0.0 && y >= 0.0
        && x < frame.size[0] as f64 && y < frame.size[1] as f64;
    if gesture.gesture == "drag" {
        return match (gesture.x, gesture.y, gesture.to_x, gesture.to_y) {
            (Some(x), Some(y), Some(to_x), Some(to_y)) if in_bounds(x,y) && in_bounds(to_x,to_y) => Ok(()),
            _ => Err("unsupported_visual_selector"),
        };
    }
    if gesture.to_x.is_some() || gesture.to_y.is_some() {
        return Err("unsupported_visual_selector");
    }
    match (gesture.gesture.as_str(), gesture.x, gesture.y) {
        ("hover" | "left_click" | "right_click" | "middle_click" | "scroll_up" | "scroll_down", Some(x), Some(y))
            if x.is_finite() && y.is_finite() && x >= 0.0 && y >= 0.0
                && x < frame.size[0] as f64 && y < frame.size[1] as f64 => Ok(()),
        ("interact" | "context_trade", None, None) => Ok(()),
        _ => Err("unsupported_visual_selector"),
    }
}

impl Gesture {
    fn modifier_key(&self) -> Result<Option<conrod_core::input::Key>, &'static str> {
        use conrod_core::input::Key;
        match self.modifiers.as_slice() {
            [] => Ok(None),
            [modifier] if self.gesture == "left_click" => match modifier.as_str() {
                "shift" => Ok(Some(Key::LShift)),
                "ctrl" => Ok(Some(Key::LCtrl)),
                _ => Err("unsupported_visual_modifier"),
            },
            _ => Err("unsupported_visual_modifier"),
        }
    }
}

/// Static implemented controls, shared by startup negotiation and frame metadata.
pub(super) fn capabilities() -> Value {
    json!({
        "schema": "native-visual-ui/1", "action_profile": "visual-ui-v1",
        "gestures": GESTURES,
        "modifiers": {"left_click": ["shift", "ctrl"]},
        "modifier_scope": "none or one; stock HUD individual/automatic quantity; released within gesture",
    })
}

pub(super) fn frame_metadata(id: &str) -> Value {
    let mut metadata = capabilities();
    metadata.as_object_mut().unwrap().extend(json!({
        "ui_state_id": id,
        "coordinates": "PNG pixels, top-left origin; stock native HUD, no overlay",
        "right_click": "native inventory item use/equip",
        "middle_click": "ordinary native HUD middle-button hit test, including map waypoint binding",
        "left_click": "ordinary native HUD hit test; no extracted widget data",
        "interact": "native visible context prompt or statement continuation",
        "freshness": "latest frame only, consumed by one gesture; changed UI/actor/owner rejected",
        "qualification": "implementation only; native task/profile qualification is separate",
    }).as_object().unwrap().clone());
    metadata
}

impl VisualUi {
    pub fn capture(&mut self, client: &Client, hud: &crate::hud::Hud, talk: &Interactables,
                   id: &str, actuator: Value, size: [u32; 2]) -> Value {
        self.frame = Some(Frame { id: id.into(), state: state_key(client, hud, talk), actuator, size });
        frame_metadata(id)
    }

    pub fn submit(&mut self, client: &mut Client, hud: &mut crate::hud::Hud, state: &mut BridgeState,
                  request: Request, talk: &Interactables) -> Result<(), (Request, &'static str)> {
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
        if !view_supported(&gesture.gesture, hud.native_pointer_available(), hud.native_prompt_id()) {
            return Err((request, "unsupported_visual_view"));
        }
        if matches!(gesture.gesture.as_str(), "interact" | "context_trade") {
            let trading = gesture.gesture == "context_trade";
            let visible = talk.input_map.get(&if trading { GameInput::Trade } else { GameInput::Interact })
                .map(|(_, target)| target);
            let event = if trading { None } else { hud.native_statement_ack() };
            let action = if let Some(event) = event {
                resolve_event(|| &*client, event)
            } else if hud.current_dialogue().is_none() {
                if let Some(Interactable::Block { block, volume_pos, interaction: BlockInteraction::Craft(tab) }) = visible {
                    if let Some(sprite) = block.get_sprite() {
                        // Open the actual station selected by the ordinary on-screen prompt.
                        hud.native_crafting_prompt(*tab, (*volume_pos, sprite));
                        protocol::reserve_visual_submission(state, &request);
                        self.frame = None;
                        protocol::complete_visual_ui(client, state, request, "completed", "visible_station_opened");
                        return Ok(());
                    }
                }
                resolve_context(|| &*client, state, visible)
            } else { None };
            let Some(mut action) = action else { return Err((request, "no_visible_interact_prompt")); };
            action["action_id"] = json!(gesture.action_id);
            protocol::reserve_visual_submission(state, &request);
            self.frame = None;
            protocol::dispatch_visual_action(client, state, request, action);
        } else {
            let size = frame.size;
            let pending_state = gameplay_key(client, talk);
            let pending_actuator = frame.actuator.clone();
            protocol::reserve_visual_submission(state, &request);
            self.frame = None;
            hud.native_pointer(gesture.x.unwrap(), gesture.y.unwrap(), size,
                               &gesture.gesture,
                               gesture.to_x.zip(gesture.to_y).map(|(x,y)| [x,y]),
                               gesture.modifier_key().expect("validated visual modifier"));
            self.pending = Some(Pending { request, state: pending_state, actuator: pending_actuator,
                                          dialogue: hud.native_dialogue_key(), prompt: hud.native_prompt_id() });
        }
        Ok(())
    }

    pub fn finish(&mut self, client: &mut Client, hud: &crate::hud::Hud, state: &mut BridgeState,
                  events: &mut Vec<crate::hud::Event>, talk: &Interactables) {
        let Some(pending) = self.pending.take() else { events.clear(); return; };
        let request = pending.request;
        if pending.state != gameplay_key(client, talk)
            || pending.dialogue != hud.native_dialogue_key()
            || pending.actuator != protocol::actuator_identity(client, state)
            || !prompt_dispatch_valid(pending.prompt, hud.native_prompt_id(), events)
        {
            events.clear();
            protocol::complete_visual_ui(client, state, request, "failed", "visual_state_changed_before_dispatch");
            return;
        }
        if let Err(code) = protocol::visual_fence(client, state, &request.args) {
            events.clear();
            protocol::complete_visual_ui(client, state, request, "failed", code);
            return;
        }
        let mut selected = None;
        let mut local = Vec::new();
        for event in routed_events(pending.prompt, events) {
            // UI-only native state changes remain with the ordinary session.
            if matches!(&event, crate::hud::Event::SettingsChange(..) | crate::hud::Event::SelectExpBar(..)) {
                local.push(event);
            } else if selected.is_none() {
                selected = resolve_event(|| &*client, event.clone());
                if selected.is_none() && pending.prompt.is_some() {
                    // Confirmations may carry stock events not represented by
                    // the private canonical action vocabulary (e.g. inventory
                    // drop warnings). Keep those on the ordinary session path,
                    // only after all original fences and modal origin checks.
                    local.push(event);
                }
            }
        }
        events.extend(local);
        if let Some(mut action) = selected {
            action["action_id"] = request.args["action"]["action_id"].clone();
            protocol::dispatch_visual_action(client, state, request, action);
        } else {
            // A UI dispatch is not a semantic effect. Existing trackers alone
            // establish craft/trade/equip/pickup effects for the private scorer.
            protocol::complete_visual_ui(client, state, request, "completed", "native_ui_gesture");
        }
    }
}

fn view_supported(gesture: &str, pointer_available: bool, prompt: Option<u64>) -> bool {
    if matches!(gesture, "interact" | "context_trade") {
        prompt.is_none()
    } else {
        pointer_available
    }
}

fn prompt_dispatch_valid(expected: Option<u64>, current: Option<u64>,
                         events: &[crate::hud::Event]) -> bool {
    let Some(expected) = expected else { return true; };
    current == Some(expected) || (current.is_none() && events.iter().any(|event|
        matches!(event, crate::hud::Event::PromptDialogOutcome(id, _) if *id == expected)))
}

fn routed_events(prompt: Option<u64>, events: &mut Vec<crate::hud::Event>) -> Vec<crate::hud::Event> {
    events.drain(..).filter_map(|event| match (prompt, event) {
        (Some(expected), crate::hud::Event::PromptDialogOutcome(id, event)) if id == expected =>
            event.map(|event| *event),
        // With a modal in front, underlying events are never eligible.
        (Some(_), _) | (None, crate::hud::Event::PromptDialogOutcome(..)) => None,
        (None, event) => Some(event),
    }).collect()
}

fn equip_name(slot: EquipSlot) -> Option<String> {
    // Reverse the existing protocol vocabulary, not a second slot policy.
    ["active_mainhand","active_offhand","inactive_mainhand","inactive_offhand",
     "lantern","glider","head","neck","shoulders","chest","hands","ring1","ring2",
     "back","belt","legs","feet","tabard","bag1","bag2","bag3","bag4"]
        .into_iter().find(|name| protocol::equip_slot_for_name(name) == Some(slot)).map(str::to_owned)
}

fn resolve_context<'a>(client: impl FnOnce() -> &'a Client, state: &BridgeState,
                       context: Option<&Interactable>) -> Option<Value> {
    match context? {
        Interactable::Entity { entity, interaction } => {
            let client = client();
            let uid = client.state().read_storage::<Uid>().get(*entity).copied()?;
            if *interaction == EntityInteraction::PickupItem {
                return Some(json!({"type":"pickup","uid":uid.0.get()}));
            }
            let observation = protocol::observation(client, state);
            let target = observation["nearby_entities"].as_array()?.iter()
                .find(|row| row["uid"] == json!(uid.0.get()))?;
            let operation = match interaction {
                EntityInteraction::Talk => "talk",
                EntityInteraction::HelpDowned => "help_downed",
                EntityInteraction::ActivatePortal => "activate_portal",
                EntityInteraction::Trade => return Some(json!({
                    "type":"social_action","operation":"invite_trade","target":target["control_target"]})),
                _ => return None,
            };
            Some(json!({"type":"context_interact","operation":operation,"target":target["control_target"]}))
        },
        Interactable::Block { block, volume_pos, interaction: BlockInteraction::Collect { steal: false } }
            if matches!(volume_pos.kind, common::mounting::Volume::Terrain) =>
            Some(json!({"type":"context_interact","operation":"collect",
                        "voxel":[volume_pos.pos.x,volume_pos.pos.y,volume_pos.pos.z],
                        "expected_sprite":format!("{:?}", block.get_sprite()?)})),
        _ => None,
    }
}

fn resolve_event<'a>(client: impl FnOnce() -> &'a Client, event: crate::hud::Event) -> Option<Value> {
    // Map/craft HUD events carry the complete native input. Only other events
    // need the ECS client; tests exercise the actual encoder without a world.
    if let crate::hud::Event::MapMarkerEvent(change) = event {
        return Some(match change {
            comp::MapMarkerChange::Update(position) =>
                json!({"type":"map_marker","operation":"update","world_xy":[position.x,position.y]}),
            comp::MapMarkerChange::Remove => json!({"type":"map_marker","operation":"remove"}),
        });
    }
    if let crate::hud::Event::CraftRecipe { recipe_name, craft_sprite, amount } = event {
        let mut action = json!({"type":"craft","recipe":recipe_name,"amount":amount});
        if let Some((position, _)) = craft_sprite {
            if !matches!(position.kind, common::mounting::Volume::Terrain) { return None; }
            action["station"] = json!([position.pos.x,position.pos.y,position.pos.z]);
        }
        return Some(action);
    }
    let client = client();
    match event {
        crate::hud::Event::UseSlot { slot: comp::slot::Slot::Inventory(slot), bypass_dialog: false } => {
            let inventory = client.current::<comp::Inventory>()?;
            inventory.get(slot)?;
            if inventory.free_after_equip(slot) < 0 { return None; }
            if let Some(equip) = inventory.use_equip_slot(inventory.get(slot)?) {
                return Some(json!({"type":"equip","slot":slot.idx(),"equip_slot":equip_name(equip)?}));
            }
            Some(json!({"type":"use_item", "slot":slot.idx()}))
        },
        crate::hud::Event::UseSlot { slot: Slot::Equip(equip), bypass_dialog: false } => {
            let inventory = client.current::<comp::Inventory>()?;
            if inventory.free_after_unequip(equip) < 0 { return None; }
            let (slot, _) = inventory.slots_with_id().find(|(_, item)| item.is_none())?;
            Some(json!({"type":"inventory_action","operation":"unequip",
                        "slot":slot.idx(),"equip_slot":equip_name(equip)?}))
        },
        crate::hud::Event::SwapSlots { slot_a, slot_b, bypass_dialog: false } => match (slot_a, slot_b) {
            (Slot::Inventory(a), Slot::Inventory(b)) =>
                Some(json!({"type":"inventory_action","operation":"swap","slot":a.idx(),"other_slot":b.idx()})),
            (Slot::Inventory(slot), Slot::Equip(equip)) | (Slot::Equip(equip), Slot::Inventory(slot)) => {
                let inventory = client.current::<comp::Inventory>()?;
                if inventory.get(slot).is_some() {
                    Some(json!({"type":"equip","slot":slot.idx(),"equip_slot":equip_name(equip)?}))
                } else {
                    Some(json!({"type":"inventory_action","operation":"unequip",
                                "slot":slot.idx(),"equip_slot":equip_name(equip)?}))
                }
            },
            _ => None,
        },
        crate::hud::Event::UnlockSkill(skill) => Some(json!({"type":"unlock_skill","skill":skill})),
        crate::hud::Event::ChangeAbility(slot, ability) =>
            Some(json!({"type":"configure_auxiliary_ability","slot":slot,"ability":ability})),
        crate::hud::Event::AcceptInvite | crate::hud::Event::DeclineInvite => {
            let operation = if matches!(event, crate::hud::Event::AcceptInvite) { "respond_accept" } else { "respond_decline" };
            let (uid, _, _, kind) = client.invite()?;
            Some(json!({"type":"social_action","operation":operation,"inviter_uid":uid.0.get(),
                        "invite_kind":format!("{kind:?}")}))
        },
        crate::hud::Event::TradeAction(selected) => {
            let uid = client.uid()?;
            let pending = client.pending_trade();
            let (id, trade, _) = pending.as_ref()?;
            resolve_trade_event(uid, *id, trade, selected)
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

fn resolve_trade_event(uid: Uid, id: common::trade::TradeId, trade: &common::trade::PendingTrade,
                       selected: common::trade::TradeAction) -> Option<Value> {
    use common::trade::TradeAction;
    let ours = trade.parties.iter().position(|party| *party == uid)?;
    let mut action = json!({"type":"trade_action","trade_id":id,
        "counterparty_uid":trade.parties[1-ours].0.get(),"phase":trade.phase});
    match selected {
        TradeAction::Accept(phase) if phase == trade.phase => action["operation"] = json!("accept"),
        TradeAction::Decline => action["operation"] = json!("decline"),
        TradeAction::AddItem { item, quantity, ours } | TradeAction::RemoveItem { item, quantity, ours } => {
            action["operation"] = json!(if matches!(selected, TradeAction::AddItem { .. }) {
                "add_item"
            } else { "remove_item" });
            action["slot"] = json!(item.idx());
            action["quantity"] = json!(quantity);
            action["ours"] = json!(ours);
        },
        _ => return None,
    }
    Some(action)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::hud::{Event, Hud, PromptDialogSettings, prompt_dialog::DialogOutcomeEvent};
    use common::trade::{PendingTrade, TradeAction, TradePhase};
    use common::comp::slot::ArmorSlot;

    #[test]
    fn native_modal_stock_producer_closure_routes_only_the_originating_event() {
        let uid = Uid(std::num::NonZeroU64::new(103).unwrap());
        let other = Uid(std::num::NonZeroU64::new(65).unwrap());
        let mut trade = PendingTrade::new(uid, other);
        trade.phase = TradePhase::Review;
        let trade_id = serde_json::from_value(json!(7)).unwrap();
        for selected in [TradeAction::Accept(TradePhase::Review), TradeAction::Decline] {
            let prompt = PromptDialogSettings::new("ordinary confirmation".into(),
                Event::TradeAction(selected.clone()), Some(Event::TradeAction(TradeAction::Decline)));
            let id = prompt.id();
            let mut current = Some(prompt);
            let outcome = if matches!(selected, TradeAction::Decline) {
                DialogOutcomeEvent::Negative(Some(Event::TradeAction(selected.clone())))
            } else { DialogOutcomeEvent::Affirmative(Event::TradeAction(selected.clone())) };
            let produced = PromptDialogSettings::finish(&mut current, outcome);
            assert!(current.is_none());
            let mut events = vec![
                Event::TradeAction(TradeAction::Decline), // underlying HUD must never win
                produced,
                Event::PromptDialogOutcome(id + 1, Some(Box::new(Event::AcceptInvite))),
            ];
            assert!(prompt_dispatch_valid(Some(id), None, &events));
            let routed = routed_events(Some(id), &mut events);
            assert!(events.is_empty());
            assert_eq!(routed.len(), 1);
            let Event::TradeAction(actual) = routed.into_iter().next().unwrap() else { panic!("stock event"); };
            assert_eq!(actual, selected);
            let encoded = resolve_trade_event(uid, trade_id, &trade, actual).unwrap();
            assert_eq!(encoded, json!({"type":"trade_action", "trade_id":7,
                "counterparty_uid":65,"phase":"Review",
                "operation":if matches!(selected, TradeAction::Decline) { "decline" } else { "accept" }}));
            assert!(resolve_trade_event(uid, trade_id, &trade, TradeAction::Accept(TradePhase::Mutate)).is_none());
        }
    }

    #[test]
    fn native_modal_close_replacement_and_reopen_invalidate_pending_dispatch() {
        let first = PromptDialogSettings::new("same".into(), Event::AcceptInvite, None);
        let second = PromptDialogSettings::new("same".into(), Event::DeclineInvite, None);
        let id = first.id();
        assert_ne!(id, second.id());
        assert!(prompt_dispatch_valid(Some(id), Some(id), &[]));
        assert!(!prompt_dispatch_valid(Some(id), None, &[]), "external close is not an outcome");
        assert!(!prompt_dispatch_valid(Some(id), Some(second.id()), &[]), "replacement/reopen");
        let mut current = Some(first);
        let produced = PromptDialogSettings::finish(&mut current, DialogOutcomeEvent::Negative(None));
        assert!(prompt_dispatch_valid(Some(id), None, &[produced.clone()]), "ordinary decline closes without an action");
        assert!(!prompt_dispatch_valid(Some(id), Some(second.id()), &[produced.clone()]),
                "even a real outcome cannot authorize dispatch after replacement");
        assert!(routed_events(Some(id), &mut vec![produced]).is_empty());
        assert!(!prompt_dispatch_valid(Some(second.id()), None,
            &[Event::PromptDialogOutcome(id, None)]), "foreign origin");
    }

    #[test]
    fn native_modal_view_priority_and_private_identity_preserve_existing_fences() {
        let prompt = PromptDialogSettings::new("private prompt".into(), Event::AcceptInvite, None);
        let id = prompt.id();
        assert!(Hud::native_pointer_view(true, Some(&prompt), false), "modal-only view supported");
        assert!(!Hud::native_pointer_view(false, Some(&prompt), true), "hidden UI remains unsupported");
        assert!(!Hud::native_pointer_view(true, None, false), "unimplemented view remains unsupported");
        for gesture in GESTURES {
            assert_eq!(view_supported(gesture, true, Some(id)),
                !matches!(*gesture, "interact" | "context_trade"), "context must not pass through modal");
        }
        let frame = Frame { id: "modal-frame".into(), state: format!("{id:?}"),
            actuator: json!({"actor":{"uid":103,"lifecycle":"one"},"control":{"generation":2}}),
            size: [960,540] };
        let gesture: Gesture = serde_json::from_value(json!({
            "type":"visual_input","action_id":"confirm","frame_id":frame.id,"ui_state_id":frame.id,
            "expected_session_id":"s","actuator":frame.actuator,"gesture":"left_click","x":521,"y":91
        })).unwrap();
        assert!(validate(&gesture, &frame, &format!("{id:?}"), &frame.actuator).is_ok());
        for changed in ["None".into(), format!("{:?}", id + 1)] {
            assert_eq!(validate(&gesture, &frame, &changed, &frame.actuator), Err("stale_visual_frame"));
        }
        for actuator in [json!({"actor":{"uid":104}}), json!({"actor":{"uid":103,"lifecycle":"two"}}),
                         json!({"actor":{"uid":103,"lifecycle":"one"},"control":{"generation":3}})] {
            assert_eq!(validate(&gesture, &frame, &frame.state, &actuator), Err("stale_visual_actuator"));
        }
        let metadata = frame_metadata("modal-frame");
        assert!(!metadata.to_string().contains("private prompt"));
        assert!(metadata.get("prompt_id").is_none());
        assert!(metadata.get("widgets").is_none());
    }

    #[test]
    fn native_modal_generic_events_keep_the_ordinary_stock_session_path() {
        let event = Event::UseSlot { slot: Slot::Equip(EquipSlot::Armor(ArmorSlot::Bag1)), bypass_dialog: true };
        let prompt = PromptDialogSettings::new("drop warning".into(), event.clone(), None);
        let id = prompt.id();
        let mut current = Some(prompt);
        let produced = PromptDialogSettings::finish(&mut current, DialogOutcomeEvent::Affirmative(event));
        let mut events = vec![produced.clone(), Event::TradeAction(TradeAction::Decline)];
        let selected = routed_events(Some(id), &mut events);
        assert!(matches!(selected.as_slice(), [Event::UseSlot {
            slot: Slot::Equip(EquipSlot::Armor(ArmorSlot::Bag1)), bypass_dialog: true }]));
        assert!(matches!(produced.into_stock_event(), Some(Event::UseSlot {
            slot: Slot::Equip(EquipSlot::Armor(ArmorSlot::Bag1)), bypass_dialog: true })));
        assert!(routed_events(None, &mut vec![Event::PromptDialogOutcome(id, None)]).is_empty());
    }
    #[test]
    fn modifiers_are_closed_optional_single_left_click_inputs_behind_existing_fences() {
        use conrod_core::input::Key;
        let frame = Frame { id:"f".into(), state:"private".into(),
            actuator:json!({"actor":1,"control":{"generation":2}}), size:[960,540] };
        let base = json!({
            "type":"visual_input","action_id":"quantity","frame_id":"f","ui_state_id":"f",
            "expected_session_id":"s", "actuator":frame.actuator,
            "gesture":"left_click","x":480,"y":270
        });
        for (modifiers, expected) in [
            (None, None), (Some(json!([])), None),
            (Some(json!(["shift"])), Some(Key::LShift)), (Some(json!(["ctrl"])), Some(Key::LCtrl)),
        ] {
            let mut action = base.clone();
            if let Some(modifiers) = modifiers { action["modifiers"] = modifiers; }
            let gesture: Gesture = serde_json::from_value(action).unwrap();
            assert_eq!(gesture.modifier_key(), Ok(expected));
            assert!(validate(&gesture, &frame, "private", &frame.actuator).is_ok());
            assert_eq!(validate(&gesture, &frame, "changed", &frame.actuator), Err("stale_visual_frame"));
            assert_eq!(validate(&gesture, &frame, "private", &json!({})), Err("stale_visual_actuator"));
        }
        for modifiers in [json!(["alt"]), json!(["shift","ctrl"]), json!(["shift","shift"])] {
            let mut action = base.clone();
            action["modifiers"] = modifiers;
            let gesture: Gesture = serde_json::from_value(action).unwrap();
            assert_eq!(validate(&gesture, &frame, "private", &frame.actuator), Err("unsupported_visual_modifier"));
        }
        for kind in ["right_click","middle_click","drag","hover","scroll_up","interact","context_trade"] {
            let mut action = base.clone();
            action["modifiers"] = json!(["shift"]);
            action["gesture"] = json!(kind);
            let gesture: Gesture = serde_json::from_value(action).unwrap();
            assert_eq!(validate(&gesture, &frame, "private", &frame.actuator), Err("unsupported_visual_modifier"));
        }
        for modifiers in [json!(null), json!("shift"), json!([1]), json!([true])] {
            let mut action = base.clone();
            action["modifiers"] = modifiers;
            assert!(serde_json::from_value::<Gesture>(action).is_err());
        }
    }

    #[test]
    fn craft_hud_station_round_trips_through_real_optional_action_parser() {
        use common::{mounting::VolumePos, terrain::SpriteKind};
        use vek::Vec3;
        for position in [Vec3::new(18705,10800,501), Vec3::new(-20,30,-4),
                         Vec3::new(i32::MIN,0,i32::MAX)] {
            let event = crate::hud::Event::CraftRecipe {
                recipe_name: "simple_leather".into(), amount: 5,
                craft_sprite: Some((VolumePos::terrain(position), SpriteKind::TanningRack)),
            };
            let mut action = resolve_event(|| panic!("craft event needs no ECS client"), event).unwrap();
            action["action_id"] = json!("visible-craft-request");
            assert_eq!(protocol::craft_station_arg(action.as_object().unwrap()).unwrap(), Some(position));
            assert_eq!(action["station"], json!([position.x,position.y,position.z]));
            assert_eq!(action["recipe"], "simple_leather");
            assert_eq!(action["amount"], 5);
            assert_eq!(action["action_id"], "visible-craft-request");
        }
        let action = resolve_event(|| panic!("stationless HUD event needs no ECS client"),
            crate::hud::Event::CraftRecipe {
                recipe_name: "skewer".into(), amount: 1, craft_sprite: None,
            }).unwrap();
        assert!(action.get("station").is_none());
        assert_eq!(protocol::craft_station_arg(action.as_object().unwrap()).unwrap(), None);
        let uid = Uid(std::num::NonZeroU64::new(7).unwrap());
        assert!(resolve_event(|| panic!("unsupported volume must not need ECS client"),
            crate::hud::Event::CraftRecipe {
                recipe_name: "simple_leather".into(), amount: 1,
                craft_sprite: Some((VolumePos::entity(Vec3::zero(), uid), SpriteKind::TanningRack)),
            }).is_none());
    }
    #[test]
    fn craft_hud_station_parser_keeps_rejecting_malformed_coordinates() {
        use common::{mounting::VolumePos, terrain::SpriteKind};
        let position = vek::Vec3::new(-20,30,-4);
        let action = resolve_event(|| panic!("no ECS needed"), crate::hud::Event::CraftRecipe {
            recipe_name: "simple_leather".into(), amount: 5,
            craft_sprite: Some((VolumePos::terrain(position), SpriteKind::TanningRack)),
        }).unwrap();
        for invalid in [
            json!(position), json!(null), json!([-20,30]), json!([-20,30,-4,0]),
            json!([-20.5,30,-4]), json!(["-20",30,-4]), json!([true,30,-4]),
            json!([i64::from(i32::MIN)-1,30,-4]), json!([i64::from(i32::MAX)+1,30,-4]),
            json!([u64::MAX,30,-4]),
        ] {
            let mut rejected = action.clone();
            rejected["station"] = invalid;
            assert!(protocol::craft_station_arg(rejected.as_object().unwrap()).is_err());
        }
    }
    #[test]
    fn collect_context_voxel_round_trips_through_real_action_parser() {
        use common::{mounting::VolumePos, terrain::{Block, SpriteKind}};
        let position = vek::Vec3::new(-20,30,144);
        let context = Interactable::Block {
            block: Block::air(SpriteKind::Mushroom),
            volume_pos: VolumePos::terrain(position),
            interaction: BlockInteraction::Collect { steal: false },
        };
        let state = BridgeState::new("s", Default::default());
        let action = resolve_context(|| panic!("terrain context needs no ECS client"),
                                     &state, Some(&context)).unwrap();
        assert_eq!(protocol::voxel_arg(action.as_object().unwrap(), "voxel").unwrap(), position);
        assert_eq!(action["voxel"], json!([position.x,position.y,position.z]));
        assert_eq!(action["operation"], "collect");
        assert_eq!(action["expected_sprite"], "Mushroom");
        let mut rejected = action.clone();
        rejected["voxel"] = json!(position);
        assert!(protocol::voxel_arg(rejected.as_object().unwrap(), "voxel").is_err());
    }
    #[test]
    fn map_marker_hud_events_round_trip_through_real_client_parser() {
        use vek::Vec2;
        for position in [
            Vec2::new(18700, 10800), Vec2::new(-18700, 10800),
            Vec2::new(18700, -10800), Vec2::new(0, 0),
            Vec2::new(i32::MIN, i32::MAX),
        ] {
            let event = crate::hud::Event::MapMarkerEvent(comp::MapMarkerChange::Update(position));
            let mut action = resolve_event(|| panic!("map event must not require an ECS client"), event)
                .expect("actual native HUD marker action");
            // The ordinary finish path attaches the original public action ID.
            action["action_id"] = json!("a3-map-visible-middle-1");
            let parsed = protocol::map_marker_request(action.as_object().unwrap())
                .expect("actual HUD encoding must satisfy the production client parser");
            let comp::MapMarkerChange::Update(actual) = parsed else { panic!("expected update"); };
            assert_eq!(actual, position);
            assert_eq!(action["world_xy"], json!([position.x, position.y]));
            assert_eq!(action["action_id"], "a3-map-visible-middle-1");
        }
        let action = resolve_event(|| panic!("remove needs no ECS client"),
            crate::hud::Event::MapMarkerEvent(comp::MapMarkerChange::Remove)).unwrap();
        assert!(action.get("world_xy").is_none());
        assert!(matches!(protocol::map_marker_request(action.as_object().unwrap()),
                         Ok(comp::MapMarkerChange::Remove)));
    }
    #[test]
    fn map_marker_client_parser_keeps_rejecting_malformed_or_widened_coordinates() {
        let position = vek::Vec2::new(-10, 20);
        let action = resolve_event(|| panic!("map event needs no ECS client"),
            crate::hud::Event::MapMarkerEvent(comp::MapMarkerChange::Update(position))).unwrap();
        for invalid in [
            json!(position), json!({"x": -10, "y": 20}), json!(null),
            json!([-10]), json!([-10,20,30]), json!([-10.5,20]), json!(["-10",20]),
            json!([i64::from(i32::MIN)-1,20]), json!([i64::from(i32::MAX)+1,20]),
            json!([u64::MAX,20]), json!([true,20]),
        ] {
            let mut rejected = action.clone();
            rejected["world_xy"] = invalid;
            assert_eq!(protocol::map_marker_request(rejected.as_object().unwrap()).unwrap_err(),
                "map marker world_xy must contain two signed 32-bit integers");
        }
        let mut unknown = action.clone();
        // Start from an actual HUD update; even a valid array cannot widen fields.
        unknown["world_xy"] = json!([-10,20]);
        unknown["extra"] = json!(1);
        assert_eq!(protocol::map_marker_request(unknown.as_object().unwrap()).unwrap_err(),
                   "unknown map marker field");
        for invalid in [
            json!({"type":"map_marker","operation":"remove","world_xy":null}),
            json!({"type":"map_marker","operation":"update"}),
            json!({"type":"map_marker","operation":"set","world_xy":[-10,20]}),
        ] {
            assert!(protocol::map_marker_request(invalid.as_object().unwrap()).is_err());
        }
    }
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
    #[test]
    fn all_mouse_buttons_keep_the_same_frame_actuator_and_pixel_fences() {
        let frame = Frame { id:"f".into(), state:"private".into(),
            actuator:json!({"actor":1,"control":{"generation":2}}), size:[960,540] };
        for button in ["left_click", "right_click", "middle_click"] {
            assert!(GESTURES.contains(&button));
            let mut gesture: Gesture = serde_json::from_value(json!({
                "type":"visual_input","action_id":"a","frame_id":"f","ui_state_id":"f",
                "expected_session_id":"s", "actuator":frame.actuator,
                "gesture":button,"x":502,"y":300
            })).unwrap();
            assert!(validate(&gesture, &frame, "private", &frame.actuator).is_ok());
            assert_eq!(validate(&gesture, &frame, "changed", &frame.actuator), Err("stale_visual_frame"));
            assert_eq!(validate(&gesture, &frame, "private", &json!({})), Err("stale_visual_actuator"));
            gesture.x = Some(960.0);
            assert_eq!(validate(&gesture, &frame, "private", &frame.actuator),
                       Err("unsupported_visual_selector"));
        }
    }
    #[test]
    fn drag_scroll_and_context_never_accept_hidden_or_out_of_frame_selectors() {
        let frame = Frame { id:"f".into(), state:"private".into(),
            actuator:json!({"actor":1,"control":{"generation":2}}), size:[960,540] };
        let mut gesture: Gesture = serde_json::from_value(json!({
            "type":"visual_input","action_id":"drag","frame_id":"f","ui_state_id":"f",
            "expected_session_id":"s", "actuator":frame.actuator,
            "gesture":"drag","x":12,"y":20,"to_x":140,"to_y":90
        })).unwrap();
        assert!(validate(&gesture, &frame, "private", &frame.actuator).is_ok());
        gesture.to_y = Some(540.0);
        assert_eq!(validate(&gesture, &frame, "private", &frame.actuator),
                   Err("unsupported_visual_selector"));
        gesture.to_y = Some(f64::NAN);
        assert!(validate(&gesture, &frame, "private", &frame.actuator).is_err());
        gesture.gesture = "scroll_up".into();
        gesture.to_x = None;
        gesture.to_y = None;
        assert!(validate(&gesture, &frame, "private", &frame.actuator).is_ok());
        gesture.gesture = "context_trade".into();
        assert!(validate(&gesture, &frame, "private", &frame.actuator).is_err());
        gesture.x = None;
        gesture.y = None;
        assert!(validate(&gesture, &frame, "private", &frame.actuator).is_ok());
        gesture.ui_state_id = "previous-frame".into();
        assert_eq!(validate(&gesture, &frame, "private", &frame.actuator), Err("stale_visual_frame"));
    }
}
