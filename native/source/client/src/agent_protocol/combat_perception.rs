//! Client-synchronized facts and necessary melee geometry, never hit prediction.
use super::*;
use common::{
    DamageSource,
    combat::{AttackSource, DamageContributor},
    comp::ability::{AbilityInput, Capability, CharacterAbility},
    comp::item::tool::AbilityContext,
};
use std::collections::HashSet;

#[derive(Debug, Hash, PartialEq, Eq)]
struct SyncedChangeKey {
    instance: u64,
    amount_bits: u32,
    local_sync_time_bits: u64,
    by: Option<DamageContributor>,
    cause: Option<DamageSource>,
    precise: bool,
}

impl From<comp::HealthChange> for SyncedChangeKey {
    fn from(change: comp::HealthChange) -> Self {
        Self {
            instance: change.instance, amount_bits: change.amount.to_bits(),
            local_sync_time_bits: change.time.0.to_bits(), by: change.by,
            cause: change.cause, precise: change.precise,
        }
    }
}

#[derive(Debug, Default)]
pub(super) struct DamageTracker {
    session_id: String,
    actor: Value,
    seen: HashSet<SyncedChangeKey>,
    last: Option<comp::HealthChange>,
    observed_tick: u64,
    fresh_tick: Option<u64>,
    sample_tick: Option<u64>,
    health: Option<f32>,
    net_change: Option<f32>,
}

impl DamageTracker {
    pub(super) fn sample(
        &mut self, session_id: &str, actor: Value, tick: u64,
        change: Option<comp::HealthChange>, health: Option<f32>,
    ) -> Option<comp::HealthChange> {
        let changed_actor = self.actor != actor || self.session_id != session_id;
        if changed_actor || actor.is_null() || change.is_none() {
            self.seen.clear();
            self.last = None;
            self.fresh_tick = None;
            self.actor = actor;
            self.session_id = session_id.to_owned();
            self.sample_tick = None;
            self.health = None;
            self.net_change = None;
        }
        if self.sample_tick.is_some_and(|previous| tick <= previous) { return None; }
        let change = change?;
        let first = self.last.is_none();
        self.net_change = health.zip(self.health).map(|(now, old)| now - old);
        self.health = health;
        self.sample_tick = Some(tick);
        if self.last.is_none_or(|old| old != change) {
            self.observed_tick = tick;
        }
        // This identifies a client observation, not a hit: the instance groups
        // changes and the receipt time is rewritten on sync. HP loss is required.
        let unseen = self.seen.insert(change.into());
        self.last = Some(change);
        if !first && !changed_actor && !self.actor.is_null() && unseen && change.amount < 0.0
            && self.net_change.is_some_and(|delta| delta < 0.0)
        {
            self.fresh_tick = Some(tick);
            Some(change)
        } else {
            None
        }
    }

    pub(super) fn observation(&self, change: comp::HealthChange, tick: u64) -> Value {
        let (cause, source) = cause(change.cause);
        json!({
            "amount": change.amount, "cause": cause, "attack_source": source,
            "by_kind": change.by.map(|by| match by {
                DamageContributor::Solo(_) => "Solo", DamageContributor::Group { .. } => "Group",
            }),
            "by_uid": change.by.map(|by| by.uid().0.get()),
            "attacker_uid": attributed_attacker(&change, self.actor["uid"].as_u64()),
            "instance": change.instance, "precise": change.precise,
            "net_health_change_on_sample": self.net_change,
            "observed_tick": self.observed_tick,
            "freshness": if self.fresh_tick == Some(tick) { "fresh_damage" } else { "historical_or_unknown" },
            "source": "synced_health.last_change",
            "client_local_sync_time": change.time.0,
            "time_provenance": "rewritten_client_local_on_sync_not_server_event_time",
            "coverage": "last_change_only_may_overwrite_intermediate_events",
            "instance_semantics": "attack_change_group_not_unique_event",
            "attribution_scope": "last_change_only_not_entire_net_health_delta",
            "freshness_basis": "advancing_same_actor_sample_hp_loss_and_unseen_synced_change_record",
        })
    }
}

fn cause(cause: Option<DamageSource>) -> (Option<&'static str>, Option<String>) {
    match cause {
        Some(DamageSource::Attack(source)) => (Some("Attack"), Some(format!("{source:?}"))),
        Some(DamageSource::Buff(_)) => (Some("Buff"), None),
        Some(DamageSource::Falling) => (Some("Falling"), None),
        Some(DamageSource::Other) => (Some("Other"), None),
        None => (None, None),
    }
}

pub(super) fn attributed_attacker(change: &comp::HealthChange, actor: Option<u64>) -> Option<u64> {
    (change.amount < 0.0 && matches!(change.cause, Some(DamageSource::Attack(_))))
        .then(|| change.by.map(|by| by.uid().0.get())).flatten()
        .filter(|uid| Some(*uid) != actor)
}

pub(super) fn binding(client: &Client, state: &BridgeState) -> Value {
    binding_record(&state.session_id, state.tick, continuous_control::actor_ref(client, state))
}

fn binding_record(session_id: &str, tick: u64, actor: Value) -> Value {
    json!({"session_id": session_id, "tick": tick, "actor": actor,
           "frame": "veloren_world_xyz_meters"})
}

fn geometry(client: &Client, entity: Entity) -> Value {
    let state = client.state();
    let bodies = state.read_storage::<comp::Body>();
    let positions = state.read_storage::<comp::Pos>();
    let scales = state.read_storage::<comp::Scale>();
    geometry_record(bodies.get(entity), positions.get(entity), scales.get(entity))
}

