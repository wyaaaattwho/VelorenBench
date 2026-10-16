//! One coherent, updateable motor owner. No tactical policy or inference timer.
use super::*;
use serde::Serialize;

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq, Hash)]
#[serde(deny_unknown_fields)]
pub struct EntityRef {
    pub(super) uid: u64,
    pub(super) lifecycle: String,
}

pub(super) fn entity_ref(state: &BridgeState, entity: Entity, uid: u64) -> Value {
    // Specs' entity generation changes when a local entity slot is reused.
    // The bridge incarnation also fences a restart with a reused session ID.
    json!({"uid": uid, "lifecycle": format!("{}:{entity:?}", state.continuous.incarnation)})
}

pub(super) fn actor_ref(client: &Client, state: &BridgeState) -> Value {
    client.uid().map_or(Value::Null, |uid| entity_ref(state, client.entity(), uid.0.get()))
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Owner {
    owner: String,
    generation: u64,
    sequence: u64,
    expected_session_id: String,
    actor: EntityRef,
    #[serde(default)]
    expected_protection_generation: Option<u64>,
}

#[derive(Clone, Debug)]
struct Desired {
    owner: Owner,
    movement: Vec2<f32>,
    speed: f32,
    vertical_input: VerticalInput,
    movement_intent: Option<MovementIntent>,
    destination: Option<Vec3<f32>>,
    target: Option<EntityRef>,
    primary: bool,
    secondary: bool,
    auxiliary: Option<usize>,
    fly: bool,
    jump: bool,
    block: bool,
    facing: Facing,
    look_direction: Option<Dir>,
    melee: Option<MeleeOperation>,
    context: Option<local_motion::MotionContext>,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum MeleeOperation { Primary }

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum VerticalInput {
    #[default]
    Neutral,
    SwimUp,
    SwimDown,
    FlyUp,
    FlyDown,
}

impl VerticalInput {
    fn value(self) -> f32 {
        match self {
            Self::Neutral => 0.0,
            Self::SwimUp | Self::FlyUp => 1.0,
            Self::SwimDown | Self::FlyDown => -1.0,
        }
    }
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum Facing {
    #[default]
    Auto,
    Travel,
    Track,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum MovementIntent {
    Approach,
    Retreat,
    Left,
    Right,
    Destination,
}

#[derive(Debug)]
pub(super) struct Controls {
    incarnation: String,
    generation: u64,
    owner: String,
    sequence: u64,
    actor: Option<EntityRef>,
    retired: bool,
    active: Option<Desired>,
    receipt: Value,
    motion: Option<local_motion::MotionFeedback>,
    route: Option<local_route::Route>,
    route_failure: Option<Value>,
    pub(super) damage: combat_perception::DamageTracker,
    pub(super) pre_hit: pre_hit::Perception,
    protection: Option<Protection>,
    protection_receipt: Value,
    protection_fence: bool,
    protection_generation: u64,
    shutdown: bool,
    melee_feedback: Value,
}

#[derive(Clone, Debug)]
struct Protection {
    actor: Value,
    evidence: Value,
    target: Option<EntityRef>,
    guard_requested_tick: Option<u64>,
    wield_deadline: Option<u64>,
    attack_source: Option<common::combat::AttackSource>,
    pre_hit: Option<pre_hit::Threat>,
}

impl Default for Controls {
    fn default() -> Self {
        use std::sync::atomic::{AtomicU64, Ordering};
        static NEXT: AtomicU64 = AtomicU64::new(1);
        let time = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_nanos();
        Self {
            incarnation: format!("{}-{time}-{}", std::process::id(), NEXT.fetch_add(1, Ordering::Relaxed)),
            generation: 0, owner: String::new(), sequence: 0, actor: None,
            retired: false, active: None, receipt: Value::Null,
            motion: None,
            route: None,
            route_failure: None,
            damage: combat_perception::DamageTracker::default(),
            pre_hit: pre_hit::Perception::default(),
            protection: None, protection_receipt: Value::Null, protection_fence: false,
            protection_generation: 0,
            shutdown: false, melee_feedback: Value::Null,
        }
    }
}

impl Controls {
    pub(super) fn is_active(&self) -> bool { self.active.is_some() || self.protection.is_some() }

    pub(super) fn vertical_input(&self) -> f32 {
        self.active.as_ref().map_or(0.0, |desired| desired.vertical_input.value())
    }

    pub(super) fn strafing(&self) -> bool {
        self.active.as_ref().is_none_or(|d|
            d.facing == Facing::Track || d.facing == Facing::Auto && d.target.is_some())
    }

    pub(super) fn observation(&self) -> Value {
        json!({
            "active": self.is_active(),
            "generation": self.generation,
            "owner": self.owner,
            "sequence": self.sequence,
            "retired": self.retired,
            "tracking_target": self.active.as_ref().and_then(|d| d.target.as_ref()),
            "movement_intent": self.active.as_ref().and_then(|d| d.movement_intent),
            "speed": self.active.as_ref().map(|d| d.speed),
            "vertical_input": self.active.as_ref().map(|d| d.vertical_input),
            "destination": self.active.as_ref().and_then(|d| d.destination)
                .map(|p| [p.x, p.y, p.z]),
            "primary_held": self.active.as_ref().is_some_and(|d| d.primary),
            "secondary_held": self.active.as_ref().is_some_and(|d| d.secondary),
            "auxiliary_held": self.active.as_ref().and_then(|d| d.auxiliary),
            "fly_held": self.active.as_ref().is_some_and(|d| d.fly),
            "jump_held": self.active.as_ref().is_some_and(|d| d.jump),
            "movement_context": self.active.as_ref().and_then(|d| d.context)
                .map(|context| context.label()),
            "block_held": self.active.as_ref().is_some_and(|d| d.block)
                || self.protection.as_ref().is_some_and(|p| p.guard_requested_tick.is_some()),
            "last_receipt": self.receipt,
            "motion_feedback": self.motion.as_ref().map(|motion| motion.observation()),
            "local_route": self.route.as_ref().map(|route| {
                let mut value = route.observation();
                value["route_id"] = json!(format!("{}:{}", self.incarnation, route.id));
                value
            }),
            "local_route_failure": self.route_failure,
            "facing": self.active.as_ref().map(|d| d.facing),
            "explicit_look_held": self.active.as_ref().is_some_and(|d| d.look_direction.is_some()),
            "native_protection": self.protection_receipt,
            "legacy_control_fence_required": self.protection_fence,
            "protection_generation": self.protection_generation,
            "melee_feedback": self.melee_feedback,
        })
    }

    fn authorize(&self, owner: &Owner) -> Result<(), &'static str> {
        if self.protection_fence && owner.expected_protection_generation != Some(self.protection_generation)
            || owner.generation < self.generation
            || (owner.generation == self.generation
                && (owner.owner != self.owner || self.retired || owner.sequence <= self.sequence))
        {
            Err("stale_control_owner")
        } else {
            Ok(())
        }
    }

    fn accept(&mut self, owner: &Owner) {
        self.generation = owner.generation;
        self.owner.clone_from(&owner.owner);
        self.sequence = owner.sequence;
        self.actor = Some(owner.actor.clone());
        self.retired = false;
    }

    fn retire_for_protection(&mut self, actor: EntityRef) -> Result<u64, &'static str> {
        let generation = self.generation.checked_add(1).ok_or("native_owner_generation_exhausted")?;
        self.generation = generation;
        self.sequence = 0;
        self.owner = "native_protection".into();
        self.actor = Some(actor);
        self.retired = true;
        self.protection_fence = true;
        self.protection_generation = generation;
        Ok(generation)
    }
}

pub(super) fn capability() -> Value {
    json!({
        "schema_version": "1.0",
        "full_state_replacement": true,
        "movement": "world_xy_unit_disk",
        "movement_intents": ["approach", "retreat", "left", "right", "destination"],
        "tracking": "observed_uid_and_lifecycle_loaded_terrain_line_of_sight",
        "held_inputs": ["primary", "secondary", "auxiliary", "fly", "jump", "block"],
        "movement_contexts": ["walk", "airborne_glide", "steered_mount", "steered_vehicle", "passenger", "entity_passenger"],
        "vertical_inputs": ["swim_up", "swim_down", "fly_up", "fly_down"],
        "owner_generation_fencing": true,
        "inference_watchdog": false,
        "gameplay_success_receipt": false,
        "facing_modes": ["auto", "travel", "track"],
        "explicit_look_direction": true,
        "local_motion_feedback": "loaded_swept_step_and_selected_segment_progress",
        "local_route": "walk_only_loaded_astar_segment_follower_v1",
        "native_protection": true,
        "pre_hit_protection": "hinted_self_supported_unemitted_melee_local_geometry_v1",
        "selected_melee": ["primary"],
        "server_health_outcomes": "server_Event_Outcome_HealthChange_v1",
        "legacy_control_fence_after_protection": true,
    })
}

fn parse(args: &Value, release: bool) -> Result<(Owner, Option<Desired>), &'static str> {
    let mut identity = args.as_object().ok_or("invalid_control_state")?.clone();
    let desired = if release {
        None
    } else {
        let movement = identity.remove("movement").ok_or("invalid_control_state")?;
        let movement: [f32; 2] =
            serde_json::from_value(movement).map_err(|_| "invalid_control_state")?;
        let movement = Vec2::new(movement[0], movement[1]);
        let speed: f32 = serde_json::from_value(
            identity.remove("speed").unwrap_or(json!(1.0)),
        ).map_err(|_| "invalid_control_state")?;
        let vertical_input: VerticalInput = serde_json::from_value(
            identity.remove("vertical_input").unwrap_or(json!("neutral")),
        ).map_err(|_| "invalid_control_state")?;
        let fly = identity.remove("fly").map_or(Ok(false), |v|
            v.as_bool().ok_or("invalid_control_state"))?;
        let jump = identity.remove("jump").map_or(Ok(false), |v|
            v.as_bool().ok_or("invalid_control_state"))?;
        if !speed.is_finite() || !(0.0..=1.0).contains(&speed)
            || !movement.x.is_finite() || !movement.y.is_finite()
            || movement.magnitude_squared() > 1.000001
        {
            return Err("invalid_control_state");
        }
        let target = identity.remove("target").ok_or("invalid_control_state")?;
        let target: Option<EntityRef> =
            serde_json::from_value(target).map_err(|_| "invalid_control_state")?;
        if target.as_ref().is_some_and(|t| t.uid == 0 || t.lifecycle.is_empty()) {
            return Err("invalid_control_state");
        }
        let movement_intent: Option<MovementIntent> = serde_json::from_value(
            identity.remove("movement_intent").unwrap_or(Value::Null),
        ).map_err(|_| "invalid_control_state")?;
        let destination: Option<[f32; 3]> = serde_json::from_value(
            identity.remove("destination").unwrap_or(Value::Null),
        ).map_err(|_| "invalid_control_state")?;
        if destination.is_some_and(|p| p.iter().any(|v| !v.is_finite()))
            || (movement_intent.is_some() && movement != Vec2::zero())
            || (movement_intent == Some(MovementIntent::Destination)) != destination.is_some()
            || (vertical_input != VerticalInput::Neutral
                && movement_intent == Some(MovementIntent::Destination))
            || (fly && movement_intent == Some(MovementIntent::Destination))
            || (jump && (fly || movement_intent == Some(MovementIntent::Destination)
                         || vertical_input != VerticalInput::Neutral))
            || (fly && matches!(vertical_input, VerticalInput::SwimUp | VerticalInput::SwimDown))
            || (!fly && matches!(vertical_input, VerticalInput::FlyUp | VerticalInput::FlyDown))
            || (movement_intent.is_some_and(|i| i != MovementIntent::Destination)
                && target.is_none())
        {
            return Err("invalid_control_state");
        }
        let destination = destination.map(|p| Vec3::new(p[0], p[1], p[2]));
        let primary = identity.remove("primary").and_then(|v| v.as_bool())
            .ok_or("invalid_control_state")?;
        let secondary = identity.remove("secondary").map_or(Ok(false), |v|
            v.as_bool().ok_or("invalid_control_state"))?;
        let auxiliary: Option<usize> = serde_json::from_value(
            identity.remove("auxiliary").unwrap_or(Value::Null),
        ).map_err(|_| "invalid_control_state")?;
        let block = identity.remove("block").and_then(|v| v.as_bool())
            .ok_or("invalid_control_state")?;
        let facing: Facing = serde_json::from_value(identity.remove("facing").unwrap_or(json!("auto")))
            .map_err(|_| "invalid_control_state")?;
        let look_direction: Option<[f32; 3]> = serde_json::from_value(
            identity.remove("look_direction").unwrap_or(Value::Null),
        ).map_err(|_| "invalid_control_state")?;
        let look_direction = look_direction.map(|point| {
            Dir::from_unnormalized(Vec3::new(point[0], point[1], point[2]))
                .ok_or("invalid_control_state")
        }).transpose()?;
        if look_direction.is_some() && (target.is_some() || facing != Facing::Auto) {
            return Err("invalid_control_state");
        }
        let melee: Option<MeleeOperation> = serde_json::from_value(
            identity.remove("melee").unwrap_or(Value::Null)).map_err(|_| "invalid_control_state")?;
        if melee.is_some() && (!primary || block || target.is_none() || facing == Facing::Travel) {
            return Err("invalid_control_state");
        }
        if facing == Facing::Track && target.is_none() { return Err("invalid_control_state"); }
        Some((movement, speed, vertical_input, movement_intent, destination, target,
              primary, secondary, auxiliary, fly, jump, block, facing, look_direction, melee))
    };
    let owner: Owner = serde_json::from_value(Value::Object(identity))
        .map_err(|_| "invalid_control_state")?;
    if owner.owner.is_empty() || owner.owner.len() > 128
        || owner.generation == 0 || owner.sequence == 0
        || owner.actor.uid == 0 || owner.actor.lifecycle.is_empty()
    {
        return Err("invalid_control_state");
    }
    let desired = desired.map(|(movement, speed, vertical_input, movement_intent, destination, target, primary, secondary, auxiliary, fly, jump, block, facing, look_direction, melee)| Desired {
        owner: owner.clone(), movement, speed, vertical_input, movement_intent, destination, target,
        primary, secondary, auxiliary, fly, jump, block, facing, look_direction, melee, context: None,
    });
    Ok((owner, desired))
}

fn actor_valid(client: &Client, state: &BridgeState, owner: &Owner) -> Result<(), &'static str> {
    if serde_json::to_value(&owner.actor).unwrap() != actor_ref(client, state) {
        return Err("control_actor_changed");
    }
    let health = client.current::<comp::Health>().ok_or("control_actor_unavailable")?;
    let character = client.current::<comp::CharacterState>()
        .ok_or("control_actor_unavailable")?;
    if health.is_dead || comp::is_downed(Some(&health), Some(&character)) {
        return Err("control_actor_unavailable");
    }
    Ok(())
}

