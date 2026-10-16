//! Pickup replies lack request IDs; Collected also lacks a target UID.
//! Never FIFO-pop a missing response: a subsequent reply might belong to either
//! attempt. Timeouts/cancellation do not discharge debt. A unique, compatible
//! current Collected can resolve the operation, NOT prove its ground source.
use super::*;
use common::comp::inventory::item::ItemDesc;

const MAX_PENDING: usize = 256;
const SCOPE_BUCKETS: usize = 256;

/// Fixed-size conservative membership, NOT a global overflow flag. Each stable
/// hash bucket stores an inclusive range of actual keys. Collisions can widen
/// uncertainty within that bucket/range, never outside it; no false negatives.
/// In particular newer UIDs above all retired UIDs are not poisoned by overflow.
/// The original keys/provenance remain in pickup_correlation_history_summarized.
#[derive(Debug)]
struct ScopedDebt<T> {
    ranges: [Option<(T, T)>; SCOPE_BUCKETS],
}

impl<T: Ord + Clone> Default for ScopedDebt<T> {
    fn default() -> Self { Self { ranges: std::array::from_fn(|_| None) } }
}

impl<T: Ord + Clone> ScopedDebt<T> {
    fn insert(&mut self, bucket: usize, key: T) {
        match &mut self.ranges[bucket] {
            Some((low, high)) => {
                *low = low.clone().min(key.clone());
                *high = high.clone().max(key);
            },
            slot @ None => *slot = Some((key.clone(), key)),
        }
    }

    fn contains(&self, bucket: usize, key: &T) -> bool {
        self.ranges[bucket].as_ref().is_some_and(|(low, high)| low <= key && key <= high)
    }
}

fn item_key(definition: &Value) -> String { definition.to_string() }
fn item_bucket(key: &str) -> usize {
    // Fixed FNV-1a, independent of process HashMap seeds. Only used to partition
    // conservative ranges; equality/ordering uses the complete serialized ID.
    (key.bytes().fold(0xcbf29ce484222325u64,
        |hash, byte| (hash ^ u64::from(byte)).wrapping_mul(0x100000001b3))
        % SCOPE_BUCKETS as u64) as usize
}

#[derive(Clone, Debug)]
pub(super) struct PendingPickup {
    pub sequence: u64,
    pub uid: u64,
    pub definition_id: Value,
    pub amount: u32,
    pub request_id: Value,
    pub action_id: String,
    pub sent_tick: u64,
}

impl PendingPickup {
    pub fn provenance(&self) -> Value {
        json!({
            "sequence": self.sequence, "uid": self.uid,
            "request_id": self.request_id, "action_id": self.action_id,
            "sent_tick": self.sent_tick, "definition_id": self.definition_id,
            "amount": self.amount,
        })
    }
}

#[derive(Debug, Default)]
pub(super) struct PickupLedger {
    next_sequence: u64,
    pending: VecDeque<PendingPickup>,
    // Eviction is reported as an event and retains uncertainty, never a retry
    // veto. Old request provenance remains in the emitted NDJSON event stream.
    summarized_unresolved: u64,
    retired_targets: ScopedDebt<u64>,
    retired_items: ScopedDebt<String>,
    // A sprite collect can also emit Collected, sometimes multiple events.
    // Remember its possible items even after its old aggregate/presence-based
    // action finishes. Only a known single-item current reply can discharge it.
    other_collections: VecDeque<(String, Option<Value>)>,
    retired_other_items: ScopedDebt<String>,
    opaque_other_collection: bool,
}

impl PickupLedger {
    pub fn register(&mut self, mut request: PendingPickup) -> (u64, Option<Value>) {
        self.next_sequence += 1;
        request.sequence = self.next_sequence;
        let retired = if self.pending.len() == MAX_PENDING {
            self.summarized_unresolved = self.summarized_unresolved.saturating_add(1);
            self.pending.pop_front().map(|old| {
                self.retired_targets.insert((old.uid % SCOPE_BUCKETS as u64) as usize, old.uid);
                let key = item_key(&old.definition_id);
                self.retired_items.insert(item_bucket(&key), key);
                old.provenance()
            })
        } else {
            None
        };
        self.pending.push_back(request);
        (self.next_sequence, retired)
    }

    pub fn register_other_collection(&mut self, action_id: String, definition: Option<Value>) {
        if self.other_collections.len() == MAX_PENDING
            && let Some((_, definition)) = self.other_collections.pop_front()
        {
            if let Some(definition) = definition {
                let key = item_key(&definition);
                self.retired_other_items.insert(item_bucket(&key), key);
            } else {
                self.opaque_other_collection = true;
            }
        }
        self.other_collections.push_back((action_id, definition));
    }

    fn other_matches(&self, definition: &Value) -> bool {
        let key = item_key(definition);
        self.opaque_other_collection
            || self.retired_other_items.contains(item_bucket(&key), &key)
            || self.other_collections.iter().any(|(_, id)| id.as_ref().is_none_or(|id| id == definition))
    }

