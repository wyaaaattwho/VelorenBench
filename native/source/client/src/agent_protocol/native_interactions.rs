use super::*;
use common::rtsim::{Dialogue, DialogueKind};

pub(super) fn text(content: &comp::Content) -> String {
    static ENGLISH: OnceLock<client_i18n::LocalizationHandle> = OnceLock::new();
    ENGLISH.get_or_init(|| client_i18n::LocalizationHandle::load_expect("en"))
        .read().get_content(content)
}

pub(super) fn capability() -> Value {
    json!({
        "schema": "rpg-native-interactions", "version": "1.0",
        "visibility": "ordinary-player-structured-or-hybrid-only",
        "actions": {
            "context_interact": ["talk", "help_downed"],
            "dialogue_action": ["ack", "respond"],
            "social_action": ["invite_trade", "invite_group", "respond_accept", "respond_decline"],
            "trade_action": ["add_item", "remove_item", "accept", "decline"],
            "unlock_skill": "typed native Skill; subsequent level and point readback",
            "configure_auxiliary_ability": "typed native AuxiliaryAbility; subsequent slot readback"
        },
        "observations": ["dialogue", "dialogue_markers", "social_and_trade",
                         "skill_tree", "auxiliary_abilities"],
        "trade_item_side": "ours boolean; omitted means true for compatibility",
        "trade_completion": "trade_complete event; result Completed only after server commit",
        "quest_public": "native dialogue content and received markers; no replicated quest database",
        "text_profile": "native en localization; raw Content retained; not image-mode disclosure",
        "quest_private": "separate independently versioned server-local journal; never model observations",
        "image_addressing": "unsupported; existing image allowlist unchanged",
        "admin_actions": false
    })
}

#[derive(Debug, Default)]
pub(super) struct Ledger {
    pub actor: Value,
    pub speaker: Value,
    pub prompt: Value,
    pub markers: Vec<Value>,
    pub last_trade_result: Value,
}

impl Ledger {
    pub fn receive_dialogue(
        &mut self, sender: Uid, speaker: Value, dialogue: &Dialogue<true>, tick: u64,
    ) -> Value {
        let mut row = json!({
            "schema_version": "1.0", "sender_uid": sender.0.get(),
            "sender": speaker, "dialogue_id": dialogue.id, "dialogue": dialogue,
            "received_client_tick": tick, "source": "validated_server_dialogue",
            "quest_resolution": null, "reward_arrival_confirmed": null,
        });
        match &dialogue.kind {
            DialogueKind::Statement { msg, given_item, tag } => {
                row["kind"] = json!("Statement");
                row["tag"] = json!(tag);
                row["content"] = json!(msg);
                row["text"] = json!(text(msg));
                row["given_item"] = json!(given_item);
            },
            DialogueKind::Question { msg, tag, responses } => {
                row["kind"] = json!("Question");
                row["tag"] = json!(tag);
                row["content"] = json!(msg);
                row["text"] = json!(text(msg));
                row["responses"] = json!(responses.iter().map(|(id, response)| json!({
                    "response_id": id, "content": response.msg,
                    "text": text(&response.msg),
                    "given_item": response.given_item,
                })).collect::<Vec<_>>());
            },
            DialogueKind::Marker(marker) => {
                row["kind"] = json!("Marker");
                row["marker"] = json!(marker);
                // Like the normal HUD's extra_markers, receipts outlive the next statement.
                let id = &row["marker"]["id"];
                if !id.is_null()
                    && let Some(previous) = self.markers.iter_mut()
                        .find(|previous| previous["marker"]["id"] == *id)
                {
                    *previous = row.clone();
                } else {
                    self.markers.push(row.clone());
                }
            },
            DialogueKind::Start => row["kind"] = json!("Start"),
            DialogueKind::End => row["kind"] = json!("End"),
            DialogueKind::Ack { .. } => row["kind"] = json!("Ack"),
            DialogueKind::Response { .. } => row["kind"] = json!("Response"),
        }
        if matches!(dialogue.kind, DialogueKind::Statement { .. } | DialogueKind::Question { .. }) {
            self.speaker = speaker;
            self.prompt = row.clone();
        }
        row
    }
}

pub(super) fn entity_reference(client: &Client, state: &BridgeState, uid: u64) -> Value {
    entity_by_uid(client, uid).map_or(Value::Null, |entity|
        json!(continuous_control::entity_ref(state, entity, uid)))
}

pub(super) fn trade_result(result: &common::trade::TradeResult,
                          trade: &common::trade::PendingTrade) -> Value {
    let offers = trade.offers.iter().map(|offer| {
        let mut rows = offer.iter().map(|(slot, amount)|
            json!({"slot": slot.idx(), "quantity": amount})).collect::<Vec<_>>();
        rows.sort_by_key(|row| row["slot"].as_u64());
        rows
    }).collect::<Vec<_>>();
    json!({
        "schema_version": "1.0", "result": result,
        "committed": *result == common::trade::TradeResult::Completed,
        "trade": {
            "parties": trade.parties.map(|uid| uid.0.get()),
            "phase": trade.phase, "offers": offers, "accept_flags": trade.accept_flags,
        },
        "source": "server_finished_trade",
        "trade_id": null,
        "identity_limit": "FinishedTrade carries parties/offers/phase, not TradeId",
        "inventory_arrival": "sample separately; not inferred from pending disappearance",
    })
}

