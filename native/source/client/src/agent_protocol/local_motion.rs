//! Feedback for the selected local segment, not a route or a task planner.
use super::*;

#[derive(Clone, Debug)]
pub(super) struct MotionFeedback {
    anchor: Vec3<f32>,
    direction: Vec2<f32>,
    previous: Vec3<f32>,
    forward_progress: f32,
    lateral_error: f32,
    since: u64,
    pub status: &'static str,
    scope: &'static str,
}

impl MotionFeedback {
    pub(super) fn new(position: Vec3<f32>, direction: Vec2<f32>, tick: u64) -> Self {
        Self { anchor: position, direction, previous: position, forward_progress: 0.0,
               lateral_error: 0.0, since: tick, status: "running",
               scope: "selected_local_segment" }
    }

    pub(super) fn route_segment(
        &mut self, anchor: Vec3<f32>, position: Vec3<f32>,
        direction: Vec2<f32>, since: u64,
    ) {
        self.anchor = anchor;
        self.direction = direction;
        self.previous = position;
        self.since = since;
        self.scope = "validated_route_segment";
        let axis = direction.try_normalized().unwrap_or_else(Vec2::zero);
        self.lateral_error = (position.xy() - anchor.xy())
            .dot(Vec2::new(-axis.y, axis.x));
    }

    pub(super) fn sample(
        &mut self, position: Vec3<f32>, direction: Vec2<f32>, tick: u64,
    ) -> Result<(), &'static str> {
        // Compare each physical step to the command that drove it. Relative
        // target tracking can turn; the distant mission goal is never a metric.
        let delta = position.xy() - self.previous.xy();
        let axis = self.direction.try_normalized().unwrap_or_else(Vec2::zero);
        self.forward_progress += delta.dot(axis);
        self.lateral_error += delta.dot(Vec2::new(-axis.y, axis.x));
        self.previous = position;
        self.direction = direction;
        if self.lateral_error.abs() > 0.75 {
            self.status = "deviation";
            return Err("control_motion_deviation");
        }
        if self.forward_progress >= 0.25 {
            self.anchor = position;
            self.since = tick;
            self.forward_progress = 0.0;
            self.status = "progress";
        } else if tick.saturating_sub(self.since) >= TPS && direction.magnitude_squared() > 0.01 {
            self.status = "nonprogress";
            return Err("control_motion_nonprogress");
        }
        Ok(())
    }

    pub(super) fn observation(&self) -> Value {
        json!({"status": self.status, "segment_origin": [self.anchor.x, self.anchor.y, self.anchor.z],
               "segment_direction": [self.direction.x, self.direction.y], "since_tick": self.since,
               "lateral_deviation": self.lateral_error,
               "scope": self.scope, "goal_success": null})
    }
}

fn columns(position: Vec3<f32>, direction: Vec2<f32>, radius: f32) -> Vec<Vec2<i32>> {
    let Some(axis) = direction.try_normalized() else { return Vec::new() };
    let side = Vec2::new(-axis.y, axis.x) * radius;
    [radius + 0.15, radius + 0.6].into_iter().flat_map(|distance|
        [-side, Vec2::zero(), side].map(|lateral| {
            let point = position.xy() + axis * distance + lateral;
            Vec2::new(point.x.floor() as i32, point.y.floor() as i32)
        })).collect()
}

pub(super) fn footprint_columns(position: Vec2<f32>, radius: f32) -> Vec<Vec2<i32>> {
    let mut points = Vec::new();
    for x in (position.x - radius).floor() as i32..=(position.x + radius).floor() as i32 {
        for y in (position.y - radius).floor() as i32..=(position.y + radius).floor() as i32 {
            if position.x - radius < x as f32 + 1.0 && position.x + radius > x as f32
                && position.y - radius < y as f32 + 1.0 && position.y + radius > y as f32
            {
                points.push(Vec2::new(x, y));
            }
        }
    }
    points
}

fn sampled_columns(position: Vec3<f32>, direction: Vec2<f32>, radius: f32) -> Vec<Vec2<i32>> {
    let mut sampled: std::collections::BTreeSet<_> = columns(position, direction, radius)
        .into_iter().map(|p| (p.x, p.y)).collect();
    let point = position.xy() + direction.normalized() * 0.05;
    for column in footprint_columns(point, radius) {
        sampled.insert((column.x, column.y));
    }
    sampled.into_iter().map(|(x, y)| Vec2::new(x, y)).collect()
}

pub(super) fn check_segment_cells(
    from: Vec3<f32>, to: Vec3<f32>, radius: f32, z_min: f32, z_max: f32,
    cell: impl FnMut(Vec3<i32>) -> Option<Block>,
) -> Result<(), &'static str> {
    check_segment_cells_with_medium(from, to, radius, z_min, z_max, MotionMedium::Walk, cell)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum MotionMedium { Walk, Swim, Fly, Airborne }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum MotionContext {
    Walk,
    Glide,
    GroundMount(Uid, Entity, Option<common::mounting::VolumePos>),
    Vehicle(Uid, Entity, common::mounting::VolumePos),
    Passenger(common::mounting::VolumePos),
    EntityPassenger(Uid, Entity),
}

impl MotionContext {
    pub(super) fn label(self) -> &'static str {
        match self {
            Self::Walk => "walk",
            Self::Glide => "airborne_glide",
            Self::GroundMount(..) => "steered_mount",
            Self::Vehicle(..) => "steered_vehicle",
            Self::Passenger(..) => "passenger",
            Self::EntityPassenger(..) => "entity_passenger",
        }
    }

    pub(super) fn same_body(self, other: Self) -> bool {
        matches!((self, other), (Self::Walk | Self::Glide, Self::Walk | Self::Glide))
            || self == other
    }
}

fn context_identity(context: MotionContext) -> Value {
    let seat = match context {
        MotionContext::GroundMount(_, _, seat) => seat,
        MotionContext::Vehicle(_, _, seat) | MotionContext::Passenger(seat) => Some(seat),
        _ => None,
    };
    json!({
        "kind": context.label(),
        "controlled_uid": match context {
            MotionContext::GroundMount(uid, ..) | MotionContext::Vehicle(uid, ..)
            | MotionContext::EntityPassenger(uid, ..) => Some(uid.0.get()),
            _ => None,
        },
        "seat": seat.map(|seat| json!({"kind": match seat.kind {
            common::mounting::Volume::Terrain => "terrain",
            common::mounting::Volume::Entity(_) => "entity",
        }, "position": [seat.pos.x, seat.pos.y, seat.pos.z]})),
        "carrier_steering_eligible":
            !matches!(context, MotionContext::Passenger(..) | MotionContext::EntityPassenger(..)),
    })
}

pub(super) fn mounted_context(
    uid: Uid, entity: Entity, seat: Option<common::mounting::VolumePos>,
    body: Option<&comp::Body>, collider: Option<&comp::Collider>, downed: bool,
) -> Result<MotionContext, &'static str> {
    let body = body.ok_or("control_motion_mount_unavailable")?;
    if downed { return Err("control_motion_mount_unavailable"); }
    if body.has_free_will() {
        return if seat.is_none() { Ok(MotionContext::EntityPassenger(uid, entity)) }
            else { Err("control_motion_mount_not_steered") };
    }
    Ok(if let Some(seat) = seat
        && collider.is_some_and(comp::Collider::is_voxel)
    {
        MotionContext::Vehicle(uid, entity, seat)
    } else { MotionContext::GroundMount(uid, entity, seat) })
}

