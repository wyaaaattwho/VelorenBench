//! Narrow, non-authoritative pre-emission melee evidence from synchronized ECS.
use super::*;
use common::{
    comp::melee::MeleeConstructorKind,
    states::utils::StageSection,
};
use continuous_control::EntityRef;

#[derive(Clone, Copy)]
struct BodyGeometry {
    position: Vec3<f32>,
    radius: f32,
    height: f32,
    eye_height: f32,
    scale: f32,
}

#[derive(Clone, Debug)]
struct Progress {
    signature: Value,
    phase: StageSection,
    exhausted: bool,
    strike: usize,
}

#[derive(Clone, Debug)]
pub(super) struct Row {
    source: EntityRef,
    progress: Option<Progress>,
    known_non_melee: bool,
    evidence: Value,
    remaining_ticks: Option<u64>,
}

#[derive(Clone, Debug)]
pub(super) struct Threat {
    pub source: EntityRef,
    pub evidence: Value,
    binding: Value,
    episode: u64,
    expires_tick: u64,
}

#[derive(Debug)]
struct Episode {
    progress: Option<Progress>,
    number: u64,
    consumed: bool,
    eligible_until: Option<u64>,
}

#[derive(Debug, Default)]
pub(super) struct Perception {
    binding: Value,
    rows: Vec<Row>,
    episodes: HashMap<EntityRef, Episode>,
}

impl Perception {
    pub(super) fn observation(&self) -> Value {
        json!({
            "schema_version": "1.0", "binding": self.binding,
            "status": if self.binding["tick"].is_u64() { "sampled" } else { "not_sampled" },
            "scope": "same_bounded_nearby_entities_projection_unobserved_entities_unknown",
            "supported_scope": "hinted_self_BasicMelee_or_ComboMelee2_positive_blockable_unemitted_strike",
            "unsupported_scope": "missing_hint_or_components_other_attack_types_and_unmodeled_secondary_effects",
            "confidence": "observed_potential_threat_not_confirmed_attacker",
            "episode_identity": "local_observed_strike_not_server_event_id_timer_rewind_does_not_rearm",
            "rows": self.rows.iter().map(|row| &row.evidence).collect::<Vec<_>>(),
        })
    }

    pub(super) fn reset(&mut self) {
        self.binding = Value::Null;
        self.rows.clear();
        self.episodes.clear();
    }

    pub(super) fn needs_sample(&self, binding: &Value) -> bool { self.binding != *binding }

    pub(super) fn sample(&mut self, binding: Value, mut rows: Vec<Row>) {
        let changed_actor = self.binding["session_id"] != binding["session_id"]
            || self.binding["actor"] != binding["actor"];
        if changed_actor { self.reset(); }
        let Some(tick) = binding["tick"].as_u64() else { return; };
        if self.binding["tick"].as_u64().is_some_and(|old| tick <= old) { return; }
        for row in &mut rows {
            let previous = self.episodes.get(&row.source).and_then(|e| e.progress.as_ref());
            let new_episode = row.progress.as_ref().is_some_and(|now| previous.is_none_or(|old|
                old.signature != now.signature || old.strike != now.strike
                || (old.exhausted || old.phase == StageSection::Recover)
                    && !now.exhausted && now.phase == StageSection::Buildup));
            if new_episode {
                self.episodes.insert(row.source.clone(), Episode {
                    progress: row.progress.clone(), number: tick, consumed: false, eligible_until: None,
                });
            } else if let Some(episode) = self.episodes.get_mut(&row.source) {
                // Missing information or visibility does not rearm an episode.
                if row.progress.is_some() || row.known_non_melee {
                    episode.progress = row.progress.clone();
                }
            }
            if let Some(episode) = self.episodes.get_mut(&row.source) {
                if let Some(remaining) = row.remaining_ticks {
                    let deadline = episode.eligible_until.get_or_insert(tick.saturating_add(remaining));
                    row.evidence["expires_tick"] = json!(deadline);
                    if tick > *deadline {
                        row.remaining_ticks = None;
                        row.evidence["status"] = json!("stale_native_window");
                        row.evidence["kind"] = json!("pre_hit_assessment");
                    }
                }
            }
        }
        self.binding = binding;
        self.rows = rows;
    }