pub(super) fn prerequisites(skill: comp::skills::Skill) -> Value {
    use comp::skillset::SkillPrerequisite;
    skill.prerequisite_skills().map_or(Value::Null, |prerequisite| {
        let (kind, skills) = match prerequisite {
            SkillPrerequisite::All(skills) => ("All", skills),
            SkillPrerequisite::Any(skills) => ("Any", skills),
        };
        let mut rows = skills.iter().map(|(skill, level)|
            json!({"skill": skill, "level": level})).collect::<Vec<_>>();
        rows.sort_by_key(|row| row["skill"].to_string());
        json!({"kind": kind, "skills": rows})
    })
}

pub(super) fn skill_readback(
    skills: Option<&comp::SkillSet>, skill: comp::skills::Skill,
    before_level: u16, before_points: u16, cost: u16, timed_out: bool,
) -> Option<(&'static str, Value)> {
    let group = skill.skill_group_kind()?;
    let level = skills.map(|s| s.skill_level(skill).unwrap_or(0));
    let points = skills.map(|s| s.available_sp(group));
    let confirmed = skills.is_some() && level == before_level.checked_add(1)
        && points == before_points.checked_sub(cost);
    (confirmed || timed_out).then(|| (
        if confirmed { "completed" } else { "failed" },
        json!({
            "skill": skill, "group": group, "submitted_to_server": true,
            "skill_unlock_confirmed": if confirmed { Some(true) } else { None },
            "effect": if confirmed { "observed" } else { "unknown" },
            "before_level": before_level, "after_level": level,
            "before_points": before_points, "after_points": points, "expected_cost": cost,
            "code": if confirmed { "skill_level_and_cost_observed" } else { "skill_effect_unknown" },
            "scope": "subsequent_synced_skillset;not_combat_effect",
        }),
    ))
}