fn geometry_record(body: Option<&comp::Body>, position: Option<&comp::Pos>, scale: Option<&comp::Scale>) -> Value {
    match (body, position) {
        (Some(body), Some(pos)) => {
            let factor = scale.map_or(1.0, |s| s.0);
            json!({"position": [pos.0.x, pos.0.y, pos.0.z], "body": body_identity(body),
                   "scale": factor, "scale_source": if scale.is_some() { "synced_scale" } else { "native_default_one" },
                   "radius_m": body.max_radius() * factor, "height_m": body.height() * factor})
        },
        _ => Value::Null,
    }
}

fn orientation(client: &Client, entity: Entity) -> Option<[f32; 3]> {
    client.state().read_storage::<comp::Ori>().get(entity).map(|ori| {
        let v = ori.look_dir().to_vec();
        [v.x, v.y, v.z]
    })
}

fn state_facts(character: Option<&comp::CharacterState>) -> Value {
    let active_strike = match character {
        Some(comp::CharacterState::BasicMelee(c)) => Some(strike(
            &c.static_data.melee_constructor, c.static_data.buildup_duration.as_secs_f32(),
            c.static_data.swing_duration.as_secs_f32(), c.static_data.hit_timing,
            c.static_data.recover_duration.as_secs_f32())),
        Some(comp::CharacterState::ComboMelee2(c)) if !c.static_data.strikes.is_empty() => {
            let s = c.strike_data();
            Some(strike(&s.melee_constructor, s.buildup_duration.as_secs_f32(),
                s.swing_duration.as_secs_f32(), s.hit_timing, s.recover_duration.as_secs_f32()))
        },
        _ => None,
    };
    json!({
        "variant": character.map(ToString::to_string),
        "attack_kind": character.map(|c| c.attack_kind().iter().map(|a| format!("{a:?}")).collect::<Vec<_>>()),
        "phase": character.and_then(|c| c.stage_section()).map(|s| format!("{s:?}")),
        "phase_timer_s": character.and_then(|c| c.timer()).map(|t| t.as_secs_f32()),
        "target_hint_uid": character.and_then(|c| c.ability_info())
            .and_then(|i| i.input_attr).and_then(|a| a.target_entity).map(|uid| uid.0.get()),
        "target_hint_source": "character_state.input_attr_non_authoritative",
        "phase_semantics": "variant_specific_BasicRanged_emits_on_Recover_not_universal_safety",
        "active_melee_strike": active_strike,
        "blockable": active_strike.as_ref().and_then(|s| s["blockable"].as_bool()),
        "exhausted": match character {
            Some(comp::CharacterState::BasicMelee(c)) => Some(c.exhausted),
            Some(comp::CharacterState::ComboMelee2(c)) => Some(c.exhausted),
            _ => None,
        },
        "completed_strikes": match character {
            Some(comp::CharacterState::ComboMelee2(c)) => Some(c.completed_strikes),
            _ => None,
        },
        "charged_ranged": match character {
            Some(comp::CharacterState::ChargedRanged(c)) => json!({
                "charge_fraction": c.charge_frac(),
                "phase": format!("{:?}", c.stage_section),
                "release_to_fire": c.stage_section == common::states::utils::StageSection::Charge
                    && !c.exhausted,
                "locally_exhausted": c.exhausted,
                "scope": "observed_character_state_not_server_hit",
            }),
            _ => Value::Null,
        },
    })
}

pub(super) fn resolve(client: &Client, input: AbilityInput) -> Option<(CharacterAbility, String)> {
    let active = client.current::<comp::ActiveAbilities>()?;
    let inventory = client.current::<comp::Inventory>()?;
    let skills = client.current::<comp::SkillSet>()?;
    let body = client.current::<comp::Body>()?;
    let character = client.current::<comp::CharacterState>()?;
    let stance = client.current::<comp::Stance>();
    let combo = client.current::<comp::Combo>();
    let stats = client.current::<comp::Stats>();
    let context = AbilityContext::from(stance.as_ref(), Some(&inventory), combo.as_ref());
    let (ability, _, specified) = active.activate_ability(
        input, Some(&inventory), &skills, Some(&body), Some(&character), &context, stats.as_ref(),
    )?;
    // These adjustments need a complete game JoinData; never publish unadjusted
    // mechanics as resolved when that additional context is required.
    if ability.ability_meta().contextual_stats.is_some() { return None; }
    let id = specified.ability_id(Some(&character), Some(&inventory))?.to_owned();
    Some((ability, id))
}

fn strike(melee: &comp::MeleeConstructor, buildup: f32, swing: f32, hit: f32, recover: f32) -> Value {
    json!({"range_m": melee.range, "half_angle_deg": melee.angle,
           "buildup_s": buildup, "swing_s": swing, "hit_timing_fraction": hit,
           "recover_s": recover, "blockable": melee.blockable})
}

pub(super) fn strikes(ability: &CharacterAbility) -> Option<Vec<Value>> {
    match ability {
        CharacterAbility::BasicMelee { melee_constructor, buildup_duration, swing_duration,
            hit_timing, recover_duration, .. } => Some(vec![strike(
                melee_constructor, *buildup_duration, *swing_duration, *hit_timing, *recover_duration,
            )]),
        CharacterAbility::ComboMelee2 { strikes, .. } => Some(strikes.iter().map(|s| strike(
            &s.melee_constructor, s.buildup_duration, s.swing_duration, s.hit_timing, s.recover_duration,
        )).collect()),
        _ => None,
    }
}