    pub(super) fn take_candidate(
        &mut self, binding: &Value, shutdown: bool, protection_active: bool,
    ) -> Option<Threat> {
        if shutdown || protection_active || *binding != self.binding || binding["actor"].is_null() {
            return None;
        }
        let tick = binding["tick"].as_u64()?;
        // Identity order only selects a guard bearing among already evidenced
        // threats; distance/alignment never establishes hostility.
        let row = self.rows.iter().filter(|r| r.remaining_ticks.is_some()
            && self.episodes.get(&r.source).is_some_and(|e| !e.consumed))
            .min_by_key(|r| r.source.uid)?;
        let episode = self.episodes.get_mut(&row.source)?;
        episode.consumed = true;
        let expires_tick = episode.eligible_until?;
        if tick > expires_tick { return None; }
        let mut evidence = row.evidence.clone();
        evidence["binding"] = binding.clone();
        evidence["local_strike_episode"] = json!(episode.number);
        evidence["expires_tick"] = json!(expires_tick);
        Some(Threat { source: row.source.clone(), evidence, binding: binding.clone(),
            episode: episode.number, expires_tick })
    }

    pub(super) fn validate(&self, threat: &Threat, binding: &Value) -> Result<(), &'static str> {
        if *binding != self.binding || binding["actor"] != threat.binding["actor"]
            || binding["session_id"] != threat.binding["session_id"] {
            return Err("pre_hit_actor_or_observation_changed");
        }
        if binding["tick"].as_u64().is_none_or(|tick| tick > threat.expires_tick) {
            return Err("pre_hit_native_window_expired");
        }
        let row = self.rows.iter().find(|r| r.source == threat.source)
            .ok_or("pre_hit_source_missing")?;
        if !self.episodes.get(&threat.source).is_some_and(|e| e.number == threat.episode) {
            return Err("pre_hit_strike_episode_changed");
        }
        if row.remaining_ticks.is_none() { return Err("pre_hit_evidence_no_longer_eligible"); }
        Ok(())
    }
}