pub(super) fn motion_context(client: &Client) -> Result<MotionContext, &'static str> {
    use common::{link::Is, mounting::{Rider, Volume, VolumeRider}};
    let (entity_mount, seat) = if let Some(rider) = client.current::<Is<Rider>>() {
        (Some(rider.mount), None)
    } else if let Some(rider) = client.current::<Is<VolumeRider>>() {
        if !rider.is_steering_entity() {
            return Ok(MotionContext::Passenger(rider.pos));
        }
        let Volume::Entity(uid) = rider.pos.kind else {
            return Ok(MotionContext::Passenger(rider.pos));
        };
        (Some(uid), Some(rider.pos))
    } else { (None, None) };
    if let Some(uid) = entity_mount {
        let mount = entity_by_uid(client, uid.0.get())
            .ok_or("control_motion_mount_unavailable")?;
        let state = client.state();
        let body = state.read_storage::<comp::Body>();
        let health = state.read_storage::<comp::Health>();
        let characters = state.read_storage::<comp::CharacterState>();
        let collider = state.read_storage::<comp::Collider>();
        return mounted_context(uid, mount, seat, body.get(mount), collider.get(mount),
            comp::is_downed_or_dead(health.get(mount), characters.get(mount)));
    }
    if client.current::<comp::CharacterState>().as_ref().is_some_and(|state| state.is_glide()) {
        if client.current::<comp::PhysicsState>().as_ref()
            .is_none_or(|state| state.on_ground.is_some())
            || client.current::<comp::Inventory>().as_ref()
                .and_then(|inventory| inventory.equipped(EquipSlot::Glider)).is_none()
        {
            return Err("control_motion_glide_not_airborne");
        }
        return Ok(MotionContext::Glide);
    }
    Ok(MotionContext::Walk)
}

fn ground_contact(client: &Client, context: MotionContext) -> Result<bool, &'static str> {
    if let MotionContext::GroundMount(uid, selected, _) | MotionContext::Vehicle(uid, selected, _) = context {
        let Some(entity) = entity_by_uid(client, uid.0.get()).filter(|entity| *entity == selected)
            else { return Err("control_motion_mount_changed") };
        return client.state().read_storage::<comp::PhysicsState>().get(entity)
            .map(|state| state.on_ground.is_some())
            .ok_or("control_motion_physics_unavailable");
    }
    client.current::<comp::PhysicsState>().as_ref()
        .map(|state| state.on_ground.is_some())
        .ok_or("control_motion_physics_unavailable")
}

fn selected_medium(
    context: MotionContext, fly: bool, swim: bool, on_ground: bool,
) -> Result<MotionMedium, &'static str> {
    if matches!(context, MotionContext::Passenger(_) | MotionContext::EntityPassenger(..)) {
        return Err("control_motion_mount_not_steered");
    }
    Ok(if fly { MotionMedium::Fly }
        else if swim { MotionMedium::Swim }
        else if context == MotionContext::Glide || !on_ground { MotionMedium::Airborne }
        else { MotionMedium::Walk })
}

fn check_segment_cells_with_medium(
    from: Vec3<f32>, to: Vec3<f32>, radius: f32, z_min: f32, z_max: f32,
    medium: MotionMedium, mut cell: impl FnMut(Vec3<i32>) -> Option<Block>,
) -> Result<(), &'static str> {
    if ![from.x, from.y, from.z, to.x, to.y, to.z, radius, z_min, z_max]
        .iter().all(|v| v.is_finite()) || radius < 0.0 || z_min >= z_max
        || (medium == MotionMedium::Walk && (to.z - from.z).abs() > 0.01)
        || (to.z - from.z).abs() > 0.1
    {
        return Err("control_motion_invalid_geometry");
    }
    let delta = to - from;
    let steps = ((delta.magnitude() / 0.2).ceil() as usize).max(1);
    for step in 1..=steps {
        let pos = from + delta * (step as f32 / steps as f32);
        let bottom = pos.z + z_min;
        let top = pos.z + z_max;
        let actor = Aabb {
            min: Vec3::new(pos.x - radius, pos.y - radius, bottom),
            max: Vec3::new(pos.x + radius, pos.y + radius, top),
        };
        let low = (bottom - TERRAIN_QUERY_MAX_SUPPORT_HEIGHT_METRES).floor() as i32;
        let high = top.ceil() as i32;
        for column in footprint_columns(pos.xy(), radius) {
            let (x, y) = (column.x, column.y);
            for z in low..=high {
                let block = cell(Vec3::new(x, y, z))
                    .ok_or("control_motion_terrain_unavailable")?;
                let block_aabb = Aabb {
                    min: Vec3::new(x as f32, y as f32, z as f32),
                    max: Vec3::new(x as f32 + 1.0, y as f32 + 1.0,
                                   z as f32 + block.solid_height()),
                };
                if block.kind() == BlockKind::Lava
                    || block.get_sprite().is_some_and(terrain_sprite_has_on_ground_debuff)
                {
                    if z as f32 <= top && (z as f32 + block.solid_height()) >= bottom {
                        return Err("control_motion_hazard");
                    }
                }
                if block.is_solid() && actor.collides_with_aabb(block_aabb)
                    && block.valid_collision_dir(actor, block_aabb, delta)
                {
                    return Err("control_motion_obstructed");
                }
            }
        }
        if matches!(medium, MotionMedium::Walk | MotionMedium::Swim) {
            let mut supported = false;
            let mut water = false;
            for column in footprint_columns(pos.xy(), radius) {
                for z in (bottom.floor() as i32 - Block::MAX_HEIGHT.ceil() as i32)
                    ..=if medium == MotionMedium::Swim {
                        top.floor() as i32
                    } else {
                        bottom.floor() as i32
                    }
                {
                    let block = cell(Vec3::new(column.x, column.y, z))
                        .ok_or("control_motion_terrain_unavailable")?;
                    let height = z as f32 + block.solid_height();
                    supported |= z <= bottom.floor() as i32 && (block.is_solid() || block.is_liquid())
                        && height <= bottom + 0.2 && height >= bottom - 1.1;
                    water |= medium == MotionMedium::Swim && block.kind() == BlockKind::Water
                        && z as f32 <= top && (z + 1) as f32 >= bottom;
                }
            }
            if medium == MotionMedium::Swim && !water {
                return Err("control_swim_destination_not_water");
            }
            if medium == MotionMedium::Walk && !supported {
                return Err("control_motion_unsupported");
            }
        }
    }
    Ok(())
}

#[cfg(test)]
pub(super) fn check_cells(
    position: Vec3<f32>, direction: Vec2<f32>, radius: f32, height: f32,
    cell: impl FnMut(Vec3<i32>) -> Option<Block>,
) -> Result<(), &'static str> {
    let axis = direction.try_normalized().ok_or("control_motion_invalid_geometry")?;
    check_segment_cells(position, position + Vec3::new(axis.x * 0.05, axis.y * 0.05, 0.0),
                        radius, 0.0, height, cell)
}

#[cfg(test)]
pub(super) fn geometry_cells(
    position: Vec3<f32>, forward: Vec2<f32>, radius: f32, height: f32,
    cell: impl FnMut(Vec3<i32>) -> Option<Block>,
) -> Value {
    geometry_cells_with_medium(position, forward, radius, height, MotionMedium::Walk, cell)
}