pub(super) fn water_contact(physics: Option<&comp::PhysicsState>) -> Option<bool> {
    physics.map(|physics| matches!(physics.in_fluid, Some(comp::fluid_dynamics::Fluid::Liquid {
        kind: comp::fluid_dynamics::LiquidKind::Water, ..
    })))
}

pub(super) fn flight_eligibility(client: &Client) -> Option<bool> {
    use common::{link::Is, mounting::{Rider, Volume, VolumeRider}};
    let body = client.current::<comp::Body>()?;
    if let Some(rider) = client.current::<Is<Rider>>() {
        let mount = entity_by_uid(client, rider.mount.0.get())?;
        return client.state().read_storage::<comp::Body>().get(mount)
            .map(|body| body.flying_height() > 0.0 && !body.has_free_will());
    }
    if let Some(rider) = client.current::<Is<VolumeRider>>() {
        let Volume::Entity(uid) = rider.pos.kind else { return Some(false) };
        if !rider.is_steering_entity() { return Some(false); }
        let mount = entity_by_uid(client, uid.0.get())?;
        return client.state().read_storage::<comp::Body>().get(mount)
            .map(|body| body.flying_height() > 0.0 && !body.has_free_will());
    }
    Some(body.flying_height() > 0.0)
}

/// Use the SAME bounded public nearby-entity projection as observe. The
/// lifecycle is independent of position, so ordinary target motion stays valid.
fn observed_target<'a>(
    nearby: &'a [Value], target: &EntityRef,
) -> Result<&'a Value, &'static str> {
    let record = nearby.iter().find(|r| r["uid"].as_u64() == Some(target.uid))
        .ok_or("control_target_missing")?;
    if record["control_target"] != serde_json::to_value(target).unwrap() {
        return Err("control_target_changed");
    }
    if record["health"]["dead"] == true {
        return Err("control_target_dead");
    }
    Ok(record)
}

fn target_aim(
    client: &Client, bridge: &BridgeState, desired: &Desired,
) -> Result<Option<(Entity, Dir, Vec3<f32>)>, &'static str> {
    let Some(target) = desired.target.as_ref() else { return Ok(None) };
    tracked_target_aim(client, bridge, target).map(Some)
}

pub(super) fn observed_target_ref(
    client: &Client, bridge: &BridgeState, uid: u64,
) -> Result<EntityRef, &'static str> {
    let position = client.position().ok_or("control_actor_unavailable")?;
    let nearby = nearby_entities(client, bridge, position);
    let row = nearby.iter().find(|row| row["uid"].as_u64() == Some(uid))
        .ok_or("control_target_missing")?;
    serde_json::from_value(row["control_target"].clone()).map_err(|_| "control_target_changed")
}

pub(super) fn tracked_target_aim(
    client: &Client, bridge: &BridgeState, target: &EntityRef,
) -> Result<(Entity, Dir, Vec3<f32>), &'static str> {
    let position = client.position().ok_or("control_actor_unavailable")?;
    observed_target(&nearby_entities(client, bridge, position), target)?;
    let entity = entity_by_uid(client, target.uid).ok_or("control_target_missing")?;
    let state = client.state();
    let positions = state.read_storage::<comp::Pos>();
    let bodies = state.read_storage::<comp::Body>();
    let scales = state.read_storage::<comp::Scale>();
    let target_pos = positions.get(entity).ok_or("control_target_missing")?.0;
    let body = bodies.get(entity).ok_or("control_target_missing")?;
    let own_body = bodies.get(client.entity()).ok_or("control_actor_unavailable")?;
    let eye = position + Vec3::unit_z() * own_body.eye_height(
        scales.get(client.entity()).map_or(1.0, |s| s.0));
    let (direction, _) = center_target_aim(
        eye, target_pos, body.height(), scales.get(entity).map_or(1.0, |s| s.0),
        |eye, aim| loaded_line_of_sight(client, eye, aim),
    )?;
    Ok((entity, direction, target_pos))
}

fn center_target_aim(
    eye: Vec3<f32>, target_pos: Vec3<f32>, height: f32, scale: f32,
    check_sight: impl FnOnce(Vec3<f32>, Vec3<f32>) -> Result<(), &'static str>,
) -> Result<(Dir, Vec3<f32>), &'static str> {
    let aim = target_pos + Vec3::unit_z() * height * scale * 0.5;
    let direction = Dir::from_unnormalized(aim - eye).ok_or("control_target_invalid_geometry")?;
    check_sight(eye, aim)?;
    Ok((direction, aim))
}

fn checked_loaded_ray(distance: f32, travelled: f32, unobstructed: bool) -> Result<(), &'static str> {
    if !unobstructed || travelled + 0.001 < distance {
        Err("control_target_occluded")
    } else {
        Ok(())
    }
}

pub(super) fn loaded_line_of_sight(client: &Client, eye: Vec3<f32>, aim: Vec3<f32>) -> Result<(), &'static str> {
    // Shared loaded-terrain center-ray check; not the server's multi-ray hit test.
    let distance = (aim - eye).magnitude();
    let terrain = client.state().terrain();
    let (travelled, hit) = terrain.ray(eye, aim)
        .until(Block::is_solid).max_iter(512).cast();
    checked_loaded_ray(distance, travelled, matches!(hit, Ok(None)))
}

/// Geometry only: target/tactic/point choice remains with the caller.
fn movement_for_tick(
    desired: &Desired, actor: Option<Vec3<f32>>, target: Option<Vec3<f32>>,
) -> Result<Vec2<f32>, &'static str> {
    let Some(intent) = desired.movement_intent else { return Ok(desired.movement) };
    let actor = actor.ok_or("control_actor_unavailable")?;
    let point = if intent == MovementIntent::Destination {
        desired.destination.ok_or("invalid_control_state")?
    } else {
        target.ok_or("control_target_missing")?
    };
    // Walking steers horizontally; requested altitude is not a jump/fly command.
    let delta = point.xy() - actor.xy();
    if !delta.x.is_finite() || !delta.y.is_finite() {
        return Err("control_movement_invalid_geometry");
    }
    let distance = delta.x.hypot(delta.y);
    if !distance.is_finite() {
        return Err("control_movement_invalid_geometry");
    }
    if distance == 0.0 { return Ok(Vec2::zero()) }
    // Destination steering tapers linearly within one horizontal metre. This is
    // a bounded proportional motor input, not arrival/completion evidence.
    let toward = delta / if intent == MovementIntent::Destination {
        distance.max(1.0)
    } else {
        distance
    };
    Ok(match intent {
        MovementIntent::Approach | MovementIntent::Destination => toward,
        MovementIntent::Retreat => -toward,
        // Left/right are relative to facing the selected target, not world axes.
        MovementIntent::Left => Vec2::new(-toward.y, toward.x),
        MovementIntent::Right => Vec2::new(toward.y, -toward.x),
    })
}

fn resolve_inputs(
    client: &Client, state: &BridgeState, desired: &Desired,
) -> Result<(Option<(Entity, Dir, Vec3<f32>)>, Vec2<f32>, local_motion::MotionContext), &'static str> {
    actor_valid(client, state, &desired.owner)?;
    let context = local_motion::motion_context(client)?;
    if desired.context.is_some_and(|installed| !installed.same_body(context)) {
        return Err("control_motion_context_changed");
    }
    if let Some(slot) = desired.auxiliary {
        let Some(ability) = super::auxiliary_ability_snapshot(client)["active_set"]
            .as_array().and_then(|slots| slots.get(slot)).cloned()
        else {
            return Err("control_auxiliary_slot_unavailable");
        };
        if ability == json!("Empty") {
            return Err("control_auxiliary_ability_empty");
        }
    }
    if desired.fly && flight_eligibility(client) != Some(true) {
        return Err("control_fly_requires_capable_body_or_steered_mount");
    }
    if matches!(desired.vertical_input, VerticalInput::SwimUp | VerticalInput::SwimDown)
        && water_contact(client.current::<comp::PhysicsState>().as_ref()) != Some(true)
    {
        return Err("control_swim_requires_observed_water");
    }
    // Relative movement uses only the target that passed the existing lifecycle,
    // nearby-projection and loaded-terrain LOS checks. No alternate lookup.
    let aim = target_aim(client, state, desired)?;
    let movement = movement_for_tick(desired, client.position(), aim.map(|(_, _, p)| p))? * desired.speed;
    carrier_inputs_eligible(context, desired, movement)?;
    Ok((aim, movement, context))
}

fn carrier_inputs_eligible(
    context: local_motion::MotionContext, desired: &Desired, movement: Vec2<f32>,
) -> Result<(), &'static str> {
    if matches!(context, local_motion::MotionContext::Passenger(_)
        | local_motion::MotionContext::EntityPassenger(..))
        && (movement.magnitude_squared() > 0.0 || desired.vertical_input != VerticalInput::Neutral
            || desired.jump || desired.fly || desired.movement_intent.is_some())
    {
        return Err("control_motion_mount_not_steered");
    }
    Ok(())
}

fn changed_controlled_body(
    old: Option<&Desired>, context: local_motion::MotionContext,
) -> bool {
    old.and_then(|desired| desired.context)
        .is_some_and(|installed| !installed.same_body(context))
}