pub(super) fn can_guard_state(character: &comp::CharacterState) -> bool {
    matches!(character, comp::CharacterState::Wielding(_)) || character.ability_info()
        .is_some_and(|i| i.ability_meta.capabilities.contains(Capability::BLOCK_INTERRUPT))
}

pub(super) fn readiness(client: &Client, ability: &CharacterAbility, guard: bool) -> Option<bool> {
    let character = client.current::<comp::CharacterState>()?;
    let health = client.current::<comp::Health>()?;
    let energy = client.current::<comp::Energy>()?;
    let stance = client.current::<comp::Stance>();
    let inventory = client.current::<comp::Inventory>();
    readiness_for(&character, !health.is_dead && !comp::is_downed(Some(&health), Some(&character)),
        energy.current(), ability, guard,
        ability.ability_meta().requirements.requirements_met(stance.as_ref(), inventory.as_ref()))
}

fn readiness_for(
    character: &comp::CharacterState, alive: bool, energy: f32, ability: &CharacterAbility,
    guard: bool, requirements_met: bool,
) -> Option<bool> {
    let supported = if guard { matches!(ability, CharacterAbility::BasicBlock { .. }) }
        else { strikes(ability).is_some_and(|s| !s.is_empty()) };
    if !supported { return None; }
    Some(alive
        && (if guard { can_guard_state(&character) }
            else { matches!(character, comp::CharacterState::Wielding(_)) })
        && energy >= ability.energy_cost() && requirements_met)
}

pub(super) fn actual_guard(character: Option<&comp::CharacterState>) -> Value {
    json!({
        "actual_basic_block": matches!(character, Some(comp::CharacterState::BasicBlock(_))),
        "phase": character.and_then(|c| c.stage_section()).map(|s| format!("{s:?}")),
        "melee_block_state": character.map(|c| c.is_block(AttackSource::Melee)),
        "mitigating_now": null,
        "mitigation_limit": "state_entry_not_hit_evidence_requires_direction_attack_filter_and_strength",
    })
}

pub(super) fn remaining_guard_transition_seconds(character: &comp::CharacterState) -> Option<f32> {
    use common::states::utils::StageSection;
    let (buildup, swing, recover, phase, timer) = match character {
        comp::CharacterState::Equipping(c) =>
            return Some(c.static_data.buildup_duration.saturating_sub(c.timer).as_secs_f32()),
        comp::CharacterState::BasicMelee(c) => (c.static_data.buildup_duration,
            c.static_data.swing_duration, c.static_data.recover_duration, c.stage_section, c.timer),
        comp::CharacterState::ComboMelee2(c) if !c.static_data.strikes.is_empty() => {
            let s = c.strike_data();
            (s.buildup_duration, s.swing_duration, s.recover_duration, c.stage_section, c.timer)
        },
        _ => return None,
    };
    let remaining = match phase {
        StageSection::Buildup => buildup.saturating_sub(timer) + swing + recover,
        StageSection::Action => swing.saturating_sub(timer) + recover,
        StageSection::Recover => recover.saturating_sub(timer),
        _ => return None,
    };
    Some(remaining.as_secs_f32())
}

fn ability_facts(client: &Client, input: AbilityInput, guard: bool) -> Value {
    let resolved = resolve(client, input);
    let ready = resolved.as_ref().and_then(|(ability, _)| readiness(client, ability, guard));
    ability_record(resolved.as_ref(), ready, guard, client.current::<comp::CharacterState>().as_ref(),
        client.current::<comp::Stats>().map(|s| s.attack_speed_modifier))
}

fn ability_record(
    resolved: Option<&(CharacterAbility, String)>, ready: Option<bool>, guard: bool,
    character: Option<&comp::CharacterState>, attack_speed: Option<f32>,
) -> Value {
    let mut result = json!({
        "ability_id": null, "kind": null, "energy_cost": null, "strikes": null,
        "can_start_now": null, "reason": "missing_or_contextual_unsupported",
    });
    if let Some((ability, id)) = resolved {
        let kind = match ability {
            CharacterAbility::BasicMelee { .. } => "BasicMelee",
            CharacterAbility::ComboMelee2 { .. } => "ComboMelee2",
            CharacterAbility::BasicBlock { .. } => "BasicBlock",
            CharacterAbility::BasicRanged { .. } => "BasicRanged",
            CharacterAbility::ChargedRanged { .. } => "ChargedRanged",
            CharacterAbility::BasicBeam { .. } => "BasicBeam",
            _ => "unsupported",
        };
        result = json!({"ability_id": id, "kind": kind, "energy_cost": ability.energy_cost(),
            "strikes": strikes(ability), "can_start_now": ready,
            "reason": match ready { Some(true) => "state_energy_requirements_met_input_not_outcome",
                Some(false) => "state_energy_or_requirements_not_ready", None => "unsupported" }});
        result["nominal"] = match ability {
            CharacterAbility::BasicRanged { buildup_duration, recover_duration,
                projectile_speed, .. } => json!({
                    "buildup_s": buildup_duration, "recover_s": recover_duration,
                    "projectile_speed_m_s": projectile_speed,
                    "emission": "during_recover_not_hit_evidence",
            }),
            CharacterAbility::ChargedRanged { buildup_duration, charge_duration,
                recover_duration, initial_projectile_speed, scaled_projectile_speed,
                energy_drain, .. } => json!({
                    "buildup_s": buildup_duration, "charge_s": charge_duration,
                    "recover_s": recover_duration, "energy_drain_per_s": energy_drain,
                    "initial_projectile_speed_m_s": initial_projectile_speed,
                    "scaled_projectile_speed_m_s": scaled_projectile_speed,
                    "emission": "release_during_charge_not_hit_evidence",
            }),
            CharacterAbility::BasicBeam { buildup_duration, recover_duration,
                beam_duration, range, energy_drain, tick_rate, .. } => json!({
                    "buildup_s": buildup_duration, "recover_s": recover_duration,
                    "beam_duration_s": beam_duration, "range_m": range,
                    "energy_drain_per_s": energy_drain, "nominal_tick_rate_hz": tick_rate,
                    "emission": "sustained_action_not_hit_evidence",
            }),
            _ => Value::Null,
        };
        if let CharacterAbility::BasicBlock { max_angle, blocked_attacks, block_strength,
            buildup_duration, recover_duration, can_hold, .. } = ability {
            result["block_half_angle_deg"] = json!(max_angle);
            result["blocked_attacks"] = json!(blocked_attacks);
            result["block_strength"] = json!(block_strength);
            result["buildup_s"] = json!(buildup_duration);
            result["recover_s"] = json!(recover_duration);
            result["can_hold"] = json!(can_hold);
        }
    }
    if guard {
        for key in ["block_half_angle_deg", "blocked_attacks", "block_strength", "buildup_s", "recover_s", "can_hold"] {
            result.as_object_mut().unwrap().entry(key).or_insert(Value::Null);
        }
        let actual = actual_guard(character);
        result.as_object_mut().unwrap().extend(actual.as_object().unwrap().clone());
    }
    result["timing_provenance"] = json!("resolved_ability_timer_seconds_not_wall_clock");
    result["attack_speed_modifier"] = json!(attack_speed);
    result
}