fn evaluate(
    source: EntityRef, actor_uid: u64, character: Option<&comp::CharacterState>,
    source_alive: Option<bool>, attacker: Option<BodyGeometry>, actor: Option<BodyGeometry>,
    look: Option<Vec3<f32>>, attack_speed: Option<f32>,
    line_of_sight: impl FnOnce() -> Result<(), &'static str>,
) -> Row {
    let mut row = Row {
        source: source.clone(), progress: None, known_non_melee: false, remaining_ticks: None,
        evidence: json!({
            "source_entity": source, "attacker_uid": null,
            "kind": "pre_hit_assessment", "confirmed_attacker": false,
            "provenance": "synced_character_state_input_hint_and_local_geometry",
            "target_hint_authoritative": false, "status": "unknown_character_state",
            "geometry": null, "line_of_sight": null, "attack_source": null,
            "variant": null, "phase": null, "phase_timer_s": null,
            "exhausted": null, "completed_strikes": null, "target_hint_uid": null,
            "active_strike": null, "nominal_seconds_until_emission": null, "timing_limit": null,
            "expires_tick": null,
        }),
    };
    let Some(character) = character else { return row; };
    row.evidence["variant"] = json!(character.to_string());
    row.evidence["phase"] = json!(character.stage_section().map(|p| format!("{p:?}")));
    row.evidence["phase_timer_s"] = json!(character.timer().map(|t| t.as_secs_f32()));
    let data = match character {
        comp::CharacterState::BasicMelee(c) => Some((
            "BasicMelee", &c.static_data.melee_constructor, c.static_data.buildup_duration,
            c.static_data.swing_duration, c.static_data.hit_timing, c.stage_section,
            c.timer, c.exhausted, 0, c.static_data.ability_info,
        )),
        comp::CharacterState::ComboMelee2(c) if !c.static_data.strikes.is_empty() => {
            let s = c.strike_data();
            Some(("ComboMelee2", &s.melee_constructor, s.buildup_duration, s.swing_duration,
                s.hit_timing, c.stage_section, c.timer, c.exhausted, c.completed_strikes,
                c.static_data.ability_info))
        },
        _ => None,
    };
    let Some((variant, melee, buildup, swing, hit_timing, phase, timer, exhausted, strike, info)) = data else {
        row.known_non_melee = !matches!(character,
            comp::CharacterState::BasicMelee(_) | comp::CharacterState::ComboMelee2(_));
        row.evidence["status"] = json!("unsupported_attack_state_not_proven_safe");
        return row;
    };
    let hint = info.input_attr.and_then(|a| a.target_entity).map(|uid| uid.0.get());
    row.progress = Some(Progress {
        signature: json!({"variant": variant, "ability": info.ability, "melee": melee,
            "buildup": buildup.as_secs_f32(), "swing": swing.as_secs_f32(), "hit_timing": hit_timing}),
        phase, exhausted, strike,
    });
    row.evidence.as_object_mut().unwrap().extend(json!({
        "variant": variant, "phase": format!("{phase:?}"), "phase_timer_s": timer.as_secs_f32(),
        "attack_source": "Melee",
        "exhausted": exhausted, "completed_strikes": strike, "target_hint_uid": hint,
        "active_strike": {"range_m": melee.range, "half_angle_deg": melee.angle,
            "buildup_s": buildup.as_secs_f32(), "swing_s": swing.as_secs_f32(),
            "hit_timing_fraction": hit_timing, "blockable": melee.blockable},
    }).as_object().unwrap().clone());
    let outcome = (|| {
        match source_alive {
            Some(true) => {},
            Some(false) => return Err("source_dead_or_downed"),
            None => return Err("unknown_source_health"),
        }
        if source.uid == actor_uid { return Err("source_is_actor"); }
        match hint {
            Some(uid) if uid == actor_uid => {},
            Some(_) => return Err("input_hint_names_other_actor"),
            None => return Err("unknown_target_hint"),
        }
        if exhausted || !matches!(phase, StageSection::Buildup | StageSection::Action) {
            return Err("supported_melee_outside_unemitted_window");
        }
        if !melee.blockable { return Err("unsupported_unblockable_melee"); }
        if melee.scaled.is_some() { return Err("unknown_scaled_constructor"); }
        let damage = match melee.kind {
            MeleeConstructorKind::Slash { damage, .. } | MeleeConstructorKind::Stab { damage, .. }
            | MeleeConstructorKind::Sear { damage, .. } | MeleeConstructorKind::Bash { damage, .. }
            | MeleeConstructorKind::Hook { damage, .. } | MeleeConstructorKind::NecroticVortex { damage, .. }
            | MeleeConstructorKind::SonicWave { damage, .. } => damage,
        };
        if !damage.is_finite() || damage <= 0.0 { return Err("unsupported_nonpositive_damage"); }
        let (a, b, look) = attacker.zip(actor).zip(look).map(|((a, b), l)| (a, b, l))
            .ok_or("unknown_body_or_orientation")?;
        if !b.scale.is_finite() || b.scale <= 0.0 { return Err("unknown_body_scale"); }
        let geometry = combat_perception::melee_geometry(
            a.position, look, a.radius * a.scale, a.height, a.scale,
            b.position, b.radius * b.scale, b.height, melee.range, melee.angle,
        );
        row.evidence["geometry"] = json!(geometry);
        if geometry != "geometry_eligible_not_guaranteed_hit" { return Err(geometry); }
        let los = line_of_sight();
        row.evidence["line_of_sight"] = json!(match los {
            Ok(()) => "loaded_center_ray_clear_not_server_multi_ray_wall_test",
            Err(_) => "occluded_or_unloaded",
        });
        los?;
        let speed = attack_speed.filter(|s| s.is_finite() && *s > 0.0)
            .ok_or("unknown_attack_clock")?;
        if !hit_timing.is_finite() || !(0.0..=1.0).contains(&hit_timing) {
            return Err("unknown_hit_timing");
        }
        let until_hit = if phase == StageSection::Buildup {
            buildup.saturating_sub(timer).as_secs_f32() + swing.as_secs_f32() * hit_timing
        } else {
            (swing.as_secs_f32() * hit_timing - timer.as_secs_f32()).max(0.0)
        };
        let seconds = until_hit / speed;
        if !seconds.is_finite() { return Err("unknown_attack_clock"); }
        row.evidence["nominal_seconds_until_emission"] = json!(seconds);
        row.evidence["timing_limit"] = json!("current_native_attack_clock_plus_three_phase_transition_ticks_not_prediction");
        ((seconds * TPS as f32).ceil() as u64).checked_add(3).ok_or("unknown_attack_clock")
    })();
    match outcome {
        Ok(ticks) => {
            row.remaining_ticks = Some(ticks);
            row.evidence["kind"] = json!("observed_potential_melee_threat");
            row.evidence["status"] = json!("eligible_observed_potential_threat");
        },
        Err(reason) => row.evidence["status"] = json!(reason),
    }
    row
}