fn receipt(state: &BridgeState, status: &str, reason: Option<&str>) -> Value {
    json!({
        "status": status,
        "owner": state.continuous.owner,
        "generation": state.continuous.generation,
        "sequence": state.continuous.sequence,
        "actor": state.continuous.actor,
        "accepted_tick": state.tick,
        "applied_tick": state.tick,
        "session_id": state.session_id,
        "gameplay_success": null,
        "reason": reason,
        "momentum_and_animation_may_continue": true,
    })
}

fn release_inputs_with(desired: &Desired, mut send: impl FnMut(InputKind, bool)) {
    if desired.primary { send(InputKind::Primary, false); }
    if desired.secondary { send(InputKind::Secondary, false); }
    if let Some(slot) = desired.auxiliary { send(InputKind::Ability(slot), false); }
    if desired.fly { send(InputKind::Fly, false); }
    if desired.jump { send(InputKind::Jump, false); }
    if desired.block { send(InputKind::Block, false); }
}

pub(super) fn stop(client: &mut Client, state: &mut BridgeState, reason: &str) {
    settle_protection(client, state, reason);
    let Some(desired) = state.continuous.active.take() else { return };
    release_inputs_with(&desired, |input, pressed| {
        client.handle_input(input, pressed, None, None);
    });
    state.persistent_move = Vec2::zero();
    state.continuous.route = None;
    state.continuous.route_failure = None;
    state.continuous.retired = true;
    state.continuous.receipt = receipt(state, "released", Some(reason));
    record_event(state, "continuous_control_released", state.continuous.receipt.clone());
}

fn replace_inputs_with(
    old: Option<&Desired>, new: &Desired, mut send: impl FnMut(InputKind, bool),
) {
    let retarget = old.is_some_and(|old| old.target != new.target);
    if let Some(slot) = old.and_then(|d| d.auxiliary)
        && (new.auxiliary != Some(slot) || retarget)
    {
        send(InputKind::Ability(slot), false);
    }
    for (input, was, now) in [
        (InputKind::Primary, old.is_some_and(|d| d.primary), new.primary),
        (InputKind::Secondary, old.is_some_and(|d| d.secondary), new.secondary),
        (InputKind::Fly, old.is_some_and(|d| d.fly), new.fly),
        (InputKind::Jump, old.is_some_and(|d| d.jump), new.jump),
        (InputKind::Block, old.is_some_and(|d| d.block), new.block),
    ] {
        if was && (!now || retarget) { send(input, false); }
        if now && (!was || retarget) { send(input, true); }
    }
    if let Some(slot) = new.auxiliary
        && (old.and_then(|d| d.auxiliary) != Some(slot) || retarget)
    {
        send(InputKind::Ability(slot), true);
    }
}

fn continue_route(
    old: Option<&Desired>, new: &Desired, route: Option<&local_route::Route>,
    motion: Option<&local_motion::MotionFeedback>,
) -> bool {
    old.is_some_and(|old| old.owner.owner == new.owner.owner
        && old.owner.actor == new.owner.actor
        && old.destination == new.destination
        && old.movement_intent == Some(MovementIntent::Destination))
        && route.is_some_and(|route| route.blocked.is_none())
        && motion.is_none_or(|motion|
            !motion.status.starts_with("control_motion_")
            && !motion.status.starts_with("control_route_"))
}

fn route_moving(desired: &Desired, route: &Option<local_route::Route>) -> bool {
    desired.movement_intent == Some(MovementIntent::Destination) && route.is_some()
}

fn install_control_state(
    state: &mut BridgeState, desired: Desired, route: Option<local_route::Route>,
    position: Option<Vec3<f32>>, movement: Vec2<f32>, same_route: bool,
) -> Value {
    state.persistent_move = movement;
    if !same_route || route.is_none() {
        state.continuous.motion = position.map(|position|
            local_motion::MotionFeedback::new(position, movement, state.tick));
    }
    state.continuous.route = route;
    state.continuous.route_failure = None;
    state.continuous.accept(&desired.owner);
    state.continuous.active = Some(desired);
    state.continuous.receipt = receipt(state, "applied", None);
    state.continuous.receipt.clone()
}

fn sample_route_tick(
    route: &mut local_route::Route, motion: &mut Option<local_motion::MotionFeedback>,
    position: Vec3<f32>, tick: u64, radius: f32, z_min: f32, z_max: f32,
    cell: &mut impl FnMut(Vec3<i32>) -> Option<Block>,
) -> Result<Vec2<f32>, &'static str> {
    let previous_progress_tick = route.progress_tick;
    let result = route.step(position, tick, radius, z_min, z_max, cell);
    if let (Ok(direction), Some(feedback)) = (&result, motion) {
        let origin = route.breadcrumbs.last().copied().unwrap_or(position);
        let axis = route.nodes.get(route.segment)
            .map_or(*direction, |target| target.xy() - origin.xy());
        feedback.route_segment(origin, position, axis, route.progress_tick);
        if route.progress_tick != previous_progress_tick {
            feedback.status = "progress";
        }
    }
    result
}

fn apply_motion_check(
    state: &mut BridgeState, check: Result<(), &'static str>, movement: Vec2<f32>,
) -> bool {
    if let Err(code) = check {
        // Release movement only; held tracking and defense remain owned.
        state.persistent_move = Vec2::zero();
        if let Some(active) = &mut state.continuous.active {
            active.movement = Vec2::zero();
            active.movement_intent = None;
            active.destination = None;
        }
        if let Some(feedback) = &mut state.continuous.motion { feedback.status = code; }
        state.continuous.receipt = receipt(state, "motion_interrupted", Some(code));
        record_event(state, "continuous_motion_feedback", state.continuous.receipt.clone());
        false
    } else {
        state.persistent_move = movement;
        true
    }
}

pub(super) fn handle(
    client: &mut Client, state: &mut BridgeState, args: &Value, release: bool,
) -> Result<Value, &'static str> {
    if state.continuous.shutdown { return Err("operator_shutdown"); }
    let (owner, desired) = parse(args, release)?;
    if owner.expected_session_id != state.session_id { return Err("control_session_changed"); }
    state.continuous.authorize(&owner)?;
    if serde_json::to_value(&owner.actor).unwrap() != actor_ref(client, state) {
        return Err("control_actor_changed");
    }
    if state.active.is_some() { return Err("avatar_busy"); }
    if release {
        settle_protection(client, state, "replaced_by_fresh_release");
        state.continuous.accept(&owner);
        stop(client, state, "owner_released");
        state.continuous.retired = true;
        state.continuous.receipt = receipt(state, "released", Some("owner_released"));
        return Ok(state.continuous.receipt.clone());
    }
    let mut desired = desired.unwrap();
    let context = local_motion::motion_context(client)?;
    if changed_controlled_body(state.continuous.active.as_ref(), context) {
        stop(client, state, "control_motion_context_changed");
        return Err("control_motion_context_changed");
    }
    if desired.movement_intent == Some(MovementIntent::Destination)
        && context != local_motion::MotionContext::Walk {
        return Err("control_route_requires_unmounted_walk");
    }
    desired.context = Some(context);
    // Invalid proposals cannot release or retire the current valid owner.
    let (aim, mut movement, _) = resolve_inputs(client, state, &desired)?;
    let same_route = continue_route(state.continuous.active.as_ref(), &desired,
        state.continuous.route.as_ref(), state.continuous.motion.as_ref());
    let mut route = if desired.movement_intent == Some(MovementIntent::Destination) {
        if same_route { state.continuous.route.clone() } else {
            let position = client.position().ok_or("control_actor_unavailable")?;
            let (radius, z_min, z_max) = local_motion::actor_shape(client)?;
            let terrain = client.state().terrain();
            let mut route = local_route::Route::new(position, desired.destination.unwrap(),
                1, state.tick, radius, z_min, z_max,
                &mut |p| terrain.get(p).ok().copied()).map_err(|(reason, edge)| {
                    state.continuous.route_failure = Some(json!({
                        "goal": [desired.destination.unwrap().x, desired.destination.unwrap().y,
                                 desired.destination.unwrap().z],
                        "reason": reason, "blocked_edge": edge,
                        "coverage": "bounded_loaded_local_walk",
                        "last_verified_progress": state.continuous.route.as_ref()
                            .map(|route| [route.last_progress.x,route.last_progress.y,route.last_progress.z]),
                    }));
                    reason
                })?;
            route.id = owner.generation;
            Some(route)
        }
    } else { None };
    if let Some(route) = &mut route {
        let position = client.position().ok_or("control_actor_unavailable")?;
        let (radius, z_min, z_max) = local_motion::actor_shape(client)?;
        let terrain = client.state().terrain();
        movement = route.step(position, state.tick, radius, z_min, z_max,
            &mut |p| terrain.get(p).ok().copied())? * desired.speed;
    } else if movement.magnitude_squared() > 0.0
        || desired.vertical_input != VerticalInput::Neutral {
        local_motion::check_selected(client, context,
            Vec3::new(movement.x, movement.y, desired.vertical_input.value()),
            desired.fly, desired.vertical_input != VerticalInput::Neutral)?;
    }
    // Validation precedes mutation; the game tick sees only the whole new group.
    settle_protection(client, state, "replaced_by_fresh_owner");
    if desired.melee.is_some() {
        state.continuous.melee_feedback = combat_perception::selected_melee(client, aim.unwrap().0);
        desired.primary = state.continuous.melee_feedback["eligible"] == true;
    } else {
        state.continuous.melee_feedback = Value::Null;
    }
    clear_control_effects(client, state);
    release_jump_with(state, |input| client.handle_input(input, false, None, None));
    replace_inputs_with(state.continuous.active.as_ref(), &desired, |input, pressed| {
        client.handle_input(input, pressed, None, aim.map(|(entity, _, _)| entity));
    });
    set_facing(state, &desired, aim, movement);
    Ok(install_control_state(state, desired, route, client.position(), movement, same_route))
}

pub(super) fn tick(client: &mut Client, state: &mut BridgeState) {
    protection_tick(client, state);
    if state.continuous.protection.is_some() || state.continuous.shutdown { return; }
    let Some(desired) = state.continuous.active.as_ref() else { return };
    match resolve_inputs(client, state, desired) {
        Ok((aim, movement, context)) => {
            let route_input = if route_moving(desired, &state.continuous.route)
                && let Some(route) = &mut state.continuous.route
            {
                let position = client.position().ok_or("control_actor_unavailable");
                let dimensions = local_motion::actor_shape(client);
                let result = position.and_then(|position| dimensions.and_then(|(radius, z_min, z_max)| {
                    let terrain = client.state().terrain();
                    sample_route_tick(route, &mut state.continuous.motion,
                        position, state.tick, radius, z_min, z_max,
                        &mut |p| terrain.get(p).ok().copied())
                }));
                result
            } else { Ok(movement) };
            let movement = route_input.unwrap_or(Vec2::zero())
                * if route_moving(desired, &state.continuous.route) { desired.speed } else { 1.0 };
            let mut desired = desired.clone();
            desired.context = Some(context);
            if desired.melee.is_some() {
                state.continuous.melee_feedback = combat_perception::selected_melee(client, aim.unwrap().0);
                desired.primary = state.continuous.melee_feedback["eligible"] == true;
                replace_inputs_with(state.continuous.active.as_ref(), &desired, |input, pressed|
                    client.handle_input(input, pressed, None, aim.map(|a| a.0)));
                state.continuous.active = Some(desired.clone());
            }
            let position = client.position();
            let arrived = state.continuous.route.is_none() && desired.destination.zip(position).is_some_and(|(goal, position)|
                (goal.xy() - position.xy()).magnitude() <= 0.5 && (goal.z - position.z).abs() <= 1.0);
            let check = if let Err(reason) = route_input { Err(reason) }
            else if arrived {
                Err("control_motion_arrived")
            } else if desired.destination.zip(position).is_some_and(|(goal, position)|
                (goal.xy() - position.xy()).magnitude() <= 0.5 && (goal.z - position.z).abs() > 1.0)
            {
                Err("control_motion_vertical_unreachable")
            } else if route_moving(&desired, &state.continuous.route) { Ok(()) }
            else if movement.magnitude_squared() > 0.0
                || desired.vertical_input != VerticalInput::Neutral {
                local_motion::check_selected(client, desired.context
                    .expect("installed control has a movement context"),
                    Vec3::new(movement.x, movement.y, desired.vertical_input.value()),
                    desired.fly, desired.vertical_input != VerticalInput::Neutral).and_then(|()| {
                    if let (Some(feedback), Some(position)) = (&mut state.continuous.motion, position) {
                        feedback.sample(position, movement, state.tick)
                    } else {
                        Err("control_actor_unavailable")
                    }
                })
            } else { Ok(()) };
            if apply_motion_check(state, check, movement) {
                set_facing(state, &desired, aim, movement);
            }
            if let Some(active) = &mut state.continuous.active {
                active.context = desired.context;
            }
        },
        Err(code) => {
            stop(client, state, code);
            state.continuous.receipt = receipt(state, "failed", Some(code));
            record_event(state, "continuous_control_failed", state.continuous.receipt.clone());
        },
    }
}