#[derive(serde::Serialize)]
struct SelfObservation {
    geometry: Value,
    orientation: Option<[f32; 3]>,
    state: Value,
    last_health_change: Option<Value>,
    primary: Value,
    guard: Value,
}

fn nearby_record(
    character: Option<&comp::CharacterState>, target: Value, geometry: Value,
    orientation: Option<[f32; 3]>, alignment: Option<String>,
) -> Value {
    let mut facts = state_facts(character);
    facts["target"] = target;
    facts["geometry"] = geometry;
    facts["orientation"] = json!(orientation);
    facts["alignment"] = json!(alignment);
    facts["alignment_provenance"] = json!("synchronized_relation_not_current_aggression");
    facts
}

fn observation_record(binding: Value, own: SelfObservation, rows: Vec<Value>, pre_hit: Value) -> Value {
    json!({"schema_version": "1.0", "binding": binding, "self": own,
        "nearby_combat": rows, "pre_hit": pre_hit,
        "provenance": "client_synced_ECS_bounded_nearby_projection_no_server_Agent"})
}

pub(super) fn observation(client: &Client, bridge: &BridgeState, nearby: &[Value]) -> Value {
    let state = client.state();
    let characters = state.read_storage::<comp::CharacterState>();
    let alignments = state.read_storage::<comp::Alignment>();
    let rows = nearby.iter().filter_map(|row| {
        let uid = row["uid"].as_u64()?;
        let entity = entity_by_uid(client, uid)?;
        if continuous_control::entity_ref(bridge, entity, uid) != row["control_target"] { return None; }
        Some(nearby_record(characters.get(entity), row["control_target"].clone(),
            geometry(client, entity), orientation(client, entity),
            alignments.get(entity).map(|a| format!("{a:?}"))))
    }).collect::<Vec<_>>();
    observation_record(binding(client, bridge), SelfObservation {
            geometry: geometry(client, client.entity()),
            orientation: orientation(client, client.entity()),
            state: state_facts(characters.get(client.entity())),
            last_health_change: client.current::<comp::Health>().map(|h|
                bridge.continuous.damage.observation(h.last_change, bridge.tick)),
            primary: ability_facts(client, AbilityInput::Primary, false),
            guard: ability_facts(client, AbilityInput::Guard, true),
        },
        rows, bridge.continuous.pre_hit.observation())
}

pub(super) fn melee_geometry(
    actor: Vec3<f32>, look: Vec3<f32>, own_radius: f32, own_height: f32, own_scale: f32,
    target: Vec3<f32>, target_radius: f32, target_height: f32, range: f32, half_angle: f32,
) -> &'static str {
    if ![actor.x, actor.y, actor.z, look.x, look.y, look.z, target.x, target.y, target.z,
        own_radius, own_height, own_scale, target_radius, target_height, range, half_angle]
        .iter().all(|v| v.is_finite())
        || own_radius < 0.0 || target_radius < 0.0 || own_height <= 0.0
        || target_height <= 0.0 || own_scale <= 0.0 || range <= 0.0 || half_angle <= 0.0
    { return "unknown_geometry"; }
    let delta = target - actor;
    if delta.magnitude_squared() >= (own_radius + target_radius + own_scale * range).powi(2) {
        return "out_of_range";
    }
    // Match this fork's server wedge test: its vertical window uses unscaled
    // body heights, unlike the scaled cylinder used for wall collision.
    let mid = actor.z + 0.5 * own_height;
    let window = (mid - range)..(mid + range);
    if !(window.contains(&target.z) || window.contains(&(target.z + target_height))
        || (target.z..target.z + target_height).contains(&mid)) { return "vertical_mismatch"; }
    if look.xy().magnitude_squared() < f32::EPSILON { return "unknown_orientation"; }
    if delta.xy().magnitude_squared() < f32::EPSILON { return "unknown_angle"; }
    if look.xy().angle_between(delta.xy()) >= half_angle.to_radians()
        + (target_radius / delta.xy().magnitude()).atan() { return "outside_angle"; }
    "geometry_eligible_not_guaranteed_hit"
}