pub(super) fn scan(client: &Client, bridge: &BridgeState) -> Vec<Row> {
    let Some(position) = client.position() else { return Vec::new(); };
    let Some(actor_uid) = client.uid().map(|uid| uid.0.get()) else { return Vec::new(); };
    let state = client.state();
    let positions = state.read_storage::<comp::Pos>();
    let bodies = state.read_storage::<comp::Body>();
    let scales = state.read_storage::<comp::Scale>();
    let orientations = state.read_storage::<comp::Ori>();
    let characters = state.read_storage::<comp::CharacterState>();
    let healths = state.read_storage::<comp::Health>();
    let stats = state.read_storage::<comp::Stats>();
    let geometry = |entity| positions.get(entity).zip(bodies.get(entity)).map(|(p, b)| {
        let scale = scales.get(entity).map_or(1.0, |s| s.0);
        BodyGeometry { position: p.0, radius: b.max_radius(), height: b.height(),
            eye_height: b.eye_height(scale), scale }
    });
    let own_geometry = geometry(client.entity());
    nearby_entities(client, bridge, position).into_iter().filter_map(|nearby| {
        let uid = nearby["uid"].as_u64()?;
        let entity = entity_by_uid(client, uid)?;
        if continuous_control::entity_ref(bridge, entity, uid) != nearby["control_target"] { return None; }
        let source: EntityRef = serde_json::from_value(nearby["control_target"].clone())
            .expect("native nearby projection produces EntityRef");
        let attacker_geometry = geometry(entity);
        let character = characters.get(entity);
        let alive = healths.get(entity).map(|h| !h.is_dead && !comp::is_downed(Some(h), character));
        Some(evaluate(source, actor_uid, character, alive, attacker_geometry, own_geometry,
            orientations.get(entity).map(|ori| ori.look_dir().to_vec()),
            stats.get(entity).map(|s| s.attack_speed_modifier), || {
                let (a, b) = attacker_geometry.zip(own_geometry).ok_or("unknown_body_geometry")?;
                continuous_control::loaded_line_of_sight(client,
                    a.position + Vec3::unit_z() * a.eye_height,
                    b.position + Vec3::unit_z() * b.height * b.scale * 0.5)
            }))
    }).collect()
}

#[cfg(test)]
pub(super) fn serializer_fixture(
    binding: Value, source: EntityRef, character: &comp::CharacterState,
    body: &comp::Body, actor_position: Vec3<f32>, source_position: Vec3<f32>,
) -> Perception {
    let geometry = |position| BodyGeometry { position, radius: body.max_radius(),
        height: body.height(), eye_height: body.eye_height(1.0), scale: 1.0 };
    let row = evaluate(source, binding["actor"]["uid"].as_u64().unwrap(), Some(character),
        Some(true), Some(geometry(source_position)), Some(geometry(actor_position)),
        Some(-Vec3::unit_x()), Some(1.0), || Ok(()));
    let mut perception = Perception::default();
    perception.sample(binding, vec![row]);
    perception
}

#[cfg(test)]
mod tests {
    use super::*;
    use common::{comp::ability::CharacterAbility, states::{basic_melee, basic_ranged, combo_melee2,
        utils::AbilityInfo}};

    fn info(hint: Option<u64>) -> AbilityInfo {
        AbilityInfo {
            tool: None, hand: None, input: InputKind::Primary,
            input_attr: Some(comp::controller::InputAttr {
                select_pos: None, target_entity: hint.and_then(NonZeroU64::new).map(Uid),
            }),
            ability_meta: Default::default(), ability: None,
        }
    }