    /// No draws, asset instantiation, or inventory simulation. The simple
    /// default Item loot spec emits one Collected. Custom/random/multiple loot
    /// is deliberately opaque here, without a protocol end-of-collect marker.
    pub fn observe_other_collection(&mut self, action_id: Option<&str>, updates: &[comp::InventoryUpdateEvent]) -> Option<Value> {
        let action_id = action_id?;
        let Some((_, Some(definition))) = self.other_collections.iter().find(|(id, _)| id == action_id) else {
            return None;
        };
        let key = item_key(definition);
        let unique = !self.opaque_other_collection
            && !self.retired_other_items.contains(item_bucket(&key), &key)
            && !self.retired_items.contains(item_bucket(&key), &key)
            && !self.pending.iter().any(|r| r.definition_id == *definition)
            && self.other_collections.iter()
                .filter(|(_, id)| id.as_ref().is_none_or(|id| id == definition)).count() == 1;
        let matching = updates.iter().filter(|update| match update {
            comp::InventoryUpdateEvent::Collected(item) =>
                json!(item.item_definition_id().to_owned()) == *definition,
            _ => false,
        }).count();
        if unique && matching == 1 {
            let evidence = json!({
                "action_id": action_id, "definition_id": definition,
                "association": "single_default_item_current_collect",
                "source_attribution": "unknown_collected_has_no_uid_or_request_id",
                "registration_event": "pickup_competing_collection_registered",
            });
            self.other_collections.retain(|(id, _)| id != action_id);
            Some(evidence)
        } else {
            None
        }
    }

    /// Return evidence even when no active request matches. Only UID-unique
    /// refusal or a justified CURRENT operational collection discharges debt.
    pub fn evidence_for(&mut self, update: &comp::InventoryUpdateEvent, current: Option<u64>,
                        batch_conflict: bool) -> Option<(Value, Vec<u64>)> {
        let (uid, definition, amount, reason) = match update {
            comp::InventoryUpdateEvent::EntityCollectFailed { entity, reason } => {
                (Some(entity.0.get()), None, None, Some(reason))
            },
            comp::InventoryUpdateEvent::Collected(item) => (
                None,
                Some(json!(item.item_definition_id().to_owned())),
                Some(item.amount().get()),
                None,
            ),
            _ => return None,
        };
        let candidates = self.pending.iter().filter(|request| {
            uid.map_or_else(
                || definition.as_ref() == Some(&request.definition_id),
                |uid| uid == request.uid,
            )
        }).collect::<Vec<_>>();
        let sequences = candidates.iter().map(|request| request.sequence).collect::<Vec<_>>();
        let summarized_match = uid.map_or_else(|| {
            let key = item_key(definition.as_ref().expect("Collected definition"));
            self.retired_items.contains(item_bucket(&key), &key)
        }, |uid| self.retired_targets.contains((uid % SCOPE_BUCKETS as u64) as usize, &uid));
        let other_match = definition.as_ref().is_some_and(|id| self.other_matches(id));
        let unique = candidates.len() == 1 && !summarized_match && !other_match;
        let candidate = candidates.first().filter(|_| candidates.len() == 1);
        let target_scope_match = candidate.is_some_and(|r|
            self.retired_targets.contains((r.uid % SCOPE_BUCKETS as u64) as usize, &r.uid));
        let pending_target_count = candidate.map(|r|
            self.pending.iter().filter(|other| other.uid == r.uid).count());
        let amount_compatible = candidate.and_then(|r|
            amount.map(|amount| amount > 0 && amount <= r.amount));
        let justified = unique && uid.is_none() && !batch_conflict
            && current == sequences.first().copied()
            && amount_compatible == Some(true)
            && !target_scope_match && pending_target_count == Some(1);
        let association = if !unique {
            if candidates.is_empty() && !summarized_match && !other_match {
                "unmatched"
            } else {
                "ambiguous"
            }
        } else if uid.is_some() {
            "unique_pending_target"
        } else if justified {
            "compatible_current_request"
        } else {
            "compatible_item_only"
        };
        // Keep the first and latest two references, with explicit omissions.
        // Full request provenance was emitted when each request was registered.
        let references = candidates.iter().enumerate()
            .filter(|(index, _)| *index < 2 || *index + 2 >= candidates.len())
            .map(|(_, r)| r.provenance()).collect::<Vec<_>>();
        let evidence = json!({
            "server_event": if uid.is_some() { "EntityCollectFailed" } else { "Collected" },
            "server_target_uid": uid,
            "server_reason": reason,
            "collected_item": definition.map(|id| json!({"id": id, "amount": amount})),
            "association": association,
            "candidate_requests": references,
            "candidate_request_count": candidates.len(),
            "candidate_requests_omitted": candidates.len().saturating_sub(4),
            "request_provenance_event": "pickup_request_registered",
            "summarized_unresolved_requests": self.summarized_unresolved,
            "summarized_scope_match": summarized_match,
            "summarized_target_scope_match": target_scope_match,
            "pending_target_count": pending_target_count,
            "current_request_matches": current.is_some_and(|sequence| sequences.contains(&sequence)),
            "collected_amount_compatible": amount_compatible,
            "summary_representation": "256_target_and_item_hash_partitioned_inclusive_ranges",
            "summary_false_positives_possible": true,
            "other_collection_source_possible": other_match,
            "other_collection_request_count": self.other_collections.len(),
            "other_collection_requests_omitted": self.other_collections.len(),
            "other_collection_provenance_event": "pickup_competing_collection_registered",
            "opaque_other_collection_source_possible": self.opaque_other_collection
                || self.other_collections.iter().any(|(_, id)| id.is_none()),
            "batch_conflict": batch_conflict,
            "operational_collection_justified": justified,
            "resolved_request_sequence": if (unique && uid.is_some()) || justified {
                sequences.first().copied()
            } else { None },
            "operational_association": if justified { "sole_compatible_current_request_no_known_competitor" }
                                       else { "unconfirmed" },
            "association_assumptions": ["sole_bridge_inventory_sender", "no_unsolicited_item_transfer",
                                       "reliable_nonduplicated_server_replies"],
            "acquisition_observed": amount.is_some(),
            "target_acquired": Value::Null,
            "target_complete": Value::Null,
            "partial_or_other_source": candidates.first().filter(|_| unique)
                .and_then(|request| amount.map(|amount| amount < request.amount)),
            "source_attribution": if uid.is_some() { "target_uid_without_request_id" }
                                  else { "unknown_collected_has_no_uid_or_request_id" },
        });
        if (unique && uid.is_some()) || justified {
            self.pending.retain(|request| request.sequence != sequences[0]);
        }
        Some((evidence, sequences))
    }