fn geometry_cells_with_medium(
    position: Vec3<f32>, forward: Vec2<f32>, radius: f32, height: f32,
    medium: MotionMedium,
    mut cell: impl FnMut(Vec3<i32>) -> Option<Block>,
) -> Value {
    let left = Vec2::new(-forward.y, forward.x);
    let directions = [("forward", forward), ("left", left), ("right", -left), ("back", -forward)];
    let low = ((position.z - TERRAIN_QUERY_MAX_SUPPORT_HEIGHT_METRES).floor() as i32)
        .min(position.z.floor() as i32 - 2);
    let high = (position.z + height).ceil() as i32;
    let mut samples = BTreeMap::new();
    let direction_columns = directions.map(|(_, direction)| sampled_columns(position, direction, radius));
    let own = position.xy().map(|p| p.floor() as i32);
    for z in low..=high + 1 {
        samples.insert((own.x, own.y, z), cell(Vec3::new(own.x, own.y, z)));
    }
    for points in &direction_columns {
        for p in points {
            for z in low..=high {
                samples.entry((p.x, p.y, z)).or_insert_with(|| cell(Vec3::new(p.x, p.y, z)));
            }
        }
    }
    let rows = samples.iter().map(|(&(x, y, z), block)| {
        json!({"position": [x, y, z], "occupancy": match block {
            None => "unknown", Some(b) if b.is_solid() => "solid",
            Some(b) if b.is_liquid() => "liquid", Some(_) => "known_free",
        }, "solid_height_m": block.map(|b| b.solid_height()),
        "hazard": block.map(|b| b.kind() == BlockKind::Lava
            || b.get_sprite().is_some_and(terrain_sprite_has_on_ground_debuff))})
    }).collect::<Vec<_>>();
    let checks = directions.into_iter().zip(direction_columns).map(|((name, direction), points)| {
        let result = direction.try_normalized().ok_or("control_motion_invalid_geometry")
            .and_then(|axis| check_segment_cells_with_medium(position,
                position + axis.with_z(0.0) * 0.05, radius, 0.0, height, medium,
                |p| samples.get(&(p.x, p.y, p.z)).copied().flatten()));
        json!({"direction": name, "world_xy": [direction.x, direction.y],
            "local_step_status": result.err().unwrap_or(if medium == MotionMedium::Walk {
                "sampled_step_clear_supported"
            } else { "sampled_step_clear" }),
            "sample_columns_xy": points.iter()
                .map(|p| [p.x, p.y]).collect::<Vec<_>>(),
            "z_inclusive": [low, high]})
    }).collect::<Vec<_>>();
    let mut unknown = false;
    let mut obstruction = false;
    let mut support = false;
    let mut overhead_obstruction = false;
    for z in low..=high + 1 {
        if let Some(block) = samples[&(own.x, own.y, z)] {
            obstruction |= terrain_block_overlaps_span(z, block, position.z + 0.2, position.z + height)
                && !block.is_liquid();
            overhead_obstruction |= terrain_block_overlaps_span(z, block, position.z + height, position.z + height + 1.0)
                && !block.is_liquid();
            let top = z as f32 + block.solid_height();
            support |= (block.is_solid() || block.is_liquid()) && top <= position.z + 0.2 && top >= position.z - 1.1;
        } else { unknown = true; }
    }
    json!({"origin": [position.x, position.y, position.z],
        "facing_xy": [forward.x, forward.y], "radius_m": radius, "height_m": height,
        "motion_medium": match medium {
            MotionMedium::Walk => "walk", MotionMedium::Swim => "swim",
            MotionMedium::Fly => "fly", MotionMedium::Airborne => "airborne",
        },
        "directions": checks, "cells": rows,
        "self_column": {"xy": [own.x, own.y], "z_inclusive": [low, high + 1],
            "body_clearance": if obstruction { "obstructed" } else if unknown { "unknown" } else { "sampled_column_clear" },
            "overhead_one_metre": if overhead_obstruction { "obstructed" } else if unknown { "unknown" } else { "sampled_column_clear" },
            "support": if support { "solid_or_liquid_support" } else if unknown { "unknown" } else { "known_unsupported" }},
        "coverage": {"domain": "all_listed_direction_and_self_columns_with_inclusive_z_cells",
            "expected_cells": samples.len(), "known_cells": samples.values().filter(|v| v.is_some()).count(),
            "unknown_cells": samples.values().filter(|v| v.is_none()).count()},
        "limitations": "sampled_local_step_not_full_volume_route_escape_or_jump_guarantee;liquid_support_is_not_walkability"})
}

pub(super) fn observation(client: &Client, state: &BridgeState) -> Value {
    let context = motion_context(client);
    let shape = context.as_ref().map_err(|reason| *reason).and_then(|context| {
        if matches!(context, MotionContext::Passenger(_) | MotionContext::EntityPassenger(..)) {
            client.position().ok_or("control_actor_unavailable")
                .and_then(|position| actor_shape(client).map(|(r, low, high)| (position, r, low, high)))
        } else { pose_and_shape(client, *context) }
    });
    let geometry = shape.as_ref().ok().and_then(|&(position, radius, z_min, z_max)| {
        if matches!(context, Ok(MotionContext::Vehicle(..) | MotionContext::Passenger(..)
            | MotionContext::EntityPassenger(..))) {
            return None;
        }
        let controlled_ori = match context.ok()? {
            MotionContext::GroundMount(_, entity, _) =>
                client.state().read_storage::<comp::Ori>().get(entity).copied(),
            _ => client.current::<comp::Ori>(),
        }?;
        let forward = controlled_ori.look_dir().to_vec().xy().try_normalized()?;
        let terrain = client.state().terrain();
        let medium = selected_medium(context.ok()?, false, false,
            ground_contact(client, context.ok()?).ok()?).ok()?;
        Some(geometry_cells_with_medium(position + Vec3::unit_z() * z_min, forward,
            radius, z_max - z_min, medium,
            |p| terrain.get(p).ok().copied()))
    });
    let mut record = observation_record(combat_perception::binding(client, state), geometry);
    record["movement_context"] = match context {
        Ok(context) => {
            let mut info = context_identity(context);
            info["status"] = json!("observed");
            info["shape"] = shape.as_ref().ok().map(|(p, radius, z_min, z_max)| json!({
                    "origin": [p.x, p.y, p.z], "radius_m": radius,
                    "z_min_m": z_min, "z_max_m": z_max,
                    "body": if matches!(context, MotionContext::Passenger(..)
                        | MotionContext::EntityPassenger(..)) { "rider" }
                        else if matches!(context, MotionContext::GroundMount(..) | MotionContext::Vehicle(..)) {
                            "carrier"
                        } else { "actor" },
                    "scope": if matches!(context, MotionContext::Vehicle(..)) {
                        "orientation_and_scale_enclosing_voxel_volume_not_exact_hull"
                    } else { "capsule_prism_enclosure" },
                })).unwrap_or(Value::Null);
            if let Err(reason) = &shape { info["shape_reason"] = json!(reason); }
            if matches!(context, MotionContext::Vehicle(..)) {
                let facing = match context {
                    MotionContext::Vehicle(_, entity, _) =>
                        client.state().read_storage::<comp::Ori>().get(entity).copied(),
                    _ => None,
                };
                info["directional_checks"] = if let Some(ori) = facing {
                    let forward = ori.look_dir().to_vec().xy().try_normalized();
                    forward.map(|forward| {
                        let left = Vec2::new(-forward.y, forward.x);
                        [("forward", forward), ("left", left), ("right", -left), ("back", -forward)]
                            .into_iter().map(|(name, direction)| json!({
                                "direction": name, "status": check_selected(client, context,
                                    direction.with_z(0.0), false, false)
                                    .err().unwrap_or("sampled_step_clear"),
                                "scope": "loaded_enclosing_voxel_volume_horizontal_nonfly",
                            })).collect::<Vec<_>>()
                    }).map_or(Value::Null, |checks| json!(checks))
                } else { Value::Null };
            }
            info
        },
        Err(reason) => json!({"status": "unavailable", "reason": reason}),
    };
    record
}

