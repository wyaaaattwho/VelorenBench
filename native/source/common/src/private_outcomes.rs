//! Opt-in server-local causal evidence; never a player protocol.
use crate::rtsim::{Actor, DialogueId, QuestId};
use serde::Serialize;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs::{File, OpenOptions},
    io::Write,
    sync::{Mutex, OnceLock},
};

struct Journal {
    file: File,
    sequence: u64,
    transaction: u64,
    nonce: String,
    previous: BTreeMap<String, Value>,
}

static JOURNAL: OnceLock<Option<Mutex<Journal>>> = OnceLock::new();

fn journal() -> Option<&'static Mutex<Journal>> {
    JOURNAL.get_or_init(|| {
        let path = std::env::var_os("VELOREN_PRIVATE_ORACLE_PATH")?;
        let nonce = format!("{}-{}", std::process::id(), std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH).expect("oracle clock predates Unix epoch")
            .as_nanos());
        let mut journal = Journal {
            file: OpenOptions::new().write(true).create_new(true).open(path)
                .expect("cannot create private oracle; refusing silent evidence loss"),
            sequence: 0, transaction: 0, nonce, previous: BTreeMap::new(),
        };
        journal.emit("opened", json!({
            "run_id": std::env::var("VELOREN_SESSION_ID").ok(),
            "pid": std::process::id(), "nonce": journal.nonce,
            "scope": "private-causal-evidence;not-model-observations-or-task-qualification",
        }));
        Some(Mutex::new(journal))
    }).as_ref()
}

impl Journal {
    fn emit(&mut self, event: &str, data: Value) {
        let row = json!({
            "schema": "rpg-native-oracle", "version": "1.1", "private": true,
            "event": event, "sequence": self.sequence, "data": data,
            "host_unix_ns": std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH).expect("oracle clock predates Unix epoch")
                .as_nanos().to_string(),
            "server_tick": null,
        });
        writeln!(self.file, "{row}").expect("private oracle write failed");
        self.file.flush().expect("private oracle flush failed");
        self.sequence = self.sequence.checked_add(1).expect("private oracle sequence exhausted");
    }
}

pub fn enabled() -> bool { journal().is_some() }

pub fn event(event: &str, data: Value) {
    if let Some(journal) = journal() {
        journal.lock().expect("private oracle poisoned").emit(event, data);
    }
}

pub fn snapshot(key: &str, data: Value) {
    if let Some(journal) = journal() {
        let mut journal = journal.lock().expect("private oracle poisoned");
        if journal.previous.get(key) != Some(&data) {
            journal.previous.insert(key.to_owned(), data.clone());
            journal.emit(key, data);
        }
    }
}

pub fn transaction_id() -> Option<String> {
    journal().map(|journal| {
        let mut journal = journal.lock().expect("private oracle poisoned");
        let id = format!("{}:{}", journal.nonce, journal.transaction);
        journal.transaction = journal.transaction.checked_add(1)
            .expect("private oracle transaction identity exhausted");
        id
    })
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Settlement {
    pub transaction_id: String,
    pub quest_id: QuestId,
    pub quest: Value,
    pub participants: Vec<Actor>,
    pub arbiter: Actor,
    pub requested_success: bool,
    pub resolution: Option<bool>,
    pub deposit_restoration: Value,
    pub delivery: Option<RewardDelivery>,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct RewardDelivery {
    pub sender: Actor,
    pub recipient: Actor,
    pub sender_entity: Option<String>,
    pub recipient_entity: Option<String>,
    pub dialogue_id: DialogueId,
    pub tag: u32,
    pub given_item: Value,
}

impl Settlement {
    pub fn matches_participants(&self, sender: Option<Actor>, recipient: Option<Actor>) -> bool {
        self.delivery.as_ref().is_some_and(|binding|
            sender == Some(binding.sender) && recipient == Some(binding.recipient))
    }

    pub fn matches_delivery(
        &self, sender_entity: &str, recipient_entity: &str,
        dialogue_id: DialogueId, tag: u32, given_item: &Value,
    ) -> bool {
        self.delivery.as_ref().is_some_and(|binding|
            binding.sender == self.arbiter
                && self.participants.contains(&binding.recipient)
                && binding.sender_entity.as_deref() == Some(sender_entity)
                && binding.recipient_entity.as_deref() == Some(recipient_entity)
                && binding.dialogue_id == dialogue_id && binding.tag == tag
                && &binding.given_item == given_item)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{character::CharacterId, rtsim::{Dialogue, DialogueKind}};

    fn settlement(id: u64) -> Settlement {
        let actor = Actor::Character(CharacterId(1));
        Settlement {
            transaction_id: format!("test:{id}"), quest_id: QuestId(id), quest: json!({}),
            participants: vec![actor, Actor::Character(CharacterId(2))], arbiter: actor,
            requested_success: true, resolution: Some(true), deposit_restoration: Value::Null,
            delivery: Some(RewardDelivery {
                sender: actor, recipient: Actor::Character(CharacterId(2)),
                sender_entity: Some("Entity(1, Generation(1))".into()),
                recipient_entity: Some("Entity(2, Generation(1))".into()),
                dialogue_id: DialogueId(id), tag: id as u32, given_item: json!(null),
            }),
        }
    }

    #[test]
    fn private_provenance_is_not_serialized_or_accepted_from_wire() {
        let dialogue = Dialogue::<false> {
            id: DialogueId(1), kind: DialogueKind::Start,
            private_settlement: Some(settlement(1)),
        };
        let mut wire = serde_json::to_value(&dialogue).unwrap();
        assert!(wire.get("private_settlement").is_none());
        wire["private_settlement"] = json!({"quest_id":999});
        let received: Dialogue = serde_json::from_value(wire).unwrap();
        assert!(received.private_settlement.is_none());
    }

    #[test]
    fn interleaved_quests_stale_generations_and_changed_rewards_do_not_cross_join() {
        let a = settlement(1);
        let b = settlement(2);
        let matches = |s: &Settlement, id, tag, recipient: &str, gift: &Value|
            s.matches_delivery("Entity(1, Generation(1))", recipient, DialogueId(id), tag, gift);
        assert!(matches(&a, 1, 1, "Entity(2, Generation(1))", &Value::Null));
        assert!(matches(&b, 2, 2, "Entity(2, Generation(1))", &Value::Null));
        assert!(!matches(&a, 2, 2, "Entity(2, Generation(1))", &Value::Null));
        assert!(!matches(&a, 1, 1, "Entity(2, Generation(2))", &Value::Null));
        assert!(!matches(&a, 1, 1, "Entity(2, Generation(1))", &json!(["coins",999])));
        let mut failed = a.clone();
        failed.resolution = Some(false);
        assert!(matches(&failed, 1, 1, "Entity(2, Generation(1))", &Value::Null));
        assert_eq!(failed.resolution, Some(false));
    }

    #[test]
    fn changed_or_unknown_actor_does_not_join_even_with_the_same_entity_generation() {
        let claim = settlement(1);
        let actor = |id| Some(Actor::Character(CharacterId(id)));
        assert!(claim.matches_participants(actor(1), actor(2)));
        assert!(!claim.matches_participants(actor(1), actor(99)));
        assert!(!claim.matches_participants(actor(99), actor(2)));
        assert!(!claim.matches_participants(None, actor(2)));
        assert!(!claim.matches_participants(actor(1), None));
    }
}