pub(super) fn selected_melee(client: &Client, target: Entity) -> Value {
    let Some((ability, id)) = resolve(client, AbilityInput::Primary) else {
        return json!({"status": "unknown_ability", "eligible": false});
    };
    let Some(strikes) = strikes(&ability).filter(|s| !s.is_empty()) else {
        return json!({"status": "unsupported_ability", "eligible": false});
    };
    let state = client.state();
    let positions = state.read_storage::<comp::Pos>();
    let bodies = state.read_storage::<comp::Body>();
    let scales = state.read_storage::<comp::Scale>();
    let orientations = state.read_storage::<comp::Ori>();
    let Some(((((a, b), own), other), ori)) = positions.get(client.entity()).zip(positions.get(target))
        .zip(bodies.get(client.entity())).zip(bodies.get(target)).zip(orientations.get(client.entity()))
        else { return json!({"status": "unknown_geometry", "eligible": false}); };
    let own_scale = scales.get(client.entity()).map_or(1.0, |s| s.0);
    let target_scale = scales.get(target).map_or(1.0, |s| s.0);
    let character = client.current::<comp::CharacterState>();
    let index = match &character {
        Some(comp::CharacterState::ComboMelee2(c)) => c.completed_strikes % strikes.len(),
        _ => 0,
    };
    let s = &strikes[index];
    let geometry = melee_geometry(a.0, ori.look_dir().to_vec(), own.max_radius() * own_scale,
        own.height(), own_scale, b.0, other.max_radius() * target_scale, other.height(),
        s["range_m"].as_f64().unwrap() as f32, s["half_angle_deg"].as_f64().unwrap() as f32);
    let continuing = character.as_ref().is_some_and(|c| {
        matches!(c, comp::CharacterState::BasicMelee(_) | comp::CharacterState::ComboMelee2(_))
            && c.ability_info().and_then(|i| i.ability).and_then(|s|
                client.current::<comp::Inventory>().and_then(|inv|
                    s.ability_id(Some(c), Some(&inv)).map(str::to_owned))) == Some(id.clone())
    });
    let ready = readiness(client, &ability, false) == Some(true);
    melee_feedback_record(geometry, ready, continuing, &id, index, character.as_ref())
}

fn melee_feedback_record(
    geometry: &str, ready: bool, continuing: bool, id: &str, index: usize,
    character: Option<&comp::CharacterState>,
) -> Value {
    let status = if geometry != "geometry_eligible_not_guaranteed_hit" { geometry }
        else if !ready { "action_in_progress_or_not_ready" } else { geometry };
    json!({"status": status, "eligible": (ready || continuing) && geometry == "geometry_eligible_not_guaranteed_hit",
        "input_continues_selected_ability": continuing,
        "ability_id": id, "strike_index": index, "geometry": geometry,
        "phase": character.and_then(|c| c.stage_section()).map(|p| format!("{p:?}")),
        "line_of_sight": "existing_loaded_center_ray_passed_not_server_multi_ray_wall_test",
        "gameplay_success": null})
}

#[cfg(test)]
mod tests {
    use super::*;
    use common::{LoadoutBuilder, resources::Time, states::{basic_block, utils::{AbilityInfo, StageSection}}};

    fn hit(instance: u64) -> comp::HealthChange {
        comp::HealthChange {
            amount: -4.0, by: Some(DamageContributor::Solo(Uid(NonZeroU64::new(2).unwrap()))),
            cause: Some(DamageSource::Attack(AttackSource::Melee)), time: Time(4.0),
            precise: false, instance,
        }
    }

    #[test]
    fn damage_requires_advancing_actor_health_and_new_observation_not_first_history() {
        let actor = json!({"uid": 1, "lifecycle": "actor-a"});
        let mut tracker = DamageTracker::default();
        assert!(tracker.sample("test", actor.clone(), 10, Some(hit(100)), Some(90.0)).is_none());
        assert!(tracker.sample("test", actor.clone(), 10, Some(hit(101)), Some(86.0)).is_none());
        assert!(tracker.sample("test", actor.clone(), 11, Some(hit(101)), Some(86.0)).is_some());
        assert_eq!(tracker.observation(hit(101), 11)["freshness"], "fresh_damage");
        assert!(tracker.sample("test", actor.clone(), 12, Some(hit(101)), Some(82.0)).is_none());
        assert!(tracker.sample("test", actor.clone(), 13, Some(hit(102)), Some(82.0)).is_none());
        assert!(tracker.sample("test", actor.clone(), 14, Some(hit(100)), Some(78.0)).is_none());
        assert!(tracker.sample("test", json!({"uid": 1, "lifecycle": "actor-b"}), 15,
            Some(hit(103)), Some(20.0)).is_none());
        assert!(tracker.sample("test", json!({"uid": 9, "lifecycle": "actor-c"}), 16,
            Some(hit(104)), Some(16.0)).is_none());
        assert_eq!(tracker.observation(hit(104), 16)["freshness"], "historical_or_unknown");
        assert!(tracker.sample("test", Value::Null, 17, Some(hit(105)), Some(12.0)).is_none());
    }