pub(super) fn observation_record(binding: Value, geometry: Option<Value>) -> Value {
    json!({"schema_version": "1.0", "binding": binding,
        "scope": "body_local_loaded_terrain", "geometry": geometry,
        "source": "local_motion_and_inspect_terrain_shared_block_overlap_rules"})
}

fn pose_and_shape(
    client: &Client, context: MotionContext,
) -> Result<(Vec3<f32>, f32, f32, f32), &'static str> {
    if let MotionContext::GroundMount(uid, selected, _) | MotionContext::Vehicle(uid, selected, _) = context {
        let mount = entity_by_uid(client, uid.0.get())
            .ok_or("control_motion_mount_unavailable")?;
        if mount != selected { return Err("control_motion_mount_changed"); }
        let state = client.state();
        let positions = state.read_storage::<comp::Pos>();
        let orientations = state.read_storage::<comp::Ori>();
        let colliders = state.read_storage::<comp::Collider>();
        let scales = state.read_storage::<comp::Scale>();
        let position = positions.get(mount).ok_or("control_motion_mount_unavailable")?.0;
        let collider = colliders.get(mount).ok_or("control_motion_collider_unavailable")?;
        let scale = scales.get(mount).map_or(1.0, |scale| scale.0);
        let (radius, z_min, z_max) =
            controlled_shape(context, collider, scale, orientations.get(mount))?;
        return Ok((position, radius, z_min, z_max));
    }
    if matches!(context, MotionContext::Passenger(_) | MotionContext::EntityPassenger(..)) {
        return Err("control_motion_mount_not_steered");
    }
    let position = client.position().ok_or("control_actor_unavailable")?;
    let (radius, z_min, z_max) = actor_shape(client)?;
    Ok((position, radius, z_min, z_max))
}

fn controlled_shape(
    context: MotionContext, collider: &comp::Collider, scale: f32,
    orientation: Option<&comp::Ori>,
) -> Result<(f32, f32, f32), &'static str> {
    if matches!(context, MotionContext::Vehicle(..)) {
        vehicle_shape(collider, scale, orientation.ok_or("control_motion_mount_unavailable")?)
    } else { shape(collider, scale) }
}

fn check_in_context(
    client: &Client, context: MotionContext, direction: Vec3<f32>, medium: MotionMedium,
) -> Result<(), &'static str> {
    let (position, radius, z_min, z_max) = pose_and_shape(client, context)?;
    let axis = direction.try_normalized().ok_or("control_motion_invalid_geometry")?;
    let terrain = client.state().terrain();
    check_segment_cells_with_medium(position, position + axis * 0.05, radius, z_min, z_max,
                                    medium, |voxel| terrain.get(voxel).ok().copied())
}

pub(super) fn check_selected(
    client: &Client, context: MotionContext, direction: Vec3<f32>,
    fly: bool, swim: bool,
) -> Result<(), &'static str> {
    let medium = selected_medium(context, fly, swim, ground_contact(client, context)?)?;
    check_in_context(client, context, direction, medium)
}

pub(super) fn actor_shape(client: &Client) -> Result<(f32, f32, f32), &'static str> {
    let collider = client.current::<comp::Collider>().ok_or("control_motion_collider_unavailable")?;
    let scale = client.current::<comp::Scale>().map_or(1.0, |s| s.0);
    actor_terrain_shape(&collider, scale)
}

fn actor_terrain_shape(
    collider: &comp::Collider, scale: f32,
) -> Result<(f32, f32, f32), &'static str> {
    let comp::Collider::CapsulePrism(prism) = collider
        else { return Err("control_motion_collider_unsupported"); };
    if !scale.is_finite() || scale <= 0.0 { return Err("control_motion_invalid_geometry"); }
    // Match the terrain collider used by common/systems/src/phys/mod.rs.
    let scale = scale.min(10.0);
    let (z_min, z_max) = (0.0, prism.z_max.clamp(1.2, 2.95) * scale);
    let radius = collider.bounding_radius().min(0.45) * scale;
    if ![radius, z_min, z_max].iter().all(|v| v.is_finite()) || z_min >= z_max {
        return Err("control_motion_invalid_geometry");
    }
    Ok((radius, z_min, z_max))
}

fn shape(collider: &comp::Collider, scale: f32) -> Result<(f32, f32, f32), &'static str> {
    let comp::Collider::CapsulePrism(prism) = collider
        else { return Err("control_motion_collider_unsupported"); };
    if !scale.is_finite() || scale <= 0.0 { return Err("control_motion_invalid_geometry"); }
    let (z_min, z_max) = (prism.z_min * scale, prism.z_max * scale);
    let radius = (prism.p0.magnitude().max(prism.p1.magnitude()) + prism.radius) * scale;
    if ![radius, z_min, z_max].iter().all(|v| v.is_finite()) || z_min >= z_max {
        return Err("control_motion_invalid_geometry");
    }
    Ok((radius, z_min, z_max))
}

fn vehicle_shape(
    collider: &comp::Collider, scale: f32, orientation: &comp::Ori,
) -> Result<(f32, f32, f32), &'static str> {
    use common::vol::SizedVol;
    if !scale.is_finite() || scale <= 0.0 { return Err("control_motion_invalid_geometry"); }
    let manifest = comp::ship::figuredata::VOXEL_COLLIDER_MANIFEST.read();
    let volume = collider.get_vol(&manifest).ok_or("control_motion_collider_unavailable")?;
    let lower = volume.volume().lower_bound().map(|v| v as f32) + volume.translation;
    let upper = volume.volume().upper_bound().map(|v| v as f32 + 1.0) + volume.translation;
    let mut radius = 0.0_f32;
    let mut z_min = f32::INFINITY;
    let mut z_max = f32::NEG_INFINITY;
    for x in [lower.x, upper.x] {
        for y in [lower.y, upper.y] {
            for z in [lower.z, upper.z] {
                let corner = orientation.local_to_global(Vec3::new(x, y, z) * scale);
                radius = radius.max(corner.xy().magnitude());
                z_min = z_min.min(corner.z);
                z_max = z_max.max(corner.z);
            }
        }
    }
    if ![radius, z_min, z_max].iter().all(|v| v.is_finite()) || radius <= 0.0 || z_min >= z_max {
        return Err("control_motion_invalid_geometry");
    }
    Ok((radius, z_min, z_max))
}

#[cfg(test)]
mod tests {
    use super::*;
    use specs::{Builder, WorldExt};