    #[cfg(test)]
    fn evidence(&mut self, update: &comp::InventoryUpdateEvent) -> Option<(Value, Vec<u64>)> {
        self.evidence_for(update, None, false)
    }
}

pub(super) fn terminal_detail(sequence: u64, evidence: &Value) -> Value {
    let mut detail = json!({
        "pickup_request_sequence": sequence,
        "pickup_feedback": evidence,
        "outcome": "unknown",
    });
    detail["code"] = json!(match evidence["association"].as_str() {
        Some("unique_pending_target") if evidence["server_event"] == "EntityCollectFailed" => {
            detail["outcome"] = json!("refused");
            match &evidence["server_reason"] {
                Value::String(reason) if reason == "InventoryFull" => "pickup_inventory_full",
                Value::Object(reason) if reason.contains_key("LootOwned") => "pickup_loot_owned",
                _ => "pickup_refused",
            }
        },
        Some("compatible_current_request")
            if evidence["server_event"] == "Collected"
                && evidence["operational_collection_justified"] == true => {
            detail["outcome"] = json!("collected");
            detail["operational_success"] = json!(true);
            if evidence["partial_or_other_source"] == true {
                "pickup_partial_collected"
            } else {
                "pickup_collected"
            }
        },
        Some("ambiguous") => "pickup_response_ambiguous",
        _ if evidence["partial_or_other_source"] == true => "pickup_partial_or_unconfirmed",
        _ => "pickup_acquisition_unattributed",
    });
    detail
}

/// Deliberately takes no inventory counters or entity-presence signal: neither
/// can turn a missing acknowledgement into pickup success.
pub(super) fn wait_detail(uid: u64, sequence: u64, feedback: &Option<Value>, expired: bool) -> Option<Value> {
    feedback.clone().or_else(|| expired.then(|| json!({
        "code": "pickup_no_server_response",
        "uid": uid,
        "pickup_request_sequence": sequence,
        "outcome": "unknown",
        "server_response_observed": false,
        "possible_causes": ["out_of_range_no_ack", "missing_or_late_response",
                            "target_unavailable", "server_state_precondition"],
    })))
}

