//! Loaded, walk-only local routes to caller-selected goals; no exploration policy.
use super::*;
use common::astar::{Astar, PathResult};
use std::collections::hash_map::RandomState;

const LOCAL_RADIUS: i32 = 20;
const LOCAL_LEG: f32 = 12.0;

#[derive(Clone, Debug)]
pub(super) struct Route {
    pub id: u64,
    pub goal: Vec3<f32>,
    pub revision: u64,
    pub nodes: Vec<Vec3<f32>>,
    pub segment: usize,
    pub last_progress: Vec3<f32>,
    pub progress_tick: u64,
    pub breadcrumbs: Vec<Vec3<f32>>,
    pub history_truncated: bool,
    pub blocked: Option<Value>,
    pub coverage: &'static str,
}

fn check_edge(
    from: Vec3<f32>, to: Vec3<f32>, radius: f32, z_min: f32, z_max: f32,
    cell: &mut impl FnMut(Vec3<i32>) -> Option<Block>,
) -> Result<(), &'static str> {
    if (to.z - from.z).abs() > 1.0 {
        return Err("control_route_step_requires_action");
    }
    // The game's capsule-prism collision can hop a block with clear overhead,
    // and ground-snap can lower the actor. Validate both standing envelopes;
    // movement and the actual height change are still measured, never assumed.
    let travel_z = from.z.max(to.z);
    let raised_from = Vec3::new(from.x, from.y, travel_z);
    let raised_to = Vec3::new(to.x, to.y, travel_z);
    local_motion::check_segment_cells(raised_from, raised_to, radius, z_min, z_max, &mut *cell)?;
    if from.z > to.z + 0.01 {
        local_motion::check_segment_cells(
            Vec3::new(to.x, to.y, to.z), Vec3::new(to.x, to.y, to.z),
            radius, z_min, z_max, cell)?;
    }
    Ok(())
}

fn support_nodes(
    xy: Vec2<i32>, near_z: f32, cell: &mut impl FnMut(Vec3<i32>) -> Option<Block>,
) -> (Vec<(Vec3<i32>, f32)>, bool) {
    let mut nodes = Vec::new();
    let mut unknown = false;
    for z in (near_z.floor() as i32 - Block::MAX_HEIGHT.ceil() as i32)
        ..=(near_z.ceil() as i32 + 2)
    {
        let voxel = Vec3::new(xy.x, xy.y, z);
        let Some(block) = cell(voxel) else { unknown = true; continue };
        if !block.is_solid() || block.is_liquid()
            || block.kind() == BlockKind::Lava
            || block.get_sprite().is_some_and(terrain_sprite_has_on_ground_debuff)
            || block.solid_height() > Block::MAX_HEIGHT
        {
            continue;
        }
        let top = z as f32 + block.solid_height();
        if top >= near_z - 1.5 && top <= near_z + 1.5 {
            nodes.push((voxel, top));
        }
    }
    nodes.sort_by(|a, b| a.1.total_cmp(&b.1));
    (nodes, unknown)
}