    #[test]
    fn direct_free_will_rider_has_a_stationary_input_context() {
        let mut world = specs::World::new();
        let entity = world.create_entity().build();
        let uid = Uid(std::num::NonZeroU64::new(43).unwrap());
        let body = comp::Body::BipedLarge(comp::body::biped_large::Body {
            species: comp::body::biped_large::Species::Ogre,
            body_type: comp::body::biped_large::BodyType::Male,
        });
        assert!(body.has_free_will());
        let context = mounted_context(uid, entity, None, Some(&body), None, false).unwrap();
        assert_eq!(context, MotionContext::EntityPassenger(uid, entity));
        assert_eq!(context_identity(context), json!({
            "kind": "entity_passenger", "controlled_uid": uid.0.get(),
            "seat": null, "carrier_steering_eligible": false,
        }));
        let other = world.create_entity().build();
        assert!(!context.same_body(MotionContext::EntityPassenger(uid, other)));
        assert!(!context.same_body(MotionContext::GroundMount(uid, entity, None)));
        assert_eq!(mounted_context(uid, entity, None, None, None, false),
            Err("control_motion_mount_unavailable"));
        assert_eq!(mounted_context(uid, entity, None, Some(&body), None, true),
            Err("control_motion_mount_unavailable"));
        let seat = common::mounting::VolumePos::entity(Vec3::new(0, 0, 0), uid);
        assert_eq!(mounted_context(uid, entity, Some(seat), Some(&body), None, false),
            Err("control_motion_mount_not_steered"));
    }

    #[test]
    fn controlled_mount_uses_real_capsule_extents_including_offset_below_feet() {
        let mut world = specs::World::new();
        let entity = world.create_entity().build();
        let uid = Uid(std::num::NonZeroU64::new(19).unwrap());
        let collider = comp::Collider::CapsulePrism(comp::CapsulePrism {
            p0: Vec2::new(1.0, 0.0), p1: Vec2::new(-1.0, 0.0),
            radius: 0.8, z_min: -0.5, z_max: 5.0,
        });
        let body = comp::Body::Ship(comp::body::ship::Body::Skiff);
        let context = mounted_context(uid, entity, None, Some(&body), Some(&collider), false).unwrap();
        assert_eq!(context, MotionContext::GroundMount(uid, entity, None));
        let (radius, low, high) = controlled_shape(context, &collider, 1.0, None).unwrap();
        assert_eq!((radius, low, high), (1.8, -0.5, 5.0));
        let from = Vec3::new(0.5, 0.5, 1.0);
        let to = from + Vec3::new(0.05, 0.0, 0.0);
        let rock = Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        assert_eq!(check_segment_cells_with_medium(from, to, radius, low, high, MotionMedium::Airborne,
            |p| Some(if p == Vec3::new(2, 0, 4) { rock } else { Block::empty() })),
            Err("control_motion_obstructed"));
        assert_eq!(check_segment_cells_with_medium(from, to, radius, low, high, MotionMedium::Airborne,
            |p| (p != Vec3::new(2, 0, 4)).then_some(Block::empty())),
            Err("control_motion_terrain_unavailable"));
        let (scaled_radius, scaled_low, scaled_high) =
            controlled_shape(context, &collider, 1.5, None).unwrap();
        assert!((scaled_radius - 2.7).abs() < 0.000001);
        assert_eq!((scaled_low, scaled_high), (-0.75, 7.5));
    }

    #[test]
    fn controller_seat_selects_real_scaled_rotated_vehicle_volume_not_rider_capsule() {
        let mut world = specs::World::new();
        let entity = world.create_entity().build();
        let uid = Uid(std::num::NonZeroU64::new(17).unwrap());
        let seat = common::mounting::VolumePos::entity(Vec3::new(2, 1, 0), uid);
        let collider = comp::Collider::Volume(std::sync::Arc::new(
            comp::ship::figuredata::VoxelCollider::from_fn(
                Vec3::new(8, 4, 6), |_| Block::empty())));
        let body = comp::Body::Ship(comp::body::ship::Body::Skiff);
        let context = mounted_context(uid, entity, Some(seat), Some(&body), Some(&collider), false)
            .unwrap();
        assert_eq!(context, MotionContext::Vehicle(uid, entity, seat));
        assert_eq!(selected_medium(context, false, false, false), Ok(MotionMedium::Airborne));
        assert_eq!(selected_medium(context, false, false, true), Ok(MotionMedium::Walk));
        let (radius, low, high) =
            controlled_shape(context, &collider, 1.5, Some(&comp::Ori::default())).unwrap();
        assert!(radius > 0.45 && low < -2.95 && high > 2.95);
        let from = Vec3::new(0.5, 0.5, 5.0);
        let to = from + Vec3::new(0.05, 0.0, 0.0);
        let rock = Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        assert_eq!(check_segment_cells_with_medium(from, to, radius, low, high, MotionMedium::Airborne,
            |_| Some(Block::empty())), Ok(()));
        assert_eq!(check_segment_cells_with_medium(from, to, radius, low, high, MotionMedium::Airborne,
            |p| Some(if p == Vec3::new(3, 0, 6) { rock } else { Block::empty() })),
            Err("control_motion_obstructed"));
        assert_eq!(check_segment_cells_with_medium(from, to, radius, low, high, MotionMedium::Airborne,
            |p| (p != Vec3::new(3, 0, 6)).then_some(Block::empty())),
            Err("control_motion_terrain_unavailable"));
        assert_eq!(mounted_context(uid, entity, Some(seat), Some(&body), Some(&collider), true),
            Err("control_motion_mount_unavailable"));
        let changed_seat = common::mounting::VolumePos::entity(Vec3::new(3, 1, 0), uid);
        assert!(!context.same_body(MotionContext::Vehicle(uid, entity, changed_seat)));
        assert_eq!(controlled_shape(context, &collider, 1.0, None),
            Err("control_motion_mount_unavailable"));
    }

    #[test]
    fn pitched_and_rolled_vehicle_volume_encloses_every_oriented_corner() {
        let mut world = specs::World::new();
        let entity = world.create_entity().build();
        let uid = Uid(std::num::NonZeroU64::new(47).unwrap());
        let seat = common::mounting::VolumePos::entity(Vec3::new(1, 2, 0), uid);
        let collider = comp::Collider::Volume(std::sync::Arc::new(
            comp::ship::figuredata::VoxelCollider::from_fn(
                Vec3::new(8, 4, 6), |_| Block::empty())));
        let body = comp::Body::Ship(comp::body::ship::Body::Skiff);
        let context = mounted_context(uid, entity, Some(seat), Some(&body), Some(&collider), false)
            .unwrap();
        let ori = comp::Ori::default()
            .rotated(vek::Quaternion::rotation_x(0.35))
            .rotated(vek::Quaternion::rotation_y(0.3));
        assert_ne!(ori, comp::Ori::default());
        let (radius, low, high) = controlled_shape(context, &collider, 1.5, Some(&ori)).unwrap();
        let (_, original_low, original_high) =
            controlled_shape(context, &collider, 1.5, Some(&comp::Ori::default())).unwrap();
        assert!(low < original_low || high > original_high);
        for x in [-4.0, 4.0] {
            for y in [-2.0, 2.0] {
                for z in [-3.0, 3.0] {
                    let corner = ori.local_to_global(Vec3::new(x, y, z) * 1.5);
                    assert!(corner.xy().magnitude() <= radius + 0.000001);
                    assert!(corner.z >= low - 0.000001 && corner.z <= high + 0.000001);
                }
            }
        }
    }