    fn attack(phase: StageSection, hint: Option<u64>) -> comp::CharacterState {
        let CharacterAbility::BasicMelee { mut melee_constructor, .. } = CharacterAbility::default()
            else { panic!("native default ability changed"); };
        melee_constructor.range = 3.0;
        melee_constructor.angle = 45.0;
        melee_constructor.kind = MeleeConstructorKind::Slash {
            damage: 5.0, poise: 0.0, knockback: 0.0, energy_regen: 0.0,
        };
        comp::CharacterState::BasicMelee(basic_melee::Data {
            static_data: basic_melee::StaticData {
                buildup_duration: Duration::from_millis(100),
                swing_duration: Duration::from_millis(200),
                recover_duration: Duration::from_millis(100), hit_timing: 0.5,
                melee_constructor, movement_modifier: Default::default(),
                ori_modifier: Default::default(), frontend_specifier: None, ability_info: info(hint),
            },
            timer: Duration::from_millis(50), stage_section: phase, exhausted: false,
            movement_modifier: None, ori_modifier: None,
        })
    }

    fn body(position: Vec3<f32>) -> BodyGeometry {
        BodyGeometry { position, radius: 0.35, height: 1.8, eye_height: 1.6, scale: 1.0 }
    }

    fn row(character: &comp::CharacterState, position: Vec3<f32>, los: Result<(), &'static str>) -> Row {
        evaluate(EntityRef { uid: 2, lifecycle: "source-a".into() }, 1, Some(character),
            Some(true), Some(body(Vec3::zero())), Some(body(position)),
            Some(Vec3::unit_x()), Some(1.0), || los)
    }

    fn binding(tick: u64) -> Value {
        json!({"session_id": "test", "actor": {"uid": 1, "lifecycle": "actor-a"},
            "tick": tick, "frame": "veloren_world_xyz_meters"})
    }

    #[test]
    fn pre_hit_routes_danger_without_hp_drop_while_remote_decisions_pending() {
        let mut perception = Perception::default();
        perception.sample(binding(10), vec![row(&attack(StageSection::Buildup, Some(1)),
            Vec3::new(2.0, 0.0, 0.0), Ok(()))]);
        let threat = perception.take_candidate(&binding(10), false, false).unwrap();
        assert_eq!(threat.evidence["confirmed_attacker"], false);
        assert!(threat.evidence["attacker_uid"].is_null());
        assert!(matches!(continuous_control::select_danger(None, Some(threat)),
            Some(continuous_control::Danger::PreHit(_))));
    }

    #[test]
    fn geometry_other_target_benign_occluded_and_missing_information_are_not_eligible() {
        let active = attack(StageSection::Buildup, Some(1));
        for (position, expected) in [
            (Vec3::new(8.0, 0.0, 0.0), "out_of_range"),
            (Vec3::new(0.0, 2.0, 0.0), "outside_angle"),
        ] {
            assert_eq!(row(&active, position, Ok(())).evidence["status"], expected);
        }
        let mut short_actor = body(Vec3::new(0.5, 0.0, -3.5));
        short_actor.height = 0.5;
        let vertical = evaluate(EntityRef { uid: 2, lifecycle: "source-a".into() }, 1,
            Some(&active), Some(true), Some(body(Vec3::zero())), Some(short_actor),
            Some(Vec3::unit_x()), Some(1.0), || Ok(()));
        assert_eq!(vertical.evidence["status"], "vertical_mismatch");
        let point = Vec3::new(2.0, 0.0, 0.0);
        for (character, expected) in [
            (attack(StageSection::Buildup, Some(9)), "input_hint_names_other_actor"),
            (attack(StageSection::Buildup, None), "unknown_target_hint"),
            (comp::CharacterState::default(), "unsupported_attack_state_not_proven_safe"),
            (attack(StageSection::Recover, Some(1)), "supported_melee_outside_unemitted_window"),
        ] {
            let result = row(&character, point, Ok(()));
            assert_eq!(result.evidence["status"], expected);
            assert!(result.remaining_ticks.is_none());
        }
        let mut harmless = active.clone();
        if let comp::CharacterState::BasicMelee(c) = &mut harmless {
            c.static_data.melee_constructor.kind = MeleeConstructorKind::Slash {
                damage: 0.0, poise: 0.0, knockback: 0.0, energy_regen: 0.0,
            };
        }
        assert_eq!(row(&harmless, point, Ok(())).evidence["status"], "unsupported_nonpositive_damage");
        for error in ["control_target_occluded", "control_target_unloaded"] {
            let result = row(&active, point, Err(error));
            assert!(result.remaining_ticks.is_none());
            assert_eq!(result.evidence["line_of_sight"], "occluded_or_unloaded");
        }
        let unknown = evaluate(EntityRef { uid: 2, lifecycle: "source-a".into() }, 1,
            Some(&active), Some(true), None, Some(body(point)), None, Some(1.0), || Ok(()));
        assert_eq!(unknown.evidence["status"], "unknown_body_or_orientation");
        for (alive, expected) in [(None, "unknown_source_health"), (Some(false), "source_dead_or_downed")] {
            let result = evaluate(EntityRef { uid: 2, lifecycle: "source-a".into() }, 1,
                Some(&active), alive, Some(body(Vec3::zero())), Some(body(point)),
                Some(Vec3::unit_x()), Some(1.0), || panic!("invalid source must not query LOS"));
            assert_eq!(result.evidence["status"], expected);
        }
        let unknown_clock = evaluate(EntityRef { uid: 2, lifecycle: "source-a".into() }, 1,
            Some(&active), Some(true), Some(body(Vec3::zero())), Some(body(point)),
            Some(Vec3::unit_x()), None, || Ok(()));
        assert_eq!(unknown_clock.evidence["status"], "unknown_attack_clock");
    }