/// Shared by the normal and deadline terminal paths.
pub(super) fn wait_outcome(uid: u64, sequence: u64, feedback: &Option<Value>, expired: bool)
    -> Option<(&'static str, Value)>
{
    wait_detail(uid, sequence, feedback, expired).map(|detail| {
        let status = if detail["operational_success"] == true
            && detail["outcome"] == "collected"
            && detail["pickup_feedback"]["server_event"] == "Collected"
            && detail["pickup_feedback"]["operational_collection_justified"] == true
        { "completed" } else { "failed" };
        (status, detail)
    })
}

pub(super) fn eligibility(item: &PickupItem, inventory: Option<&comp::Inventory>, distance: f32) -> Value {
    json!({
        "local_candidate": true,
        "within_observed_center_range": distance <= MAX_PICKUP_RANGE,
        "pickable_means": "pickup_component_and_center_range_only",
        "stackable": item.item().is_stackable(),
        // PickupItem may contain multiple nonstackable items. can_stack tests
        // its next native item, not the entire ground entity.
        "next_item_can_stack": inventory.map(|inv| inv.can_stack(item.item())),
        "free_slots": inventory.map(comp::Inventory::free_slots),
        "auto_equip_slot": inventory.and_then(|inv| inv.pickup_equip_slot(item.item()))
            .map(|slot| format!("{slot:?}")),
        "inventory_observed": inventory.is_some(),
        "remaining_capacity": Value::Null,
        "ownership": "unknown",
        "server_acceptance": "unknown",
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request(uid: u64, action: &str) -> PendingPickup {
        PendingPickup {
            sequence: 0, uid, definition_id: json!({"Simple": "common.items.crafting_ing.twigs"}),
            amount: 3, request_id: json!(format!("request-{action}")),
            action_id: action.into(), sent_tick: 10,
        }
    }

    fn refusal(uid: u64) -> comp::InventoryUpdateEvent {
        comp::InventoryUpdateEvent::EntityCollectFailed {
            entity: Uid(NonZeroU64::new(uid).unwrap()),
            reason: comp::inventory::CollectFailedReason::InventoryFull,
        }
    }

    #[test]
    fn unique_refusal_preserves_reason_and_request() {
        let mut ledger = PickupLedger::default();
        ledger.register(request(42, "one"));
        let (evidence, sequences) = ledger.evidence(&refusal(42)).unwrap();
        assert_eq!(sequences, [1]);
        assert_eq!(evidence["candidate_requests"][0]["request_id"], "request-one");
        assert_eq!(terminal_detail(1, &evidence)["code"], "pickup_inventory_full");
        assert!(ledger.pending.is_empty());
    }

    #[test]
    fn late_same_uid_missing_response_is_not_fifo_request_identity() {
        let mut ledger = PickupLedger::default();
        ledger.register(request(42, "timed-out"));
        let (status, detail) = wait_outcome(42, 1, &None, true).unwrap();
        assert_eq!(status, "failed");
        assert_eq!(detail["code"], "pickup_no_server_response");
        ledger.register(request(42, "later"));
        for _ in 0..2 {
            let (evidence, sequences) = ledger.evidence(&refusal(42)).unwrap();
            assert_eq!(sequences, [1, 2]);
            assert_eq!(terminal_detail(2, &evidence)["code"], "pickup_response_ambiguous");
            assert_eq!(evidence["target_complete"], Value::Null);
        }
        assert_eq!(ledger.pending.len(), 2);
    }

    #[test]
    fn unrelated_uid_does_not_discharge_request() {
        let mut ledger = PickupLedger::default();
        ledger.register(request(42, "one"));
        let (evidence, sequences) = ledger.evidence(&refusal(99)).unwrap();
        assert!(sequences.is_empty());
        assert_eq!(evidence["association"], "unmatched");
        assert_eq!(ledger.pending.len(), 1);
        assert!(ledger.evidence(&comp::InventoryUpdateEvent::Given).is_none());
        ledger.pending[0].definition_id = json!({"Simple": "other"});
        let (unrelated, candidates) = ledger.evidence(&collected(3)).unwrap();
        assert_eq!(unrelated["association"], "unmatched");
        assert!(candidates.is_empty());
    }

    #[test]
    fn missing_ack_wait_never_uses_global_growth_or_entity_disappearance() {
        assert!(wait_detail(42, 1, &None, false).is_none());
        let detail = wait_detail(42, 1, &None, true).unwrap();
        assert_eq!(detail["code"], "pickup_no_server_response");
        assert_eq!(detail["outcome"], "unknown");
        assert_eq!(detail["server_response_observed"], false);
    }

    fn active(sequence: u64) -> ActiveAction {
        ActiveAction {
            request_id: json!("request-current"), action_id: "current".into(),
            action_type: "pickup".into(), signature: String::new(), started_tick: 10,
            deadline_tick: 610, start_position: None, progress_position: None,
            last_progress_tick: 10, survival_before: None,
            kind: ActiveKind::InteractWait { mode: InteractWait::Pickup {
                uid: 42, request_sequence: sequence, start_item_count: 36, feedback: None,
            }},
        }
    }

    fn feedback(state: &BridgeState) -> &Option<Value> {
        let ActiveKind::InteractWait { mode: InteractWait::Pickup { feedback, .. } } =
            &state.active.as_ref().unwrap().kind else { panic!("pickup") };
        feedback
    }

    #[test]
    fn ordinary_collection_completes_both_actual_terminal_paths() {
        let mut state = BridgeState::new("test-session", Dir::default());
        state.pickup_ledger.register(request(42, "current"));
        state.active = Some(active(1));
        route_inventory_update(&mut state, &[collected(3)]);
        for expired in [false, true] {
            let (status, detail) = wait_outcome(42, 1, feedback(&state), expired).unwrap();
            assert_eq!(status, "completed");
            assert_eq!(detail["pickup_feedback"]["acquisition_observed"], true);
            assert_eq!(detail["pickup_feedback"]["target_acquired"], Value::Null);
            assert_eq!(detail["pickup_feedback"]["target_complete"], Value::Null);
        }
        assert!(state.pickup_ledger.pending.is_empty());
    }

    /// Export the REAL router -> wait_outcome -> wire-core result, not hand-made
    /// terminal_detail fields. Python tests consume this line with
    /// PICKUP_NATIVE_CONTRACT_LOG; no Client, sockets, player or world is created.
    #[test]
    fn native_terminal_contract_for_python() {
        let mut cases = serde_json::Map::new();
        for scenario in ["ordinary", "partial", "full", "owned", "ambiguous-refusal",
                         "ambiguous-collected", "incompatible", "excessive", "missing",
                         "sprite-debt", "batch-conflict"] {
            let mut state = BridgeState::new("test-session", Dir::default());
            state.tick = 10;
            if scenario.starts_with("ambiguous") {
                state.pickup_ledger.register(request(42, "old"));
            }
            if scenario == "sprite-debt" {
                state.pickup_ledger.register_other_collection("old-sprite".into(),
                    Some(request(42, "unused").definition_id));
            }
            let (sequence, _) = state.pickup_ledger.register(request(42, "current"));
            state.active = Some(active(sequence));
            let updates = match scenario {
                "partial" => vec![collected(1)],
                "full" | "ambiguous-refusal" => vec![refusal(42)],
                "owned" => vec![ownership(42)],
                "incompatible" => vec![collected_item("common.items.recipes.food", 1)],
                "excessive" => vec![collected(4)],
                "missing" => vec![comp::InventoryUpdateEvent::Given],
                "batch-conflict" => vec![collected(1), collected(2)],
                _ => vec![collected(3)],
            };
            route_inventory_update(&mut state, &updates);
            let (status, mut detail) = wait_outcome(42, sequence, feedback(&state), true).unwrap();
            assert_eq!(status, if matches!(scenario, "ordinary" | "partial") { "completed" } else { "failed" }, "{scenario}");
            // finish_active's context fields are deliberately not effect proof.
            detail["uid"] = json!(42);
            detail["target_acquired"] = Value::Null;
            detail["inventory_and_presence_are_context_only"] = json!(true);
            cases.insert(scenario.into(), action_result_core(state.active.as_ref().unwrap(), 10, status, detail));
        }
        println!("PICKUP_TERMINAL_CONTRACT={}", Value::Object(cases));
    }

    #[test]
    fn real_router_routes_prompt_refusal_keeps_unrelated_and_late_receipts() {
        let mut state = BridgeState::new("test-session", Dir::default());
        state.tick = 10; // registration and reply in one tick need no magic time test
        state.pickup_ledger.register(request(42, "current"));
        state.active = Some(active(1));
        route_inventory_update(&mut state, &[comp::InventoryUpdateEvent::Given, refusal(99)]);
        assert!(feedback(&state).is_none());
        assert_eq!(state.events.back().unwrap()["data"]["association"], "unmatched");
        route_inventory_update(&mut state, &[refusal(42)]);
        assert_eq!(feedback(&state).as_ref().unwrap()["code"], "pickup_inventory_full");
        state.active = None;
        route_inventory_update(&mut state, &[refusal(42)]);
        assert_eq!(state.events.back().unwrap()["data"]["server_target_uid"], 42);
        assert_eq!(state.events.back().unwrap()["data"]["association"], "unmatched");
    }

    #[test]
    fn real_router_does_not_credit_timed_out_predecessor_to_new_same_uid() {
        let mut state = BridgeState::new("test-session", Dir::default());
        state.pickup_ledger.register(request(42, "old"));
        // Timeout/cancel doesn't remove unresolved correlation history.
        state.pickup_ledger.register(request(42, "current"));
        state.active = Some(active(2));
        route_inventory_update(&mut state, &[refusal(42)]);
        let detail = feedback(&state).as_ref().unwrap();
        assert_eq!(detail["code"], "pickup_response_ambiguous");
        assert_eq!(detail["outcome"], "unknown");
    }

    #[test]
    fn concrete_refusal_supersedes_unattributed_collection_without_losing_either() {
        let mut state = BridgeState::new("test-session", Dir::default());
        state.pickup_ledger.register(request(42, "current"));
        state.active = Some(active(1));
        route_inventory_update(&mut state, &[collected(3), refusal(42)]);
        let detail = feedback(&state).as_ref().unwrap();
        assert_eq!(detail["code"], "pickup_inventory_full");
        assert_eq!(detail["preceding_pickup_feedback"]["acquisition_observed"], true);
        assert_eq!(state.events.len(), 2);
    }

    #[test]
    fn bounded_ledger_reports_retired_provenance_without_banning_requests() {
        let mut ledger = PickupLedger::default();
        for i in 0..MAX_PENDING {
            assert!(ledger.register(request(i as u64 + 1, "old")).1.is_none());
        }
        let (sequence, retired) = ledger.register(request(999, "new"));
        assert_eq!(sequence, MAX_PENDING as u64 + 1);
        assert_eq!(retired.unwrap()["sequence"], 1);
        let (evidence, _) = ledger.evidence(&refusal(999)).unwrap();
        assert_eq!(evidence["association"], "unique_pending_target");
        assert_eq!(evidence["summarized_unresolved_requests"], 1);
        assert_eq!(evidence["summarized_scope_match"], false);
        // The retired UID remains ambiguous; neither overflow nor timeout can
        // erase its late-reply debt.
        ledger.register(request(1, "same-retired-target"));
        let (evidence, _) = ledger.evidence(&refusal(1)).unwrap();
        assert_eq!(evidence["association"], "ambiguous");
        assert_eq!(evidence["summarized_scope_match"], true);
    }

    #[test]
    fn more_than_256_ordinary_collections_resolve_without_history_poison() {
        let mut ledger = PickupLedger::default();
        for i in 1..=1024 {
            // Repeated same-target partial acquisitions also resolve normally.
            let uid = if i % 2 == 0 { 42 } else { i };
            let (sequence, retired) = ledger.register(request(uid, "ordinary"));
            assert!(retired.is_none());
            let (evidence, _) = ledger.evidence_for(
                &collected(if i % 2 == 0 { 1 } else { 3 }), Some(sequence), false,
            ).unwrap();
            let feedback = Some(terminal_detail(sequence, &evidence));
            for expired in [false, true] {
                assert_eq!(wait_outcome(i, sequence, &feedback, expired).unwrap().0, "completed");
            }
            assert!(ledger.pending.is_empty());
            assert_eq!(ledger.summarized_unresolved, 0);
        }
        ledger.register(request(2048, "full"));
        let (evidence, _) = ledger.evidence(&refusal(2048)).unwrap();
        assert_eq!(terminal_detail(1025, &evidence)["code"], "pickup_inventory_full");
    }

    #[test]
    fn scoped_range_collisions_are_conservative_but_never_global() {
        let mut debt = ScopedDebt::<u64>::default();
        for key in [1, 513] { debt.insert((key % SCOPE_BUCKETS as u64) as usize, key); }
        assert!(debt.contains(1, &1));
        assert!(debt.contains(1, &513));
        assert!(debt.contains(1, &257)); // explicit conservative collision
        assert!(!debt.contains(1, &769)); // new target outside this bucket's range
        assert!(!debt.contains(2, &258)); // unrelated bucket
    }

    #[test]
    fn same_target_overflow_does_not_delay_new_uid_refusal_in_real_router() {
        for owned in [false, true] {
            let mut state = BridgeState::new("test-session", Dir::default());
            for _ in 0..1024 { state.pickup_ledger.register(request(7, "old")); }
            let (sequence, _) = state.pickup_ledger.register(request(42, "current"));
            state.active = Some(active(sequence));
            route_inventory_update(&mut state, &[if owned { ownership(42) } else { refusal(42) }]);
            // No tick advance/timeout is needed to recognize the unrelated UID.
            let (status, detail) = wait_outcome(42, sequence, feedback(&state), false).unwrap();
            assert_eq!(status, "failed");
            assert_eq!(detail["outcome"], "refused");
            assert_eq!(detail["code"], if owned { "pickup_loot_owned" } else { "pickup_inventory_full" });
            assert_eq!(detail["pickup_feedback"]["summarized_scope_match"], false);
        }
    }

    #[test]
    fn overflow_is_bounded_and_target_item_scoped_not_a_global_uniqueness_veto() {
        let mut ledger = PickupLedger::default();
        for i in 1..=1024 {
            ledger.register(request(i, "timed-out-or-cancelled"));
        }
        assert_eq!(ledger.pending.len(), MAX_PENDING);
        assert_eq!(ledger.retired_targets.ranges.len(), SCOPE_BUCKETS);
        assert_eq!(ledger.retired_items.ranges.len(), SCOPE_BUCKETS);
        for uid in [2048, 4096] {
            let (sequence, _) = ledger.register(request(uid, "unrelated-target"));
            let event = if uid == 2048 { refusal(uid) } else { ownership(uid) };
            let (evidence, _) = ledger.evidence(&event).unwrap();
            assert_eq!(evidence["association"], "unique_pending_target");
            assert_eq!(wait_outcome(uid, sequence, &Some(terminal_detail(sequence, &evidence)), false).unwrap().0, "failed");
            assert_eq!(terminal_detail(sequence, &evidence)["code"],
                if uid == 2048 { "pickup_inventory_full" } else { "pickup_loot_owned" });
        }
        // New item too: old twig debt cannot contaminate an ordinary recipe pickup.
        let mut new_item = request(8192, "new-item");
        new_item.definition_id = json!({"Simple": "common.items.recipes.food"});
        let (sequence, _) = ledger.register(new_item);
        let (evidence, _) = ledger.evidence_for(&collected_item("common.items.recipes.food", 1),
                                              Some(sequence), false).unwrap();
        assert_eq!(evidence["operational_collection_justified"], true);
        assert_eq!(evidence["target_acquired"], Value::Null);
        // A new twig UID still cannot take credit for an old untagged twig reply.
        let (sequence, _) = ledger.register(request(16384, "new-target-same-item"));
        let (evidence, _) = ledger.evidence_for(&collected(3), Some(sequence), false).unwrap();
        assert_eq!(evidence["association"], "ambiguous");
        assert_eq!(evidence["operational_collection_justified"], false);
        // Nor can a delayed refusal complete a new request for the retired UID.
        ledger.register(request(1, "new-same-target"));
        for _ in 0..2 {
            let (evidence, _) = ledger.evidence(&refusal(1)).unwrap();
            assert_eq!(evidence["association"], "ambiguous");
            assert_eq!(evidence["server_reason"], "InventoryFull");
        }
    }

    #[test]
    fn current_partial_collection_preserves_effect_without_remaining_or_source_proof() {
        let mut state = BridgeState::new("test-session", Dir::default());
        state.pickup_ledger.register(request(42, "current"));
        state.active = Some(active(1));
        route_inventory_update(&mut state, &[collected(1)]);
        let (status, detail) = wait_outcome(42, 1, feedback(&state), false).unwrap();
        assert_eq!(status, "completed");
        assert_eq!(detail["code"], "pickup_partial_collected");
        assert_eq!(detail["pickup_feedback"]["collected_item"]["amount"], 1);
        assert_eq!(detail["pickup_feedback"]["partial_or_other_source"], true);
        assert_eq!(detail["pickup_feedback"]["target_complete"], Value::Null);
        assert!(state.pickup_ledger.pending.is_empty());
    }

    #[test]
    fn late_incompatible_excessive_and_multiple_collections_do_not_complete_current() {
        for variant in ["late", "excessive", "incompatible", "multiple", "old-compatible", "sprite"] {
            let mut state = BridgeState::new("test-session", Dir::default());
            if variant == "old-compatible" {
                state.pickup_ledger.register(request(99, "old"));
            }
            if variant == "sprite" {
                state.pickup_ledger.register_other_collection("old-sprite".into(),
                    Some(request(99, "old").definition_id));
            }
            let (sequence, _) = state.pickup_ledger.register(request(42, "current"));
            if variant == "incompatible" {
                state.pickup_ledger.pending.back_mut().unwrap().definition_id = json!({"Simple": "other"});
            }
            if variant != "late" { state.active = Some(active(sequence)); }
            let updates = if variant == "multiple" { vec![collected(1), collected(2)] }
                          else { vec![collected(if variant == "excessive" { 4 } else { 3 })] };
            route_inventory_update(&mut state, &updates);
            if state.active.is_some() {
                assert_eq!(wait_outcome(42, sequence, feedback(&state), true).unwrap().0, "failed", "{variant}");
            }
            assert!(!state.pickup_ledger.pending.is_empty(), "{variant}");
            assert_eq!(state.events.back().unwrap()["data"]["acquisition_observed"], true);
        }
    }

    #[test]
    fn known_current_sprite_reply_resolves_but_missing_or_opaque_sprite_reply_cannot_be_erased() {
        let definition = request(42, "unused").definition_id;
        let mut ledger = PickupLedger::default();
        ledger.register_other_collection("sprite".into(), Some(definition.clone()));
        assert!(ledger.observe_other_collection(Some("sprite"), &[collected(1)]).is_some());
        assert!(ledger.other_collections.is_empty());
        let (sequence, _) = ledger.register(request(42, "current"));
        assert_eq!(ledger.evidence_for(&collected(3), Some(sequence), false).unwrap().0["operational_collection_justified"], true);
        for definition in [Some(definition), None] {
            let mut ledger = PickupLedger::default();
            ledger.register_other_collection("missing".into(), definition);
            assert!(ledger.observe_other_collection(None, &[collected(1)]).is_none());
            let (sequence, _) = ledger.register(request(42, "current"));
            let (evidence, _) = ledger.evidence_for(&collected(3), Some(sequence), false).unwrap();
            assert_eq!(evidence["other_collection_source_possible"], true);
            assert_eq!(evidence["operational_collection_justified"], false);
            // Even opaque sprite history cannot poison UID-bearing refusals.
            let (evidence, _) = ledger.evidence(&refusal(42)).unwrap();
            assert_eq!(evidence["association"], "unique_pending_target");
        }
    }

    fn ownership(uid: u64) -> comp::InventoryUpdateEvent {
        comp::InventoryUpdateEvent::EntityCollectFailed {
            entity: Uid(NonZeroU64::new(uid).unwrap()),
            reason: comp::inventory::CollectFailedReason::LootOwned {
                owner: comp::loot_owner::LootOwnerKind::Player(Uid(NonZeroU64::new(7).unwrap())),
                expiry_secs: 19,
            },
        }
    }

    fn collected(amount: u32) -> comp::InventoryUpdateEvent {
        collected_item("common.items.crafting_ing.twigs", amount)
    }

    fn collected_item(id: &str, amount: u32) -> comp::InventoryUpdateEvent {
        use common::comp::inventory::item::{AbilityMap, MaterialStatManifest};
        let mut item = Item::new_from_asset_expect(id);
        item.set_amount(amount).unwrap();
        comp::InventoryUpdateEvent::Collected(item.frontend_item(
            &AbilityMap::load().read(), &MaterialStatManifest::load().read(),
        ))
    }

    #[test]
    fn acknowledged_acquisition_is_not_exact_source_or_goal_proof() {
        let mut ledger = PickupLedger::default();
        ledger.register(request(42, "one"));
        let (evidence, sequences) = ledger.evidence(&collected(3)).unwrap();
        assert_eq!(sequences, [1]);
        assert_eq!(evidence["acquisition_observed"], true);
        assert_eq!(evidence["collected_item"]["amount"], 3);
        assert_eq!(evidence["association"], "compatible_item_only");
        assert_eq!(evidence["target_acquired"], Value::Null);
        assert_eq!(terminal_detail(1, &evidence)["code"], "pickup_acquisition_unattributed");
        // A later same-UID refusal cannot be credited to a new request after an
        // untagged Collected event (which might have been from a sprite).
        ledger.register(request(42, "later"));
        let (late, _) = ledger.evidence(&refusal(42)).unwrap();
        assert_eq!(late["association"], "ambiguous");
    }

    #[test]
    fn partial_acquisition_and_ownership_are_distinct() {
        let mut ledger = PickupLedger::default();
        ledger.register(request(42, "partial"));
        let (partial, _) = ledger.evidence(&collected(1)).unwrap();
        assert_eq!(partial["partial_or_other_source"], true);
        assert_eq!(terminal_detail(1, &partial)["code"], "pickup_partial_or_unconfirmed");
        let owned = comp::InventoryUpdateEvent::EntityCollectFailed {
            entity: Uid(NonZeroU64::new(42).unwrap()),
            reason: comp::inventory::CollectFailedReason::LootOwned {
                owner: comp::loot_owner::LootOwnerKind::Player(Uid(NonZeroU64::new(7).unwrap())),
                expiry_secs: 19,
            },
        };
        let (evidence, _) = ledger.evidence(&owned).unwrap();
        assert_eq!(evidence["server_reason"]["LootOwned"]["expiry_secs"], 19);
        assert_eq!(evidence["server_reason"]["LootOwned"]["owner"]["Player"], 7);
        assert_eq!(terminal_detail(1, &evidence)["code"], "pickup_loot_owned");
    }

    #[test]
    fn full_inventory_nonstackable_and_stack_or_equip_opportunities() {
        use common::resources::ProgramTime;
        let mut inventory = comp::Inventory::with_empty();
        while inventory.free_slots() > 0 {
            inventory.push(Item::new_from_asset_expect("common.items.recipes.food")).unwrap();
        }
        let recipe = PickupItem::new(Item::new_from_asset_expect("common.items.recipes.food"),
                                     ProgramTime(0.0), true);
        let facts = eligibility(&recipe, Some(&inventory), 0.4);
        assert_eq!(facts["free_slots"], 0);
        assert_eq!(facts["stackable"], false);
        assert_eq!(facts["next_item_can_stack"], false);
        assert_eq!(facts["auto_equip_slot"], Value::Null);
        assert_eq!(facts["within_observed_center_range"], true);
        assert_eq!(facts["server_acceptance"], "unknown");
        assert!(inventory.pickup_item(recipe.pick_up().0).is_err());
        let twig = PickupItem::new(Item::new_from_asset_expect("common.items.crafting_ing.twigs"),
                                   ProgramTime(0.0), true);
        inventory.insert_at(InvSlotId::new(0, 0),
                            Item::new_from_asset_expect("common.items.crafting_ing.twigs")).unwrap();
        assert_eq!(eligibility(&twig, Some(&inventory), 0.4)["next_item_can_stack"], true);
        assert_eq!(inventory.free_slots(), 0);
        assert!(inventory.pickup_item(twig.pick_up().0).is_ok());
        let sword = PickupItem::new(Item::new_from_asset_expect("common.items.weapons.sword.starter"),
                                    ProgramTime(0.0), true);
        let facts = eligibility(&sword, Some(&inventory), 0.4);
        assert!(facts["auto_equip_slot"].is_string());
        assert_eq!(facts["free_slots"], 0);
        assert_eq!(facts["remaining_capacity"], Value::Null);
        assert_eq!(eligibility(&sword, None, 99.0)["within_observed_center_range"], false);
        assert_eq!(eligibility(&sword, None, 0.4)["free_slots"], Value::Null);
        assert!(inventory.try_equip(sword.pick_up().0).is_ok());
        // A full inventory can accept part of a stack, even though can_stack
        // correctly says the complete next item does not fit.
        let mut almost_full = Item::new_from_asset_expect("common.items.crafting_ing.twigs");
        almost_full.set_amount(u32::MAX - 1).unwrap();
        inventory.insert_at(InvSlotId::new(0, 0), almost_full).unwrap();
        let mut incoming = Item::new_from_asset_expect("common.items.crafting_ing.twigs");
        incoming.set_amount(3).unwrap();
        assert!(!inventory.can_stack(&incoming));
        let (remaining, inserted) = inventory.pickup_item(incoming).unwrap_err();
        assert_eq!(inserted.unwrap().get(), 1);
        assert_eq!(remaining.amount(), 2);
    }
}