    #[test]
    fn actual_selected_glide_medium_steers_in_loaded_air_without_waiving_walk_or_unknown() {
        let rock = Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        let air = |_| Some(Block::empty());
        let from = Vec3::new(0.5, 0.5, 5.0);
        let to = from + Vec3::new(0.05, 0.0, 0.0);
        assert_eq!(selected_medium(MotionContext::Glide, false, false, false), Ok(MotionMedium::Airborne));
        assert_eq!(check_segment_cells_with_medium(from, to, 0.3, 0.0, 2.3,
            MotionMedium::Airborne, air), Ok(()));
        assert_eq!(check_segment_cells_with_medium(from, to, 0.3, 0.0, 2.3,
            MotionMedium::Walk, air), Err("control_motion_unsupported"));
        assert_eq!(selected_medium(MotionContext::Walk, false, false, true), Ok(MotionMedium::Walk));
        assert_eq!(check_segment_cells_with_medium(from, to, 0.3, 0.0, 2.3,
            MotionMedium::Airborne, |p| (p != Vec3::new(0, 0, 5)).then_some(Block::empty())),
            Err("control_motion_terrain_unavailable"));
        assert_eq!(check_segment_cells_with_medium(from, to, 0.3, 0.0, 2.3,
            MotionMedium::Airborne, |p| Some(if p.z == 5 { rock } else { Block::empty() })),
            Err("control_motion_obstructed"));
    }

    #[test]
    fn steered_ground_mount_uses_mount_feet_not_elevated_rider_for_loaded_support() {
        let rock = Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        let mount = Vec3::new(0.5, 0.5, 1.0);
        let rider = mount + Vec3::unit_z() * 1.98;
        let next = Vec3::new(0.05, 0.0, 0.0);
        let ground = |p: Vec3<i32>| Some(if p.z == 0 { rock } else { Block::empty() });
        assert_eq!(check_segment_cells(mount, mount + next, 0.4, 0.0, 1.6, ground), Ok(()));
        assert_eq!(check_segment_cells(rider, rider + next, 0.4, 0.0, 1.6, ground),
            Err("control_motion_unsupported"));
        assert_eq!(check_segment_cells(mount, mount + next, 0.4, 0.0, 1.6,
            |p| (p != Vec3::new(0, 0, 0)).then_some(if p.z == 0 { rock } else { Block::empty() })),
            Err("control_motion_terrain_unavailable"));
        assert_eq!(check_segment_cells(mount, mount + next, 0.4, 0.0, 1.6,
            |_| Some(Block::empty())), Err("control_motion_unsupported"));
    }

    #[test]
    fn observed_airborne_guard_keeps_steering_after_owned_jump_release() {
        let from = Vec3::new(0.5, 0.5, 2.0);
        let to = from + Vec3::new(0.05, 0.0, 0.0);
        assert_eq!(selected_medium(MotionContext::Walk, false, false, false), Ok(MotionMedium::Airborne));
        assert_eq!(selected_medium(MotionContext::Walk, false, false, true), Ok(MotionMedium::Walk));
        assert_eq!(check_segment_cells_with_medium(from, to, 0.3, 0.0, 2.3,
            selected_medium(MotionContext::Walk, false, false, false).unwrap(),
            |_| Some(Block::empty())), Ok(()));
        assert_eq!(check_segment_cells_with_medium(from, to, 0.3, 0.0, 2.3,
            MotionMedium::Walk, |_| Some(Block::empty())), Err("control_motion_unsupported"));
        assert_eq!(check_segment_cells_with_medium(Vec3::new(0.5, 0.5, 1.0),
            Vec3::new(0.55, 0.5, 1.0), 0.3, 0.0, 2.3,
            selected_medium(MotionContext::Walk, false, false, true).unwrap(),
            |_| Some(Block::empty())), Err("control_motion_unsupported"));
        assert!(MotionContext::Walk.same_body(MotionContext::Glide));
        let view = geometry_cells_with_medium(from, Vec2::unit_x(), 0.3, 2.3,
            selected_medium(MotionContext::Walk, false, false, false).unwrap(),
            |_| Some(Block::empty()));
        assert_eq!(view["directions"][0]["local_step_status"], "sampled_step_clear");
        assert_eq!(view["motion_medium"], "airborne");
    }

    #[test]
    fn water_contact_supports_safe_lateral_swim_not_walk_or_hazardous_unknown_cavity() {
        let from = Vec3::new(0.8, 0.5, 1.0);
        let to = from + Vec3::new(0.05, 0.0, 0.0);
        let water = Block::new(BlockKind::Water, vek::Rgb::new(1, 1, 1));
        let rock = Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        let lava = Block::new(BlockKind::Lava, vek::Rgb::new(1, 1, 1));
        let cell = |p: Vec3<i32>| Some(if p.z == 2 { water } else { Block::empty() });
        assert_eq!(check_segment_cells(from, to, 0.3, 0.0, 2.3, cell),
            Err("control_motion_unsupported"));
        assert_eq!(check_segment_cells_with_medium(
            from, to, 0.3, 0.0, 2.3, MotionMedium::Swim, cell), Ok(()));
        assert_eq!(check_segment_cells_with_medium(from, to, 0.3, 0.0, 2.3, MotionMedium::Swim,
            |p| (p != Vec3::new(1, 0, 2)).then_some(
                if p.z == 2 { water } else { Block::empty() })),
            Err("control_motion_terrain_unavailable"));
        assert_eq!(check_segment_cells_with_medium(from, to, 0.3, 0.0, 2.3, MotionMedium::Swim,
            |p| Some(if p == Vec3::new(1, 0, 1) { rock }
                else if p.z == 2 { water } else { Block::empty() })),
            Err("control_motion_obstructed"));
        assert_eq!(check_segment_cells_with_medium(from, to, 0.3, 0.0, 2.3, MotionMedium::Swim,
            |p| Some(if p == Vec3::new(1, 0, 1) { lava }
                else if p.z == 2 { water } else { Block::empty() })),
            Err("control_motion_hazard"));
        assert_eq!(check_segment_cells_with_medium(from, to, 0.3, 0.0, 2.3, MotionMedium::Swim,
            |_| Some(Block::empty())), Err("control_swim_destination_not_water"));
    }

    #[test]
    fn vertical_flight_and_swim_keep_loaded_full_actor_clearance() {
        let from = Vec3::new(0.5, 0.5, 1.0);
        let to = from + Vec3::new(0.0, 0.0, 0.05);
        let rock = Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        let water = Block::new(BlockKind::Water, vek::Rgb::new(1, 1, 1));
        assert_eq!(check_segment_cells_with_medium(from, to, 0.3, 0.0, 2.3,
            MotionMedium::Fly, |_| Some(Block::empty())), Ok(()));
        assert_eq!(check_segment_cells_with_medium(from, to, 0.3, 0.0, 2.3,
            MotionMedium::Fly, |p| (p != Vec3::new(0, 0, 3)).then_some(Block::empty())),
            Err("control_motion_terrain_unavailable"));
        assert_eq!(check_segment_cells_with_medium(from, to, 0.3, 0.0, 2.3,
            MotionMedium::Fly, |p| Some(if p == Vec3::new(0, 0, 3) { rock }
                else { Block::empty() })), Err("control_motion_obstructed"));
        assert_eq!(check_segment_cells_with_medium(from, to, 0.3, 0.0, 2.3,
            MotionMedium::Swim, |p| Some(if p.z >= 1 { water } else { Block::empty() })),
            Ok(()));
        assert_eq!(check_segment_cells_with_medium(from, to, 0.3, 0.0, 2.3,
            MotionMedium::Swim, |_| Some(Block::empty())),
            Err("control_swim_destination_not_water"));
        assert_eq!(check_segment_cells(from, to, 0.3, 0.0, 2.3,
            |_| Some(Block::empty())), Err("control_motion_invalid_geometry"));
    }