    #[test]
    fn strike_episode_is_idempotent_across_pending_inference_and_fresh_owner_takeover() {
        let mut perception = Perception::default();
        let active = attack(StageSection::Buildup, Some(1));
        let point = Vec3::new(2.0, 0.0, 0.0);
        perception.sample(binding(10), vec![row(&active, point, Ok(()))]);
        let threat = perception.take_candidate(&binding(10), false, false).unwrap();
        for tick in 11..=threat.expires_tick {
            perception.sample(binding(tick), vec![row(&active, point, Ok(()))]);
            assert!(perception.validate(&threat, &binding(tick)).is_ok());
            assert!(perception.take_candidate(&binding(tick), false, true).is_none());
            // Releasing/replacing native protection does not rearm the same strike.
            assert!(perception.take_candidate(&binding(tick), false, false).is_none());
        }
        let expired = threat.expires_tick + 1;
        perception.sample(binding(expired), vec![row(&active, point, Ok(()))]);
        assert_eq!(perception.validate(&threat, &binding(expired)), Err("pre_hit_native_window_expired"));
        perception.sample(binding(expired + 1), vec![row(&attack(StageSection::Recover, Some(1)), point, Ok(()))]);
        perception.sample(binding(expired + 2), vec![row(&active, point, Ok(()))]);
        assert!(perception.take_candidate(&binding(expired + 2), false, false).is_some());
    }

    #[test]
    fn shutdown_stale_actor_stale_tick_and_lost_source_cannot_act() {
        let mut perception = Perception::default();
        let active = attack(StageSection::Action, Some(1));
        let point = Vec3::new(2.0, 0.0, 0.0);
        perception.sample(binding(10), vec![row(&active, point, Ok(()))]);
        assert!(perception.take_candidate(&binding(10), true, false).is_none());
        assert!(perception.take_candidate(&binding(9), false, false).is_none());
        let mut wrong = binding(10);
        wrong["actor"]["lifecycle"] = json!("respawned");
        assert!(perception.take_candidate(&wrong, false, false).is_none());
        let threat = perception.take_candidate(&binding(10), false, false).unwrap();
        assert_eq!(perception.validate(&threat, &wrong), Err("pre_hit_actor_or_observation_changed"));
        perception.sample(binding(11), vec![]);
        assert_eq!(perception.validate(&threat, &binding(11)), Err("pre_hit_source_missing"));
        perception.sample(binding(12), vec![row(&active, point, Ok(()))]);
        assert!(perception.take_candidate(&binding(12), false, false).is_none());
    }