pub(super) fn legacy_fence(
    state: &BridgeState, args: &Value, actor: &Value,
) -> Result<(), &'static str> {
    if state.continuous.shutdown { return Err("operator_shutdown"); }
    if !state.continuous.protection_fence { return Ok(()); }
    let fence = &args["control_fence"];
    if fence["generation"].as_u64() != Some(state.continuous.generation)
        || fence["expected_session_id"].as_str() != Some(&state.session_id)
        || fence["actor"] != *actor || actor.is_null()
    { return Err("stale_control_owner"); }
    Ok(())
}

pub(super) fn shutdown(client: &mut Client, state: &mut BridgeState, reason: &str) {
    state.continuous.shutdown = true;
    state.continuous.pre_hit.reset();
    cancel_active(client, state, reason);
    clear_control_effects(client, state);
    release_jump_with(state, |input| client.handle_input(input, false, None, None));
    state.persistent_move = Vec2::zero();
}

fn settle_protection(client: &mut Client, state: &mut BridgeState, reason: &str) {
    let Some(protection) = state.continuous.protection.take() else { return; };
    if protection.guard_requested_tick.is_some() {
        client.handle_input(InputKind::Block, false, None, None);
    }
    state.persistent_move = Vec2::zero();
    state.continuous.retired = true;
    let actual_actor = actor_ref(client, state);
    let actual = if actual_actor == protection.actor {
        combat_perception::actual_guard(client.current::<comp::CharacterState>().as_ref())
    } else { Value::Null };
    let settled = json!({
        "status": "settled", "reason": reason, "generation": state.continuous.generation,
        "tick": state.tick, "actor": protection.actor, "evidence": protection.evidence,
        "guard_requested": protection.guard_requested_tick.is_some(),
        "actual": actual, "actual_actor": actual_actor, "gameplay_success": null,
    });
    state.continuous.protection_receipt.as_object_mut().expect("protection receipt is an object")
        .extend(settled.as_object().unwrap().clone());
    record_event(state, "native_protection_settled", state.continuous.protection_receipt.clone());
}

fn protective_release_with(inputs: &[InputKind], mut send: impl FnMut(InputKind, bool)) {
    // Primary must leave the ordered input map before Block can become first.
    send(InputKind::Primary, false);
    for input in inputs.iter().copied().filter(|i| *i != InputKind::Primary) {
        send(input, false);
    }
}

fn request_guard_with(mut send: impl FnMut(InputKind, bool)) {
    send(InputKind::Primary, false);
    send(InputKind::Block, true);
}

pub(super) enum Danger {
    Damage(comp::HealthChange),
    PreHit(pre_hit::Threat),
}

pub(super) fn select_danger(
    fresh_damage: Option<comp::HealthChange>, pre_hit: Option<pre_hit::Threat>,
) -> Option<Danger> {
    fresh_damage.map(Danger::Damage).or_else(|| pre_hit.map(Danger::PreHit))
}

fn pre_hit_guard_progress(requested_tick: u64, tick: u64, actual_block: bool) -> Result<&'static str, &'static str> {
    if tick <= requested_tick { Ok("guard_requested") }
    else if actual_block { Ok("guard_holding_pre_hit") }
    else { Err("pre_hit_guard_not_active") }
}

fn damage_evidence(state: &BridgeState, change: comp::HealthChange) -> Value {
    let mut evidence = state.continuous.damage.observation(change, state.tick);
    evidence["kind"] = json!("observed_health_decrease");
    evidence
}

fn protection_started_receipt(
    state: &BridgeState, actor: &Value, evidence: &Value, retired_generation: u64,
    cancelled_action: Option<&str>, queued_inputs_observed: bool,
) -> Value {
    json!({
        "status": "protecting", "generation": state.continuous.generation, "tick": state.tick, "actor": actor,
        "retired_generation": retired_generation, "cancelled_action_id": cancelled_action,
        "evidence": evidence, "primary_released": true, "movement_neutralized": true,
        "queued_inputs_observed": queued_inputs_observed,
        "guard_requested": false, "gameplay_success": null,
    })
}

pub(super) fn protection_tick(client: &mut Client, state: &mut BridgeState) {
    if state.continuous.shutdown { return; }
    let actor = actor_ref(client, state);
    let health = client.current::<comp::Health>();
    let character = client.current::<comp::CharacterState>();
    let alive = health.as_ref().zip(character.as_ref()).is_some_and(|(h, c)|
        !h.is_dead && !comp::is_downed(Some(h), Some(c)));
    let fresh = state.continuous.damage.sample(&state.session_id, actor.clone(), state.tick,
        health.as_ref().filter(|_| alive).map(|h| h.last_change),
        health.as_ref().filter(|_| alive).map(|h| h.current()));
    if !alive || actor.is_null() {
        state.continuous.pre_hit.reset();
        stop(client, state, "protection_actor_unavailable");
        return;
    }
    let binding = combat_perception::binding(client, state);
    if state.continuous.pre_hit.needs_sample(&binding) {
        let rows = pre_hit::scan(client, state);
        state.continuous.pre_hit.sample(binding.clone(), rows);
    }
    if state.continuous.protection.as_ref().is_some_and(|p| p.actor != actor) {
        settle_protection(client, state, "protection_actor_changed");
    }
    let invalid_pre_hit = state.continuous.protection.as_ref().and_then(|p| p.pre_hit.as_ref())
        .and_then(|threat| state.continuous.pre_hit.validate(threat, &binding).err());
    if let Some(reason) = invalid_pre_hit { settle_protection(client, state, reason); }
    let candidate = if fresh.is_none() {
        state.continuous.pre_hit.take_candidate(
            &binding, state.continuous.shutdown, state.continuous.protection.is_some())
    } else { None };
    if let Some(danger) = select_danger(fresh, candidate) {
        let (evidence, target, attack_source, environmental, pre_hit) = match danger {
            Danger::Damage(change) => {
                let evidence = damage_evidence(state, change);
                let uid = combat_perception::attributed_attacker(&change, actor["uid"].as_u64());
                let target = client.position().and_then(|pos| nearby_entities(client, state, pos).into_iter()
                    .find(|r| r["uid"].as_u64() == uid && uid.is_some() && r["health"]["dead"] == false))
                    .map(|r| serde_json::from_value(r["control_target"].clone())
                        .expect("native nearby projection produces EntityRef"));
                let source = match change.cause {
                    Some(common::DamageSource::Attack(source)) => Some(source), _ => None,
                };
                (evidence, target, source,
                    matches!(change.cause, Some(common::DamageSource::Falling | common::DamageSource::Buff(_))), None)
            },
            Danger::PreHit(threat) => (threat.evidence.clone(), Some(threat.source.clone()),
                Some(common::combat::AttackSource::Melee), false, Some(threat)),
        };
        let retired_generation = state.continuous.generation;
        let cancelled_action = state.active.as_ref().map(|a| a.action_id.clone());
        cancel_active(client, state, "native_protection_interrupted");
        clear_control_effects(client, state);
        release_jump_with(state, |input| client.handle_input(input, false, None, None));
        let queued = client.current::<comp::Controller>()
            .map(|c| c.queued_inputs.keys().copied().collect::<Vec<_>>());
        protective_release_with(queued.as_deref().unwrap_or(&[]), |i, p| client.handle_input(i, p, None, None));
        state.persistent_move = Vec2::zero();
        let Ok(_) = state.continuous.retire_for_protection(
            serde_json::from_value(actor.clone()).expect("native actor_ref produces EntityRef")) else {
            record_event(state, "native_protection_failed", json!({"reason": "native_owner_generation_exhausted"}));
            shutdown(client, state, "native_owner_generation_exhausted");
            return;
        };
        state.continuous.protection = Some(Protection {
            actor: actor.clone(), evidence: evidence.clone(), target,
            guard_requested_tick: None, wield_deadline: None,
            attack_source, pre_hit,
        });
        state.continuous.protection_receipt = protection_started_receipt(
            state, &actor, &evidence, retired_generation, cancelled_action.as_deref(), queued.is_some());
        state.continuous.receipt = receipt(state, "interrupted", Some("native_protection"));
        record_event(state, "native_protection_interrupted", state.continuous.protection_receipt.clone());
        if queued.is_none() {
            settle_protection(client, state, "guard_controller_state_unknown");
            return;
        }
        if environmental {
            settle_protection(client, state, "environmental_damage_neutralized_no_retaliatory_target");
            return;
        }
    }
    let Some(protection) = state.continuous.protection.clone() else { return; };
    if let Some(tick) = protection.guard_requested_tick && protection.pre_hit.is_none() {
        // One locally applied input pulse; animation may continue. Observed
        // BasicBlock is reported separately from the request and from mitigation.
        if state.tick > tick { settle_protection(client, state, "guard_pulse_observed"); }
        return;
    }
    let Some((ability, _)) = combat_perception::resolve(client, common::comp::ability::AbilityInput::Guard)
        else { settle_protection(client, state, "guard_unavailable_or_unknown"); return; };
    let common::comp::ability::CharacterAbility::BasicBlock {
        max_angle, blocked_attacks, block_strength, ..
    } = &ability else { settle_protection(client, state, "guard_unsupported"); return; };
    let (max_angle, blocked_attacks, block_strength) = match &character {
        Some(comp::CharacterState::BasicBlock(c)) =>
            (&c.static_data.max_angle, &c.static_data.blocked_attacks, &c.static_data.block_strength),
        _ => (max_angle, blocked_attacks, block_strength),
    };
    let filter_supported = protection.attack_source.map(|source| blocked_attacks.applies(source));
    state.continuous.protection_receipt["attack_filter_supported"] = json!(filter_supported);
    state.continuous.protection_receipt["block_half_angle_deg"] = json!(max_angle);
    state.continuous.protection_receipt["direction_observed_tick"] = json!(state.tick);
    state.continuous.protection_receipt["block_strength"] = json!(block_strength);
    if filter_supported == Some(false) {
        settle_protection(client, state, "guard_cannot_mitigate_evidenced_attack"); return;
    }
    if let Some(target) = &protection.target {
        let desired = Desired {
            owner: Owner { owner: "native_protection".into(), generation: state.continuous.generation,
                sequence: 1, expected_session_id: state.session_id.clone(),
                actor: state.continuous.actor.clone().unwrap(),
                expected_protection_generation: Some(state.continuous.protection_generation) },
            movement: Vec2::zero(), vertical_input: VerticalInput::Neutral,
            movement_intent: None, destination: None,
            target: Some(target.clone()), speed: 1.0, primary: false, secondary: false,
            auxiliary: None, fly: false, jump: false, block: false,
            facing: Facing::Track, look_direction: None, melee: None, context: None,
        };
        match target_aim(client, state, &desired) {
            Ok(Some((_, dir, _))) => {
                state.look_dir = dir;
                state.continuous.protection_receipt["direction_eligible_now"] = json!(
                    client.current::<comp::Ori>().map(|ori|
                        ori.look_dir().to_vec().angle_between(dir.to_vec()) < max_angle.to_radians()));
            },
            _ => {
                if protection.pre_hit.is_some() {
                    settle_protection(client, state, "pre_hit_guard_bearing_unavailable");
                    return;
                }
                // Loss/occlusion never authorizes tracking a replacement entity.
                state.continuous.protection.as_mut().unwrap().target = None;
                state.continuous.protection_receipt["direction_limit"] = json!("attacker_unavailable_current_facing_only");
                state.continuous.protection_receipt["direction_eligible_now"] = Value::Null;
            },
        }
    } else {
        state.continuous.protection_receipt["direction_limit"] = json!("unknown_attacker_current_facing_only");
        state.continuous.protection_receipt["direction_eligible_now"] = Value::Null;
    }
    let already_blocking = matches!(&character, Some(comp::CharacterState::BasicBlock(c))
        if matches!(c.stage_section, common::states::utils::StageSection::Buildup
            | common::states::utils::StageSection::Action)
            && protection.attack_source.is_none_or(|source| c.static_data.blocked_attacks.applies(source)));
    if protection.pre_hit.is_some() && let Some(requested_tick) = protection.guard_requested_tick {
        state.continuous.protection_receipt["actual"] = combat_perception::actual_guard(character.as_ref());
        state.continuous.protection_receipt["pre_hit_observed_tick"] = json!(state.tick);
        match pre_hit_guard_progress(requested_tick, state.tick, already_blocking) {
            Ok(status) => state.continuous.protection_receipt["status"] = json!(status),
            Err(reason) => settle_protection(client, state, reason),
        }
        return;
    }
    if already_blocking || combat_perception::readiness(client, &ability, true) == Some(true) {
        request_guard_with(|i, pressed| client.handle_input(i, pressed, None, None));
        state.continuous.protection.as_mut().unwrap().guard_requested_tick = Some(state.tick);
        state.continuous.protection_receipt["guard_requested"] = json!(true);
        return;
    }
    if protection.wield_deadline.is_some_and(|end| state.tick <= end)
        && character.as_ref().and_then(combat_perception::remaining_guard_transition_seconds).is_some() { return; }
    if matches!(character, Some(comp::CharacterState::Idle(_))) && protection.wield_deadline.is_none() {
        let duration = client.current::<comp::Inventory>().map(|inv|
            [EquipSlot::ActiveMainhand, EquipSlot::ActiveOffhand].into_iter().filter_map(|slot|
                inv.equipped(slot).and_then(|item| match &*item.kind() {
                    ItemKind::Tool(tool) => Some(tool.stats(item.stats_durability_multiplier()).equip_time_secs),
                    _ => None,
                })).fold(0.0_f32, f32::max)).unwrap_or(0.0);
        let Some(speed) = client.current::<comp::Stats>().map(|s| s.attack_speed_modifier)
            .filter(|speed| speed.is_finite() && *speed > 0.0)
            else { settle_protection(client, state, "unknown_wield_timing"); return; };
        client.toggle_wield();
        state.continuous.protection.as_mut().unwrap().wield_deadline =
            Some(state.tick + (duration / speed * TPS as f32).ceil() as u64 + 2);
        state.continuous.protection_receipt["wield_requested"] = json!(true);
        return;
    }
    if protection.wield_deadline.is_none()
        && let Some(remaining) = character.as_ref().and_then(combat_perception::remaining_guard_transition_seconds)
        && let Some(speed) = client.current::<comp::Stats>().map(|s| s.attack_speed_modifier)
        && speed.is_finite() && speed > 0.0
    {
        state.continuous.protection.as_mut().unwrap().wield_deadline =
            Some(state.tick + (remaining / speed * TPS as f32).ceil() as u64 + 2);
        state.continuous.protection_receipt["waiting_for"] = json!("current_native_equip_or_melee_transition");
        return;
    }
    settle_protection(client, state, "guard_state_or_energy_not_ready");
}