    #[test]
    fn eligible_flight_checks_loaded_actor_volume_not_floor_support() {
        let from = Vec3::new(0.8, 0.5, 12.0);
        let to = from + Vec3::new(0.05, 0.0, 0.0);
        let rock = Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        assert_eq!(check_segment_cells(from, to, 0.3, 0.0, 2.0,
            |_| Some(Block::empty())), Err("control_motion_unsupported"));
        assert_eq!(check_segment_cells_with_medium(
            from, to, 0.3, 0.0, 2.0, MotionMedium::Fly,
            |_| Some(Block::empty())), Ok(()));
        assert_eq!(check_segment_cells_with_medium(
            from, to, 0.3, 0.0, 2.0, MotionMedium::Fly,
            |p| (p != Vec3::new(1, 0, 12)).then_some(Block::empty())),
            Err("control_motion_terrain_unavailable"));
        assert_eq!(check_segment_cells_with_medium(
            from, to, 0.3, 0.0, 2.0, MotionMedium::Fly,
            |p| Some(if p == Vec3::new(1, 0, 12) { rock } else { Block::empty() })),
            Err("control_motion_obstructed"));
    }

    #[test]
    fn diagonal_footprint_contact_supports_known_short_step_and_route_start() {
        let rock = Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        let position = Vec3::new(0.162109375, 0.68359375, 1.0);
        let radius = 0.449999988;
        let height = 2.333333254;
        let mut terrain = |p| Some(if p == Vec3::new(-1, 1, 0) { rock } else { Block::empty() });
        assert_eq!(check_cells(position, -Vec2::unit_y(), radius, height, terrain), Ok(()));
        let geometry = geometry_cells(position, -Vec2::unit_y(), radius, height, terrain);
        assert_eq!(geometry["self_column"]["support"], "known_unsupported");
        assert_eq!(geometry["directions"][0]["local_step_status"], "sampled_step_clear_supported");
        assert!(local_route::Route::new(
            position, Vec3::new(position.x, 0.6, position.z), 1, 1,
            radius, 0.0, height, &mut terrain,
        ).is_ok(), "a route may start on actual overlapping support");
        assert_eq!(check_cells(position, -Vec2::unit_y(), radius, height,
            |_| Some(Block::empty())), Err("control_motion_unsupported"));
        assert!(local_route::Route::new(
            position, Vec3::new(position.x, 0.6, position.z), 1, 1,
            radius, 0.0, height, &mut |_| Some(Block::empty()),
        ).is_err());
        assert_eq!(check_cells(position, -Vec2::unit_y(), radius, height, |_| None),
            Err("control_motion_terrain_unavailable"));
        assert_eq!(check_cells(position, -Vec2::unit_y(), radius, height, |p|
            Some(if p == Vec3::new(-2, 1, 0) { rock } else { Block::empty() })),
            Err("control_motion_unsupported"));
        let lava = Block::new(BlockKind::Lava, vek::Rgb::new(1, 1, 1));
        assert_eq!(check_cells(position, -Vec2::unit_y(), radius, height, |p|
            Some(if p == Vec3::new(-1, 1, 0) { lava } else { Block::empty() })),
            Err("control_motion_hazard"));
        assert_eq!(check_cells(position, -Vec2::unit_y(), radius, height, |p| Some(
            if p == Vec3::new(-1, 1, 0) || p == Vec3::new(0, 0, 1) { rock } else { Block::empty() }
        )), Err("control_motion_obstructed"));
    }

    #[test]
    fn swept_footprint_sample_domain_is_exactly_advertised() {
        let rock = Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        let lava = Block::new(BlockKind::Lava, vek::Rgb::new(1, 1, 1));
        let geometry = geometry_cells(Vec3::new(0.5, 0.5, 0.0), Vec2::unit_x(), 1.4, 1.8,
            |p| if p.x < 0 { None }
            else if p == Vec3::new(1, 1, 0) { Some(lava) }
            else { Some(if p.z < 0 { rock } else { Block::empty() }) });
        println!("NATIVE_GEOMETRY_COVERAGE_JSON={}", observation_record(json!({
            "session_id": "offline-geometry", "tick": 1, "actor": null,
            "frame": "veloren_world_xyz_meters"
        }), Some(geometry.clone())));
        let mut listed = std::collections::BTreeSet::new();
        for direction in geometry["directions"].as_array().unwrap() {
            let bounds = direction["z_inclusive"].as_array().unwrap();
            for column in direction["sample_columns_xy"].as_array().unwrap() {
                for z in bounds[0].as_i64().unwrap()..=bounds[1].as_i64().unwrap() {
                    listed.insert((column[0].as_i64().unwrap(), column[1].as_i64().unwrap(), z));
                }
            }
        }
        let own = &geometry["self_column"];
        for z in own["z_inclusive"][0].as_i64().unwrap()..=own["z_inclusive"][1].as_i64().unwrap() {
            listed.insert((own["xy"][0].as_i64().unwrap(), own["xy"][1].as_i64().unwrap(), z));
        }
        let cells = geometry["cells"].as_array().unwrap();
        let sampled: std::collections::BTreeSet<_> = cells.iter().map(|row| {
            let p = &row["position"];
            (p[0].as_i64().unwrap(), p[1].as_i64().unwrap(), p[2].as_i64().unwrap())
        }).collect();
        assert!(sampled.contains(&(1, 1, 0)));
        assert_eq!(sampled, listed, "swept footprint samples must have a declared domain");
        assert_eq!(geometry["coverage"]["expected_cells"].as_u64().unwrap() as usize, sampled.len());
        assert_eq!(geometry["coverage"]["known_cells"].as_u64().unwrap()
            + geometry["coverage"]["unknown_cells"].as_u64().unwrap(), sampled.len() as u64);
        assert!(cells.iter().any(|row| row["position"] == json!([1, 1, 0])
            && row["hazard"] == true));
        assert!(geometry["coverage"]["unknown_cells"].as_u64().unwrap() > 0);
    }

    #[test]
    fn automatic_geometry_lists_complete_sample_domain_without_treating_sparse_as_air() {
        let rock = Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        let geometry = geometry_cells(Vec3::new(0.5, 0.5, 0.0), Vec2::unit_x(), 0.35, 1.8,
            |p| if p.x < 0 { None } else { Some(if p.z < 0 { rock } else { Block::empty() }) });
        let coverage = &geometry["coverage"];
        assert_eq!(coverage["expected_cells"].as_u64().unwrap(),
            coverage["known_cells"].as_u64().unwrap() + coverage["unknown_cells"].as_u64().unwrap());
        assert!(coverage["unknown_cells"].as_u64().unwrap() > 0);
        assert_eq!(geometry["directions"][0]["local_step_status"], "sampled_step_clear_supported");
        assert_eq!(geometry["directions"][3]["local_step_status"], "sampled_step_clear_supported");
        assert_eq!(check_cells(Vec3::new(0.3, 0.5, 0.0), -Vec2::unit_x(), 0.35, 1.8,
            |p| if p.x < 0 { None } else { Some(if p.z < 0 { rock } else { Block::empty() }) }),
            Err("control_motion_terrain_unavailable"));
        for row in geometry["cells"].as_array().unwrap() {
            if row["position"][0].as_i64().unwrap() < 0 {
                assert_eq!(row["occupancy"], "unknown");
                assert!(row["solid_height_m"].is_null());
            }
        }
        let wall = geometry_cells(Vec3::new(0.5, 0.5, 0.0), Vec2::unit_x(), 0.35, 1.8, |_| Some(rock));
        assert_eq!(wall["directions"][0]["local_step_status"], "control_motion_obstructed");
        assert_eq!(wall["coverage"]["unknown_cells"], 0);
    }