    #[test]
    fn environmental_ambiguous_healing_self_damage_never_names_a_retaliatory_target() {
        let mut change = hit(7);
        assert_eq!(attributed_attacker(&change, Some(1)), Some(2));
        change.by = Some(DamageContributor::Group {
            entity_uid: Uid(NonZeroU64::new(3).unwrap()), group: comp::group::NPC,
        });
        assert_eq!(attributed_attacker(&change, Some(1)), Some(3));
        change.cause = Some(DamageSource::Falling);
        assert_eq!(attributed_attacker(&change, Some(1)), None);
        change.cause = None;
        assert_eq!(attributed_attacker(&change, Some(1)), None);
        change = hit(8);
        change.by = None;
        assert_eq!(attributed_attacker(&change, Some(1)), None);
        change = hit(9);
        change.amount = 4.0;
        assert_eq!(attributed_attacker(&change, Some(1)), None);
        assert_eq!(attributed_attacker(&hit(10), Some(2)), None);
    }

    #[test]
    fn distinct_synced_changes_can_share_attack_instance_without_attributing_whole_hp_delta() {
        let actor = json!({"uid": 1, "lifecycle": "actor-a"});
        let mut tracker = DamageTracker::default();
        let initial = hit(77);
        assert!(tracker.sample("test", actor.clone(), 1, Some(initial), Some(100.0)).is_none());
        let mut next = initial;
        next.time = Time(5.0);
        let fresh = tracker.sample("test", actor.clone(), 2, Some(next), Some(88.0));
        assert_eq!(fresh, Some(next));
        let evidence = tracker.observation(next, 2);
        assert_eq!(evidence["observed_tick"], 2);
        assert_eq!(evidence["amount"], -4.0);
        assert_eq!(evidence["net_health_change_on_sample"], -12.0);
        assert_eq!(evidence["attribution_scope"], "last_change_only_not_entire_net_health_delta");
        assert!(tracker.sample("test", actor.clone(), 3, Some(next), Some(88.0)).is_none());
        let mut later = next;
        later.time = Time(6.0);
        later.amount = -2.0;
        assert_eq!(tracker.sample("test", actor.clone(), 4, Some(later), Some(86.0)), Some(later));
        assert!(tracker.sample("test", actor.clone(), 5, Some(next), Some(82.0)).is_none());
        later.time = Time(7.0);
        assert!(tracker.sample("test", actor.clone(), 6, Some(later), Some(82.0)).is_none());
        later.amount = -1.0;
        assert_eq!(tracker.sample("test", actor, 7, Some(later), Some(81.0)), Some(later));
    }

    #[test]
    fn same_actor_new_session_or_respawn_seeds_a_non_actionable_baseline() {
        let actor = json!({"uid": 1, "lifecycle": "actor-a"});
        let mut tracker = DamageTracker::default();
        assert!(tracker.sample("first", actor.clone(), 10, Some(hit(1)), Some(100.0)).is_none());
        assert!(tracker.sample("second", actor.clone(), 11, Some(hit(2)), Some(90.0)).is_none());
        assert!(tracker.sample("second", actor.clone(), 12, None, None).is_none());
        assert!(tracker.sample("second", actor.clone(), 13, Some(hit(3)), Some(80.0)).is_none());
        assert!(tracker.sample("second", actor, 14, Some(hit(4)), Some(76.0)).is_some());
    }

    fn sword() -> (comp::Inventory, comp::SkillSet, comp::CharacterState) {
        let inv = comp::Inventory::with_loadout_humanoid(LoadoutBuilder::empty()
            .active_mainhand(Some(Item::new_from_asset_expect("common.items.weapons.sword.starter")))
            .build());
        (inv, comp::SkillSet::default(),
            comp::CharacterState::Wielding(common::states::wielding::Data { is_sneaking: false }))
    }