fn plan(
    start: Vec3<f32>, goal: Vec3<f32>, radius: f32, z_min: f32, z_max: f32,
    cell: &mut impl FnMut(Vec3<i32>) -> Option<Block>,
) -> Result<(Vec<Vec3<f32>>, &'static str), (&'static str, Option<Value>)> {
    let start_cell = start.xy().map(|v| v.floor() as i32);
    let goal_cell = goal.xy().map(|v| v.floor() as i32);
    let delta = goal.xy() - start.xy();
    let coverage = if delta.magnitude() > LOCAL_LEG { "local_leg" } else { "goal" };
    let (mut starts, mut start_unknown) = support_nodes(start_cell, start.z, cell);
    if !starts.iter().any(|(_, top)| (start.z - top).abs() <= 0.2) {
        for column in local_motion::footprint_columns(start.xy(), radius) {
            if column == start_cell { continue; }
            let (candidates, missing) = support_nodes(column, start.z, cell);
            start_unknown |= missing;
            starts.extend(candidates);
        }
    }
    let start_node = starts.into_iter().find(|(_, top)| (start.z - top).abs() <= 0.2)
        .map(|(node, _)| node).ok_or((
            if start_unknown { "control_route_unknown" } else { "control_motion_unsupported" },
            Some(json!({"from": [start.x,start.y,start.z], "cause": "start_support_unverified"}))))?;
    local_motion::check_segment_cells(start, start, radius, z_min, z_max, &mut *cell)
        .map_err(|reason| (reason, Some(json!({"from": [start.x,start.y,start.z],
                                               "cause": "start_footprint_unverified"}))))?;
    if coverage == "goal" && start_cell == goal_cell
        && (start.z - goal.z).abs() <= 1.0
    {
        check_edge(start, Vec3::new(goal.x, goal.y, start.z), radius, z_min, z_max, cell)
            .map_err(|code| (code, Some(json!({"from": [start.x,start.y,start.z],
                                               "to": [goal.x,goal.y,start.z]}))))?;
        return Ok((vec![Vec3::new(goal.x, goal.y, start.z)], coverage));
    }
    let unknown = std::cell::Cell::new(false);
    let mut first_blocked = None;
    let mut goal_blocked = None;
    let mut requires_action = false;
    let mut vertical_unreachable = false;
    let mut astar = Astar::new(2048, start_node, RandomState::new());
    let sampled = std::cell::RefCell::new(cell);
    let result = astar.poll(2048,
        |node| (node.x as f32 + 0.5 - goal.x).abs()
            + (node.y as f32 + 0.5 - goal.y).abs(),
        |node| {
            let node = *node;
            [Vec2::unit_x(), Vec2::unit_y(), -Vec2::unit_x(), -Vec2::unit_y()]
                .into_iter().flat_map(|direction| {
                    let column = node.xy() + direction;
                    if (column.x - start_cell.x).abs() > LOCAL_RADIUS
                        || (column.y - start_cell.y).abs() > LOCAL_RADIUS
                    {
                        return Vec::new();
                    }
                    let from = if node == start_node { start } else {
                        let Some(block) = (*sampled.borrow_mut())(node) else {
                            unknown.set(true);
                            return Vec::new();
                        };
                        Vec3::new(node.x as f32 + 0.5, node.y as f32 + 0.5,
                                  node.z as f32 + block.solid_height())
                    };
                    let (candidates, missing) = support_nodes(
                        column, from.z, &mut **sampled.borrow_mut());
                    unknown.set(unknown.get() || missing);
                    candidates.into_iter().filter_map(|(next, height)| {
                        let to = Vec3::new(column.x as f32 + 0.5, column.y as f32 + 0.5, height);
                        match check_edge(from, to, radius, z_min, z_max,
                                         &mut **sampled.borrow_mut()) {
                            Ok(()) => Some((next, 1.0 + (height - from.z).abs())),
                            Err(code) => {
                                if code == "control_motion_terrain_unavailable" {
                                    unknown.set(true);
                                }
                                requires_action |= code == "control_route_step_requires_action";
                                if first_blocked.is_none() || code == "control_motion_terrain_unavailable"
                                    || code == "control_route_step_requires_action"
                                        && first_blocked.as_ref().is_none_or(|edge: &Value|
                                            edge["cause"] != "control_motion_terrain_unavailable")
                                {
                                    first_blocked = Some(json!({"from": [from.x,from.y,from.z],
                                        "to": [to.x,to.y,to.z], "cause": code}));
                                }
                                None
                            }
                        }
                    }).collect::<Vec<_>>()
                }).collect::<Vec<_>>().into_iter()
        },
        |node| {
            let xy = Vec2::new(node.x as f32 + 0.5, node.y as f32 + 0.5);
            if coverage == "local_leg" {
                (xy - start.xy()).magnitude() >= LOCAL_LEG
                    && (goal.xy() - xy).magnitude() < delta.magnitude() - LOCAL_LEG * 0.5
            } else if node.xy() == goal_cell {
                let Some(top) = (*sampled.borrow_mut())(*node)
                    .map(|block| node.z as f32 + block.solid_height())
                else {
                    unknown.set(true);
                    return false;
                };
                if (goal.z - top).abs() > 1.0 {
                    vertical_unreachable = true;
                    return false;
                }
                let from = if *node == start_node { start } else {
                    Vec3::new(xy.x, xy.y, top)
                };
                let to = Vec3::new(goal.x, goal.y, from.z);
                match check_edge(from, to, radius, z_min, z_max, &mut **sampled.borrow_mut()) {
                    Ok(()) => true,
                    Err(code) => {
                        if code == "control_motion_terrain_unavailable" {
                            unknown.set(true);
                        }
                        goal_blocked = Some(json!({"from": [from.x,from.y,from.z],
                            "to": [to.x,to.y,to.z], "cause": code}));
                        false
                    }
                }
            } else { false }
        });
    let path = match result {
        PathResult::Path(path, _) => path,
        PathResult::None(_) => return Err((
            if unknown.get() { "control_route_unknown" } else if requires_action {
                "control_route_step_requires_action"
            } else if vertical_unreachable { "control_route_vertical_unsupported" }
            else { "control_route_no_path" },
            goal_blocked.or(first_blocked))),
        PathResult::Exhausted(_) | PathResult::Pending =>
            return Err(("control_route_coverage_limit", goal_blocked.or(first_blocked))),
    };
    let mut nodes = Vec::new();
    for p in path.nodes().iter().skip(1) {
        let block = (*sampled.borrow_mut())(*p).ok_or(("control_route_unknown", None))?;
        nodes.push(Vec3::new(p.x as f32 + 0.5, p.y as f32 + 0.5,
                             p.z as f32 + block.solid_height()));
    }
    if coverage == "goal" {
        let last = nodes.last().copied().unwrap_or(start);
        check_edge(last, Vec3::new(goal.x, goal.y, last.z),
            radius, z_min, z_max, &mut **sampled.borrow_mut())
            .map_err(|code| (code, Some(json!({"from": [last.x,last.y,last.z],
                                                "to": [goal.x,goal.y,last.z]}))))?;
        nodes.push(Vec3::new(goal.x, goal.y, last.z));
    }
    Ok((nodes, coverage))
}