    #[test]
    fn wall_sliding_is_deviation_not_forward_progress() {
        let mut feedback = MotionFeedback::new(Vec3::zero(), Vec2::unit_x(), 0);
        assert_eq!(feedback.sample(Vec3::new(0.0, 1.0, 0.0), Vec2::unit_x(), 20),
                   Err("control_motion_deviation"));
    }

    #[test]
    fn diagonal_wall_sliding_cannot_reset_away_accumulated_deviation() {
        let direction = Vec2::new(1.0, 1.0).normalized();
        let mut feedback = MotionFeedback::new(Vec3::zero(), direction, 0);
        let mut interrupted = false;
        for tick in 1..=10 {
            if feedback.sample(Vec3::new(0.0, tick as f32 * 0.2, 0.0), direction, tick).is_err() {
                interrupted = true;
                break;
            }
        }
        assert!(interrupted, "partial forward projection must not erase sustained lateral deviation");
    }

    #[test]
    fn selected_detour_and_backstep_are_progress_even_away_from_final_goal() {
        for direction in [Vec2::unit_y(), -Vec2::unit_x()] {
            let mut feedback = MotionFeedback::new(Vec3::zero(), direction, 0);
            assert!(feedback.sample(Vec3::new(direction.x, direction.y, 0.0), direction, 20).is_ok());
            assert_eq!(feedback.status, "progress");
        }
    }

    #[test]
    fn collision_stall_and_unknown_terrain_are_not_free_motion() {
        let mut feedback = MotionFeedback::new(Vec3::zero(), Vec2::unit_x(), 0);
        assert_eq!(feedback.sample(Vec3::zero(), Vec2::unit_x(), TPS),
                   Err("control_motion_nonprogress"));
        assert_eq!(check_cells(Vec3::zero(), Vec2::unit_x(), 0.35, 1.8, |_| None),
                   Err("control_motion_terrain_unavailable"));
        let solid = Block::new(common::terrain::BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        assert_eq!(check_cells(Vec3::zero(), Vec2::unit_x(), 0.35, 1.8, |_| Some(solid)),
                   Err("control_motion_obstructed"));
    }

    #[test]
    fn physics_clear_west_step_is_not_rejected_by_side_column_footprint() {
        let rock = Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        let actor = Vec3::new(0.54, 0.45, 1.0);
        let cells = |p: Vec3<i32>| Some(if p.z < 1 || (p.x == -1 || p.x == 1) && p.z >= 1 {
            rock
        } else {
            Block::empty()
        });
        assert_eq!(check_cells(actor, -Vec2::unit_x(), 0.45, 1.8, cells), Ok(()));
        assert_eq!(check_cells(actor, Vec2::unit_x(), 0.45, 1.8, cells),
                   Err("control_motion_obstructed"));
    }

    #[test]
    fn recorded_pocket_uses_physics_capsule_radius_for_local_admission() {
        // E199 Jev frame 9617: every recorded local cell, in native z-up coordinates.
        let rows: [[i32; 5]; 22] = [
            [-1, -6, -1, 6, 0], [-1, 0, -1, 2, 1], [-1, 2, -1, 2, 0],
            [-1, -6, 0, 6, 0], [-1, 0, 0, 3, 1], [-1, 3, 0, 1, 0],
            [-1, -6, 1, 6, 0], [-1, 0, 1, 3, 1], [-1, 3, 1, 1, 0],
            [0, -6, -1, 8, 0], [0, 2, -1, 1, 1], [0, 3, -1, 1, 0],
            [0, -6, 0, 6, 0], [0, 0, 0, 3, 1], [0, 3, 0, 1, 0],
            [0, 4, 0, 1, 1], [0, -6, 1, 6, 0], [0, 0, 1, 3, 1],
            [0, 3, 1, 1, 0], [1, -6, -1, 10, 0],
            [1, -6, 0, 10, 0], [1, -6, 1, 10, 0],
        ];
        assert_eq!(rows.iter().map(|row| row[3]).sum::<i32>(), 91);
        let radius = 0.45;
        let center = Vec2::new(0.68627447 - radius, 0.0);
        let collider = comp::Collider::CapsulePrism(comp::CapsulePrism {
            p0: -center, p1: center, radius, z_min: 0.0, z_max: 2.3333333,
        });
        assert!((collider.bounding_radius() - 0.68627447).abs() < 0.00001);
        let (admitted_radius, z_min, z_max) = actor_terrain_shape(&collider, 1.0).unwrap();
        assert_eq!(admitted_radius, collider.bounding_radius().min(0.45));
        let position = Vec3::new(18745.55078125, 10729.4501953125, 380.0);
        let rock = Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        let cell = |p: Vec3<i32>| rows.iter().find(|r| {
            p.x == 18745 + r[0] && p.y == 10729 + r[2]
                && p.z >= 380 + r[1] && p.z < 380 + r[1] + r[3]
        }).map(|r| if r[4] == 0 { rock } else { Block::empty() });
        assert_eq!(check_segment_cells(position,
            position - Vec3::unit_x() * 0.05, admitted_radius, z_min, z_max, cell), Ok(()));
        assert_eq!(check_segment_cells(position,
            position + Vec3::unit_x() * 0.05, admitted_radius, z_min, z_max, cell),
            Err("control_motion_obstructed"));
        for direction in [Vec3::unit_x(), -Vec3::unit_x(),
                          Vec3::unit_y(), -Vec3::unit_y()] {
            assert_eq!(check_segment_cells(position, position + direction * 0.05,
                collider.bounding_radius(), z_min, z_max, cell),
                Err("control_motion_obstructed"));
        }
    }

    #[test]
    fn loaded_supported_corridor_moves_but_wall_and_ledge_do_not() {
        let rock = Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        let air = Block::empty();
        for direction in [Vec2::unit_x(), -Vec2::unit_x(), Vec2::unit_y()] {
            assert!(check_cells(Vec3::zero(), direction, 0.35, 1.8,
                |p| Some(if p.z < 0 { rock } else { air })).is_ok());
        }
        assert_eq!(check_cells(Vec3::zero(), Vec2::unit_x(), 0.35, 1.8,
            |p| Some(if p.z < 0 || p.x >= 0 && p.z == 0 { rock } else { air })),
            Err("control_motion_obstructed"));
        assert_eq!(check_cells(Vec3::zero(), Vec2::unit_x(), 0.35, 1.8, |_| Some(air)),
            Err("control_motion_unsupported"));
        let water = Block::new(BlockKind::Water, vek::Rgb::new(1, 1, 1));
        assert!(check_cells(Vec3::zero(), Vec2::unit_x(), 0.35, 1.8, |_| Some(water)).is_ok());
    }
}