    #[test]
    fn export_native_serializer_fixture() {
        use specs::Builder;
        let mut world = specs::World::new();
        let actor_entity = world.create_entity().build();
        let source_entity = world.create_entity().build();
        let mut bridge = BridgeState::new("native-offline-serializer", Dir::from_unnormalized(Vec3::unit_x()).unwrap());
        let actor = continuous_control::entity_ref(&bridge, actor_entity, 1);
        let source = continuous_control::entity_ref(&bridge, source_entity, 2);
        let body: comp::Body = comp::body::humanoid::Body::iter().find(|b|
            b.species == comp::body::humanoid::Species::Human
                && b.body_type == comp::body::humanoid::BodyType::Female).unwrap().into();
        let position = comp::Pos(Vec3::new(10.25, -4.5, 7.0));
        let source_position = comp::Pos(position.0 + Vec3::unit_x() * 2.0);
        let scale = comp::Scale(1.0);
        let (inventory, skills, character) = sword();
        let abilities = comp::ActiveAbilities::default();
        let context = AbilityContext::from(None, Some(&inventory), None);
        let resolve = |input| {
            let (ability, _, specified) = abilities.activate_ability(input, Some(&inventory),
                &skills, Some(&body), Some(&character), &context, None).unwrap();
            let id = specified.ability_id(Some(&character), Some(&inventory)).unwrap().to_owned();
            (ability, id, specified)
        };
        let (primary, primary_id, primary_specified) = resolve(AbilityInput::Primary);
        let (guard, guard_id, _) = resolve(AbilityInput::Guard);
        let CharacterAbility::ComboMelee2 { strikes, energy_cost_per_strike, specifier, auto_progress, .. } = &primary
            else { panic!("starter sword primary changed"); };
        let source_character = comp::CharacterState::ComboMelee2(common::states::combo_melee2::Data {
            static_data: common::states::combo_melee2::StaticData {
                strikes: strikes.iter().cloned().map(|s| s.to_duration()).collect(),
                energy_cost_per_strike: *energy_cost_per_strike, specifier: *specifier,
                auto_progress: *auto_progress,
                ability_info: AbilityInfo { tool: Some(comp::tool::ToolKind::Sword), hand: None,
                    input: InputKind::Primary, input_attr: Some(comp::controller::InputAttr {
                        select_pos: None, target_entity: Some(Uid(NonZeroU64::new(1).unwrap())),
                    }), ability_meta: primary.ability_meta(), ability: Some(primary_specified) },
            },
            exhausted: false, start_next_strike: false, timer: Duration::from_millis(25),
            stage_section: StageSection::Buildup, completed_strikes: 0,
            movement_modifier: None, ori_modifier: None,
        });
        let mut change = hit(77);
        change.time = Time(10.0);
        assert!(bridge.continuous.damage.sample(&bridge.session_id, actor.clone(), 109, Some(change), Some(100.0)).is_none());
        let mut health_records = Vec::new();
        for (tick, sync_time, health) in [(110, 11.25, 96.0), (111, 11.5, 92.0)] {
            bridge.tick = tick;
            change.time = Time(sync_time);
            assert!(bridge.continuous.damage.sample(&bridge.session_id, actor.clone(), tick, Some(change), Some(health)).is_some());
            health_records.push(bridge.continuous.damage.observation(change, tick));
        }
        let binding = binding_record(&bridge.session_id, bridge.tick, actor.clone());
        bridge.continuous.pre_hit = pre_hit::serializer_fixture(binding.clone(),
            serde_json::from_value(source.clone()).unwrap(), &source_character, &body, position.0, source_position.0);
        let primary_ready = readiness_for(&character, true, 100.0, &primary, false, true);
        let guard_ready = readiness_for(&character, true, 100.0, &guard, true, true);
        let envelope = melee_geometry(position.0, Vec3::unit_x(), body.max_radius(), body.height(), 1.0,
            source_position.0, body.max_radius(), body.height(), strikes[0].melee_constructor.range,
            strikes[0].melee_constructor.angle);
        let feedback = melee_feedback_record(envelope, primary_ready == Some(true), false, &primary_id, 0, Some(&character));
        let primary = ability_record(Some(&(primary, primary_id)), primary_ready, false, Some(&character), Some(1.0));
        let guard = ability_record(Some(&(guard, guard_id)), guard_ready, true, Some(&character), Some(1.0));
        let own = SelfObservation {
            geometry: geometry_record(Some(&body), Some(&position), Some(&scale)),
            orientation: Some([1.0, 0.0, 0.0]), state: state_facts(Some(&character)),
            last_health_change: Some(bridge.continuous.damage.observation(change, bridge.tick)),
            primary, guard,
        };
        let nearby = nearby_record(Some(&source_character), source.clone(),
            geometry_record(Some(&body), Some(&source_position), Some(&scale)),
            Some([-1.0, 0.0, 0.0]), Some(format!("{:?}", comp::Alignment::Npc)));
        let combat = observation_record(binding.clone(), own, vec![nearby], bridge.continuous.pre_hit.observation());
        let rock = Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        let geometry = local_motion::geometry_cells(position.0, Vec2::unit_x(), body.max_radius(), body.height(),
            |p| if p.x < 10 { None } else { Some(if p.z < 7 { rock } else { Block::empty() }) });
        let local = local_motion::observation_record(binding, Some(geometry));
        let controls = continuous_control::serializer_fixture(&mut bridge,
            serde_json::from_value(actor).unwrap(), serde_json::from_value(source).unwrap(), change, feedback);
        let mut capabilities = ready_event(&bridge.session_id, bridge.tick, "offline-fixture", Some(1))["capabilities"].clone();
        capabilities.as_object_mut().unwrap().retain(|name, _|
            matches!(name.as_str(), "combat_perception" | "local_geometry" | "continuous_control"));
        let fixture = json!({
            "fixture_schema": "native_production_serializer_component_bundle_v1",
            "capabilities": capabilities, "combat_perception": combat,
            "local_geometry": local, "continuous_control": controls,
            "health_change_records": health_records,
        });
        assert_eq!(fixture["combat_perception"]["self"]["last_health_change"]["client_local_sync_time"], 11.5);
        assert_eq!(fixture["health_change_records"][0]["instance"], fixture["health_change_records"][1]["instance"]);
        assert_ne!(fixture["health_change_records"][0]["client_local_sync_time"], fixture["health_change_records"][1]["client_local_sync_time"]);
        assert_eq!(fixture["continuous_control"]["owner"], "native_protection");
        assert_eq!(fixture["continuous_control"]["generation"], 8);
        assert_eq!(fixture["continuous_control"]["protection_generation"], 8);
        assert_eq!(fixture["continuous_control"]["melee_feedback"]["gameplay_success"], Value::Null);
        assert_eq!(fixture["combat_perception"]["pre_hit"]["rows"][0]["status"], "eligible_observed_potential_threat");
        assert_eq!(fixture["capabilities"]["continuous_control"]["selected_melee"], json!(["primary"]));
        assert_eq!(fixture["capabilities"]["continuous_control"]["native_protection"], true);
        assert_eq!(fixture["capabilities"]["continuous_control"]["legacy_control_fence_after_protection"], true);
        if let Some(path) = std::env::var_os("VELOREN_NATIVE_SERIALIZER_FIXTURE") {
            std::fs::write(path, serde_json::to_vec(&fixture).unwrap()).unwrap();
        }
    }