fn set_facing(
    state: &mut BridgeState, desired: &Desired,
    aim: Option<(Entity, Dir, Vec3<f32>)>, movement: Vec2<f32>,
) {
    if let Some(direction) = desired.look_direction {
        state.look_dir = direction;
        return;
    }
    if desired.facing == Facing::Travel || desired.facing == Facing::Auto && aim.is_none() {
        if let Some(direction) = Dir::from_unnormalized(Vec3::new(movement.x, movement.y, 0.0)) {
            state.look_dir = direction;
        }
    } else if let Some((_, direction, _)) = aim { state.look_dir = direction; }
}

#[cfg(test)]
pub(super) fn serializer_fixture(
    state: &mut BridgeState, actor: EntityRef, target: EntityRef,
    change: comp::HealthChange, melee_feedback: Value,
) -> Value {
    state.continuous.generation = 7;
    let actor_value = serde_json::to_value(&actor).unwrap();
    let evidence = damage_evidence(state, change);
    state.continuous.retire_for_protection(actor).unwrap();
    state.continuous.protection = Some(Protection {
        actor: actor_value.clone(), evidence: evidence.clone(), target: Some(target),
        guard_requested_tick: None, wield_deadline: None,
        attack_source: Some(common::combat::AttackSource::Melee), pre_hit: None,
    });
    state.continuous.protection_receipt = protection_started_receipt(state, &actor_value, &evidence, 7, None, true);
    state.continuous.receipt = receipt(state, "interrupted", Some("native_protection"));
    state.continuous.melee_feedback = melee_feedback;
    state.continuous.observation()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn native_route_transition_serializer_fixture() {
        let rock = Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        let mut clear = |p: Vec3<i32>| Some(if p.z < 1 { rock } else { Block::empty() });
        let start = Vec3::new(0.5, 1.5, 1.0);
        let goal = Vec3::new(3.5, 1.5, 1.0);
        let mut state = BridgeState::new("route-serializer-fixture", Dir::default());
        let mut args = args(7, 1);
        args["expected_session_id"] = json!("route-serializer-fixture");
        args["movement"] = json!([0.0, 0.0]);
        args["target"] = Value::Null;
        args["primary"] = json!(false);
        args["block"] = json!(false);
        args["movement_intent"] = json!("destination");
        args["destination"] = json!([goal.x, goal.y, goal.z]);
        let (owner, desired) = parse(&args, false).unwrap();
        state.continuous.authorize(&owner).unwrap();
        let mut route = local_route::Route::new(
            start, goal, 1, 0, 0.4, 0.0, 1.8, &mut clear).unwrap();
        route.id = owner.generation;
        let movement = route.step(start, state.tick, 0.4, 0.0, 1.8, &mut clear).unwrap();
        install_control_state(&mut state, desired.unwrap(), Some(route), Some(start), movement, false);
        let planned = state.continuous.observation();
        assert_eq!(planned["local_route"]["goal"], json!([3.5, 1.5, 1.0]));
        assert!(planned["local_route"]["waypoint"].is_array());
        assert_eq!(planned["local_route"]["goal_success"], Value::Null);
        assert_eq!(planned["motion_feedback"]["status"], "running");
        assert_eq!(planned["generation"], 7);
        assert_eq!(planned["last_receipt"]["status"], "applied");
        state.tick = 1;
        let movement = sample_route_tick(
            state.continuous.route.as_mut().unwrap(), &mut state.continuous.motion,
            Vec3::new(1.5, 1.5, 1.0), state.tick, 0.4, 0.0, 1.8, &mut clear).unwrap();
        assert!(apply_motion_check(&mut state, Ok(()), movement));
        let progressed = state.continuous.observation();
        assert_eq!(progressed["motion_feedback"]["status"], "progress");
        assert_eq!(progressed["motion_feedback"]["since_tick"], 1);
        assert_eq!(progressed["local_route"]["since_tick"], 1);
        assert_eq!(progressed["local_route"]["last_verified_progress"], json!([1.5, 1.5, 1.0]));
        assert_eq!(progressed["last_receipt"]["status"], "applied");
        state.tick = 2;
        let arrival = sample_route_tick(
            state.continuous.route.as_mut().unwrap(), &mut state.continuous.motion,
            goal, state.tick, 0.4, 0.0, 1.8, &mut clear).unwrap_err();
        assert_eq!(arrival, "control_motion_arrived");
        assert!(!apply_motion_check(&mut state, Err(arrival), Vec2::zero()));
        let arrived = state.continuous.observation();
        assert_eq!(arrived["last_receipt"]["reason"], "control_motion_arrived");
        assert_eq!(arrived["motion_feedback"]["status"], "control_motion_arrived");
        assert_eq!(arrived["last_receipt"]["applied_tick"], 2);
        assert_eq!(arrived["generation"], 7);
        assert!(arrived["destination"].is_null());
        state.tick = 3;
        args["generation"] = json!(8);
        let (next_owner, next_desired) = parse(&args, false).unwrap();
        state.continuous.authorize(&next_owner).unwrap();
        let mut next_route = local_route::Route::new(
            start, goal, 1, state.tick, 0.4, 0.0, 1.8, &mut clear).unwrap();
        next_route.id = next_owner.generation;
        let next_movement = next_route.step(
            start, state.tick, 0.4, 0.0, 1.8, &mut clear).unwrap();
        install_control_state(&mut state, next_desired.unwrap(), Some(next_route),
            Some(start), next_movement, false);
        let replanned = state.continuous.observation();
        assert_eq!(replanned["generation"], 8);
        assert_eq!(replanned["motion_feedback"]["status"], "running");
        let mut obstructed = |p: Vec3<i32>| Some(if p.z < 1 || p.x == 1 && p.z == 1 {
            rock
        } else { Block::empty() });
        state.tick = 4;
        let obstruction = sample_route_tick(
            state.continuous.route.as_mut().unwrap(), &mut state.continuous.motion,
            start, state.tick, 0.4, 0.0, 1.8, &mut obstructed).unwrap_err();
        assert_eq!(obstruction, "control_motion_obstructed");
        assert!(!apply_motion_check(&mut state, Err(obstruction), Vec2::zero()));
        let blocked = state.continuous.observation();
        assert_eq!(blocked["motion_feedback"]["status"], "control_motion_obstructed");
        assert_eq!(blocked["last_receipt"]["reason"], "control_motion_obstructed");
        assert_eq!(blocked["last_receipt"]["applied_tick"], 4);
        assert_eq!(blocked["generation"], 8);
        assert_eq!(blocked["local_route"]["blocked_edge"]["cause"], "control_motion_obstructed");
        assert_eq!(blocked["local_route"]["last_verified_progress"], json!([0.5, 1.5, 1.0]));
        println!("NATIVE_ROUTE_TRANSITIONS_JSON={}", json!({
            "planned": {"continuous_control": planned},
            "progressed": {"continuous_control": progressed},
            "arrived": {"continuous_control": arrived},
            "replanned": {"continuous_control": replanned},
            "blocked": {"continuous_control": blocked}
        }));
    }

    #[test]
    fn pre_hit_guard_hold_requires_actual_state_after_request_and_never_requests_a_second_press() {
        assert_eq!(pre_hit_guard_progress(10, 10, false), Ok("guard_requested"));
        assert_eq!(pre_hit_guard_progress(10, 11, false), Err("pre_hit_guard_not_active"));
        for tick in 11..20 {
            assert_eq!(pre_hit_guard_progress(10, tick, true), Ok("guard_holding_pre_hit"));
        }
        assert_eq!(pre_hit_guard_progress(10, 20, false), Err("pre_hit_guard_not_active"));
    }

    #[test]
    fn autonomy_contract_advertises_native_protection_and_selected_melee() {
        assert_eq!(capability()["native_protection"], true);
        assert_eq!(capability()["selected_melee"], json!(["primary"]));
    }

    #[test]
    fn protection_retires_old_step_stop_and_control_but_allows_fresh_replacement() {
        let mut state = BridgeState::new("test", Dir::default());
        let (old, _) = parse(&args(5, 3), false).unwrap();
        let actor = serde_json::to_value(&old.actor).unwrap();
        state.continuous.accept(&old);
        let queued = json!({"control_fence": {"generation": 5, "expected_session_id": "test", "actor": actor}});
        assert!(legacy_fence(&state, &queued, &actor).is_ok());
        assert_eq!(state.continuous.retire_for_protection(old.actor.clone()), Ok(6));
        assert_eq!(legacy_fence(&state, &queued, &actor), Err("stale_control_owner"));
        assert_eq!(legacy_fence(&state, &json!({}), &actor), Err("stale_control_owner"));
        assert_eq!(state.continuous.authorize(&old), Err("stale_control_owner"));
        let mut delayed = old.clone();
        delayed.sequence = 900;
        assert_eq!(state.continuous.authorize(&delayed), Err("stale_control_owner"));
        let mut fresh = queued.clone();
        fresh["control_fence"]["generation"] = json!(6);
        assert!(legacy_fence(&state, &fresh, &actor).is_ok());
        fresh["control_fence"]["actor"]["lifecycle"] = json!("other");
        assert_eq!(legacy_fence(&state, &fresh, &actor), Err("stale_control_owner"));
        delayed.generation = 7;
        assert_eq!(state.continuous.authorize(&delayed), Err("stale_control_owner"));
        delayed.expected_protection_generation = Some(6);
        assert!(state.continuous.authorize(&delayed).is_ok());
        state.continuous.shutdown = true;
        assert_eq!(legacy_fence(&state, &queued, &actor), Err("operator_shutdown"));
    }

    #[test]
    fn protection_releases_primary_and_other_abilities_before_block_can_be_first() {
        let mut controller = comp::Controller::default();
        for input in [InputKind::Primary, InputKind::Secondary, InputKind::Block] {
            controller.queued_inputs.insert(input, comp::controller::InputAttr { select_pos: None, target_entity: None });
        }
        assert_eq!(controller.queued_inputs.keys().next(), Some(&InputKind::Primary));
        let mut sent = Vec::new();
        let held = controller.queued_inputs.keys().copied().collect::<Vec<_>>();
        protective_release_with(&held, |i, p| {
            sent.push((i, p));
            assert!(!p);
            controller.queued_inputs.remove(&i);
        });
        assert_eq!(sent[0], (InputKind::Primary, false));
        request_guard_with(|i, pressed| {
            sent.push((i, pressed));
            if pressed {
                controller.queued_inputs.insert(i,
                    comp::controller::InputAttr { select_pos: None, target_entity: None });
            } else { controller.queued_inputs.remove(&i); }
        });
        assert_eq!(&sent[sent.len() - 2..], &[(InputKind::Primary, false), (InputKind::Block, true)]);
        assert_eq!(controller.queued_inputs.keys().next(), Some(&InputKind::Block));
    }

    #[test]
    fn selected_melee_is_explicit_not_an_implicit_primary_chase() {
        let mut value = args(1, 1);
        assert!(parse(&value, false).unwrap().1.unwrap().melee.is_none());
        value["melee"] = json!("primary");
        assert!(parse(&value, false).is_err()); // contradictory block intent
        value["block"] = json!(false);
        let desired = parse(&value, false).unwrap().1.unwrap();
        assert_eq!(desired.melee, Some(MeleeOperation::Primary));
        assert!(desired.movement_intent.is_none());
        value["target"] = Value::Null;
        assert!(parse(&value, false).is_err());
    }

    fn args(generation: u64, sequence: u64) -> Value {
        json!({
            "owner": "jev", "generation": generation, "sequence": sequence,
            "expected_session_id": "test", "actor": {"uid": 1, "lifecycle": "actor-1"},
            "movement": [0.6, 0.8], "target": {"uid": 2, "lifecycle": "target-1"},
            "primary": true, "block": true,
        })
    }

    #[test]
    fn secondary_ability_input_is_owned_and_released_on_replacement_and_stop() {
        let mut value = args(1, 1);
        value["primary"] = json!(false);
        value["block"] = json!(false);
        value["secondary"] = json!(true);
        let (_, desired) = parse(&value, false).unwrap();
        let desired = desired.unwrap();
        let mut sent = Vec::new();
        replace_inputs_with(None, &desired, |i, pressed| sent.push((i, pressed)));
        assert_eq!(sent, vec![(InputKind::Secondary, true)]);
        let mut controls = Controls::default();
        controls.active = Some(desired.clone());
        assert_eq!(controls.observation()["secondary_held"], true);
        value["secondary"] = json!(false);
        let (_, next) = parse(&value, false).unwrap();
        replace_inputs_with(Some(&desired), &next.unwrap(), |i, pressed| sent.push((i, pressed)));
        assert_eq!(sent.last(), Some(&(InputKind::Secondary, false)));
        sent.clear();
        release_inputs_with(&desired, |i, pressed| sent.push((i, pressed)));
        assert_eq!(sent, vec![(InputKind::Secondary, false)]);
        value["secondary"] = json!("false");
        assert!(parse(&value, false).is_err());
    }

    #[test]
    fn horizontal_jump_is_one_owner_bundle_with_explicit_release_and_stale_fence() {
        let mut value = args(1, 1);
        value["target"] = Value::Null;
        value["primary"] = json!(false);
        value["block"] = json!(false);
        value["movement"] = json!([1.0, 0.0]);
        value["jump"] = json!(true);
        let (owner, desired) = parse(&value, false).unwrap();
        let desired = desired.unwrap();
        assert_eq!(desired.movement, Vec2::new(1.0, 0.0));
        let mut sent = Vec::new();
        replace_inputs_with(None, &desired, |input, pressed| sent.push((input, pressed)));
        assert_eq!(sent, vec![(InputKind::Jump, true)]);
        let mut controls = Controls::default();
        controls.accept(&owner);
        controls.active = Some(desired.clone());
        assert_eq!(controls.observation()["jump_held"], true);
        value["sequence"] = json!(2);
        value["jump"] = json!(false);
        let (next_owner, next) = parse(&value, false).unwrap();
        controls.authorize(&next_owner).unwrap();
        replace_inputs_with(Some(&desired), &next.unwrap(),
            |input, pressed| sent.push((input, pressed)));
        assert_eq!(sent.last(), Some(&(InputKind::Jump, false)));
        release_inputs_with(&desired, |input, pressed| sent.push((input, pressed)));
        assert_eq!(sent.last(), Some(&(InputKind::Jump, false)));
        controls.accept(&next_owner);
        assert_eq!(controls.authorize(&owner), Err("stale_control_owner"));
        value["jump"] = json!("yes");
        assert!(parse(&value, false).is_err());
        value["jump"] = json!(true);
        value["movement_intent"] = json!("destination");
        value["destination"] = json!([4.0, 0.0, 1.0]);
        value["movement"] = json!([0.0, 0.0]);
        assert!(parse(&value, false).is_err());
    }

    #[test]
    fn airborne_refresh_and_release_remain_owner_fenced_without_repressing_jump() {
        let mut value = args(4, 1);
        value["target"] = Value::Null;
        value["primary"] = json!(false);
        value["block"] = json!(false);
        value["movement"] = json!([1.0, 0.0]);
        value["jump"] = json!(true);
        let (initial, desired) = parse(&value, false).unwrap();
        let desired = desired.unwrap();
        let mut controls = Controls::default();
        controls.accept(&initial);
        controls.active = Some(desired.clone());
        let mut input = Vec::new();
        replace_inputs_with(None, &desired, |kind, pressed| input.push((kind, pressed)));
        assert_eq!(input, vec![(InputKind::Jump, true)]);
        value["sequence"] = json!(2);
        value["movement"] = json!([0.0, 1.0]);
        let (refreshed_owner, refreshed) = parse(&value, false).unwrap();
        controls.authorize(&refreshed_owner).unwrap();
        let refreshed = refreshed.unwrap();
        replace_inputs_with(Some(&desired), &refreshed,
            |kind, pressed| input.push((kind, pressed)));
        assert_eq!(input, vec![(InputKind::Jump, true)]);
        assert_eq!(refreshed.movement, Vec2::new(0.0, 1.0));
        controls.accept(&refreshed_owner);
        value["sequence"] = json!(3);
        value["jump"] = json!(false);
        let (released_owner, released) = parse(&value, false).unwrap();
        controls.authorize(&released_owner).unwrap();
        let released = released.unwrap();
        replace_inputs_with(Some(&refreshed), &released,
            |kind, pressed| input.push((kind, pressed)));
        assert_eq!(input.last(), Some(&(InputKind::Jump, false)));
        assert_eq!(released.movement, refreshed.movement);
        controls.accept(&released_owner);
        assert_eq!(controls.authorize(&refreshed_owner), Err("stale_control_owner"));
        release_inputs_with(&released, |kind, pressed| input.push((kind, pressed)));
        assert_eq!(input.iter().filter(|(kind, _)| *kind == InputKind::Jump).count(), 2);
    }

    #[test]
    fn passenger_can_hold_rider_actions_but_not_steer_or_jump_carrier() {
        let uid = Uid(NonZeroU64::new(41).unwrap());
        let seat = common::mounting::VolumePos::entity(Vec3::new(3, 2, 1), uid);
        let context = local_motion::MotionContext::Passenger(seat);
        let mut value = args(5, 1);
        value["target"] = Value::Null;
        value["movement"] = json!([0.0, 0.0]);
        value["secondary"] = json!(true);
        let (owner, passenger) = parse(&value, false).unwrap();
        let passenger = passenger.unwrap();
        let mut installed = passenger.clone();
        installed.context = Some(context);
        assert!(!changed_controlled_body(Some(&installed), context));
        let moved_seat = common::mounting::VolumePos::entity(Vec3::new(4, 2, 1), uid);
        assert!(changed_controlled_body(
            Some(&installed), local_motion::MotionContext::Passenger(moved_seat)));
        assert_eq!(carrier_inputs_eligible(context, &passenger, Vec2::zero()), Ok(()));
        let mut inputs = Vec::new();
        replace_inputs_with(None, &passenger, |kind, pressed| inputs.push((kind, pressed)));
        assert!(inputs.contains(&(InputKind::Primary, true)));
        assert!(inputs.contains(&(InputKind::Secondary, true)));
        assert!(inputs.contains(&(InputKind::Block, true)));
        let mut controls = Controls::default();
        controls.accept(&owner);
        value["sequence"] = json!(2);
        value["movement"] = json!([1.0, 0.0]);
        let (next_owner, next) = parse(&value, false).unwrap();
        controls.authorize(&next_owner).unwrap();
        assert_eq!(carrier_inputs_eligible(context, &next.unwrap(), Vec2::new(1.0, 0.0)),
            Err("control_motion_mount_not_steered"));
        value["movement"] = json!([0.0, 0.0]);
        value["jump"] = json!(true);
        let (_, jumping) = parse(&value, false).unwrap();
        assert_eq!(carrier_inputs_eligible(context, &jumping.unwrap(), Vec2::zero()),
            Err("control_motion_mount_not_steered"));
        assert_eq!(controls.authorize(&owner), Err("stale_control_owner"));
    }

    #[test]
    fn free_will_direct_mount_keeps_owned_rider_actions_not_carrier_motion() {
        use specs::{Builder, WorldExt};
        let mut world = specs::World::new();
        let carrier = world.create_entity().build();
        let uid = Uid(NonZeroU64::new(53).unwrap());
        let body = comp::Body::BipedLarge(comp::body::biped_large::Body {
            species: comp::body::biped_large::Species::Ogre,
            body_type: comp::body::biped_large::BodyType::Male,
        });
        let context =
            local_motion::mounted_context(uid, carrier, None, Some(&body), None, false).unwrap();
        assert_eq!(context, local_motion::MotionContext::EntityPassenger(uid, carrier));
        let mut value = args(6, 1);
        value["target"] = Value::Null;
        value["movement"] = json!([0.0, 0.0]);
        value["secondary"] = json!(true);
        value["look_direction"] = json!([1.0, 0.0, 0.4]);
        let (owner, desired) = parse(&value, false).unwrap();
        let desired = desired.unwrap();
        assert_eq!(carrier_inputs_eligible(context, &desired, Vec2::zero()), Ok(()));
        let mut held = Vec::new();
        replace_inputs_with(None, &desired, |input, pressed| held.push((input, pressed)));
        assert!(held.contains(&(InputKind::Primary, true)));
        assert!(held.contains(&(InputKind::Secondary, true)));
        assert!(held.contains(&(InputKind::Block, true)));
        let mut bridge = BridgeState::new("test", Dir::default());
        set_facing(&mut bridge, &desired, None, Vec2::zero());
        assert!(bridge.look_dir.to_vec().z > 0.3);
        let mut installed = desired.clone();
        installed.context = Some(context);
        let mut controls = Controls::default();
        controls.accept(&owner);
        assert!(!changed_controlled_body(Some(&installed), context));
        let other = world.create_entity().build();
        assert!(changed_controlled_body(Some(&installed),
            local_motion::MotionContext::EntityPassenger(uid, other)));
        assert!(changed_controlled_body(Some(&installed),
            local_motion::MotionContext::EntityPassenger(
                Uid(NonZeroU64::new(54).unwrap()), carrier)));
        assert!(changed_controlled_body(Some(&installed), local_motion::MotionContext::Walk));
        value["sequence"] = json!(2);
        value["movement"] = json!([0.5, 0.0]);
        let (next_owner, moving) = parse(&value, false).unwrap();
        controls.authorize(&next_owner).unwrap();
        let moving = moving.unwrap();
        assert_eq!(carrier_inputs_eligible(context, &moving, moving.movement),
            Err("control_motion_mount_not_steered"));
        let steerable = local_motion::MotionContext::GroundMount(uid, carrier, None);
        assert_eq!(carrier_inputs_eligible(steerable, &moving, moving.movement), Ok(()));
        value["movement"] = json!([0.0, 0.0]);
        value["jump"] = json!(true);
        let (_, jumping) = parse(&value, false).unwrap();
        assert_eq!(carrier_inputs_eligible(context, &jumping.unwrap(), Vec2::zero()),
            Err("control_motion_mount_not_steered"));
        release_inputs_with(&installed, |input, pressed| held.push((input, pressed)));
        assert!(held.contains(&(InputKind::Primary, false)));
        assert!(held.contains(&(InputKind::Block, false)));
    }

    #[test]
    fn selected_look_can_hold_glider_pitch_without_target_or_native_fly() {
        let mut value = args(1, 1);
        value["target"] = Value::Null;
        value["primary"] = json!(false);
        value["block"] = json!(false);
        value["movement"] = json!([1.0, 0.0]);
        value["look_direction"] = json!([1.0, 0.0, 0.5]);
        let (_, desired) = parse(&value, false).unwrap();
        let desired = desired.unwrap();
        assert!(!desired.fly);
        let mut state = BridgeState::new("look-fixture", Dir::default());
        set_facing(&mut state, &desired, None, desired.movement);
        assert!(state.look_dir.to_vec().z > 0.4);
        value["look_direction"] = json!([0.0, 0.0, 0.0]);
        assert!(parse(&value, false).is_err());
        value["look_direction"] = json!([1.0, 0.0, 0.5]);
        value["target"] = json!({"uid": 2, "lifecycle": "target-1"});
        assert!(parse(&value, false).is_err());
    }

    #[test]
    fn auxiliary_slot_is_released_on_replacement_retarget_and_stop() {
        let mut value = args(1, 1);
        value["primary"] = json!(false);
        value["block"] = json!(false);
        value["auxiliary"] = json!(0);
        let (_, desired) = parse(&value, false).unwrap();
        let desired = desired.unwrap();
        let mut sent = Vec::new();
        replace_inputs_with(None, &desired, |input, pressed| sent.push((input, pressed)));
        assert_eq!(sent, vec![(InputKind::Ability(0), true)]);
        let mut controls = Controls::default();
        controls.active = Some(desired.clone());
        assert_eq!(controls.observation()["auxiliary_held"], 0);
        value["auxiliary"] = json!(2);
        let (_, next) = parse(&value, false).unwrap();
        let next = next.unwrap();
        sent.clear();
        replace_inputs_with(Some(&desired), &next, |input, pressed| sent.push((input, pressed)));
        assert_eq!(sent, vec![
            (InputKind::Ability(0), false),
            (InputKind::Ability(2), true),
        ]);
        sent.clear();
        release_inputs_with(&next, |input, pressed| sent.push((input, pressed)));
        assert_eq!(sent, vec![(InputKind::Ability(2), false)]);
        value["auxiliary"] = json!(-1);
        assert!(parse(&value, false).is_err());
    }

    #[test]
    fn owned_speed_and_flight_vertical_are_explicitly_bounded() {
        let mut value = args(1, 1);
        value["movement"] = json!([1.0, 0.0]);
        value["target"] = Value::Null;
        value["primary"] = json!(false);
        value["block"] = json!(false);
        value["speed"] = json!(0.35);
        value["fly"] = json!(true);
        value["vertical_input"] = json!("fly_up");
        let (_, desired) = parse(&value, false).unwrap();
        let desired = desired.unwrap();
        assert_eq!(desired.vertical_input.value(), 1.0);
        assert!((movement_for_tick(&desired, None, None).unwrap().x
            * desired.speed - 0.35).abs() < 1e-6);
        value["fly"] = json!(false);
        assert!(parse(&value, false).is_err());
        value["fly"] = json!(true);
        value["vertical_input"] = json!("swim_up");
        assert!(parse(&value, false).is_err());
        value["vertical_input"] = json!("fly_down");
        value["speed"] = json!(1.1);
        assert!(parse(&value, false).is_err());
        value["speed"] = json!(-0.1);
        assert!(parse(&value, false).is_err());
    }

    #[test]
    fn eligible_flight_input_is_owner_released_and_cannot_join_walk_route_or_swim() {
        let mut value = args(3, 1);
        value["primary"] = json!(false);
        value["block"] = json!(false);
        value["fly"] = json!(true);
        let (_, desired) = parse(&value, false).unwrap();
        let desired = desired.unwrap();
        let mut sent = Vec::new();
        replace_inputs_with(None, &desired, |input, pressed| sent.push((input, pressed)));
        assert_eq!(sent, vec![(InputKind::Fly, true)]);
        release_inputs_with(&desired, |input, pressed| sent.push((input, pressed)));
        assert_eq!(sent.last(), Some(&(InputKind::Fly, false)));
        value["vertical_input"] = json!("swim_up");
        assert!(parse(&value, false).is_err());
        value["vertical_input"] = json!("neutral");
        value["movement_intent"] = json!("destination");
        value["destination"] = json!([2.0, 0.0, 1.0]);
        value["movement"] = json!([0.0, 0.0]);
        assert!(parse(&value, false).is_err());
        value["fly"] = json!("true");
        assert!(parse(&value, false).is_err());
    }

    #[test]
    fn swim_input_uses_actual_water_contact_and_retired_control_has_no_vertical_input() {
        assert_eq!(water_contact(None), None);
        assert_eq!(water_contact(Some(&comp::PhysicsState::default())), Some(false));
        let water = comp::PhysicsState {
            in_fluid: Some(comp::Fluid::Liquid {
                kind: comp::fluid_dynamics::LiquidKind::Water,
                vel: comp::Vel(Vec3::zero()), depth: 1.0,
            }),
            ..comp::PhysicsState::default()
        };
        let lava = comp::PhysicsState {
            in_fluid: Some(comp::Fluid::Liquid {
                kind: comp::fluid_dynamics::LiquidKind::Lava,
                vel: comp::Vel(Vec3::zero()), depth: 1.0,
            }),
            ..comp::PhysicsState::default()
        };
        assert_eq!(water_contact(Some(&water)), Some(true));
        assert_eq!(water_contact(Some(&lava)), Some(false));
        let mut value = args(1, 1);
        value["vertical_input"] = json!("swim_down");
        let (_, desired) = parse(&value, false).unwrap();
        let desired = desired.unwrap();
        let mut controls = Controls::default();
        controls.active = Some(desired);
        assert_eq!(controls.vertical_input(), -1.0);
        assert_eq!(controls.observation()["vertical_input"], "swim_down");
        let mut state = BridgeState::new("swim-input-test", Dir::default());
        state.continuous.active = controls.active.clone();
        assert_eq!(base_inputs_for_tick(&state).move_z, -1.0);
        controls.active = None;
        state.continuous.active = None;
        assert_eq!(controls.vertical_input(), 0.0);
        assert_eq!(base_inputs_for_tick(&state).move_z, 0.0);
        value["movement_intent"] = json!("destination");
        value["destination"] = json!([1.0, 0.0, 1.0]);
        value["movement"] = json!([0.0, 0.0]);
        assert!(parse(&value, false).is_err());
        value["vertical_input"] = json!("fly");
        assert!(parse(&value, false).is_err());
    }

    #[test]
    fn bridge_incarnation_fences_reused_session_and_entity_ids() {
        use specs::Builder;
        let mut world = specs::World::new();
        let entity = world.create_entity().build();
        let first = BridgeState::new("reused-session", Dir::default());
        let second = BridgeState::new("reused-session", Dir::default());
        assert_eq!(entity_ref(&first, entity, 7), entity_ref(&first, entity, 7));
        assert_ne!(entity_ref(&first, entity, 7), entity_ref(&second, entity, 7));
    }

    #[test]
    fn coherent_movement_tracking_attack_and_block_are_not_serial_steps() {
        let (owner, desired) = parse(&args(1, 1), false).unwrap();
        let desired = desired.unwrap();
        let mut controls = Controls::default();
        controls.authorize(&owner).unwrap();
        controls.accept(&owner);
        let mut inputs = Vec::new();
        replace_inputs_with(None, &desired, |input, pressed| inputs.push((input, pressed)));
        assert_eq!(desired.movement, Vec2::new(0.6, 0.8));
        assert_eq!(desired.target.as_ref().unwrap().uid, 2);
        assert_eq!(inputs, vec![(InputKind::Primary, true), (InputKind::Block, true)]);
        controls.active = Some(desired.clone());
        let observation = controls.observation();
        assert_eq!(observation["primary_held"], true);
        assert_eq!(observation["block_held"], true);
        assert_eq!(observation["tracking_target"]["uid"], 2);
        let (next, replacement) = parse(&args(1, 2), false).unwrap();
        controls.authorize(&next).unwrap(); // overlap is an UPDATE, not avatar_busy
        inputs.clear();
        replace_inputs_with(controls.active.as_ref(), &replacement.unwrap(),
            |input, pressed| inputs.push((input, pressed)));
        assert!(inputs.is_empty()); // continuing inference does not repulse held attacks
        release_inputs_with(&desired, |input, pressed| inputs.push((input, pressed)));
        assert_eq!(inputs, vec![(InputKind::Primary, false), (InputKind::Block, false)]);
    }

    #[test]
    fn old_owner_and_late_sequences_cannot_replace_or_stop_new_owner() {
        let mut controls = Controls::default();
        let (old, _) = parse(&args(1, 1), false).unwrap();
        controls.accept(&old);
        let mut value = args(2, 1);
        value["owner"] = json!("brain");
        let (new, _) = parse(&value, false).unwrap();
        controls.authorize(&new).unwrap();
        controls.accept(&new);
        assert_eq!(controls.authorize(&old), Err("stale_control_owner"));
        assert_eq!(controls.authorize(&new), Err("stale_control_owner"));
        let mut next = new.clone();
        next.sequence += 1;
        controls.authorize(&next).unwrap();
        controls.retired = true;
        assert_eq!(controls.authorize(&next), Err("stale_control_owner"));
        next.generation += 1;
        controls.authorize(&next).unwrap();
    }

    #[test]
    fn route_continues_under_same_goal_but_never_crosses_owner_goal_or_retirement() {
        let mut value = semantic_args("destination");
        value["destination"] = json!([3.5, 0.5, 1.0]);
        value["target"] = Value::Null;
        let old = parse(&value, false).unwrap().1.unwrap();
        let rock = Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        let mut terrain = |p: Vec3<i32>| Some(if p.z < 1 { rock } else { Block::empty() });
        let mut route = local_route::Route::new(
            Vec3::new(0.5, 0.5, 1.0), Vec3::new(3.5, 0.5, 1.0), 1, 0,
            0.4, 0.0, 1.8, &mut terrain).unwrap();
        route.id = old.owner.generation;
        let mut next = old.clone();
        next.owner.sequence += 1;
        next.facing = Facing::Travel;
        assert!(continue_route(Some(&old), &next, Some(&route), None));
        assert_eq!(route.id, old.owner.generation);
        next.destination = Some(Vec3::new(4.5, 0.5, 1.0));
        assert!(!continue_route(Some(&old), &next, Some(&route), None));
        next.destination = old.destination;
        next.owner.owner = "other_owner".into();
        assert!(!continue_route(Some(&old), &next, Some(&route), None));
        next.owner.owner = old.owner.owner.clone();
        let mut interrupted = local_motion::MotionFeedback::new(
            Vec3::new(0.5, 0.5, 1.0), Vec2::unit_x(), 0);
        interrupted.status = "control_motion_arrived";
        assert!(!continue_route(Some(&old), &next, Some(&route), Some(&interrupted)));
        let mut interrupted_desired = old.clone();
        interrupted_desired.movement_intent = None;
        interrupted_desired.destination = None;
        assert!(!route_moving(&interrupted_desired, &Some(route)));
    }

    #[test]
    fn release_can_fence_a_first_update_still_in_flight() {
        let mut controls = Controls::default();
        let (update, _) = parse(&args(1, 1), false).unwrap();
        let mut release = update.clone();
        release.sequence = 2;
        controls.authorize(&release).unwrap();
        controls.accept(&release);
        controls.retired = true;
        assert_eq!(controls.authorize(&update), Err("stale_control_owner"));
        // Even an old owner that queued a later sequence cannot resurrect it.
        release.sequence = 3;
        assert_eq!(controls.authorize(&release), Err("stale_control_owner"));
    }

    #[test]
    fn full_replacement_requires_explicit_neutral_values() {
        for field in ["target", "primary", "block", "movement", "actor"] {
            let mut value = args(1, 1);
            value.as_object_mut().unwrap().remove(field);
            assert!(parse(&value, false).is_err(), "{field}");
        }
        let mut neutral = args(1, 2);
        neutral["target"] = Value::Null;
        neutral["movement"] = json!([0.0, 0.0]);
        neutral["primary"] = json!(false);
        neutral["block"] = json!(false);
        let old = parse(&args(1, 1), false).unwrap().1.unwrap();
        let new = parse(&neutral, false).unwrap().1.unwrap();
        let mut inputs = Vec::new();
        replace_inputs_with(Some(&old), &new, |i, p| inputs.push((i, p)));
        assert_eq!(inputs, vec![(InputKind::Primary, false), (InputKind::Block, false)]);
        assert_eq!(new.movement, Vec2::zero());
        neutral["movement"] = json!([1, 1]);
        assert!(parse(&neutral, false).is_err());
        neutral["movement"] = json!([0, 0]);
        neutral["unexpected"] = json!(true);
        assert!(parse(&neutral, false).is_err());
    }

    #[test]
    fn moving_same_target_is_valid_but_missing_dead_or_reused_lifecycle_is_not() {
        let target = EntityRef { uid: 2, lifecycle: "target-1".into() };
        let mut record = json!({"uid": 2, "control_target": target, "position": [1, 2, 3]});
        assert!(observed_target(&[record.clone()], &target).is_ok());
        record["position"] = json!([4, 5, 6]);
        assert!(observed_target(&[record.clone()], &target).is_ok());
        assert_eq!(observed_target(&[], &target).unwrap_err(), "control_target_missing");
        record["health"] = json!({"dead": true});
        assert_eq!(observed_target(&[record.clone()], &target).unwrap_err(), "control_target_dead");
        record["control_target"]["lifecycle"] = json!("target-2");
        assert_eq!(observed_target(&[record], &target).unwrap_err(), "control_target_changed");
    }

    #[test]
    fn targeted_and_continuous_aim_use_fresh_scaled_center_and_reject_occlusion() {
        let target = EntityRef { uid: 2, lifecycle: "small-target".into() };
        let eye = Vec3::new(0.0, 0.0, 1.6);
        for (position, direction_axis) in [
            (Vec3::new(8.0, 0.0, 0.0), 0),
            (Vec3::new(0.0, 8.0, 0.0), 1),
        ] {
            let record = json!({"uid": 2, "control_target": target, "position":
                [position.x, position.y, position.z]});
            observed_target(&[record], &target).unwrap();
            let (direction, center) = center_target_aim(
                eye, position, 0.2, 0.2, |from, aim| {
                    assert_eq!(from, eye);
                    assert!((aim.z - 0.02).abs() < 1e-6);
                    checked_loaded_ray((aim - from).magnitude(), (aim - from).magnitude(), true)
                },
            ).unwrap();
            assert!((center.z - 0.02).abs() < 1e-6);
            assert!(if direction_axis == 0 {
                direction.to_vec().x > 0.9 && direction.to_vec().y.abs() < 1e-6
            } else {
                direction.to_vec().y > 0.9 && direction.to_vec().x.abs() < 1e-6
            });
        }
        assert_eq!(observed_target(&[], &target).unwrap_err(), "control_target_missing");
        assert_eq!(
            center_target_aim(eye, Vec3::new(8.0, 0.0, 0.0), 0.2, 0.2,
                              |_, _| checked_loaded_ray(8.0, 2.0, false)).unwrap_err(),
            "control_target_occluded");
        assert_eq!(checked_loaded_ray(8.0, 2.0, true), Err("control_target_occluded"));
    }

    fn semantic_args(intent: &str) -> Value {
        let mut value = args(1, 1);
        value["movement"] = json!([0, 0]);
        value["movement_intent"] = json!(intent);
        value
    }

    #[test]
    fn relative_geometry_updates_for_ordinary_motion_of_the_same_target() {
        let target_ref = EntityRef { uid: 2, lifecycle: "target-1".into() };
        for (intent, first, moved) in [
            ("approach", Vec2::new(1.0, 0.0), Vec2::new(0.0, 1.0)),
            ("retreat", Vec2::new(-1.0, 0.0), Vec2::new(0.0, -1.0)),
            ("left", Vec2::new(0.0, 1.0), Vec2::new(-1.0, 0.0)),
            ("right", Vec2::new(0.0, -1.0), Vec2::new(1.0, 0.0)),
        ] {
            let desired = parse(&semantic_args(intent), false).unwrap().1.unwrap();
            for (position, expected) in [(Vec3::new(10.0, 0.0, 0.0), first),
                                         (Vec3::new(0.0, 10.0, 4.0), moved)] {
                let record = json!({
                    "uid": 2, "control_target": target_ref,
                    "position": [position.x, position.y, position.z],
                });
                assert!(observed_target(&[record], &target_ref).is_ok());
                assert_eq!(movement_for_tick(&desired, Some(Vec3::zero()), Some(position)),
                           Ok(expected), "{intent}");
            }
            assert_eq!(desired.target.as_ref(), Some(&target_ref));
        }
    }

    #[test]
    fn semantic_geometry_uses_fresh_actor_and_rejects_missing_geometry() {
        let desired = parse(&semantic_args("approach"), false).unwrap().1.unwrap();
        let target = Some(Vec3::new(10.0, 0.0, 3.0));
        assert_eq!(movement_for_tick(&desired, Some(Vec3::zero()), target),
                   Ok(Vec2::new(1.0, 0.0)));
        assert_eq!(movement_for_tick(&desired, Some(Vec3::new(20.0, 0.0, 0.0)), target),
                   Ok(Vec2::new(-1.0, 0.0)));
        assert_eq!(movement_for_tick(&desired, target, target), Ok(Vec2::zero()));
        assert_eq!(movement_for_tick(&desired, None, target), Err("control_actor_unavailable"));
        assert_eq!(movement_for_tick(&desired, target, None), Err("control_target_missing"));
        assert_eq!(movement_for_tick(&desired, Some(Vec3::new(f32::NAN, 0.0, 0.0)), target),
                   Err("control_movement_invalid_geometry"));
    }

    #[test]
    fn destination_bearing_helper_is_not_itself_arrival_or_route_following() {
        let mut value = semantic_args("destination");
        value["target"] = Value::Null;
        value["destination"] = json!([3, 4, 100]);
        let desired = parse(&value, false).unwrap().1.unwrap();
        assert_eq!(movement_for_tick(&desired, Some(Vec3::zero()), None),
                   Ok(Vec2::new(0.6, 0.8)));
        assert_eq!(movement_for_tick(&desired, Some(Vec3::new(2.625, 3.5, 0.0)), None),
                   Ok(Vec2::new(0.375, 0.5)));
        assert_eq!(movement_for_tick(&desired, Some(Vec3::new(3.0, 4.0, -100.0)), None),
                   Ok(Vec2::zero()));
        // The raw helper remains a bearing; the native route follower overrides
        // this value during destination handling and on each game tick.
        assert_eq!(movement_for_tick(&desired, Some(Vec3::new(3.0, 5.0, 0.0)), None),
                   Ok(Vec2::new(0.0, -1.0)));
    }

    #[test]
    fn semantic_arguments_are_explicit_and_world_vectors_remain_compatible() {
        assert_eq!(capability()["movement_intents"],
                   json!(["approach", "retreat", "left", "right", "destination"]));
        let original = parse(&args(1, 1), false).unwrap().1.unwrap();
        assert!(original.movement_intent.is_none());
        assert_eq!(movement_for_tick(&original, None, None), Ok(Vec2::new(0.6, 0.8)));
        for patch in [
            json!({"movement_intent": "closest_enemy"}),
            json!({"movement_intent": true}),
            json!({"movement": [0.1, 0]}),
            json!({"target": null}),
            json!({"destination": [1, 2, 3]}),
            json!({"movement_intent": "destination"}),
            json!({"movement_intent": "destination", "destination": [1, 2]}),
            json!({"movement_intent": "destination", "destination": [1, true, 3]}),
            json!({"movement_intent": "destination", "destination": [1, 1e100, 3]}),
            json!({"movement_intent": null, "destination": [1, 2, 3]}),
        ] {
            let mut invalid = semantic_args("approach");
            invalid.as_object_mut().unwrap().extend(patch.as_object().unwrap().clone());
            assert!(parse(&invalid, false).is_err(), "{invalid}");
        }
        let mut nulls = args(1, 1);
        nulls["movement_intent"] = Value::Null;
        nulls["destination"] = Value::Null;
        assert!(parse(&nulls, false).is_ok());
        let mut destination_with_aim = semantic_args("destination");
        destination_with_aim["destination"] = json!([1, 2, 3]);
        assert!(parse(&destination_with_aim, false).unwrap().1.unwrap().target.is_some());
    }

    #[test]
    fn unsafe_direction_only_arm_keeps_pushing_but_segment_feedback_interrupts() {
        let mut value = semantic_args("destination");
        value["destination"] = json!([10, 0, 0]);
        value["target"] = Value::Null;
        let desired = parse(&value, false).unwrap().1.unwrap();
        let mut feedback = local_motion::MotionFeedback::new(Vec3::zero(), Vec2::unit_x(), 0);
        // Execute the old unguarded bearing repeatedly with a collision-held
        // actor; time alone never changed that command.
        for tick in 0..=TPS {
            let unguarded = movement_for_tick(&desired, Some(Vec3::zero()), None).unwrap();
            assert_eq!(unguarded, Vec2::unit_x());
            let guarded = feedback.sample(Vec3::zero(), unguarded, tick);
            if tick == TPS { assert_eq!(guarded, Err("control_motion_nonprogress")); }
            else { assert!(guarded.is_ok()); }
        }
    }

    #[test]
    fn explicit_travel_and_target_tracking_have_distinct_facing() {
        let mut state = BridgeState::new("test", Dir::default());
        let desired = parse(&args(1, 1), false).unwrap().1.unwrap();
        state.continuous.active = Some(desired.clone());
        assert!(state.continuous.strafing());
        let mut travel = desired.clone();
        travel.facing = Facing::Travel;
        state.continuous.active = Some(travel.clone());
        assert!(!state.continuous.strafing());
        set_facing(&mut state, &travel, None, Vec2::unit_x());
        assert_eq!(state.look_dir, Dir::from_unnormalized(Vec3::unit_x()).unwrap());
        let mut invalid = args(2, 1);
        invalid["facing"] = json!("track");
        invalid["target"] = Value::Null;
        assert_eq!(parse(&invalid, false).unwrap_err(), "invalid_control_state");
        assert!(!state.continuous.strafing());
    }
}