impl Route {
    pub(super) fn new(
        start: Vec3<f32>, goal: Vec3<f32>, revision: u64, tick: u64,
        radius: f32, z_min: f32, z_max: f32,
        cell: &mut impl FnMut(Vec3<i32>) -> Option<Block>,
    ) -> Result<Self, (&'static str, Option<Value>)> {
        let (nodes, coverage) = plan(start, goal, radius, z_min, z_max, cell)?;
        Ok(Self {
            id: 0, goal, revision, nodes, segment: 0, last_progress: start, progress_tick: tick,
            breadcrumbs: vec![start], blocked: None, coverage,
            history_truncated: false,
        })
    }

    pub(super) fn step(
        &mut self, position: Vec3<f32>, tick: u64, radius: f32, z_min: f32, z_max: f32,
        cell: &mut impl FnMut(Vec3<i32>) -> Option<Block>,
    ) -> Result<Vec2<f32>, &'static str> {
        if self.coverage == "goal" && (position.xy() - self.goal.xy()).magnitude() <= 0.5
            && (position.z - self.goal.z).abs() <= 1.0
        {
            let endpoint = Vec3::new(self.goal.x, self.goal.y, position.z);
            if let Err(reason) = check_edge(position, endpoint, radius, z_min, z_max, cell) {
                self.blocked = Some(json!({"from": [position.x,position.y,position.z],
                    "to": [endpoint.x,endpoint.y,endpoint.z], "cause": reason}));
                return Err(reason);
            }
            return Err("control_motion_arrived");
        }
        if self.segment == self.nodes.len() {
            let revision = self.revision.checked_add(1).ok_or("control_route_revision_exhausted")?;
            let mut next = Self::new(position, self.goal, revision, tick, radius, z_min, z_max, cell)
                .map_err(|(reason, edge)| {
                    self.blocked = edge;
                    reason
                })?;
            next.breadcrumbs = self.breadcrumbs.clone();
            next.id = self.id;
            if next.breadcrumbs.last().is_none_or(|last| last.distance(position) > 0.05) {
                next.breadcrumbs.push(position);
            }
            next.history_truncated = self.history_truncated;
            if next.breadcrumbs.len() > 64 {
                next.breadcrumbs.remove(0);
                next.history_truncated = true;
            }
            *self = next;
        }
        let target = self.nodes[self.segment];
        let delta = target.xy() - position.xy();
        let origin = self.breadcrumbs.last().copied().unwrap_or(self.last_progress);
        let axis = target.xy() - origin.xy();
        let lateral = if axis.magnitude_squared() > 0.01 {
            ((position.xy() - origin.xy()).x * axis.y
                - (position.xy() - origin.xy()).y * axis.x).abs() / axis.magnitude()
        } else { 0.0 };
        if lateral > 0.7 {
            self.blocked = Some(json!({"from": [position.x,position.y,position.z],
                "to": [target.x,target.y,target.z], "cause": "control_route_deviation"}));
            return Err("control_route_deviation");
        }
        if delta.magnitude() <= 0.3 && (position.z - target.z).abs() <= 0.25 {
            self.segment += 1;
            if self.breadcrumbs.last().is_none_or(|last| last.distance(position) > 0.05) {
                self.breadcrumbs.push(position);
            }
            if self.breadcrumbs.len() > 64 {
                self.breadcrumbs.remove(0);
                self.history_truncated = true;
            }
            self.last_progress = position;
            self.progress_tick = tick;
            return self.step(position, tick, radius, z_min, z_max, cell);
        }
        if delta.magnitude() <= 0.3 && tick.saturating_sub(self.progress_tick) >= TPS {
            self.blocked = Some(json!({"from": [position.x,position.y,position.z],
                "to": [target.x,target.y,target.z], "cause": "control_route_step_requires_action"}));
            return Err("control_route_step_requires_action");
        }
        let direction = delta.try_normalized().unwrap_or_else(Vec2::zero);
        let next = position + Vec3::new(direction.x * delta.magnitude().min(0.2),
                                        direction.y * delta.magnitude().min(0.2),
                                        target.z - position.z);
        if let Err(reason) = check_edge(position, next, radius, z_min, z_max, cell) {
            self.blocked = Some(json!({"from": [position.x,position.y,position.z],
                "to": [next.x,next.y,next.z], "cause": reason}));
            return Err(reason);
        }
        let travel_axis = axis.try_normalized().unwrap_or_else(Vec2::zero);
        if (position.xy() - self.last_progress.xy()).dot(travel_axis) >= 0.25 {
            self.last_progress = position;
            self.progress_tick = tick;
        } else if tick.saturating_sub(self.progress_tick) >= TPS {
            self.blocked = Some(json!({"from": [position.x,position.y,position.z],
                "to": [target.x,target.y,target.z], "cause": "control_motion_nonprogress"}));
            return Err("control_motion_nonprogress");
        }
        Ok(delta / delta.magnitude().max(1.0))
    }

    pub(super) fn observation(&self) -> Value {
        json!({"goal": [self.goal.x,self.goal.y,self.goal.z],
            "revision": self.revision, "segment": self.segment, "segments": self.nodes.len(),
            "waypoint": self.nodes.get(self.segment).map(|p| [p.x,p.y,p.z]),
            "last_verified_progress": [self.last_progress.x,self.last_progress.y,self.last_progress.z],
            "since_tick": self.progress_tick, "coverage": self.coverage, "blocked_edge": self.blocked,
            "breadcrumbs": self.breadcrumbs.iter().rev().take(8)
                .map(|p| [p.x,p.y,p.z]).collect::<Vec<_>>(),
            "earliest_retained_breadcrumb": self.breadcrumbs.first().map(|p| [p.x,p.y,p.z]),
            "breadcrumb_count": self.breadcrumbs.len(),
            "history_truncated": self.history_truncated,
            "reverse_status": "must_revalidate_each_segment; no_drop_or_dynamic_safety_claim",
            "goal_success": null})
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn terrain(p: Vec3<i32>) -> Option<Block> {
        let rock = Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        Some(if p.z < 1 || p.x == 2 && p.y >= -1 && p.y < 2 && p.z < 3 {
            rock
        } else { Block::empty() })
    }

    #[test]
    fn route_around_wall_and_validated_progress_not_lateral_sliding() {
        let start = Vec3::new(0.5, 0.5, 1.0);
        let goal = Vec3::new(4.5, 0.5, 1.0);
        let route = Route::new(start, goal, 1, 0, 0.4, 0.0, 1.8, &mut terrain).unwrap();
        assert!(route.nodes.iter().any(|p| p.y.abs() > 1.0), "{:?}", route.nodes);
        let mut route = route;
        let first = route.step(start, 1, 0.4, 0.0, 1.8, &mut terrain).unwrap();
        assert!(first.magnitude_squared() > 0.0);
        assert_eq!(route.segment, 0);
        assert_eq!(route.last_progress, start);
    }

    #[test]
    fn no_path_unknown_and_unsupported_are_distinct() {
        let start = Vec3::new(0.5, 0.5, 1.0);
        let goal = Vec3::new(3.5, 0.5, 1.0);
        let unknown = Route::new(start, goal, 1, 0, 0.4, 0.0, 1.8, &mut |_| None);
        assert_eq!(unknown.unwrap_err().0, "control_route_unknown");
        let unsupported = Route::new(start, goal, 1, 0, 0.4, 0.0, 1.8,
            &mut |p| Some(if p.z < 1 && p.x == 0 { Block::new(BlockKind::Rock, vek::Rgb::new(1,1,1)) }
                          else { Block::empty() }));
        assert!(unsupported.is_err());
    }

    #[test]
    fn route_uses_room_opening_not_wall_proximity_or_visual_door_guess() {
        let rock = Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        let start = Vec3::new(2.5, 1.5, 1.0);
        let goal = Vec3::new(6.5, 1.5, 1.0);
        let mut room = |p: Vec3<i32>| Some(if p.z < 1
            || p.x == 4 && p.y != 2 && p.z < 3 { rock } else { Block::empty() });
        let route = Route::new(start, goal, 1, 0, 0.4, 0.0, 1.8, &mut room).unwrap();
        assert!(route.nodes.contains(&Vec3::new(4.5, 2.5, 1.0)), "{:?}", route.nodes);
        assert_eq!(route.coverage, "goal");
        assert_eq!(route.observation()["goal_success"], Value::Null);
    }

    #[test]
    fn enclosure_reports_no_path_and_checked_reverse_is_not_assumed_safe() {
        let rock = Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        let start = Vec3::new(0.5, 0.5, 1.0);
        let goal = Vec3::new(2.5, 0.5, 1.0);
        let enclosed = Route::new(start, goal, 1, 0, 0.4, 0.0, 1.8,
            &mut |p| Some(if p.z < 1 || p.z < 3
                && (p.x == -1 || p.x == 1 || p.y == -1 || p.y == 1) {
                    rock
                } else { Block::empty() }));
        assert_eq!(enclosed.unwrap_err().0, "control_route_no_path");
        let walk = Route::new(start, goal, 1, 0, 0.4, 0.0, 1.8,
            &mut |p| Some(if p.z < 1 { rock } else { Block::empty() })).unwrap();
        assert_eq!(walk.observation()["reverse_status"],
                   "must_revalidate_each_segment; no_drop_or_dynamic_safety_claim");
        let clear_reverse = local_motion::check_segment_cells(
            Vec3::new(1.5, 0.5, 1.0), start, 0.4, 0.0, 1.8,
            |p| Some(if p.z < 1 { rock } else { Block::empty() }));
        assert!(clear_reverse.is_ok());
        let changed_reverse = local_motion::check_segment_cells(
            Vec3::new(1.5, 0.5, 1.0), start, 0.4, 0.0, 1.8,
            |p| Some(if p.z < 1 || p.x == 0 && p.z == 1 { rock } else { Block::empty() }));
        assert_eq!(changed_reverse, Err("control_motion_obstructed"));
    }

    #[test]
    fn lateral_sliding_does_not_relabel_itself_route_progress_or_arrival() {
        let rock = Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        let start = Vec3::new(0.5, 0.5, 1.0);
        let mut clear = |p: Vec3<i32>| Some(if p.z < 1 { rock } else { Block::empty() });
        let mut route = Route::new(start, Vec3::new(3.5, 0.5, 1.0),
            1, 0, 0.4, 0.0, 1.8, &mut clear).unwrap();
        assert!(route.step(Vec3::new(0.5, 0.9, 1.0), 30, 0.4, 0.0, 1.8, &mut clear).is_ok());
        assert_eq!(route.last_progress, start);
        assert_eq!(route.step(Vec3::new(0.5, 1.15, 1.0), TPS, 0.4, 0.0, 1.8,
                              &mut clear), Err("control_motion_nonprogress"));
        assert_eq!(route.segment, 0);
    }

    #[test]
    fn route_world_heading_stays_fixed_during_a_look_change() {
        let rock = Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        let start = Vec3::new(0.5, 0.5, 1.0);
        let mut clear = |p: Vec3<i32>| Some(if p.z < 1 { rock } else { Block::empty() });
        let route = Route::new(start, Vec3::new(3.5, 0.5, 1.0),
            1, 0, 0.4, 0.0, 1.8, &mut clear).unwrap();
        let mut changed_look = route.clone();
        let initial = route.clone().step(start, 1, 0.4, 0.0, 1.8, &mut clear).unwrap();
        let mut state = BridgeState::new("test", Dir::default());
        state.look_dir = Dir::from_unnormalized(Vec3::unit_y()).unwrap();
        assert_eq!(changed_look.step(start, 1, 0.4, 0.0, 1.8, &mut clear), Ok(initial));
        assert_eq!(initial, Vec2::unit_x());
    }

    #[test]
    fn same_voxel_goal_with_vertical_arrival_tolerance_still_checks_horizontal_walk() {
        let rock = Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        let start = Vec3::new(0.5, 0.5, 1.0);
        let goal = Vec3::new(0.6, 0.6, 1.2);
        let mut cell = |p: Vec3<i32>| Some(if p.z < 1 { rock } else { Block::empty() });
        let mut route = Route::new(start, goal, 1, 0, 0.4, 0.0, 1.8, &mut cell).unwrap();
        assert_eq!(route.nodes[0].z, 1.0);
        assert_eq!(route.step(start, 1, 0.4, 0.0, 1.8, &mut cell),
                   Err("control_motion_arrived"));
    }

    #[test]
    fn same_voxel_goal_outside_arrival_radius_has_nonempty_checked_route() {
        let rock = Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        let start = Vec3::new(0.05, 0.05, 1.0);
        let goal = Vec3::new(0.95, 0.95, 1.0);
        let mut cell = |p: Vec3<i32>| Some(if p.z < 1 { rock } else { Block::empty() });
        let mut route = Route::new(start, goal, 1, 0, 0.4, 0.0, 1.8, &mut cell).unwrap();
        assert_eq!(route.nodes.last(), Some(&goal));
        assert!(route.step(start, 1, 0.4, 0.0, 1.8, &mut cell).unwrap().magnitude() > 0.0);
    }

    #[test]
    fn near_corner_goal_on_clear_support_uses_checked_exact_connector() {
        let rock = Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        let start = Vec3::new(0.5, 0.5, 1.0);
        let goal = Vec3::new(3.05, 0.05, 1.0);
        let mut clear = |p: Vec3<i32>| Some(if p.z < 1 { rock } else { Block::empty() });
        let mut route = Route::new(start, goal, 1, 0, 0.4, 0.0, 1.8, &mut clear).unwrap();
        assert_eq!(route.coverage, "goal");
        assert_eq!(route.nodes.last(), Some(&goal));
        assert!(route.step(start, 1, 0.4, 0.0, 1.8, &mut clear).is_ok());
        assert_eq!(route.step(goal, 2, 0.4, 0.0, 1.8, &mut clear),
                   Err("control_motion_arrived"));
    }

    #[test]
    fn near_corner_exact_endpoint_obstacle_is_not_a_valid_route() {
        let rock = Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        let start = Vec3::new(0.5, 0.5, 1.0);
        let goal = Vec3::new(3.05, 0.05, 1.0);
        let mut blocked = |p: Vec3<i32>| Some(if p.z < 1 || p.x == 3 && p.y == -1 && p.z == 1 {
            rock
        } else { Block::empty() });
        let error = Route::new(start, goal, 1, 0, 0.4, 0.0, 1.8, &mut blocked).unwrap_err();
        assert_eq!(error.0, "control_route_no_path");
        let edge = error.1.unwrap();
        assert_eq!(edge["to"], json!([goal.x, goal.y, goal.z]));
        assert_eq!(edge["cause"], "control_motion_obstructed");
    }

    #[test]
    fn newly_blocked_exact_endpoint_does_not_turn_proximity_into_arrival() {
        let rock = Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        let start = Vec3::new(0.5, 0.5, 1.0);
        let goal = Vec3::new(3.05, 0.05, 1.0);
        let mut clear = |p: Vec3<i32>| Some(if p.z < 1 { rock } else { Block::empty() });
        let mut route = Route::new(start, goal, 1, 0, 0.4, 0.0, 1.8, &mut clear).unwrap();
        let mut blocked = |p: Vec3<i32>| Some(if p.z < 1 || p.x == 3 && p.y == -1 && p.z == 1 {
            rock
        } else { Block::empty() });
        assert_eq!(route.step(Vec3::new(3.05, 0.45, 1.0), 2, 0.4, 0.0, 1.8, &mut blocked),
                   Err("control_motion_obstructed"));
        let edge = route.blocked.unwrap();
        assert_eq!(edge["to"], json!([goal.x, goal.y, goal.z]));
    }

    #[test]
    fn near_corner_exact_endpoint_unloaded_footprint_stays_unknown() {
        let rock = Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        let start = Vec3::new(0.5, 0.5, 1.0);
        let goal = Vec3::new(3.05, 0.05, 1.0);
        let mut unknown = |p: Vec3<i32>| {
            if p.x == 3 && p.y == -1 && p.z == 1 { None }
            else { Some(if p.z < 1 { rock } else { Block::empty() }) }
        };
        let error = Route::new(start, goal, 1, 0, 0.4, 0.0, 1.8, &mut unknown).unwrap_err();
        assert_eq!(error.0, "control_route_unknown");
        let edge = error.1.unwrap();
        assert_eq!(edge["to"], json!([goal.x, goal.y, goal.z]));
        assert_eq!(edge["cause"], "control_motion_terrain_unavailable");
    }

    #[test]
    fn near_corner_goal_without_supported_height_is_not_horizontal_arrival() {
        let rock = Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        let start = Vec3::new(0.5, 0.5, 1.0);
        let goal = Vec3::new(3.05, 0.05, 3.1);
        let mut clear = |p: Vec3<i32>| Some(if p.z < 1 { rock } else { Block::empty() });
        assert_eq!(Route::new(start, goal, 1, 0, 0.4, 0.0, 1.8, &mut clear)
            .unwrap_err().0, "control_route_vertical_unsupported");
    }

    #[test]
    fn walkable_support_rise_and_cumulative_uphill_are_not_initial_altitude_failures() {
        let rock = Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        let bedroll = Block::air(SpriteKind::Bedroll);
        let start = Vec3::new(0.5, 0.5, 1.0);
        let mut gentle = |p: Vec3<i32>| Some(if p.z < 1 { rock }
            else if p.x == 1 && p.z == 1 { bedroll }
            else { Block::empty() });
        let route = Route::new(start, Vec3::new(2.5, 0.5, 1.0), 1, 0, 0.4, 0.0, 1.8,
                               &mut gentle).unwrap();
        assert!(route.nodes.iter().any(|node| (node.z - 1.3).abs() < 0.01), "{:?}", route.nodes);
        let mut corridor = |p: Vec3<i32>| Some(if p.z < 1
            || (p.y == -1 || p.y == 1) && p.z < 4
            || (1..=5).contains(&p.x) && p.z == 1
            || (3..=5).contains(&p.x) && p.z == 2 { rock } else { Block::empty() });
        let mut uphill = Route::new(start, Vec3::new(5.5, 0.5, 3.0),
                                    1, 0, 0.4, 0.0, 1.8, &mut corridor).unwrap();
        assert!(uphill.nodes.iter().any(|node| node.z >= 2.0));
        assert_eq!(uphill.nodes.last().unwrap().z, 3.0);
        assert!(uphill.step(start, 1, 0.4, 0.0, 1.8, &mut corridor).is_ok());
    }

    #[test]
    fn obstructed_projected_leg_point_has_reachable_same_goal_frontier() {
        let rock = Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        let start = Vec3::new(0.5, 0.5, 1.0);
        let mut terrain = |p: Vec3<i32>| Some(if p.z < 1
            || p.x == 12 && p.y == 0 && p.z < 3 { rock } else { Block::empty() });
        let route = Route::new(start, Vec3::new(30.5, 0.5, 1.0),
                               1, 0, 0.4, 0.0, 1.8, &mut terrain).unwrap();
        assert_eq!(route.coverage, "local_leg");
        assert!(route.nodes.last().unwrap().x > 10.0);
        assert!(route.nodes.iter().any(|node| (node.y - start.y).abs() >= 1.0), "{:?}", route.nodes);
    }

    #[test]
    fn local_leg_rollover_does_not_duplicate_measured_breadcrumb() {
        let rock = Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        let mut clear = |p: Vec3<i32>| Some(if p.z < 1 { rock } else { Block::empty() });
        let mut route = Route::new(Vec3::new(0.5, 0.5, 1.0), Vec3::new(25.5, 0.5, 1.0),
                                   1, 0, 0.4, 0.0, 1.8, &mut clear).unwrap();
        assert_eq!(route.coverage, "local_leg");
        let last = *route.nodes.last().unwrap();
        route.breadcrumbs.push(last);
        route.segment = route.nodes.len();
        route.step(last, 10, 0.4, 0.0, 1.8, &mut clear).unwrap();
        assert_eq!(route.breadcrumbs.last(), Some(&last));
        assert_ne!(route.breadcrumbs[route.breadcrumbs.len() - 2], last);
    }

    #[test]
    fn ledge_requiring_explicit_action_is_not_declared_walkable() {
        let rock = Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1));
        let fence = Block::air(SpriteKind::FenceWoodWoodland);
        let start = Vec3::new(0.5, 0.5, 1.0);
        let goal = Vec3::new(1.5, 0.5, 2.09);
        let mut terrain = |p: Vec3<i32>| Some(if p.z < 1 || p.y != 0 && p.z < 4 { rock }
            else if p.x == 1 && p.z == 1 { fence }
            else { Block::empty() });
        let error = Route::new(start, goal, 1, 0, 0.4, 0.0, 1.8, &mut terrain).unwrap_err();
        assert_eq!(error.0, "control_route_step_requires_action");
        assert_eq!(error.1.unwrap()["cause"], "control_route_step_requires_action");
    }
}