pub(super) fn ability_readback(
    snapshot: &Value, slot: usize, ability: comp::ability::AuxiliaryAbility,
    timed_out: bool,
) -> Option<(&'static str, Value)> {
    let observed = snapshot["active_set"].get(slot);
    let confirmed = observed == Some(&json!(ability));
    (confirmed || timed_out).then(|| (
        if confirmed { "completed" } else { "failed" },
        json!({
            "slot": slot, "ability": ability, "observed": observed,
            "submitted_to_server": true,
            "ability_configuration_confirmed": if confirmed { Some(true) } else { None },
            "effect": if confirmed { "observed" } else { "unknown" },
            "code": if confirmed { "ability_slot_observed" } else { "ability_effect_unknown" },
            "scope": "subsequent_synced_active_set;not_ability_use_or_combat_effect",
        }),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dialogue_text_uses_native_localization_not_key_as_answer() {
        assert_eq!(text(&comp::Content::Plain("A native statement".into())), "A native statement");
        let yes = text(&comp::Content::localized("common-yes"));
        assert!(!yes.is_empty() && !yes.contains("common-yes"));
    }

    #[test]
    fn markers_do_not_replace_question_or_disappear_on_statement() {
        let mut ledger = Ledger::default();
        let sender = Uid(NonZeroU64::new(7).unwrap());
        let dialogue = |kind| Dialogue {
            id: common::rtsim::DialogueId(19), kind, private_settlement: None,
        };
        let question = dialogue(DialogueKind::Question {
            msg: comp::Content::Plain("Choose".into()), tag: 42,
            responses: vec![(3, comp::Content::Plain("Workshop".into()).into())],
        });
        let row = ledger.receive_dialogue(sender, json!({"uid":7,"lifecycle":"a"}), &question, 1);
        assert_eq!(row["tag"], 42);
        assert_eq!(row["responses"][0]["response_id"], 3);
        assert_eq!(row["dialogue_id"], 19);
        assert!(row["quest_resolution"].is_null());
        assert_eq!(ledger.prompt["received_client_tick"], 1);
        let marker = common::map::Marker::at(Vec2::new(100.0, 200.0))
            .with_kind(common::map::MarkerKind::Town);
        ledger.receive_dialogue(sender, Value::Null, &dialogue(DialogueKind::Marker(marker)), 2);
        assert_eq!(ledger.prompt["received_client_tick"], 1);
        assert_eq!(ledger.prompt["tag"], 42);
        ledger.receive_dialogue(sender, Value::Null, &dialogue(DialogueKind::Statement {
            msg: comp::Content::Plain("Thanks".into()), given_item: None, tag: 43,
        }), 3);
        assert_eq!(ledger.markers.len(), 1);
        assert_eq!(ledger.markers[0]["sender"]["lifecycle"], Value::Null);
        assert_eq!(ledger.markers[0]["marker"]["wpos"], json!({"x":100.0,"y":200.0}));
    }

    #[test]
    fn only_completed_trade_is_commit_evidence() {
        let uid = |n| Uid(NonZeroU64::new(n).unwrap());
        let mut trade = common::trade::PendingTrade::new(uid(1), uid(2));
        trade.offers[1].insert(InvSlotId::new(0, 3), 2);
        for (result, committed) in [
            (common::trade::TradeResult::Completed, true),
            (common::trade::TradeResult::Declined, false),
            (common::trade::TradeResult::NotEnoughSpace, false),
        ] {
            let row = trade_result(&result, &trade);
            assert_eq!(row["committed"], committed);
            assert!(row["trade_id"].is_null());
            assert_eq!(row["trade"]["offers"][1][0], json!({"slot":3,"quantity":2}));
        }
    }

    #[test]
    fn every_native_skill_prerequisite_has_lossless_json_rows() {
        use comp::skillset::{SKILL_GROUP_DEFS, SkillPrerequisite};
        for definition in SKILL_GROUP_DEFS.values() {
            for skill in &definition.skills {
                let row = prerequisites(*skill);
                if let Some(prerequisite) = skill.prerequisite_skills() {
                    let skills = match prerequisite {
                        SkillPrerequisite::All(skills) | SkillPrerequisite::Any(skills) => skills,
                    };
                    assert_eq!(row["skills"].as_array().unwrap().len(), skills.len());
                    for (required, level) in skills {
                        assert!(row["skills"].as_array().unwrap().contains(
                            &json!({"skill":required,"level":level})));
                    }
                } else {
                    assert!(row.is_null());
                }
            }
        }
    }

    #[test]
    fn persistent_markers_follow_native_id_deduplication() {
        let mut ledger = Ledger::default();
        let sender = Uid(NonZeroU64::new(7).unwrap());
        for (tick, x) in [(1, 100.0), (2, 200.0)] {
            ledger.receive_dialogue(sender, Value::Null, &Dialogue {
                id: common::rtsim::DialogueId(19),
                private_settlement: None,
                kind: DialogueKind::Marker(common::map::Marker::at(Vec2::new(x, 20.0))
                    .with_id("same native marker")),
            }, tick);
        }
        assert_eq!(ledger.markers.len(), 1);
        assert_eq!(ledger.markers[0]["received_client_tick"], 2);
        assert_eq!(ledger.markers[0]["marker"]["wpos"]["x"], 200.0);
    }

    #[test]
    fn skill_confirmation_requires_level_and_legal_cost_not_just_points_or_dispatch() {
        let skill = comp::skills::Skill::UnlockGroup(
            comp::skillset::SkillGroupKind::Weapon(comp::tool::ToolKind::Sword));
        let mut skills = comp::SkillSet::default();
        skills.add_skill_points(comp::skillset::SkillGroupKind::General, 3);
        assert!(skill_readback(Some(&skills), skill, 0, 3, 1, false).is_none());
        skills.unlock_skill(skill).unwrap();
        assert_eq!(skill_readback(Some(&skills), skill, 0, 3, 1, false)
            .unwrap().1["skill_unlock_confirmed"], true);
        assert!(skill_readback(Some(&skills), skill, 0, 4, 1, false).is_none());
    }

    #[test]
    fn missing_skill_readback_stays_unknown_not_no_effect() {
        let skill = comp::skills::Skill::UnlockGroup(
            comp::skillset::SkillGroupKind::Weapon(comp::tool::ToolKind::Sword));
        assert!(skill_readback(None, skill, 0, 2, 1, false).is_none());
        let (_, detail) = skill_readback(None, skill, 0, 2, 1, true).unwrap();
        assert_eq!(detail["effect"], "unknown");
        assert!(detail["skill_unlock_confirmed"].is_null());
        assert!(skill_readback(None, skill, u16::MAX, 0, 1, false).is_none());
        let (_, detail) = skill_readback(None, skill, u16::MAX, 0, 1, true).unwrap();
        assert_eq!(detail["effect"], "unknown");
        assert!(detail["skill_unlock_confirmed"].is_null());
    }

    #[test]
    fn ability_configuration_requires_exact_slot_readback() {
        let ability = comp::ability::AuxiliaryAbility::Empty;
        assert!(ability_readback(&json!({"active_set":[]}), 0, ability, false).is_none());
        let (_, detail) = ability_readback(&json!({"active_set":[]}), 0, ability, true).unwrap();
        assert_eq!(detail["effect"], "unknown");
        let (_, detail) = ability_readback(&json!({"active_set":["Empty"]}), 0, ability, false).unwrap();
        assert_eq!(detail["ability_configuration_confirmed"], true);
    }
}