    #[test]
    fn unconsumed_frozen_telegraph_cannot_be_renewed_by_repeated_native_samples() {
        let mut perception = Perception::default();
        let active = attack(StageSection::Buildup, Some(1));
        let point = Vec3::new(2.0, 0.0, 0.0);
        perception.sample(binding(10), vec![row(&active, point, Ok(()))]);
        assert!(perception.take_candidate(&binding(10), false, true).is_none());
        perception.sample(binding(100), vec![row(&active, point, Ok(()))]);
        assert!(perception.take_candidate(&binding(100), false, false).is_none());
        assert_eq!(perception.observation()["rows"][0]["status"], "stale_native_window");
    }

    #[test]
    fn unemitted_action_remains_potential_even_when_emission_is_due_but_exhausted_does_not() {
        let mut active = attack(StageSection::Action, Some(1));
        if let comp::CharacterState::BasicMelee(c) = &mut active { c.timer = Duration::from_millis(150); }
        let point = Vec3::new(2.0, 0.0, 0.0);
        assert_eq!(row(&active, point, Ok(())).remaining_ticks, Some(3));
        if let comp::CharacterState::BasicMelee(c) = &mut active { c.exhausted = true; }
        assert!(row(&active, point, Ok(())).remaining_ticks.is_none());
    }

    #[test]
    fn combo_uses_active_strike_not_a_universal_first_strike_envelope() {
        let comp::CharacterState::BasicMelee(basic) = attack(StageSection::Buildup, Some(1)) else { unreachable!() };
        let first = combo_melee2::Strike {
            melee_constructor: basic.static_data.melee_constructor,
            buildup_duration: Duration::from_millis(100), swing_duration: Duration::from_millis(200),
            hit_timing: 0.5, recover_duration: Duration::from_millis(100),
            movement: Default::default(), movement_modifier: Default::default(),
            ori_modifier: Default::default(), custom_combo: Default::default(),
        };
        let mut second = first.clone();
        second.melee_constructor.range = 0.5;
        let mut combo = comp::CharacterState::ComboMelee2(combo_melee2::Data {
            static_data: combo_melee2::StaticData { strikes: vec![first, second],
                energy_cost_per_strike: 0.0, specifier: None, auto_progress: false, ability_info: info(Some(1)) },
            exhausted: false, start_next_strike: false, timer: Duration::ZERO,
            stage_section: StageSection::Buildup, completed_strikes: 1,
            movement_modifier: None, ori_modifier: None,
        });
        let point = Vec3::new(2.0, 0.0, 0.0);
        assert_eq!(row(&combo, point, Ok(())).evidence["status"], "out_of_range");
        if let comp::CharacterState::ComboMelee2(c) = &mut combo { c.completed_strikes = 0; }
        assert!(row(&combo, point, Ok(())).remaining_ticks.is_some());
    }

    #[test]
    fn basic_ranged_recover_is_explicitly_unsupported_not_harmless() {
        let ranged = comp::CharacterState::BasicRanged(basic_ranged::Data {
            static_data: basic_ranged::StaticData {
                buildup_duration: Duration::from_millis(100), recover_duration: Duration::from_millis(100),
                projectile_spread: None,
                projectile: comp::projectile::ProjectileConstructor {
                    kind: comp::projectile::ProjectileConstructorKind::Pointed, attack: None,
                    scaled: None, homing_rate: None, split: None, lifetime_override: None,
                    limit_per_ability: false, override_collider: None,
                },
                projectile_body: comp::Body::Object(comp::object::Body::GrenadeClay),
                projectile_light: None, projectile_speed: 20.0,
                num_projectiles: common::comp::ability::Amount::Value(1), ability_info: info(Some(1)),
                movement_modifier: Default::default(), ori_modifier: Default::default(),
                auto_aim: false, vertical_angle_offset: 0.0, marker: None,
            },
            timer: Duration::ZERO, stage_section: StageSection::Recover, exhausted: false,
            movement_modifier: None, ori_modifier: None,
        });
        let result = row(&ranged, Vec3::new(2.0, 0.0, 0.0), Ok(()));
        assert_eq!(result.evidence["status"], "unsupported_attack_state_not_proven_safe");
        assert!(result.remaining_ticks.is_none());
    }
}