    #[test]
    fn starter_sword_uses_actual_inventory_resolver_two_strikes_and_equipped_guard() {
        let (inv, skills, wielding) = sword();
        let context = AbilityContext::from(None, Some(&inv), None);
        let active = comp::ActiveAbilities::default();
        let (primary, _, specified) = active.activate_ability(AbilityInput::Primary,
            Some(&inv), &skills, None, Some(&wielding), &context, None).unwrap();
        assert_eq!(specified.ability_id(Some(&wielding), Some(&inv)),
                   Some("common.abilities.sword.basic_double_slash"));
        let rows = strikes(&primary).unwrap();
        assert_eq!(rows.len(), 2);
        for s in &rows {
            assert_eq!(s["range_m"], 3.0);
            assert_eq!(s["half_angle_deg"], 45.0);
            assert_eq!(s["hit_timing_fraction"], 0.5);
        }
        // The actual starter item has speed 0.75, unlike the bare ability asset.
        assert_eq!(rows[0]["buildup_s"].as_f64().unwrap() as f32, 0.15 / 0.75);
        assert_eq!(rows[1]["recover_s"].as_f64().unwrap() as f32, 0.2 / 0.75);
        assert_eq!(readiness_for(&wielding, true, 0.0, &primary, false, true), Some(true));
        let (guard, _, _) = active.activate_ability(AbilityInput::Guard, Some(&inv), &skills,
            None, Some(&wielding), &context, None).unwrap();
        assert!(matches!(guard, CharacterAbility::BasicBlock { .. }));
        assert_eq!(readiness_for(&wielding, true, guard.energy_cost(), &guard, true, true), Some(true));
        assert_eq!(readiness_for(&wielding, true, guard.energy_cost() - 0.1, &guard, true, true), Some(false));
        assert_eq!(readiness_for(&wielding, false, 100.0, &guard, true, true), Some(false));
        assert_eq!(readiness_for(&wielding, true, 100.0, &guard, true, false), Some(false));
        let idle = comp::CharacterState::Idle(Default::default());
        assert_eq!(readiness_for(&idle, true, 100.0, &guard, true, true), Some(false));
    }

    #[test]
    fn pressed_guard_is_not_actual_basic_block_or_proven_mitigation() {
        let (_, _, wielding) = sword();
        assert_eq!(actual_guard(Some(&wielding))["actual_basic_block"], false);
        let info = AbilityInfo { tool: None, hand: None, input: InputKind::Block,
            input_attr: None, ability_meta: Default::default(), ability: None };
        let block = |stage_section| comp::CharacterState::BasicBlock(basic_block::Data {
            static_data: basic_block::StaticData {
                buildup_duration: Duration::from_secs_f32(0.1), recover_duration: Duration::from_secs_f32(0.1),
                max_angle: 45.0, block_strength: 10.0,
                parry_window: basic_block::ParryWindow { buildup: true, recover: false },
                ability_info: info, energy_cost: 1.0, energy_regen: 0.0, can_hold: true,
                blocked_attacks: comp::character_state::AttackFilters {
                    melee: true, projectiles: false, beams: false, ground_shockwaves: false,
                    air_shockwaves: false, explosions: false, arcs: false, pools: false,
                },
            }, timer: Duration::ZERO, stage_section, is_parry: false,
        });
        for phase in [StageSection::Buildup, StageSection::Action] {
            let actual = actual_guard(Some(&block(phase)));
            assert_eq!(actual["actual_basic_block"], true);
            assert_eq!(actual["melee_block_state"], true);
            assert!(actual["mitigating_now"].is_null());
        }

        assert_eq!(actual_guard(Some(&block(StageSection::Recover)))["melee_block_state"], false);
    }

    #[test]
    fn guard_wait_uses_native_remaining_equip_duration_not_inference_delay() {
        let equip = comp::CharacterState::Equipping(common::states::equipping::Data {
            static_data: common::states::equipping::StaticData { buildup_duration: Duration::from_millis(400) },
            timer: Duration::from_millis(100), is_sneaking: false,
        });
        assert_eq!(remaining_guard_transition_seconds(&equip), Some(0.3));
        assert!(!can_guard_state(&equip));
        assert_eq!(remaining_guard_transition_seconds(&comp::CharacterState::default()), None);
    }

    #[test]
    fn selected_melee_geometry_uses_range_halfangle_body_and_vertical_not_universal_distance() {
        let test = |target, range, angle| melee_geometry(
            Vec3::zero(), Vec3::unit_x(), 0.3, 1.8, 1.0, target, 0.3, 1.8, range, angle);
        assert_eq!(test(Vec3::new(3.2, 0.0, 0.0), 3.0, 45.0), "geometry_eligible_not_guaranteed_hit");
        assert_eq!(test(Vec3::new(3.2, 0.0, 0.0), 1.0, 45.0), "out_of_range");
        assert_eq!(test(Vec3::new(0.0, 2.0, 0.0), 3.0, 45.0), "outside_angle");
        assert_eq!(test(Vec3::new(0.0, 2.0, 0.0), 3.0, 100.0), "geometry_eligible_not_guaranteed_hit");
        assert_eq!(test(Vec3::new(0.5, 0.0, -3.5), 3.0, 45.0), "geometry_eligible_not_guaranteed_hit");
        assert_eq!(melee_geometry(Vec3::zero(), Vec3::unit_x(), 0.3, 1.8, 1.0,
            Vec3::new(0.5, 0.0, -3.5), 1.5, 0.5, 3.0, 45.0), "vertical_mismatch");
        assert_eq!(test(Vec3::new(f32::NAN, 0.0, 0.0), 3.0, 45.0), "unknown_geometry");
    }
}
