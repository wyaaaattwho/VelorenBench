//! Shared NDJSON agent bridge protocol and action state machine.
//!
//! Used by the headless `agent_bridge` example and (later) Voxygen `--agent-bridge`.

mod pickup_feedback;
mod player_map;
mod goal_recipes;
mod local_doors;
mod continuous_control;
mod local_motion;
mod local_route;
mod combat_perception;
mod pre_hit;
mod operation_preconditions;
mod give_up;
mod native_interactions;

use crate::{Client, ClientType, Event, addr::ConnectionArgs};
use common::{
    ViewDistances,
    clock::Clock,
    consts::{MAX_MOUNT_RANGE, MAX_PICKUP_RANGE, MAX_SPRITE_MOUNT_RANGE},
    comp::{
        self, ControllerInputs, InputKind, PickupItem,
        inventory::{
            item::{Item, ItemKind},
            slot::{ArmorSlot, EquipSlot, InvSlotId, Slot},
        },
    },
    mounting::{Volume, VolumePos},
    path::{Chaser, EdgeCostMode, PathLength, PathState, TraversalConfig},
    recipe::{Recipe, RecipeBookManifest, RecipeInput},
    terrain::{Block, BlockKind, SpriteKind},
    uid::{IdMaps, Uid},
    util::{
        Dir,
        find_dist::{self, FindDist},
    },
    vol::ReadVol,
};
use serde::Deserialize;
use serde_json::{Value, json};
use specs::{Entity, Join, WorldExt};
use std::{
    collections::{BTreeMap, HashMap, VecDeque},
    env,
    io::{self, BufRead, Write},
    num::NonZeroU64,
    sync::{Arc, OnceLock, mpsc},
    thread,
    time::Duration,
};
use tokio::runtime::Runtime;
use vek::{Aabb, Rgb, Vec2, Vec3};

pub const PROTOCOL_VERSION: &str = "0.2";
pub const OBSERVATION_SCHEMA_VERSION: &str = "1.4";
const TPS: u64 = 60;
const STARTUP_TIMEOUT_TICKS: u64 = TPS * 30;
const DEFAULT_ACTION_TICKS: u64 = TPS;
const DEFAULT_TIMEOUT_TICKS: u64 = TPS * 10;
const RESPAWN_OBSERVATION_TICKS: u64 = TPS * 5;
const STALL_TICKS: u64 = TPS * 2;
const CHASER_MIN_TARGET_DISTANCE: f32 = 0.05;
const PLANNER_NONE_TICKS: u64 = STALL_TICKS;
// Pre-registered E060 bounds: arrivals used at most 555 Pending ticks; one
// pathological move used 1,042. Arrivals had a sampled 1,761-tick no-improvement span.
const MAX_CUMULATIVE_PENDING_TICKS: u64 = TPS * 12;
const MAX_NO_BEST_DISTANCE_IMPROVEMENT_TICKS: u64 = TPS * 35;
// Pre-registered deterministic recovery cadence: one grounded jump per 30
// Chaser-stuck ticks.
const STUCK_RECOVERY_JUMP_CADENCE_TICKS: u64 = TPS / 2;
// Bounds an explicitly long caller timeout while leaving a minute for a detour.
const MAX_MOVE_TO_TICKS: u64 = TPS * 60;
const MAX_EVENTS: usize = 256;
const MAX_COMPLETED_ACTIONS: usize = 256;
const MAX_CRAFT_AMOUNT: u32 = 100;
const MAX_NEARBY_BLOCKS: usize = 100;
const MAX_NEARBY_INTERACTABLE_SPRITES: usize = 24;
const MAX_KNOWN_SITES: usize = 12;
const MAX_KNOWN_MAP_CAVES: usize = 4;
const TERRAIN_QUERY_SCHEMA_VERSION: &str = "1.0";
// A terrain request is limited to exactly the runner's existing one-dispatch
// movement envelope. It samples one loaded voxel column, not a route or map.
const TERRAIN_QUERY_MAX_HORIZONTAL_METRES: f32 = 10.0;
const TERRAIN_QUERY_MAX_VERTICAL_METRES: f32 = 2.0;
const TERRAIN_QUERY_CLEARANCE_HEIGHT_METRES: f32 = 2.0;
// This is the largest value currently returned by SpriteKind::solid_height
// (TerracottaStatue). Block::MAX_HEIGHT is only 3.0 despite that existing
// 5.29-metre case, so using it here would silently omit a support level.
const TERRAIN_QUERY_MAX_SUPPORT_HEIGHT_METRES: f32 = 5.29;
// Collision broadphase only searches this far below an actor. Geometry taller
// than this remains reportable but cannot be certified as physics-usable
// support by this query.
const TERRAIN_QUERY_MAX_PHYSICS_SUPPORT_HEIGHT_METRES: f32 = Block::MAX_HEIGHT;
// A 24-recipe cap ranked by (satisfied, missing count) structurally hid every
// station-free recipe whose ingredients come from sprites this character finds,
// and the host could not reorder its way back to them because they never arrived.
// The host compacts this list again against its own prompt budget, so the bridge
// only needs a bound generous enough to carry the whole default book of 76.
const MAX_KNOWN_RECIPES: usize = 96;
const MAX_SITE_NAME_BYTES: usize = 256;
// An 8 m radius let the agent stand in a workshop without seeing it: across an
// 85-cycle live episode the character never once observed a collectible or a
// station. A 49³ scan costs nothing measurable next to the tick itself, with an
// observation round trip of 16.7 ms median at either radius over 25 samples.
const INTERACTABLE_SPRITE_SCAN_RADIUS: i32 = 24;
// A 25³ scan stays bounded while reaching stations that are just beyond interaction range.
const CRAFT_STATION_SCAN_RADIUS: i32 = 12;
const MAX_CHAT_MESSAGE_BYTES: usize = comp::ChatMsg::MAX_BYTES_PLAYER_CHAT_MSG;
const MAX_SERVER_COMMAND_NAME_BYTES: usize = 64;
const MAX_SERVER_COMMAND_ARGS: usize = 32;
const MAX_SERVER_COMMAND_ARG_BYTES: usize = 256;
const DEFAULT_AGENT_MIN_PATH_LENGTH: PathLength = PathLength::Small;
const DEFAULT_AGENT_EDGE_COST_MODE: EdgeCostMode = EdgeCostMode::LegacyTierDerived;
static AGENT_MIN_PATH_LENGTH: OnceLock<PathLength> = OnceLock::new();
static AGENT_EDGE_COST_MODE: OnceLock<EdgeCostMode> = OnceLock::new();
static OUTPUT_PROJECTION: OnceLock<fn(&Value) -> Value> = OnceLock::new();

/// A renderer host may restrict all responses and asynchronous events together.
pub fn install_output_projection(projection: fn(&Value) -> Value) -> Result<(), String> {
    OUTPUT_PROJECTION.set(projection).map_err(|_| "output projection already installed".into())
}

#[derive(Debug)]
struct Config {
    server: String,
    username: String,
    password: String,
    character: Option<String>,
    session_id: String,
}

#[derive(Debug, Deserialize)]
pub struct Request {
    #[serde(default)]
    pub id: Value,
    #[serde(default)]
    pub protocol_version: Option<String>,
    pub cmd: String,
    #[serde(default)]
    pub args: Value,
}

#[derive(Clone, Debug)]
pub enum InteractWait {
    Skill {
        skill: comp::skills::Skill,
        before_level: u16,
        before_points: u16,
        cost: u16,
    },
    Ability {
        slot: usize,
        ability: comp::ability::AuxiliaryAbility,
    },
    Pickup {
        uid: u64,
        request_sequence: u64,
        start_item_count: u32,
        feedback: Option<Value>,
    },
    Collect {
        position: Vec3<i32>,
        start_item_count: u32,
        start_collectible: bool,
    },
    UseItem {
        start_item_count: u32,
        definition_id: String,
        start_definition_count: u32,
        consumable: bool,
        entered_use: bool,
    },
    ToggleWield {
        want_wielding: bool,
    },
    Sit,
    Respawn,
    GiveUp,
    Drop {
        start_item_count: u32,
        start_free_slots: usize,
    },
    Equip {
        equip_slot: EquipSlot,
        expected_item_id: String,
    },
}

#[derive(Clone, Debug)]
pub enum ActiveKind {
    TimedMove {
        direction: Vec2<f32>,
        speed: f32,
        finish_tick: u64,
    },
    MoveTo {
        target: Vec3<f32>,
        speed: f32,
        tolerance: f32,
        best_distance: f32,
        last_progress_tick: u64,
        last_best_distance_tick: u64,
        max_no_best_distance_improvement_ticks: u64,
        reached_tick: Option<u64>,
        chaser: Box<Chaser>,
        pending_ticks: u64,
        consecutive_pending_ticks: u64,
        planner_none_ticks: u64,
        chaser_none_ticks: u64,
        stuck_ever: bool,
        stuck_ticks: u64,
        last_bearing: Option<Vec3<f32>>,
        last_path_position: Option<Vec3<f32>>,
        actual_path_length: f32,
        max_path_length_reached: PathLength,
    },
    Jump {
        earliest_finish_tick: u64,
        was_airborne: bool,
    },
    Look {
        target: Dir,
    },
    Attack {
        input: InputKind,
        finish_tick: u64,
        saw_attack: bool,
        target_uid: Option<u64>,
        target_ref: Option<continuous_control::EntityRef>,
        start_health: Option<f32>,
    },
    Mine {
        voxel: Vec3<i32>,
        finish_tick: u64,
        start_kind: String,
    },
    HoldAbility {
        input: InputKind,
        finish_tick: u64,
    },
    Craft {
        recipe: String,
        requested_amount: u64,
        amount: u32,
        station: Option<Vec3<i32>>,
        required_sprite: Option<SpriteKind>,
        craft_request_sequence: u64,
        output_definition_id: String,
        expected_output_amount: u64,
        start_inventory: InventorySnapshot,
        saw_craft_update: bool,
    },
    InteractWait {
        mode: InteractWait,
    },
}

#[derive(Clone, Debug)]
pub struct InventorySnapshot {
    definition_counts: BTreeMap<String, u32>,
    free_slots: usize,
}

#[derive(Clone, Copy, Debug)]
struct NearbyInteractableSprite {
    voxel: Vec3<i32>,
    sprite: SpriteKind,
    distance: f32,
    crafting_station: bool,
    directly_collectible: bool,
}

#[derive(Debug)]
struct KnownSite {
    kind: String,
    name: Option<String>,
    wpos: Vec2<f32>,
}

#[derive(Debug)]
struct KnownRecipeRecord {
    recipe: String,
    value: Value,
    satisfied: Option<bool>,
    missing_inputs: usize,
}

#[derive(Clone, Debug)]
struct InteractableSpriteScan {
    tick: u64,
    position: Vec3<f32>,
    radius: i32,
    sprites: Vec<NearbyInteractableSprite>,
}

#[derive(Clone, Debug)]
pub struct ActiveAction {
    pub request_id: Value,
    pub action_id: String,
    pub action_type: String,
    pub signature: String,
    pub started_tick: u64,
    pub deadline_tick: u64,
    pub start_position: Option<Vec3<f32>>,
    pub progress_position: Option<Vec3<f32>>,
    pub last_progress_tick: u64,
    pub survival_before: Option<Value>,
    pub kind: ActiveKind,
}

#[derive(Clone, Debug)]
pub struct CachedAction {
    pub signature: String,
    pub result: Value,
}

#[derive(Debug)]
struct VisualSubmission {
    args: Value,
    // Error emission has shared state; a synchronous refusal is written once.
    refusal: std::cell::OnceCell<Value>,
}

#[derive(Debug)]
enum InputMessage {
    Line(String),
    Closed,
}

#[derive(Debug)]
pub struct BridgeState {
    pub session_id: String,
    pub tick: u64,
    pub look_dir: Dir,
    pub persistent_move: Vec2<f32>,
    pub active: Option<ActiveAction>,
    continuous: continuous_control::Controls,
    pub completed: HashMap<String, CachedAction>,
    pub completed_order: VecDeque<String>,
    visual_origins: HashMap<String, Value>,
    visual_submissions: HashMap<String, VisualSubmission>,
    visual_dispatch: Option<(String, Value)>,
    pub events: VecDeque<Value>,
    pub next_event_seq: u64,
    // Craft markers carry no request identifier. Keep sent requests in order so a
    // delayed marker consumes its own request before it can confirm a later one.
    next_craft_request_sequence: u64,
    pending_craft_request_sequences: VecDeque<u64>,
    pickup_ledger: pickup_feedback::PickupLedger,
    interactable_sprite_scan: Option<InteractableSpriteScan>,
    pub release_jump: bool,
    pub release_wall_jump: bool,
    dialogue: Option<(Uid, common::rtsim::Dialogue<true>)>,
    interactions: native_interactions::Ledger,
    pub held_input: Option<InputKind>,
    pub break_block_pos: Option<Vec3<f32>>,
}

impl BridgeState {
    pub fn new(session_id: impl Into<String>, look_dir: Dir) -> Self {
        Self {
            session_id: session_id.into(),
            tick: 0,
            look_dir,
            persistent_move: Vec2::zero(),
            active: None,
            continuous: continuous_control::Controls::default(),
            completed: HashMap::new(),
            completed_order: VecDeque::new(),
            visual_origins: HashMap::new(),
            visual_submissions: HashMap::new(),
            visual_dispatch: None,
            events: VecDeque::new(),
            next_event_seq: 1,
            next_craft_request_sequence: 1,
            pending_craft_request_sequences: VecDeque::new(),
            pickup_ledger: pickup_feedback::PickupLedger::default(),
            interactable_sprite_scan: None,
            release_jump: false,
            release_wall_jump: false,
            dialogue: None,
            interactions: native_interactions::Ledger::default(),
            held_input: None,
            break_block_pos: None,
        }
    }
}

fn ready_event(session_id: &str, tick: u64, character: &str, uid: Option<u64>) -> Value {
    json!({
        "type": "event",
        "event": "ready",
        "protocol_version": PROTOCOL_VERSION,
        "observation_schema_version": OBSERVATION_SCHEMA_VERSION,
        "capabilities": {
            "rpg_interactions": native_interactions::capability(),
            "action_preconditions": {"schema_version": "1.0", "actor": true, "slot": true,
                                     "inventory": true, "target_lifecycle": true},
            "continuous_control": continuous_control::capability(),
            "combat_perception": {"schema_version": "1.0", "source": "client_synced_ECS"},
            "local_geometry": {"schema_version": "1.0", "automatic": true, "loaded_terrain_only": true},
            "remove_owned_invulnerability": {
                "schema_version": "1.0",
                "requires_exact_ids": true,
                "self_only": true,
            },
            "sit": {
                "parameters": [],
                "idempotent": true,
                "completion": "observed_character_state_sit",
                "checkpoint_save_confirmation": false,
            },
            "give_up": {
                "parameters": [],
                "automatic": false,
                "precondition": "alive_downed_crawl",
                "completion": "fresh_same_actor_session_observed_dead",
                "respawn": false,
            },
            "waypoint_saved": {
                "event": true,
                "location_name": true,
                "coordinates": false,
                "absence_is_failure": false,
            },
            "pickup_feedback": {
                "schema_version": "1.0",
                "targeted_refusals": true,
                "collected_has_target_uid": false,
                "untagged_acquisition_is_target_unconfirmed": true,
                "justified_operational_collection_can_complete": true,
                "observed_eligibility": true,
            },
            "inspect_map": player_map::capability(),
            "goal_recipes": goal_recipes::capability(),
            "inspect_terrain": {
                "schema_version": TERRAIN_QUERY_SCHEMA_VERSION,
                "read_only": true,
                "loaded_terrain_only": true,
                "max_horizontal_distance_metres": TERRAIN_QUERY_MAX_HORIZONTAL_METRES,
                "max_vertical_delta_metres": TERRAIN_QUERY_MAX_VERTICAL_METRES,
                "query_shape": "one_voxel_column",
            },
        },
        "session_id": session_id,
        "tick": tick,
        "seq": 0,
        "character": character,
        "uid": uid,
    })
}

pub fn emit_ready(session_id: &str, tick: u64, character: &str) {
    emit(&ready_event(session_id, tick, character, None));
}

pub fn emit_ready_with_uid(session_id: &str, tick: u64, character: &str, uid: Option<u64>) {
    emit(&ready_event(session_id, tick, character, uid));
}

pub fn parse_request_line(line: &str) -> Result<Request, String> {
    serde_json::from_str(line).map_err(|error| error.to_string())
}

fn argument(name: &str) -> Option<String> {
    let mut args = env::args();
    while let Some(arg) = args.next() {
        if arg == name {
            return args.next();
        }
    }
    None
}

impl Config {
    fn from_env_and_args() -> Self {
        let username = argument("--username")
            .or_else(|| env::var("VELOREN_USERNAME").ok())
            .unwrap_or_else(|| "agent".to_owned());
        Self {
            server: argument("--server")
                .or_else(|| env::var("VELOREN_SERVER").ok())
                .unwrap_or_else(|| "127.0.0.1:14004".to_owned()),
            username: username.clone(),
            password: argument("--password")
                .or_else(|| env::var("VELOREN_PASSWORD").ok())
                .unwrap_or_default(),
            character: argument("--character").or_else(|| env::var("VELOREN_CHARACTER").ok()),
            session_id: env::var("VELOREN_SESSION_ID")
                .unwrap_or_else(|_| format!("{}-{}", username, std::process::id())),
        }
    }
}

fn parse_min_path_length(raw: Option<&str>) -> PathLength {
    let Some(raw) = raw.map(str::trim) else {
        return DEFAULT_AGENT_MIN_PATH_LENGTH;
    };
    if raw.eq_ignore_ascii_case("small") {
        PathLength::Small
    } else if raw.eq_ignore_ascii_case("medium") {
        PathLength::Medium
    } else if raw.eq_ignore_ascii_case("long") {
        PathLength::Long
    } else if raw.eq_ignore_ascii_case("longest") {
        PathLength::Longest
    } else {
        DEFAULT_AGENT_MIN_PATH_LENGTH
    }
}

fn is_valid_min_path_length(raw: &str) -> bool {
    let raw = raw.trim();
    raw.eq_ignore_ascii_case("small")
        || raw.eq_ignore_ascii_case("medium")
        || raw.eq_ignore_ascii_case("long")
        || raw.eq_ignore_ascii_case("longest")
}

fn agent_min_path_length() -> PathLength {
    *AGENT_MIN_PATH_LENGTH.get_or_init(|| {
        let raw = env::var("VELOREN_AGENT_MIN_PATH_LENGTH").ok();
        if raw.as_deref().is_some_and(|raw| {
            !raw.trim().is_empty() && !is_valid_min_path_length(raw)
        }) {
            eprintln!(
                "unrecognised VELOREN_AGENT_MIN_PATH_LENGTH={:?}; using Small",
                raw.as_deref().unwrap_or_default(),
            );
        }
        parse_min_path_length(raw.as_deref())
    })
}

fn parse_edge_cost_mode(raw: Option<&str>) -> EdgeCostMode {
    let Some(raw) = raw.map(str::trim) else {
        return DEFAULT_AGENT_EDGE_COST_MODE;
    };
    if raw.eq_ignore_ascii_case("default") {
        EdgeCostMode::LegacyTierDerived
    } else if raw.eq_ignore_ascii_case("on") {
        EdgeCostMode::ForceOn
    } else if raw.eq_ignore_ascii_case("off") {
        EdgeCostMode::ForceOff
    } else {
        DEFAULT_AGENT_EDGE_COST_MODE
    }
}

fn is_valid_edge_cost_mode(raw: &str) -> bool {
    let raw = raw.trim();
    raw.eq_ignore_ascii_case("default")
        || raw.eq_ignore_ascii_case("on")
        || raw.eq_ignore_ascii_case("off")
}

fn agent_edge_cost_mode() -> EdgeCostMode {
    *AGENT_EDGE_COST_MODE.get_or_init(|| {
        let raw = env::var("VELOREN_AGENT_EDGE_COST").ok();
        if raw.as_deref().is_some_and(|raw| {
            !raw.trim().is_empty() && !is_valid_edge_cost_mode(raw)
        }) {
            eprintln!(
                "unrecognised VELOREN_AGENT_EDGE_COST={:?}; using default",
                raw.as_deref().unwrap_or_default(),
            );
        }
        parse_edge_cost_mode(raw.as_deref())
    })
}

pub fn emit(value: &Value) {
    let projected = OUTPUT_PROJECTION.get().map(|project| project(value));
    let value = projected.as_ref().unwrap_or(value);
    let stdout = io::stdout();
    let mut output = stdout.lock();
    if serde_json::to_writer(&mut output, value).is_ok() {
        let _ = writeln!(output);
        let _ = output.flush();
    }
}

fn response(state: &BridgeState, id: Value, result: Value) {
    emit(&json!({
        "type": "response",
        "protocol_version": PROTOCOL_VERSION,
        "session_id": state.session_id,
        "tick": state.tick,
        "id": id,
        "ok": true,
        "result": result,
        "error": null,
    }));
}

fn error_response(state: &BridgeState, id: Value, code: &str, message: impl Into<String>) {
    let error = json!({"code": code, "message": message.into()});
    if let Some((action_id, request_id)) = &state.visual_dispatch
        && request_id == &id
        && let Some(submission) = state.visual_submissions.get(action_id)
    {
        submission.refusal.set(error.clone()).expect("visual dispatch refused more than once");
    }
    emit(&json!({
        "type": "response",
        "protocol_version": PROTOCOL_VERSION,
        "session_id": state.session_id,
        "tick": state.tick,
        "id": id,
        "ok": false,
        "result": null,
        "error": error,
    }));
}

fn record_event(state: &mut BridgeState, event: &str, data: Value) {
    let seq = state.next_event_seq;
    state.next_event_seq += 1;
    let value = json!({
        "type": "event",
        "protocol_version": PROTOCOL_VERSION,
        "session_id": state.session_id,
        "tick": state.tick,
        "seq": seq,
        "sequence_domain": {"kind": "client-update-counter", "session_id": state.session_id},
        "event": event,
        "data": data,
    });
    emit(&value);
    state.events.push_back(value);
    while state.events.len() > MAX_EVENTS {
        state.events.pop_front();
    }
}

fn server_health_change_data(outcome: &common::outcome::Outcome) -> Option<Value> {
    let common::outcome::Outcome::HealthChange { pos, info } = outcome else { return None };
    Some(json!({
        "position": [pos.x, pos.y, pos.z],
        "target_uid": info.target.0.get(),
        "contributor_uid": info.by.map(|by| by.uid().0.get()),
        "contributor_kind": info.by.map(|by| match by {
            common::combat::DamageContributor::Solo(_) => "Solo",
            common::combat::DamageContributor::Group { .. } => "Group",
        }),
        "cause": info.cause.map(|cause| format!("{cause:?}")),
        "amount": info.amount,
        "precise": info.precise,
        "instance": info.instance,
        "source": "server_outcome_health_change",
        "amount_scope": "server_reported_change_not_clamped_hp_or_exclusive_tool_damage",
    }))
}

fn canonical_value(value: &Value) -> Value {
    match value {
        Value::Array(values) => Value::Array(values.iter().map(canonical_value).collect()),
        Value::Object(values) => {
            let mut keys = values.keys().collect::<Vec<_>>();
            keys.sort_unstable();
            let mut canonical = serde_json::Map::new();
            for key in keys {
                canonical.insert(key.clone(), canonical_value(&values[key]));
            }
            Value::Object(canonical)
        },
        _ => value.clone(),
    }
}

fn action_signature(args: &Value) -> String {
    serde_json::to_string(&canonical_value(args)).unwrap_or_default()
}

fn number_arg(
    object: &serde_json::Map<String, Value>,
    field: &str,
    default: f32,
    minimum: f32,
    maximum: f32,
) -> Result<f32, String> {
    let Some(value) = object.get(field) else {
        return Ok(default);
    };
    let value = value
        .as_f64()
        .ok_or_else(|| format!("{field} must be a number"))?;
    if !value.is_finite() || value < minimum as f64 || value > maximum as f64 {
        return Err(format!(
            "{field} must be finite and between {minimum} and {maximum}"
        ));
    }
    Ok(value as f32)
}

fn ticks_arg(args: &Value, field: &str, default: u64, maximum: u64) -> Result<u64, String> {
    let Some(value) = args.get(field) else {
        return Ok(default);
    };
    let value = value
        .as_u64()
        .ok_or_else(|| format!("{field} must be a positive integer"))?;
    if value == 0 || value > maximum {
        return Err(format!("{field} must be between 1 and {maximum}"));
    }
    Ok(value)
}

fn craft_amount_arg(action: &serde_json::Map<String, Value>) -> Result<(u64, u32), String> {
    let requested = match action.get("amount") {
        None => 1,
        Some(value) => value
            .as_u64()
            .ok_or_else(|| "amount must be a positive integer".to_owned())?,
    };
    if requested == 0 {
        return Err("amount must be a positive integer".to_owned());
    }
    Ok((requested, requested.min(u64::from(MAX_CRAFT_AMOUNT)) as u32))
}

fn craft_request_detail(recipe: &str, requested_amount: u64, amount: u32) -> Value {
    let mut detail = json!({
        "recipe": recipe,
        "requested_amount": requested_amount,
        "submitted_amount": amount,
        "amount_was_clamped": requested_amount != u64::from(amount),
    });
    if requested_amount != u64::from(amount) {
        detail["amount_clamped_from"] = json!(requested_amount);
        detail["amount_clamped_to"] = json!(amount);
    }
    detail
}

fn craft_failure_detail(
    recipe: &str,
    requested_amount: u64,
    amount: u32,
    code: &str,
    message: impl Into<String>,
) -> Value {
    let mut detail = craft_request_detail(recipe, requested_amount, amount);
    detail["code"] = json!(code);
    detail["message"] = json!(message.into());
    detail
}

fn recipe_input_description(input: &RecipeInput) -> String {
    match input {
        RecipeInput::Item(item) => item.id().to_owned(),
        RecipeInput::Tag(tag) => format!("an item tagged {tag:?}"),
        RecipeInput::TagSameItem(tag) => format!("one item kind tagged {tag:?}"),
        RecipeInput::ListSameItem(items) => format!(
            "one item kind from [{}]",
            items
                .iter()
                .map(|item| item.id())
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

fn missing_ingredients_json(missing: &[(&RecipeInput, u32)]) -> Value {
    Value::Array(
        missing
            .iter()
            .map(|(input, amount)| {
                json!({
                    "input": recipe_input_description(input),
                    "missing_amount": amount,
                })
            })
            .collect(),
    )
}

fn chat_message_arg<'a>(
    action: &'a serde_json::Map<String, Value>,
) -> Result<&'a str, String> {
    let message = action
        .get("message")
        .and_then(Value::as_str)
        .ok_or_else(|| "message must be a string".to_owned())?;
    if message.len() > MAX_CHAT_MESSAGE_BYTES {
        return Err(format!(
            "message must contain at most {MAX_CHAT_MESSAGE_BYTES} bytes"
        ));
    }

    Ok(message)
}

fn private_chat_args(
    action: &serde_json::Map<String, Value>,
) -> Result<(&str, &str), String> {
    let message = chat_message_arg(action)?;
    if message.trim().is_empty() {
        return Err("private message cannot be empty".to_owned());
    }
    let recipient = action.get("recipient").and_then(Value::as_str)
        .ok_or_else(|| "recipient must be a player alias".to_owned())?;
    if recipient.is_empty() || recipient.len() > 64
        || recipient.chars().any(|c| c.is_whitespace() || c.is_control())
    {
        return Err("recipient must be one nonempty player alias of at most 64 bytes".to_owned());
    }
    Ok((recipient, message))
}

fn server_command_args(
    action: &serde_json::Map<String, Value>,
) -> Result<(&str, Vec<String>), String> {
    let name = action
        .get("name")
        .and_then(Value::as_str)
        .ok_or_else(|| "name must be a string".to_owned())?;
    if name.is_empty()
        || name.len() > MAX_SERVER_COMMAND_NAME_BYTES
        || name.chars().any(|character| character.is_whitespace() || character.is_control())
    {
        return Err(format!(
            "name must contain 1..={MAX_SERVER_COMMAND_NAME_BYTES} non-whitespace, non-control bytes"
        ));
    }
    let args = action
        .get("args")
        .and_then(Value::as_array)
        .ok_or_else(|| "args must be an array of strings".to_owned())?;
    if args.len() > MAX_SERVER_COMMAND_ARGS {
        return Err(format!(
            "args must contain at most {MAX_SERVER_COMMAND_ARGS} strings"
        ));
    }
    let args = args
        .iter()
        .map(|argument| {
            let argument = argument
                .as_str()
                .ok_or_else(|| "args must be an array of strings".to_owned())?;
            if argument.len() > MAX_SERVER_COMMAND_ARG_BYTES {
                return Err(format!(
                    "each args entry must contain at most {MAX_SERVER_COMMAND_ARG_BYTES} bytes"
                ));
            }
            Ok(argument.to_owned())
        })
        .collect::<Result<Vec<_>, _>>()?;
    Ok((name, args))
}

fn chat_kind(chat_type: &comp::ChatType<String>) -> &'static str {
    match chat_type {
        comp::ChatType::Online(_) => "online",
        comp::ChatType::Offline(_) => "offline",
        comp::ChatType::CommandInfo => "command_info",
        comp::ChatType::CommandError => "command_error",
        comp::ChatType::Kill(_, _) => "kill",
        comp::ChatType::GroupMeta(_) => "group_meta",
        comp::ChatType::FactionMeta(_) => "faction_meta",
        comp::ChatType::Tell(_, _) => "tell",
        comp::ChatType::Say(_) => "say",
        comp::ChatType::Group(_, _) => "group",
        comp::ChatType::Faction(_, _) => "faction",
        comp::ChatType::Region(_) => "region",
        comp::ChatType::World(_) => "world",
        comp::ChatType::Npc(_) => "npc",
        comp::ChatType::NpcSay(_) => "npc_say",
        comp::ChatType::NpcTell(_, _) => "npc_tell",
        comp::ChatType::Meta => "meta",
    }
}

fn chat_event_data(message: &comp::ChatMsg) -> Value {
    let content = message.content();
    let content_kind = match content {
        comp::Content::Plain(_) => "plain",
        comp::Content::Key(_) => "key",
        comp::Content::Attr(_, _) => "attr",
        comp::Content::Localized { .. } => "localized",
        comp::Content::WithFallback(_, _) => "with_fallback",
    };
    let mut data = json!({
        "kind": chat_kind(&message.chat_type),
        "sender_uid": message.uid().map(|uid| uid.0.get()),
        "text": content.as_plain(),
        "content_kind": content_kind,
    });
    // Command replies are often localized, so as_plain() alone loses the
    // server's reason. Preserve the native Content enum (key/args/fallback),
    // not an invented translation. Do not expand ordinary/private chat data.
    if matches!(message.chat_type, comp::ChatType::CommandError | comp::ChatType::CommandInfo) {
        data["content"] = json!(content);
    }
    data
}

/// Preserve the legacy chat event fields, then append sender identity that is
/// available from the client ECS/player list at event receipt time.
fn chat_event_data_with_sender_identity(client: &Client, message: &comp::ChatMsg) -> Value {
    let mut event_data = chat_event_data(message);
    if let Value::Object(object) = &mut event_data {
        object.insert(
            "sender_identity".to_owned(),
            sender_identity(client, message.uid()),
        );
    }
    event_data
}

fn event_batch(state: &BridgeState, after_seq: u64, limit: usize) -> Value {
    let oldest_seq = state
        .events
        .front()
        .and_then(|event| event.get("seq"))
        .and_then(Value::as_u64)
        .unwrap_or(state.next_event_seq);
    let mut events = state
        .events
        .iter()
        .filter(|event| event["seq"].as_u64().is_some_and(|seq| seq > after_seq))
        .cloned()
        .collect::<Vec<_>>();
    let has_more = events.len() > limit;
    events.truncate(limit);
    json!({
        "events": events,
        "oldest_seq": oldest_seq,
        "next_seq": state.next_event_seq,
        "truncated": after_seq.saturating_add(1) < oldest_seq,
        "has_more": has_more,
    })
}

fn tick_once(
    client: &mut Client,
    inputs: ControllerInputs,
    dt: Duration,
) -> Result<Vec<Event>, String> {
    let events = client
        .tick(inputs, dt)
        .map_err(|error| format!("{error:?}"))?;
    client.cleanup();
    Ok(events)
}

fn wait_for_character_list(client: &mut Client, dt: Duration) -> Result<(), String> {
    for _ in 0..STARTUP_TIMEOUT_TICKS {
        tick_once(client, ControllerInputs::default(), dt)?;
        if !client.character_list().loading {
            return Ok(());
        }
        thread::sleep(dt);
    }
    Err("timed out waiting for character list".to_owned())
}

fn default_body() -> comp::body::humanoid::Body {
    comp::body::humanoid::Body {
        species: comp::body::humanoid::Species::Human,
        body_type: comp::body::humanoid::BodyType::Male,
        hair_style: 0,
        beard: 0,
        eyes: 0,
        accessory: 0,
        hair_color: 0,
        skin: 0,
        eye_color: 0,
    }
}

/// Starter humanoid body used by headless bridge and Voxygen agent auto-login
/// when creating a missing character.
pub fn default_agent_body() -> comp::body::humanoid::Body { default_body() }

fn enter_world(client: &mut Client, config: &Config, dt: Duration) -> Result<String, String> {
    client.load_character_list();
    wait_for_character_list(client, dt)?;

    if client.character_list().characters.is_empty() {
        let alias = config
            .character
            .clone()
            .unwrap_or_else(|| config.username.clone());
        // Prefer a settlement starting site over the raw global spawn
        // (which can land near lava / cliffs on some seeds).
        let start_site = client.possible_starting_sites().first().copied();
        client.create_character(
            alias,
            Some("common.items.weapons.sword.starter".to_owned()),
            None,
            default_body().into(),
            false,
            start_site,
        );
        wait_for_character_list(client, dt)?;
    }

    let character = config
        .character
        .as_ref()
        .and_then(|wanted| {
            client
                .character_list()
                .characters
                .iter()
                .find(|entry| entry.character.alias == *wanted)
        })
        .or_else(|| client.character_list().characters.first())
        .ok_or_else(|| "server returned no playable character".to_owned())?;
    let character_id = character
        .character
        .id
        .ok_or_else(|| "selected character has no id".to_owned())?;
    let alias = character.character.alias.clone();
    client.request_character(character_id, ViewDistances {
        terrain: 5,
        entity: 5,
    });

    for _ in 0..STARTUP_TIMEOUT_TICKS {
        let events = tick_once(client, ControllerInputs::default(), dt)?;
        if client.position().is_some()
            || events
                .iter()
                .any(|event| matches!(event, Event::CharacterJoined(_)))
        {
            for _ in 0..(TPS / 2) {
                tick_once(client, ControllerInputs::default(), dt)?;
                thread::sleep(dt);
            }
            return Ok(alias);
        }
        thread::sleep(dt);
    }
    Err("timed out entering the game world".to_owned())
}

fn item_json(item: &Item) -> Value {
    let tool_kind = match &*item.kind() {
        ItemKind::Tool(tool) => Some(tool.kind.identifier_name().to_owned()),
        _ => None,
    };
    let consumable = match &*item.kind() {
        ItemKind::Consumable { kind, effects, container } => {
            let (buildup, use_time, recover) =
                common::states::use_item::ItemUseKind::Consumable(*kind).durations();
            json!({
                "kind": format!("{kind:?}"),
                "effects": effects,
                "container": container,
                "buildup_seconds": buildup.as_secs_f64(),
                "use_seconds": use_time.as_secs_f64(),
                "recover_seconds": recover.as_secs_f64(),
            })
        },
        _ => Value::Null,
    };
    json!({
        "id": item.item_definition_id().to_owned(),
        "amount": item.amount(),
        "kind": item.kind().get_itemkind_string(),
        "tool_kind": tool_kind,
        "durability": {
            "has_durability": item.has_durability(),
            "lost": item.durability_lost(),
            "maximum": item.has_durability().then_some(Item::MAX_DURABILITY),
            "stat_multiplier": item.has_durability().then_some(item.stats_durability_multiplier().0),
            "scope": "observed_item_wear_not_transaction_receipt",
        },
        "consumable": consumable,
    })
}

fn inventory_item_count(inventory: &comp::Inventory) -> u32 {
    inventory
        .slots_with_id()
        .filter_map(|(_, slot)| slot.as_ref().map(|item| item.amount()))
        .sum()
}

fn item_definition_id_key(
    definition_id: common::comp::inventory::item::ItemDefinitionId<'_>,
) -> String {
    definition_id
        .itemdef_id()
        .map(str::to_owned)
        .unwrap_or_else(|| format!("{definition_id:?}"))
}

fn item_definition_key(item: &Item) -> String {
    item_definition_id_key(item.item_definition_id())
}

fn inventory_snapshot(inventory: &comp::Inventory) -> InventorySnapshot {
    let mut definition_counts = BTreeMap::new();
    for item in inventory
        .slots_with_id()
        .filter_map(|(_, slot)| slot.as_ref())
        .chain(inventory.overflow_items())
    {
        *definition_counts.entry(item_definition_key(item)).or_insert(0) += item.amount();
    }
    InventorySnapshot {
        definition_counts,
        free_slots: inventory.free_slots(),
    }
}

fn inventory_delta(
    start: &BTreeMap<String, u32>,
    end: &BTreeMap<String, u32>,
) -> BTreeMap<String, i64> {
    let mut delta = BTreeMap::new();
    for id in start.keys().chain(end.keys()) {
        if delta.contains_key(id) {
            continue;
        }
        let difference = i64::from(*end.get(id).unwrap_or(&0)) - i64::from(*start.get(id).unwrap_or(&0));
        if difference != 0 {
            delta.insert(id.clone(), difference);
        }
    }
    delta
}

fn inventory_json(inventory: Option<&comp::Inventory>) -> Value {
    let Some(inventory) = inventory else {
        return Value::Null;
    };
    let slots = inventory
        .slots_with_id()
        .filter_map(|(slot_id, slot)| {
            slot.as_ref().map(|item| {
                json!({
                    "slot": slot_id.idx(),
                    "loadout_idx": slot_id.loadout_idx(),
                    "slot_idx": slot_id.slot_idx(),
                    "item": item_json(item),
                })
            })
        })
        .collect::<Vec<_>>();
    let empty_slots = inventory.slots_with_id()
        .filter_map(|(slot, content)| content.is_none().then_some(slot.idx()))
        .collect::<Vec<_>>();
    let wielded = inventory.equipped(EquipSlot::ActiveMainhand).map(item_json);
    let mainhand_tool = inventory
        .equipped(EquipSlot::ActiveMainhand)
        .and_then(|item| match &*item.kind() {
            ItemKind::Tool(tool) => Some(tool.kind.identifier_name().to_owned()),
            _ => None,
        });
    json!({
        "capacity": inventory.capacity(),
        "free_slots": inventory.free_slots(),
        "occupied_amount": inventory_item_count(inventory),
        "items": slots,
        "empty_slots": empty_slots,
        "wielded_slot": "active_mainhand",
        "wielded_item": wielded,
        "mainhand_tool": mainhand_tool,
        "equipment": equipment_json(inventory),
    })
}

// Include empty slots explicitly; names are the same ones accepted by `equip`.
fn equipment_json(inventory: &comp::Inventory) -> Value {
    let names = [
        "active_mainhand", "active_offhand", "inactive_mainhand", "inactive_offhand",
        "lantern", "glider", "head", "neck", "shoulders", "chest", "hands",
        "ring1", "ring2", "back", "belt", "legs", "feet", "tabard",
        "bag1", "bag2", "bag3", "bag4",
    ];
    Value::Object(names.into_iter().map(|name| {
        let item = parse_equip_slot(name)
            .and_then(|slot| inventory.equipped(slot))
            .map(item_json).unwrap_or(Value::Null);
        (name.to_owned(), item)
    }).collect())
}

fn buffs_json(buffs: Option<&comp::Buffs>, now: Option<f64>) -> Value {
    let Some(buffs) = buffs else { return Value::Null };
    Value::Array(buffs.buffs.iter().map(|(key, buff)| {
        let selected = buffs.iter_active().flatten().any(|active| std::ptr::eq(active, buff));
        json!({
            "id": format!("{key:?}"),
            "kind": format!("{:?}", buff.kind),
            "strength": buff.data.strength,
            "duration_seconds": buff.data.duration.map(|time| time.0),
            "start_time": buff.start_time.0,
            "end_time": buff.end_time.map(|time| time.0),
            "remaining_seconds": now.and_then(|now| buff.end_time.map(|time| (time.0 - now).max(0.0))),
            "starts_in_seconds": now.map(|now| (buff.start_time.0 - now).max(0.0)),
            "selected": selected,
            "active": now.map(|now| selected && buff.start_time.0 <= now
                && buff.end_time.is_none_or(|time| time.0 > now)),
            "queues": buff.kind.queues(),
            "stacks": buff.kind.stacks(),
        })
    }).collect())
}

fn survival_buff_snapshot(ecs: &specs::World, buffs: Option<&comp::Buffs>) -> Value {
    let now = crate::time_sync::synchronized_time(ecs);
    json!({
        "clock": {
            "basis": "client_physics_time_after_server_sync",
            "synchronized": now.is_some(), "now": now,
        },
        "buffs": buffs_json(buffs, now),
    })
}

fn survival_snapshot(client: &Client, state: &BridgeState) -> Value {
    let inventory = client.current::<comp::Inventory>();
    let buffs = client.current::<comp::Buffs>();
    let buff_snapshot = survival_buff_snapshot(client.state().ecs(), buffs.as_ref());
    json!({
        "session_id": state.session_id,
        "tick": state.tick,
        "sequence_domain": {"kind": "client-update-counter", "session_id": state.session_id},
        "event_cursor": state.next_event_seq - 1,
        "self_identity": self_identity(client),
        "position": client.position().map(|p| [p.x, p.y, p.z]),
        "health": client.current::<comp::Health>().map(|health| json!({
            "current": health.current(), "maximum": health.maximum(),
            "fraction": health.fraction(), "dead": health.is_dead,
            "death_protection": health.death_protection,
            "can_have_death_protection": health.can_have_death_protection,
            "downed": !health.is_dead && health.has_consumed_death_protection(),
        })),
        "character_state": client.current::<comp::CharacterState>().map(|s| s.to_string()),
        "wielding": client.is_wielding(),
        "equipment": inventory.as_ref().map(equipment_json),
        "buff_clock": buff_snapshot["clock"],
        "buffs": buff_snapshot["buffs"],
    })
}

fn is_survival_action(name: &str) -> bool {
    matches!(name, "use_item" | "equip" | "swap_weapons" | "toggle_wield" | "respawn" | "give_up")
}

fn use_item_reference(
    inventory: Option<&comp::Inventory>, slot: InvSlotId,
) -> Result<(u32, String, u32, bool), &'static str> {
    let (inventory, item) = inventory
        .and_then(|inv| inv.get(slot).map(|item| (inv, item)))
        .ok_or("inventory slot is empty or unavailable")?;
    let definition_id = item_definition_key(item);
    let count = inventory_snapshot(inventory).definition_counts
        .get(&definition_id).copied().unwrap_or(0);
    Ok((inventory_item_count(inventory), definition_id, count,
        matches!(&*item.kind(), ItemKind::Consumable { .. })))
}

fn use_item_completion_observed(entered: bool, using: bool, delta: Option<i64>) -> bool {
    !using && (entered || delta.is_some_and(|delta| delta < 0))
}

fn recipe_input_observation(input: &RecipeInput) -> (&'static str, String) {
    match input {
        RecipeInput::Item(item) => ("item", item.id().to_owned()),
        RecipeInput::Tag(tag) => ("tag", format!("tag:{tag:?}")),
        RecipeInput::TagSameItem(tag) => ("tag_same_item", format!("tag_same_item:{tag:?}")),
        RecipeInput::ListSameItem(items) => (
            "list_same_item",
            format!(
                "list_same_item:[{}]",
                items
                    .iter()
                    .map(|item| item.id())
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
        ),
    }
}

fn known_recipe_record(
    recipe_key: &str,
    station: Option<SpriteKind>,
    recipe: &Recipe,
    inventory: Option<&comp::Inventory>,
) -> KnownRecipeRecord {
    // Use the same inventory check as the craft action so slot claims and its
    // contains-any requirement are represented exactly for concrete inputs.
    let exact_missing = inventory
        .and_then(|inventory| recipe.inventory_contains_ingredients(inventory, 1).err())
        .unwrap_or_default();
    let has_unknown_input = recipe
        .inputs()
        .any(|(input, _, _)| !matches!(input, RecipeInput::Item(_)));
    let mut missing = Vec::new();
    let mut inputs = Vec::new();

    for (input, amount, _) in recipe.inputs() {
        let (kind, item) = recipe_input_observation(input);
        inputs.push(json!({
            "item": item,
            "amount": amount,
            "kind": kind,
        }));

        let missing_amount = match input {
            RecipeInput::Item(_) => {
                if inventory.is_some() {
                    exact_missing
                        .iter()
                        .find_map(|(missing_input, amount)| {
                            std::ptr::eq(*missing_input, input).then_some(*amount)
                        })
                } else {
                    Some(amount)
                }
            },
            RecipeInput::Tag(_) | RecipeInput::TagSameItem(_) | RecipeInput::ListSameItem(_) => {
                Some(amount)
            },
        };
        if let Some(amount) = missing_amount {
            missing.push(json!({
                "item": item,
                "amount": amount,
            }));
        }
    }

    let satisfied = if inventory.is_none() || has_unknown_input {
        None
    } else {
        Some(exact_missing.is_empty())
    };
    let missing_inputs = missing.len();
    KnownRecipeRecord {
        recipe: recipe_key.to_owned(),
        value: json!({
            "recipe": recipe_key,
            "output": {
                "item": recipe.output.0.id(),
                "amount": recipe.output.1,
            },
            "inputs": inputs,
            "station": station.map(|sprite| format!("{sprite:?}")),
            "missing": missing,
            "satisfied": satisfied,
        }),
        satisfied,
        missing_inputs,
    }
}

fn known_recipe_records_json(total: usize, mut recipes: Vec<KnownRecipeRecord>) -> Value {
    recipes.sort_by(|left, right| {
        let left_is_satisfied = left.satisfied == Some(true);
        let right_is_satisfied = right.satisfied == Some(true);
        right_is_satisfied
            .cmp(&left_is_satisfied)
            .then_with(|| left.missing_inputs.cmp(&right.missing_inputs))
            .then_with(|| left.recipe.cmp(&right.recipe))
    });
    recipes.truncate(MAX_KNOWN_RECIPES);
    let items = recipes
        .into_iter()
        .map(|recipe| recipe.value)
        .collect::<Vec<_>>();
    json!({
        "total": total,
        "omitted": total.saturating_sub(items.len()),
        "items": items,
    })
}

// `Client::available_recipes` is already filtered down to what the character can
// craft this instant, so it reports nothing at all whenever the pack is short of
// an ingredient. The character's own recipe book is what lets the agent see what
// it could make and what it still has to find.
fn known_recipes_json(client: &Client, inventory: Option<&comp::Inventory>) -> Value {
    let Some(inventory) = inventory else {
        return known_recipe_records_json(0, Vec::new());
    };
    let total = inventory.recipes_iter().len();
    let manifest = client.state().ecs().read_resource::<RecipeBookManifest>();
    let recipes = inventory
        .recipes_iter()
        .filter_map(|recipe_key| {
            manifest.get(recipe_key).map(|recipe| {
                known_recipe_record(recipe_key, recipe.craft_sprite, recipe, Some(inventory))
            })
        })
        .collect();
    known_recipe_records_json(total, recipes)
}

fn block_json(position: Vec3<i32>, block: &common::terrain::Block) -> Value {
    let lamp_enabled = block.get_sprite()
        .filter(|sprite| sprite.category() == common::terrain::sprite::Category::Lamp)
        .and_then(|_| {
            block.get_attr().ok().map(|common::terrain::sprite::LightEnabled(enabled)| enabled)
        });
    json!({
        "position": [position.x, position.y, position.z],
        "kind": block.kind().to_string(),
        "solid": block.is_solid(),
        "collectible": block.is_collectible(),
        "directly_collectible": block.is_directly_collectible(),
        "mine_tool": block.mine_tool().map(|tool| tool.identifier_name().to_owned()),
        "sprite": block.get_sprite().map(|sprite| format!("{sprite:?}")),
        "lamp_enabled": lamp_enabled,
    })
}

fn block_at(client: &Client, position: Vec3<i32>) -> Option<Value> {
    client
        .state()
        .terrain()
        .get(position)
        .ok()
        .map(|block| block_json(position, block))
}

#[derive(Clone, Copy, Debug)]
enum TerrainCellSample {
    Loaded(Block),
    Unloaded,
    Unknown,
}

fn terrain_cell_json(position: Vec3<i32>, sample: TerrainCellSample) -> Value {
    match sample {
        TerrainCellSample::Loaded(block) => json!({
            "position": [position.x, position.y, position.z],
            "status": "loaded",
            "kind": block.kind().to_string(),
            "sprite": block.get_sprite().map(|sprite| format!("{sprite:?}")),
            "solid": block.is_solid(),
            "solid_height": block.solid_height(),
            "liquid": block.is_liquid(),
        }),
        TerrainCellSample::Unloaded => json!({
            "position": [position.x, position.y, position.z],
            "status": "unloaded",
            "kind": null,
            "sprite": null,
            "solid": null,
            "solid_height": null,
            "liquid": null,
        }),
        TerrainCellSample::Unknown => json!({
            "position": [position.x, position.y, position.z],
            "status": "unknown",
            "kind": null,
            "sprite": null,
            "solid": null,
            "solid_height": null,
            "liquid": null,
        }),
    }
}

fn terrain_sprite_has_on_ground_debuff(sprite: SpriteKind) -> bool {
    // Keep this narrow list synchronized with the explicit on_ground cases in
    // common/systems/src/buff.rs. It is not a claim to classify every hazard.
    matches!(
        sprite,
        SpriteKind::EnsnaringVines
            | SpriteKind::EnsnaringWeb
            | SpriteKind::SeaUrchin
            | SpriteKind::HaniwaTrap
            | SpriteKind::IronSpike
            | SpriteKind::HaniwaTrapTriggered
            | SpriteKind::HotSurface
            | SpriteKind::IceSpike
            | SpriteKind::FireBlock
    )
}

fn terrain_block_is_hazardous(block: Block) -> bool {
    if matches!(block.kind(), BlockKind::Water | BlockKind::Lava) {
        return true;
    }
    block.get_sprite().is_some_and(|sprite| {
        if terrain_sprite_has_on_ground_debuff(sprite) {
            return true;
        }
        let name = format!("{sprite:?}").to_ascii_lowercase();
        ["water", "lava", "fire", "spike", "poison", "liquid"]
            .iter()
            .any(|word| name.contains(word))
    })
}

fn terrain_block_overlaps_span(block_z: i32, block: Block, start: f32, end: f32) -> bool {
    let hazardous = terrain_block_is_hazardous(block);
    let geometry_height = if hazardous {
        block.solid_height().max(1.0)
    } else {
        block.solid_height()
    };
    let occupied_top = block_z as f32 + geometry_height;
    // Every endpoint and geometry operation in this certificate is already
    // canonical native f32. Strict half-open overlap preserves exact boundary
    // contact while never scaling a tolerance large enough to hide an overlap.
    (block.is_solid() || block.is_liquid() || hazardous)
        && occupied_top > start
        && (block_z as f32) < end
}

fn terrain_clearance_json(
    column: Vec2<i32>,
    start_height: f32,
    end_height: f32,
    samples: &BTreeMap<i32, TerrainCellSample>,
) -> Value {
    // Every loaded voxel from this lower bound could overlap the span under
    // the explicit maximum geometry extent. Returning the whole contributor
    // search range lets the host independently reject omissions and duplicates.
    let first_z =
        (start_height - TERRAIN_QUERY_MAX_SUPPORT_HEIGHT_METRES).floor() as i32 + 1;
    let last_z = end_height.ceil() as i32 - 1;
    let mut known = true;
    let mut clear = true;
    let mut cells = Vec::new();
    for z in first_z..=last_z {
        let position = Vec3::new(column.x, column.y, z);
        let sample = samples
            .get(&z)
            .copied()
            .unwrap_or(TerrainCellSample::Unknown);
        match sample {
            TerrainCellSample::Loaded(block) => {
                if terrain_block_overlaps_span(z, block, start_height, end_height) {
                    clear = false;
                }
            },
            TerrainCellSample::Unloaded | TerrainCellSample::Unknown => {
                known = false;
                clear = false;
            },
        }
        cells.push(terrain_cell_json(position, sample));
    }
    json!({
        "known": known,
        "clear": clear,
        "span": {
            "minimum_inclusive": start_height,
            "maximum_exclusive": end_height,
            "axis": "veloren_world_z_metres",
        },
        "cells": cells,
    })
}

// Proposed-pose certificate, not a motion command or current-pose observation.
// The AABB and intersecting columns are the same native geometry used by local
// motion and the terrain physics collider. Exact contact is not penetration.
fn terrain_body_clearance(
    position: Vec3<f32>,
    shape: Option<(f32, f32, f32)>,
    sample_cell: &mut impl FnMut(Vec3<i32>) -> TerrainCellSample,
) -> Value {
    let Some((radius, z_min, z_max)) = shape else {
        return json!({"known": false, "clear": false, "status": "actor_shape_unavailable"});
    };
    let actor = local_motion::actor_aabb(position, radius, z_min, z_max);
    let low = (actor.min.z - TERRAIN_QUERY_MAX_SUPPORT_HEIGHT_METRES).floor() as i32;
    let high = actor.max.z.ceil() as i32 - 1;
    let mut known = true;
    let mut clear = true;
    let mut cells = Vec::new();
    for column in local_motion::footprint_columns(position.xy(), radius) {
        for z in low..=high {
            let voxel = Vec3::new(column.x, column.y, z);
            let sample = sample_cell(voxel);
            match sample {
                TerrainCellSample::Loaded(block) => {
                    let solid = local_motion::terrain_block_aabb(voxel, block);
                    if (block.is_solid() && actor.collides_with_aabb(solid))
                        || terrain_block_overlaps_span(z, block, actor.min.z, actor.max.z)
                            && (block.is_liquid() || terrain_block_is_hazardous(block))
                    {
                        clear = false;
                    }
                },
                TerrainCellSample::Unloaded | TerrainCellSample::Unknown => {
                    known = false;
                    clear = false;
                },
            }
            cells.push(terrain_cell_json(voxel, sample));
        }
    }
    json!({"known": known, "clear": clear, "position": [position.x, position.y, position.z],
           "radius_m": radius, "z_min_m": z_min, "z_max_m": z_max, "cells": cells,
           "scope": "proposed native actor terrain AABB; not route, support or future NPC geometry"})
}

fn loaded_terrain_support_query(
    state: &BridgeState,
    actor_position: Vec3<f32>,
    self_identity: Value,
    readiness: Value,
    target_position: Vec3<f32>,
    actor_shape: Option<(f32, f32, f32)>,
    mut sample_cell: impl FnMut(Vec3<i32>) -> TerrainCellSample,
) -> Value {
    let column = target_position.xy().map(|value| value.floor() as i32);
    let standing_height_min = actor_position.z - TERRAIN_QUERY_MAX_VERTICAL_METRES;
    let standing_height_max = actor_position.z + TERRAIN_QUERY_MAX_VERTICAL_METRES;
    // Include every support voxel whose block/sprite top could fall inside the
    // standing-height window.
    let support_z_min = standing_height_min.floor() as i32
        - TERRAIN_QUERY_MAX_SUPPORT_HEIGHT_METRES.ceil() as i32;
    let support_z_max = standing_height_max.ceil() as i32;
    let clearance_z_max =
        (standing_height_max + TERRAIN_QUERY_CLEARANCE_HEIGHT_METRES).ceil() as i32 - 1;

    let mut samples = BTreeMap::new();
    for z in support_z_min..=clearance_z_max {
        samples.insert(z, sample_cell(Vec3::new(column.x, column.y, z)));
    }

    let mut support_candidates = Vec::new();
    for support_z in support_z_min..=support_z_max {
        let TerrainCellSample::Loaded(block) = samples[&support_z] else {
            continue;
        };
        let hazardous = terrain_block_is_hazardous(block);
        if !block.is_solid() && !block.is_liquid() && !hazardous {
            continue;
        }
        let standing_height = support_z as f32 + block.solid_height();
        if standing_height < standing_height_min || standing_height > standing_height_max {
            continue;
        }
        let foot_clearance = terrain_clearance_json(
            column,
            standing_height,
            standing_height + 1.0,
            &samples,
        );
        let head_clearance = terrain_clearance_json(
            column,
            standing_height + 1.0,
            standing_height + TERRAIN_QUERY_CLEARANCE_HEIGHT_METRES,
            &samples,
        );
        let support_usable = block.is_solid()
            && !block.is_liquid()
            && !hazardous
            && block.solid_height() <= TERRAIN_QUERY_MAX_PHYSICS_SUPPORT_HEIGHT_METRES;
        let known_clearance = foot_clearance["known"] == json!(true)
            && head_clearance["known"] == json!(true);
        let clearance_clear = foot_clearance["clear"] == json!(true)
            && head_clearance["clear"] == json!(true);
        let body_clearance = terrain_body_clearance(
            Vec3::new(target_position.x, target_position.y, standing_height),
            actor_shape, &mut sample_cell,
        );
        let body_clear = body_clearance["known"] == json!(true)
            && body_clearance["clear"] == json!(true);
        support_candidates.push(json!({
            "standing_position": [
                target_position.x,
                target_position.y,
                standing_height,
            ],
            "standing_height": standing_height,
            "support": terrain_cell_json(
                Vec3::new(column.x, column.y, support_z),
                TerrainCellSample::Loaded(block),
            ),
            "support_usable": support_usable,
            "foot_clearance": foot_clearance,
            "head_clearance": head_clearance,
            "known_clearance": known_clearance,
            "clearance_clear": clearance_clear,
            "body_clearance": body_clearance,
            "occupiable": support_usable && known_clearance && clearance_clear && body_clear,
        }));
    }

    let non_loaded_cells = samples
        .iter()
        .filter_map(|(z, sample)| match sample {
            TerrainCellSample::Loaded(_) => None,
            TerrainCellSample::Unloaded | TerrainCellSample::Unknown => Some(terrain_cell_json(
                Vec3::new(column.x, column.y, *z),
                *sample,
            )),
        })
        .collect::<Vec<_>>();
    let has_unknown = samples
        .values()
        .any(|sample| matches!(sample, TerrainCellSample::Unknown));
    let has_unloaded = samples
        .values()
        .any(|sample| matches!(sample, TerrainCellSample::Unloaded));
    let status = if has_unknown {
        "unknown_cells"
    } else if has_unloaded {
        "unloaded_cells"
    } else if support_candidates.is_empty() {
        "no_candidate_in_window"
    } else {
        "complete"
    };
    let horizontal_distance = (target_position.xy() - actor_position.xy()).magnitude();
    let vertical_delta = (target_position.z - actor_position.z).abs();

    json!({
        "terrain_query_schema_version": TERRAIN_QUERY_SCHEMA_VERSION,
        "status": status,
        "binding": {
            "protocol_version": PROTOCOL_VERSION,
            "observation_schema_version": OBSERVATION_SCHEMA_VERSION,
            "session_id": state.session_id,
            "tick": state.tick,
            "sequence_domain": {"kind": "client-update-counter", "session_id": state.session_id},
            "event_cursor": state.next_event_seq - 1,
            "coordinate_frame": {
                "position": "world_xyz_meters",
                "movement": "world_xy",
                "z_axis": "up",
                "look": "unit_vector_xyz",
            },
            "self_identity": self_identity,
            "readiness": readiness,
            "actor_position": [
                actor_position.x,
                actor_position.y,
                actor_position.z,
            ],
        },
        "request": {
            "target_position": [
                target_position.x,
                target_position.y,
                target_position.z,
            ],
            "target_voxel_column": [column.x, column.y],
            "horizontal_distance_metres": horizontal_distance,
            "vertical_delta_metres": vertical_delta,
            "bounds": {
                "max_horizontal_distance_metres": TERRAIN_QUERY_MAX_HORIZONTAL_METRES,
                "max_vertical_delta_metres": TERRAIN_QUERY_MAX_VERTICAL_METRES,
                "within_bounds": horizontal_distance <= TERRAIN_QUERY_MAX_HORIZONTAL_METRES
                    && vertical_delta <= TERRAIN_QUERY_MAX_VERTICAL_METRES,
            },
        },
        "vertical_scan": {
            "standing_height_minimum_inclusive": standing_height_min,
            "standing_height_maximum_inclusive": standing_height_max,
            "max_support_solid_height_metres": TERRAIN_QUERY_MAX_SUPPORT_HEIGHT_METRES,
            "max_geometry_contributor_height_metres": TERRAIN_QUERY_MAX_SUPPORT_HEIGHT_METRES,
            "max_physics_support_solid_height_metres":
                TERRAIN_QUERY_MAX_PHYSICS_SUPPORT_HEIGHT_METRES,
            "support_voxel_z_minimum_inclusive": support_z_min,
            "support_voxel_z_maximum_inclusive": support_z_max,
            "clearance_voxel_z_maximum_inclusive": clearance_z_max,
            "all_cells_loaded": !has_unknown && !has_unloaded,
        },
        "support_candidates": support_candidates,
        "non_loaded_cells": non_loaded_cells,
        "semantics": {
            "scope": "one avatar-local support column plus proposed actor terrain AABB clearance",
            "voxel_position": "integer lower corner in Veloren world_xyz coordinates",
            "standing_position": "continuous target x/y with z equal to support voxel z plus the block or sprite solid height",
            "clearance": "two one-metre vertical spans above each support surface, each with the complete lower-voxel contributor search range",
            "support_geometry_vs_physics": "geometry taller than the collision broadphase support-height contract is reported but never certified support_usable or occupiable",
            "missing_or_unloaded_is_open": false,
            "route_or_traversal_certificate": false,
            "global_world_absence": false,
        },
    })
}

fn terrain_query_target(
    args: &Value,
    actor_position: Vec3<f32>,
) -> Result<Vec3<f32>, (&'static str, String)> {
    let object = args
        .as_object()
        .ok_or_else(|| ("invalid_request", "args must be an object".to_owned()))?;
    if object.len() != 1 || !object.contains_key("position") {
        return Err((
            "invalid_request",
            "inspect_terrain args must contain only position".to_owned(),
        ));
    }
    let target_position = vec3_arg(args, "position")
        .map_err(|message| ("invalid_request", message))?;
    let horizontal_distance = (target_position.xy() - actor_position.xy()).magnitude();
    let vertical_delta = (target_position.z - actor_position.z).abs();
    if horizontal_distance > TERRAIN_QUERY_MAX_HORIZONTAL_METRES
        || vertical_delta > TERRAIN_QUERY_MAX_VERTICAL_METRES
    {
        return Err((
            "terrain_query_out_of_bounds",
            format!(
                "position must be within {} horizontal metres and {} vertical metres of the actor",
                TERRAIN_QUERY_MAX_HORIZONTAL_METRES, TERRAIN_QUERY_MAX_VERTICAL_METRES,
            ),
        ));
    }
    Ok(target_position)
}

fn real_terrain_block(
    terrain: &common::terrain::TerrainGrid, position: Vec3<i32>,
) -> Result<Option<Block>, &'static str> {
    let key = common::terrain::TerrainGrid::chunk_key(position);
    if !terrain.map_size_lg().contains_chunk(key) {
        return Err("terrain_position_out_of_world_bounds");
    }
    if !terrain.contains_key_real(key) { return Ok(None); }
    Ok(terrain.get(position).ok().copied())
}

fn inspect_terrain(
    client: &Client,
    state: &BridgeState,
    args: &Value,
) -> Result<Value, (&'static str, String)> {
    let actor_position = client.position().ok_or_else(|| {
        (
            "terrain_query_unavailable",
            "current actor position is unavailable".to_owned(),
        )
    })?;
    let target_position = terrain_query_target(args, actor_position)?;
    let identity = self_identity(client);
    let current_observation = observation(client, state);
    let readiness = json!({
        "health": current_observation["health"],
        "in_liquid": current_observation["in_liquid"],
        "on_ground": current_observation["on_ground"],
        "moving": current_observation["moving"],
        "velocity": current_observation["velocity"],
        "character_state": current_observation["character_state"],
        "held_input": current_observation["held_input"],
        "current_action": current_observation["current_action"],
        "fixture_authoring": current_observation["character_controls"]["fixture_authoring"],
    });
    let terrain = client.state().terrain();
    let target_voxel = target_position.map(|value| value.floor() as i32);
    real_terrain_block(&terrain, target_voxel).map_err(|code| (
        code, "target column is outside the actual world; synthetic Air is not writable terrain".into(),
    ))?;
    let mut query = loaded_terrain_support_query(
        state,
        actor_position,
        identity,
        readiness,
        target_position,
        local_motion::actor_shape(client).ok(),
        |position| match real_terrain_block(&terrain, position) {
            Ok(Some(block)) => TerrainCellSample::Loaded(block),
            Ok(None) => TerrainCellSample::Unloaded,
            Err(_) => TerrainCellSample::Unknown,
        },
    );
    query["binding"]["terrain_region"] = json!({
        "in_world": true,
        "real_client_chunk": terrain.contains_key_real(common::terrain::TerrainGrid::chunk_key(target_voxel)),
        "server_write_confirmation": null,
    });
    Ok(query)
}

fn aimed_block(client: &Client, position: Vec3<f32>, look_dir: Dir) -> Option<Value> {
    let from = position + Vec3::unit_z() * 1.5;
    let direction = look_dir.to_vec();
    let mut previous = None;
    for step in 1..=24 {
        let sample = from + direction * (step as f32 * 0.25);
        let voxel = sample.map(|value| value.floor() as i32);
        if previous == Some(voxel) {
            continue;
        }
        previous = Some(voxel);
        if let Ok(block) = client.state().terrain().get(voxel)
            && !block.kind().is_air()
        {
            return Some(block_json(voxel, block));
        }
    }
    None
}

fn nearby_blocks(client: &Client, position: Vec3<f32>) -> Vec<Value> {
    let center = position.map(|value| value.floor() as i32);
    let mut blocks = Vec::with_capacity(MAX_NEARBY_BLOCKS);
    for x in -2..=2 {
        for y in -2..=2 {
            for z in -1..=2 {
                let voxel = center + Vec3::new(x, y, z);
                if let Ok(block) = client.state().terrain().get(voxel)
                    && (!block.kind().is_air()
                        || block
                            .get_sprite()
                            .is_some_and(|sprite| sprite != SpriteKind::Empty))
                    && blocks.len() < MAX_NEARBY_BLOCKS
                {
                    blocks.push(block_json(voxel, block));
                }
            }
        }
    }
    blocks
}

fn is_crafting_station(sprite: SpriteKind) -> bool {
    matches!(
        sprite,
        SpriteKind::Anvil
            | SpriteKind::Cauldron
            | SpriteKind::CookingPot
            | SpriteKind::CraftingBench
            | SpriteKind::Forge
            | SpriteKind::Loom
            | SpriteKind::SpinningWheel
            | SpriteKind::DismantlingBench
            | SpriteKind::RepairBench
            | SpriteKind::TanningRack
    )
}

fn nearby_interactable_sprites(
    client: &Client,
    position: Vec3<f32>,
    radius: i32,
    required_sprite: Option<SpriteKind>,
) -> Vec<NearbyInteractableSprite> {
    let center = position.map(|value| value.floor() as i32);
    let mut sprites = Vec::new();
    let terrain = client.state().terrain();
    terrain.for_each_in(
        Aabb {
            min: center - Vec3::broadcast(radius),
            max: center + Vec3::broadcast(radius),
        },
        |voxel, block| {
            let Some(sprite) = block.get_sprite() else {
                return;
            };
            let crafting_station = is_crafting_station(sprite);
            let directly_collectible = block.is_directly_collectible();
            if !crafting_station && !directly_collectible && Some(sprite) != required_sprite {
                return;
            }
            let voxel_center = voxel.map(|component| component as f32 + 0.5);
            sprites.push(NearbyInteractableSprite {
                voxel,
                sprite,
                distance: (voxel_center - position).magnitude(),
                crafting_station,
                directly_collectible,
            });
        }
    );
    sprites.sort_by(|left, right| {
        left.distance
            .partial_cmp(&right.distance)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    sprites
}

fn nearby_interactable_sprite_json(sprite: NearbyInteractableSprite) -> Value {
    let mut roles = Vec::new();
    if sprite.crafting_station {
        roles.push("crafting_station");
    }
    if sprite.directly_collectible {
        roles.push("directly_collectible");
    }
    json!({
        "sprite": format!("{:?}", sprite.sprite),
        "position": [sprite.voxel.x, sprite.voxel.y, sprite.voxel.z],
        "distance": sprite.distance,
        "roles": roles,
    })
}

fn nearby_interactables_json(
    client: &Client,
    state: &BridgeState,
    position: Vec3<f32>,
) -> Vec<Value> {
    let sprites = state
        .interactable_sprite_scan
        .as_ref()
        .filter(|scan| {
            scan.tick == state.tick
                && scan.position == position
                && scan.radius >= INTERACTABLE_SPRITE_SCAN_RADIUS
        })
        .map(|scan| scan.sprites.clone())
        .unwrap_or_else(|| {
            nearby_interactable_sprites(client, position, INTERACTABLE_SPRITE_SCAN_RADIUS, None)
        });
    sprites
        .into_iter()
        .filter(|sprite| sprite.crafting_station || sprite.directly_collectible)
        .take(MAX_NEARBY_INTERACTABLE_SPRITES)
        .map(nearby_interactable_sprite_json)
        .collect()
}

fn marker_wpos_to_world_xy(wpos: Vec2<f32>) -> Vec2<f32> {
    // World-map generation has already converted site chunk coordinates to blocks before
    // constructing a Marker, so applying the chunk scale here would misplace every site.
    wpos
}

fn bounded_site_name(name: &str) -> String {
    let mut end = name.len().min(MAX_SITE_NAME_BYTES);
    while !name.is_char_boundary(end) {
        end -= 1;
    }
    name[..end].to_owned()
}

fn known_site_records_json(
    sites: impl IntoIterator<Item = KnownSite>,
    character_position: Option<Vec3<f32>>,
) -> Vec<Value> {
    let mut sites = sites
        .into_iter()
        .map(|site| {
            let horizontal_distance = character_position
                .map(|position| (site.wpos - position.xy()).magnitude());
            (site, horizontal_distance)
        })
        .collect::<Vec<_>>();
    sites.sort_by(|(left, left_distance), (right, right_distance)| {
        left_distance
            .unwrap_or(f32::INFINITY)
            .partial_cmp(&right_distance.unwrap_or(f32::INFINITY))
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| {
                left.wpos
                    .x
                    .partial_cmp(&right.wpos.x)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
            .then_with(|| {
                left.wpos
                    .y
                    .partial_cmp(&right.wpos.y)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
            .then_with(|| left.kind.cmp(&right.kind))
            .then_with(|| left.name.cmp(&right.name))
    });
    sites.truncate(MAX_KNOWN_SITES);
    sites
        .into_iter()
        .map(|(site, horizontal_distance)| {
            // Markers have only horizontal coordinates; reporting a height would invent terrain data.
            let map_id = format!("{}@{},{}", site.kind, site.wpos.x, site.wpos.y);
            json!({
                "kind": site.kind,
                "name": site.name,
                "map_id": map_id,
                "position": [site.wpos.x, site.wpos.y, null],
                "horizontal_distance": horizontal_distance,
            })
        })
        .collect()
}

fn map_marker_records_json<'a>(
    markers: impl IntoIterator<Item = &'a common::map::Marker>,
    character_position: Option<Vec3<f32>>,
) -> Vec<Value> {
    known_site_records_json(
        markers.into_iter().map(|marker| KnownSite {
            kind: format!("{:?}", marker.kind),
            name: marker
                .label
                .as_ref()
                .and_then(|label| label.as_plain())
                .map(bounded_site_name),
            wpos: marker_wpos_to_world_xy(marker.wpos),
        }),
        character_position,
    )
}

fn map_cave_records_json<'a>(
    markers: impl IntoIterator<Item = &'a common::map::Marker>,
    character_position: Option<Vec3<f32>>,
) -> Vec<Value> {
    let mut caves = map_marker_records_json(
        markers.into_iter().filter(|marker| matches!(marker.kind, common::map::MarkerKind::Cave)),
        character_position,
    );
    caves.truncate(MAX_KNOWN_MAP_CAVES);
    caves
}

fn known_sites_json(client: &Client, character_position: Option<Vec3<f32>>) -> Vec<Value> {
    map_marker_records_json(client.markers(), character_position)
}

/// Structured body data retains the species nested below a body family.
///
/// `Body`'s Display implementation intentionally returns only the top-level
/// family (for example, `QuadrupedMedium`), which is insufficient to
/// distinguish a wolf from an elephant in nearby-entity observations.
fn body_identity(body: &comp::Body) -> Value {
    serde_json::to_value(body).unwrap_or(Value::Null)
}

fn sender_identity(client: &Client, sender_uid: Option<Uid>) -> Value {
    let alias = sender_uid
        .and_then(|uid| client.player_list().get(&uid))
        .map(|player_info| player_info.player_alias.clone());
    let body = sender_uid
        .and_then(|uid| entity_by_uid(client, uid.0.get()))
        .and_then(|entity| {
            let state = client.state();
            let bodies = state.read_storage::<comp::Body>();
            bodies
                .get(entity)
                .map(|body| (body.to_string(), body_identity(body)))
        });
    let (body, body_identity) = body
        .map(|(family, identity)| (Some(family), Some(identity)))
        .unwrap_or((None, None));
    json!({
        "uid": sender_uid.map(|uid| uid.0.get()),
        "alias": alias,
        "body": body,
        "body_identity": body_identity,
    })
}

fn nearby_entities(client: &Client, bridge: &BridgeState, position: Vec3<f32>) -> Vec<Value> {
    let state = client.state();
    let player_list = client.player_list();
    let entities = state.ecs().entities();
    let positions = state.read_storage::<comp::Pos>();
    let bodies = state.read_storage::<comp::Body>();
    let healths = state.read_storage::<comp::Health>();
    let uids = state.read_storage::<Uid>();
    let pickups = state.read_storage::<PickupItem>();
    let inventories = state.read_storage::<comp::Inventory>();
    let alignments = state.read_storage::<comp::Alignment>();
    let masses = state.read_storage::<comp::Mass>();
    let activities = state.read_storage::<comp::CharacterActivity>();
    let stats = state.read_storage::<comp::Stats>();
    let groups = state.read_storage::<comp::Group>();
    let mounts = state.read_storage::<common::link::Is<common::mounting::Mount>>();
    let inventory = inventories.get(client.entity());
    let own_entity = client.entity();
    let own_uid = client.uid();
    let mut nearby = (&entities, &positions, &bodies)
        .join()
        .filter_map(|(entity, entity_position, body)| {
            if entity == own_entity {
                return None;
            }
            let delta = entity_position.0 - position;
            let distance = delta.magnitude();
            (distance <= 32.0).then(|| {
                let health = healths.get(entity).map(|value| {
                    json!({
                        "current": value.current(),
                        "maximum": value.maximum(),
                        "dead": value.is_dead,
                    })
                });
                let pickup = pickups.get(entity).map(|item| {
                    json!({
                        "id": item.item().item_definition_id().to_owned(),
                        "amount": item.amount(),
                        "kind": item.item().kind().get_itemkind_string(),
                    })
                });
                let alias = uids
                    .get(entity)
                    .and_then(|uid| player_list.get(uid))
                    .map(|player_info| player_info.player_alias.as_str());
                let owned_pet = own_uid.is_some_and(|uid|
                    matches!(alignments.get(entity), Some(comp::Alignment::Owned(owner))
                        if *owner == uid));
                let can_mount = owned_pet && distance < MAX_MOUNT_RANGE
                    && mounts.get(entity).is_none()
                    && healths.get(entity).is_some_and(|h| !h.is_dead)
                    && masses.get(entity).is_some_and(|mass|
                        common::comp::pet::is_mountable(body, mass,
                            bodies.get(own_entity), masses.get(own_entity)));
                json!({
                    "entity_id": entity.id(),
                    "uid": uids.get(entity).map(|uid| uid.0.get()),
                    "control_target": uids.get(entity).map(|uid|
                        continuous_control::entity_ref(bridge, entity, uid.0.get())),
                    "alias": alias,
                    "name": stats.get(entity).map(|stats| &stats.name),
                    "name_text": stats.get(entity).map(|stats| native_interactions::text(&stats.name)),
                    "group": groups.get(entity),
                    "same_native_group": groups.get(own_entity).zip(groups.get(entity))
                        .map(|(ours, theirs)| ours == theirs),
                    "body": body.to_string(),
                    "body_identity": body_identity(body),
                    "position": [entity_position.0.x, entity_position.0.y, entity_position.0.z],
                    "distance": distance,
                    "health": health,
                    "pickup": pickup,
                    "pickable": pickup.is_some() && distance <= MAX_PICKUP_RANGE,
                    "pickup_eligibility": pickups.get(entity).map(|item|
                        pickup_feedback::eligibility(item, inventory, distance)),
                    "companion": {
                        "owned_by_actor": owned_pet,
                        "entity_mount_eligible_now": can_mount,
                        "pet_staying": activities.get(entity).map(|a| a.is_pet_staying),
                        "already_mounted": mounts.get(entity).is_some(),
                        "scope": "current_client_components_server_rechecks_on_submission",
                    },
                })
            })
        })
        .collect::<Vec<_>>();
    nearby.sort_by(|left, right| {
        left["distance"]
            .as_f64()
            .partial_cmp(&right["distance"].as_f64())
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    nearby.truncate(32);
    nearby
}

fn nearby_mountable_volumes(client: &Client, bridge: &BridgeState, position: Vec3<f32>) -> Value {
    let ecs = client.state().ecs();
    let terrain = client.state().terrain();
    let ids = ecs.read_resource::<IdMaps>();
    let colliders = ecs.read_storage::<comp::Collider>();
    let positions = ecs.read_storage::<comp::Pos>();
    let orientations = ecs.read_storage::<comp::Ori>();
    let uids = ecs.read_storage::<Uid>();
    let entities = ecs.entities();
    let mut seats = Vec::new();
    let center = position.map(|component| component.floor() as i32);
    terrain.for_each_in(Aabb {
        min: center - Vec3::broadcast(3),
        max: center + Vec3::broadcast(3),
    }, |voxel, block| {
        if block.mount_offset().is_some() {
            let volume = VolumePos::terrain(voxel);
            if let Some((mat, _)) = volume.get_mount_mat(&terrain, &ids,
                |entity| positions.get(entity).copied()
                    .zip(orientations.get(entity).copied()), &colliders)
            {
                let world = mat.mul_point(Vec3::zero());
                if position.distance_squared(world) < MAX_SPRITE_MOUNT_RANGE.powi(2) {
                    seats.push(json!({
                        "kind": "terrain", "position": [voxel.x, voxel.y, voxel.z],
                        "carrier": null, "world_position": [world.x, world.y, world.z],
                    }));
                }
            }
        }
    });
    let voxel_colliders = common::comp::ship::figuredata::VOXEL_COLLIDER_MANIFEST.read();
    for (entity, carrier_position, orientation, collider, uid) in
        (&entities, &positions, &orientations, &colliders, &uids).join()
    {
        if entity == client.entity() || position.distance_squared(carrier_position.0) > 256.0 {
            continue;
        }
        let Some(volume) = collider.get_vol(&voxel_colliders) else { continue };
        let shape = volume.volume().sz.map(|value| value as i32);
        let transform = vek::Mat4::<f32>::from(orientation.to_quat())
            .translated_3d(carrier_position.0)
            * vek::Mat4::translation_3d(volume.translation);
        let local = transform.inverted().mul_point(position);
        let start = (local.map(|component| component.floor() as i32)
            - Vec3::broadcast(4)).map(|component| component.max(0));
        let end = (local.map(|component| component.floor() as i32)
            + Vec3::broadcast(4)).map2(shape - Vec3::one(), i32::min);
        for x in start.x..=end.x {
            for y in start.y..=end.y {
                for z in start.z..=end.z {
                    let voxel = Vec3::new(x, y, z);
                    let mount = VolumePos::entity(voxel, *uid);
                    if let Some((mat, _)) = mount.get_mount_mat(&terrain, &ids,
                        |entity| positions.get(entity).copied()
                            .zip(orientations.get(entity).copied()), &colliders)
                    {
                        let world = mat.mul_point(Vec3::zero());
                        if position.distance_squared(world) < MAX_SPRITE_MOUNT_RANGE.powi(2) {
                            seats.push(json!({
                                "kind": "entity", "position": [x, y, z],
                                "carrier": continuous_control::entity_ref(
                                    bridge, entity, uid.0.get()),
                                "world_position": [world.x, world.y, world.z],
                            }));
                        }
                    }
                }
            }
        }
    }
    let total = seats.len();
    seats.truncate(64);
    json!({"seats": seats, "selected": seats.len(), "total": total,
           "truncated": total > seats.len(),
           "scope": "currently_loaded_nearby_terrain_and_entity_volume_mount_points"})
}

fn self_identity(client: &Client) -> Value {
    let entity = client.entity();
    let uid = client.uid();
    let alias = uid
        .and_then(|uid| client.player_list().get(&uid))
        .map(|player_info| player_info.player_alias.as_str());
    let mut identity = self_identity_record(entity.id(), uid.map(|uid| uid.0.get()), alias);
    identity["native_character_id"] = match client.presence() {
        Some(comp::PresenceKind::Character(id)) => json!(id),
        _ => Value::Null,
    };
    identity
}

fn social_and_trade_snapshot(client: &Client) -> Value {
    let players = client.player_list();
    let mut player_rows = players.iter().map(|(uid, info)| json!({
        "uid": uid.0.get(), "alias": info.player_alias,
    })).collect::<Vec<_>>();
    player_rows.sort_by_key(|row| row["uid"].as_u64());
    let mut members = client.group_members().iter().map(|(uid, role)| json!({
        "uid": uid.0.get(), "role": format!("{role:?}"),
    })).collect::<Vec<_>>();
    members.sort_by_key(|row| row["uid"].as_u64());
    let mut outgoing = client.pending_invites().iter().map(|uid| uid.0.get()).collect::<Vec<_>>();
    outgoing.sort_unstable();
    let group = client.group_info().map(|(_, leader)| json!({
        "leader_uid": leader.0.get(),
        "self_is_leader": client.uid() == Some(leader),
        "members": members,
        "max_members": client.max_group_size(),
    }));
    let incoming = client.invite().map(|(uid, started, lifetime, kind)| json!({
        "inviter_uid": uid.0.get(), "kind": format!("{kind:?}"),
        "remaining_seconds": lifetime.saturating_sub(started.elapsed()).as_secs_f64(),
    }));
    let trade = client.pending_trade().as_ref().map(|(id, pending, prices)| {
        let ours = client.uid().and_then(|uid| pending.parties.iter()
            .position(|party| *party == uid));
        let offers = pending.offers.iter().map(|offer| {
            let mut rows = offer.iter().map(|(slot, amount)| json!({
                "slot": slot.idx(), "quantity": amount,
            })).collect::<Vec<_>>();
            rows.sort_by_key(|row| row["slot"].as_u64());
            rows
        }).collect::<Vec<_>>();
        let inventories = client.inventories();
        let party_inventories = pending.parties.map(|uid|
            entity_by_uid(client, uid.0.get()).and_then(|entity| inventories.get(entity)));
        let reduced = party_inventories.map(|inventory|
            inventory.map(common::trade::ReducedInventory::from));
        let stock = party_inventories.iter().enumerate().map(|(who, inventory)| {
            inventory.map(|inventory| inventory.slots_with_id().filter_map(|(slot, item)| {
                item.as_ref().map(|item| {
                    let mut row = item_json(item);
                    row["slot"] = json!(slot.idx());
                    // Same valuation routine as the normal trade UI; null is not a free item.
                    let mut unit_offer: [hashbrown::HashMap<InvSlotId, u32>; 2] =
                        Default::default();
                    unit_offer[who].insert(slot, 1);
                    row["unit_site_value"] = json!(prices.as_ref().and_then(|prices|
                        prices.balance(&unit_offer, &reduced, who, ours == Some(who))));
                    row
                })
            }).collect::<Vec<_>>())
        }).collect::<Vec<_>>();
        let price_rows = prices.as_ref().map(|prices| {
            let mut rows = prices.values.iter().map(|(good, value)| json!({
                "good": good, "value": value,
            })).collect::<Vec<_>>();
            rows.sort_by_key(|row| row["good"].to_string());
            rows
        });
        json!({
            "trade_id": id, "parties": pending.parties.map(|uid| uid.0.get()),
            "self_party_index": ours, "phase": format!("{:?}", pending.phase),
            "offers": offers, "accept_flags": pending.accept_flags,
            "inventories": stock, "site_prices": price_rows,
            "offer_site_values": ([0, 1].map(|who| prices.as_ref().and_then(|prices|
                reduced[who].as_ref().and_then(|_|
                    prices.balance(&pending.offers, &reduced, who, ours == Some(who)))))),
            "price_scope": "native_SitePrices_balance;player_sells_discounted;not_free_negotiation",
            "effect_scope": "synced_pending_trade_not_completion",
        })
    });
    json!({
        "players": player_rows,
        "group": group,
        "incoming_invite": incoming,
        "outgoing_invitee_uids": outgoing,
        "pending_trade": trade,
        "scope": "current_client_social_and_trade_state_server_rechecks",
    })
}

fn selected_trade_party(
    action: &serde_json::Map<String, Value>,
    actor_uid: Uid,
    id: common::trade::TradeId,
    trade: &common::trade::PendingTrade,
) -> Result<usize, &'static str> {
    let ours = trade.parties.iter().position(|uid| *uid == actor_uid)
        .ok_or("actor is not a participant in this trade")?;
    if action.get("trade_id") != Some(&json!(id))
        || action.get("counterparty_uid").and_then(Value::as_u64)
            != Some(trade.parties[1 - ours].0.get())
        || action.get("phase").and_then(Value::as_str) != Some(match trade.phase {
            common::trade::TradePhase::Mutate => "Mutate",
            common::trade::TradePhase::Review => "Review",
            common::trade::TradePhase::Complete => "Complete",
        })
    {
        return Err("selected trade, counterparty or phase no longer matches current state");
    }
    Ok(ours)
}

fn skill_snapshot(client: &Client) -> Value {
    use common::comp::skillset::SKILL_GROUP_DEFS;
    let Some(skills) = client.current::<comp::SkillSet>() else {
        return Value::Null;
    };
    let mut groups: Vec<Value> = SKILL_GROUP_DEFS.iter().map(|(group, definition)| {
        let mut available: Vec<Value> = definition.skills.iter()
            .filter(|skill| !skills.is_at_max_level(**skill)
                && skills.prerequisites_met(**skill)
                && skills.sufficient_skill_points(**skill))
            .map(|skill| json!(skill))
            .collect();
        available.sort_by_key(|skill| skill.to_string());
        json!({"group": group, "available_points": skills.available_sp(*group),
               "earned_points": skills.earned_sp(*group),
               "accessible": skills.skill_group_accessible(*group),
               "skills": ({
                   let mut rows = definition.skills.iter().map(|skill| json!({
                       "skill": skill, "learned_level": skills.skill_level(*skill).ok(),
                       "maximum_level": skill.max_level(), "next_cost": skills.skill_cost(*skill),
                       "prerequisites": native_interactions::prerequisites(*skill),
                       "prerequisites_met": skills.prerequisites_met(*skill),
                       "at_maximum": skills.is_at_max_level(*skill),
                   })).collect::<Vec<_>>();
                   rows.sort_by_key(|row| row["skill"].to_string());
                   rows
               }),
               "unlockable_now": available})
    }).collect();
    groups.sort_by_key(|group| group["group"].to_string());
    json!({"groups": groups, "scope": "sampled_skillset_prerequisites_cost_and_points"})
}

fn auxiliary_ability_snapshot(client: &Client) -> Value {
    let Some(abilities) = client.current::<comp::ActiveAbilities>() else {
        return Value::Null;
    };
    let inventory = client.current::<comp::Inventory>();
    let skills = client.current::<comp::SkillSet>();
    json!({
        "active_set": abilities.auxiliary_set(inventory.as_ref(), skills.as_ref()).as_ref(),
        "available": comp::ActiveAbilities::all_available_abilities(
            inventory.as_ref(), skills.as_ref()),
        "slot_limit": abilities.limit,
        "scope": "observed_active_loadout_and_skill_eligible_auxiliary_abilities",
    })
}

fn component_recipe_snapshot(client: &Client) -> Value {
    let Some(inventory) = client.current::<comp::Inventory>() else {
        return Value::Null;
    };
    let mut records = Vec::new();
    for (key, recipe) in client.component_recipe_book().iter() {
        if !inventory.recipe_is_known(&recipe.recipe_book_key) { continue; }
        for (material_slot, slot) in inventory.slots_with_id() {
            let matches_material = slot.as_ref().is_some_and(|item|
                item.item_definition_id().itemdef_id() == Some(key.material.as_str()));
            if !matches_material { continue; }
            let modifiers: Vec<Option<InvSlotId>> = if let Some(modifier) = &key.modifier {
                inventory.slots_with_id()
                    .filter_map(|(slot_id, slot)| slot.as_ref()
                        .filter(|item| item.item_definition_id().itemdef_id() == Some(modifier.as_str()))
                        .map(|_| Some(slot_id))).collect()
            } else { vec![None] };
            for modifier in modifiers {
                records.push(json!({
                    "toolkind": key.toolkind,
                    "material_slot": material_slot.idx(),
                    "modifier_slot": modifier.map(|slot| slot.idx()),
                    "required_station_sprite": recipe.craft_sprite.map(|sprite| format!("{sprite:?}")),
                    "additional_ingredients_available":
                        recipe.inventory_contains_additional_ingredients(&inventory).is_ok(),
                    "recipe_key": recipe.recipe_book_key,
                }));
            }
        }
    }
    records.sort_by_key(|record| record.to_string());
    json!({"known_material_recipes": records,
           "scope": "currently_owned_material_modifier_and_known_recipe;server_rechecks"})
}

fn dialogue_snapshot(state: &BridgeState) -> Value {
    if state.dialogue.is_some() { state.interactions.prompt.clone() } else { Value::Null }
}

// RemoveBuff is kind-wide on the server. Never send it if even one currently
// observed Invulnerability is not in the operator's explicit ownership set.
// The server still restricts removal to this authenticated client's own entity.
fn owned_invulnerability_removal(
    action: &serde_json::Map<String, Value>, identity: &Value, buffs: &Value,
) -> Result<bool, &'static str> {
    if action.keys().any(|key| !matches!(key.as_str(),
        "type" | "action_id" | "expected_self" | "owned_ids"))
        || identity["uid"].as_u64().is_none()
        || identity["native_character_id"].as_i64().is_none()
        || action.get("expected_self") != Some(identity)
    {
        return Err("expected authenticated self identity mismatch");
    }
    let owned = action.get("owned_ids").and_then(Value::as_array)
        .ok_or("owned_ids required")?;
    if owned.is_empty() || owned.iter().any(|id| id.as_str().is_none_or(str::is_empty))
        || owned.iter().enumerate().any(|(i, id)| owned[..i].contains(id))
    {
        return Err("nonempty unique buff IDs required");
    }
    let buffs = buffs.as_array().ok_or("native buffs unavailable")?;
    let mut present = false;
    for buff in buffs.iter().filter(|buff| buff["kind"] == "Invulnerability") {
        if !owned.contains(&buff["id"]) {
            return Err("unowned Invulnerability present; no removal sent");
        }
        present = true;
    }
    Ok(present)
}

fn current_site_json(client: &Client) -> Value {
    let chunk = client.current_chunk();
    json!({
        "kind": format!("{:?}", client.current_site()),
        "location_name": chunk.as_ref().and_then(|chunk| chunk.meta().name()),
        "loaded_chunk": chunk.is_some(),
        "source": "native_current_site_and_loaded_chunk",
    })
}

fn self_identity_record(entity_id: u32, uid: Option<u64>, alias: Option<&str>) -> Value {
    json!({
        "entity_id": entity_id,
        "uid": uid,
        "alias": alias,
    })
}

fn entity_by_uid(client: &Client, uid_value: u64) -> Option<Entity> {
    let uid = Uid(NonZeroU64::new(uid_value)?);
    let state = client.state();
    let entities = state.ecs().entities();
    let uids = state.read_storage::<Uid>();
    (&entities, &uids)
        .join()
        .find(|(_, candidate)| **candidate == uid)
        .map(|(entity, _)| entity)
}

fn nearest_pickup(client: &Client, max_range: f32) -> Option<(Entity, u64, f32)> {
    let position = client.position()?;
    let state = client.state();
    let entities = state.ecs().entities();
    let positions = state.read_storage::<comp::Pos>();
    let pickups = state.read_storage::<PickupItem>();
    let uids = state.read_storage::<Uid>();
    (&entities, &positions, &pickups, &uids)
        .join()
        .map(|(entity, entity_position, _, uid)| {
            let distance = (entity_position.0 - position).magnitude();
            (entity, uid.0.get(), distance)
        })
        .filter(|(_, _, distance)| *distance <= max_range)
        .min_by(|left, right| {
            left.2
                .partial_cmp(&right.2)
                .unwrap_or(std::cmp::Ordering::Equal)
        })
}

fn health_of_entity(client: &Client, entity: Entity) -> Option<f32> {
    client
        .state()
        .read_storage::<comp::Health>()
        .get(entity)
        .map(|health| health.fraction())
}

fn equipped_item_id(client: &Client, equip_slot: EquipSlot) -> Option<String> {
    client
        .current::<comp::Inventory>()?
        .equipped(equip_slot)
        .map(|item| match item.item_definition_id().to_owned() {
            common::comp::inventory::item::ItemDefinitionIdOwned::Simple(id) => id,
            other => format!("{other:?}"),
        })
}

fn inv_slot_from_action(
    action: &serde_json::Map<String, Value>,
) -> Result<InvSlotId, String> {
    if let Some(slot) = action.get("slot") {
        let slot = slot
            .as_u64()
            .ok_or_else(|| "slot must be a non-negative integer".to_owned())?;
        if slot > u32::MAX as u64 {
            return Err("slot is out of range".to_owned());
        }
        let slot = slot as u32;
        return Ok(InvSlotId::new((slot >> 16) as u16, slot as u16));
    }
    let loadout_idx = action
        .get("loadout_idx")
        .and_then(Value::as_u64)
        .ok_or_else(|| "slot or loadout_idx/slot_idx is required".to_owned())?;
    let slot_idx = action
        .get("slot_idx")
        .and_then(Value::as_u64)
        .ok_or_else(|| "slot_idx is required when loadout_idx is set".to_owned())?;
    if loadout_idx > u16::MAX as u64 || slot_idx > u16::MAX as u64 {
        return Err("loadout_idx/slot_idx out of range".to_owned());
    }
    Ok(InvSlotId::new(loadout_idx as u16, slot_idx as u16))
}

fn parse_equip_slot(name: &str) -> Option<EquipSlot> {
    Some(match name {
        "active_mainhand" => EquipSlot::ActiveMainhand,
        "active_offhand" => EquipSlot::ActiveOffhand,
        "inactive_mainhand" => EquipSlot::InactiveMainhand,
        "inactive_offhand" => EquipSlot::InactiveOffhand,
        "lantern" => EquipSlot::Lantern,
        "glider" => EquipSlot::Glider,
        "head" => EquipSlot::Armor(ArmorSlot::Head),
        "neck" => EquipSlot::Armor(ArmorSlot::Neck),
        "shoulders" => EquipSlot::Armor(ArmorSlot::Shoulders),
        "chest" => EquipSlot::Armor(ArmorSlot::Chest),
        "hands" => EquipSlot::Armor(ArmorSlot::Hands),
        "ring1" => EquipSlot::Armor(ArmorSlot::Ring1),
        "ring2" => EquipSlot::Armor(ArmorSlot::Ring2),
        "back" => EquipSlot::Armor(ArmorSlot::Back),
        "belt" => EquipSlot::Armor(ArmorSlot::Belt),
        "legs" => EquipSlot::Armor(ArmorSlot::Legs),
        "feet" => EquipSlot::Armor(ArmorSlot::Feet),
        "tabard" => EquipSlot::Armor(ArmorSlot::Tabard),
        "bag1" => EquipSlot::Armor(ArmorSlot::Bag1),
        "bag2" => EquipSlot::Armor(ArmorSlot::Bag2),
        "bag3" => EquipSlot::Armor(ArmorSlot::Bag3),
        "bag4" => EquipSlot::Armor(ArmorSlot::Bag4),
        _ => return None,
    })
}

/// Shared protocol vocabulary for native HUD event normalization.
pub fn equip_slot_for_name(name: &str) -> Option<EquipSlot> { parse_equip_slot(name) }

pub fn voxel_arg(action: &serde_json::Map<String, Value>, field: &str) -> Result<Vec3<i32>, String> {
    let values = action
        .get(field)
        .and_then(Value::as_array)
        .ok_or_else(|| format!("{field} must be a three-integer array"))?;
    if values.len() != 3 {
        return Err(format!("{field} must contain exactly three integers"));
    }
    let mut components = [0_i32; 3];
    for (index, value) in values.iter().enumerate() {
        let number = value
            .as_i64()
            .or_else(|| value.as_f64().map(|float| float.floor() as i64))
            .ok_or_else(|| format!("{field}[{index}] must be a number"))?;
        if !(i32::MIN as i64..=i32::MAX as i64).contains(&number) {
            return Err(format!("{field}[{index}] is out of i32 range"));
        }
        components[index] = number as i32;
    }
    Ok(Vec3::new(components[0], components[1], components[2]))
}

fn integer_voxel_arg(
    action: &serde_json::Map<String, Value>,
    field: &str,
) -> Result<Vec3<i32>, String> {
    let values = action
        .get(field)
        .and_then(Value::as_array)
        .ok_or_else(|| format!("{field} must be a three-integer array"))?;
    if values.len() != 3 {
        return Err(format!("{field} must contain exactly three integers"));
    }
    let mut components = [0_i32; 3];
    for (index, value) in values.iter().enumerate() {
        let number = value
            .as_i64()
            .ok_or_else(|| format!("{field}[{index}] must be an integer"))?;
        if !(i32::MIN as i64..=i32::MAX as i64).contains(&number) {
            return Err(format!("{field}[{index}] is out of i32 range"));
        }
        components[index] = number as i32;
    }
    Ok(Vec3::new(components[0], components[1], components[2]))
}

/// The craft dispatcher's optional station, using its existing strict parser.
pub fn craft_station_arg(
    action: &serde_json::Map<String, Value>,
) -> Result<Option<Vec3<i32>>, String> {
    action.get("station").map(|_| integer_voxel_arg(action, "station")).transpose()
}

fn build_request(
    action: &serde_json::Map<String, Value>,
    permission: Option<(bool, usize)>,
    current: Option<Block>,
) -> Result<Option<Block>, &'static str> {
    if action.keys().any(|key| !matches!(key.as_str(),
        "type" | "action_id" | "operation" | "position" | "expected_kind"
        | "new_kind" | "color_rgb"))
    {
        return Err("unknown build action field");
    }
    let (enabled, areas) = permission.ok_or("build permission state unavailable")?;
    if !enabled { return Err("actor does not have enabled CanBuild permission"); }
    if areas == 0 { return Err("actor has no assigned build areas"); }
    let current = current.ok_or("target voxel is not loaded")?;
    let expected = action.get("expected_kind").and_then(Value::as_str)
        .ok_or("expected_kind must be the currently observed block kind")?;
    if current.kind().to_string() != expected {
        return Err("target voxel kind changed since observation");
    }
    match action.get("operation").and_then(Value::as_str) {
        Some("remove") if !action.contains_key("new_kind") && !action.contains_key("color_rgb")
            && !current.kind().is_air() => Ok(None),
        Some("place") => {
            let kind = match action.get("new_kind").and_then(Value::as_str) {
                Some("Rock") => BlockKind::Rock,
                Some("Grass") => BlockKind::Grass,
                Some("Earth") => BlockKind::Earth,
                Some("Sand") => BlockKind::Sand,
                Some("Wood") => BlockKind::Wood,
                Some("Ice") => BlockKind::Ice,
                _ => return Err("new_kind must be an allowed solid block kind"),
            };
            let rgb: [u8; 3] = serde_json::from_value(
                action.get("color_rgb").cloned().ok_or("place requires color_rgb")?,
            ).map_err(|_| "color_rgb must contain exactly three byte components")?;
            Ok(Some(Block::new(kind, Rgb::new(rgb[0], rgb[1], rgb[2]))))
        },
        _ => Err("invalid build operation or fields for current target"),
    }
}

fn craft_station_distance_from_cylinder(
    character: find_dist::Cylinder,
    station: Vec3<i32>,
) -> f32 {
    let station_center = station.map(|component| component as f32 + 0.5);
    station_center.min_distance(character)
}

fn craft_station_distance(client: &Client, station: Vec3<i32>) -> Option<f32> {
    let position = client.position()?;
    let entity = client.entity();
    let state = client.state();
    let scales = state.read_storage::<comp::Scale>();
    let colliders = state.read_storage::<comp::Collider>();
    let character_states = state.read_storage::<comp::CharacterState>();
    let character = find_dist::Cylinder::from_components(
        position,
        scales.get(entity).copied(),
        colliders.get(entity),
        character_states.get(entity),
    );
    Some(craft_station_distance_from_cylinder(character, station))
}

fn craft_station_in_range(distance: f32) -> bool { distance < MAX_PICKUP_RANGE }

fn clear_control_effects_with(state: &mut BridgeState, mut release: impl FnMut(InputKind)) {
    if let Some(input) = state.held_input.take() {
        release(input);
    }
    state.break_block_pos = None;
}

fn clear_control_effects(client: &mut Client, state: &mut BridgeState) {
    clear_control_effects_with(state, |input| client.handle_input(input, false, None, None));
}

fn release_jump_with(state: &mut BridgeState, mut release: impl FnMut(InputKind)) {
    if state.release_jump {
        release(InputKind::Jump);
        state.release_jump = false;
    }
    if state.release_wall_jump {
        release(InputKind::WallJump);
        state.release_wall_jump = false;
    }
}

fn prepare_sit_controls(state: &mut BridgeState, mut release: impl FnMut(InputKind)) {
    clear_control_effects_with(state, &mut release);
    release_jump_with(state, release);
    state.persistent_move = Vec2::zero();
}

fn sit_pending(state: &BridgeState) -> bool {
    matches!(
        state.active.as_ref().map(|active| &active.kind),
        Some(ActiveKind::InteractWait { mode: InteractWait::Sit })
    )
}

/// Canonical private map action parser, shared with native HUD interop tests.
/// Keep the wire format strict: vectors are integer arrays, never serde objects.
pub fn map_marker_request(action: &serde_json::Map<String, Value>)
    -> Result<comp::MapMarkerChange, &'static str>
{
    let change = match action.get("operation").and_then(Value::as_str) {
        Some("remove") if !action.contains_key("world_xy") => comp::MapMarkerChange::Remove,
        Some("update") => {
            let point = action.get("world_xy").and_then(Value::as_array);
            if let Some([x, y]) = point.map(Vec::as_slice)
                && let (Some(x), Some(y)) = (x.as_i64(), y.as_i64())
                && let (Ok(x), Ok(y)) = (i32::try_from(x), i32::try_from(y))
            {
                comp::MapMarkerChange::Update(Vec2::new(x, y))
            } else {
                return Err("map marker world_xy must contain two signed 32-bit integers");
            }
        },
        _ => return Err("map marker requires update with world_xy or remove without coordinates"),
    };
    if action.keys().any(|key| !matches!(key.as_str(),
        "type" | "action_id" | "operation" | "world_xy"))
    {
        return Err("unknown map marker field");
    }
    Ok(change)
}

fn sit_action_has_no_parameters(action: &serde_json::Map<String, Value>) -> bool {
    action.keys().all(|field| field == "action_id" || field == "type")
}

/// Only an exact Sit state completes. Food's `is_sitting()` animation, a sent
/// toggle, and proximity to a campfire are not completion/save evidence.
fn sit_outcome(
    character: Option<&comp::CharacterState>,
    health: Option<&comp::Health>,
    deadline_reached: bool,
) -> Option<(&'static str, Value)> {
    let code = if health.is_some_and(|health| health.is_dead) {
        Some("character_dead")
    } else if comp::is_downed(health, character) {
        Some("character_downed")
    } else if character.is_none() || health.is_none() {
        Some("state_unavailable")
    } else if deadline_reached {
        Some("action_timeout")
    } else {
        None
    };
    let sitting = character.map(|character| matches!(character, comp::CharacterState::Sit));
    let mut detail = json!({
        "observed_sitting": sitting,
        "character_state": character.map(ToString::to_string),
    });
    if let Some(code) = code {
        detail["code"] = json!(code);
        Some(("failed", detail))
    } else if sitting == Some(true) {
        Some(("completed", detail))
    } else {
        None
    }
}

#[derive(Debug, PartialEq)]
enum SitControl {
    Release(InputKind),
    Toggle,
}

fn begin_sit_with(
    state: &mut BridgeState,
    character: Option<&comp::CharacterState>,
    health: Option<&comp::Health>,
    mut send: impl FnMut(SitControl),
) -> Option<(&'static str, Value)> {
    prepare_sit_controls(state, |input| send(SitControl::Release(input)));
    let outcome = sit_outcome(character, health, false);
    if outcome.is_none() {
        send(SitControl::Toggle);
    }
    outcome
}

fn with_current_observation_in_liquid(
    mut observation: Value,
    physics: Option<&comp::PhysicsState>,
) -> Value {
    let object = observation
        .as_object_mut()
        .expect("current observation must serialize as an object");
    object.insert(
        "in_liquid".to_owned(),
        json!(physics.map(|value| value.in_liquid().is_some())),
    );
    observation
}

pub fn observation(client: &Client, state: &BridgeState) -> Value {
    let position = client.position();
    let inventory = client.current::<comp::Inventory>();
    let known_recipes = known_recipes_json(client, inventory.as_ref());
    let velocity = client
        .current::<comp::Vel>()
        .map(|value| [value.0.x, value.0.y, value.0.z]);
    let look = client.current::<comp::Ori>().map(|orientation| {
        let direction = orientation.look_dir().to_vec();
        [direction.x, direction.y, direction.z]
    });
    let desired_look = state.look_dir.to_vec();
    let health = client.current::<comp::Health>().map(|value| {
        json!({
            "current": value.current(),
            "maximum": value.maximum(),
            "fraction": value.fraction(),
            "dead": value.is_dead,
        })
    });
    let energy = client.current::<comp::Energy>().map(|value| {
        json!({
            "current": value.current(),
            "maximum": value.maximum(),
            "fraction": value.fraction(),
        })
    });
    let physics = client.current::<comp::PhysicsState>();
    let on_ground = physics
        .as_ref()
        .map(|value| value.on_ground.is_some());
    let moving =
        velocity.map(|value| Vec3::new(value[0], value[1], value[2]).magnitude_squared() > 0.0025);
    let character_state = client
        .current::<comp::CharacterState>()
        .map(|value| value.to_string());
    let lantern = inventory.as_ref()
        .and_then(|value| value.equipped(EquipSlot::Lantern));
    let light = client.current::<comp::LightEmitter>();
    let matching_lantern_light = lantern.as_ref().zip(light.as_ref())
        .is_some_and(|(item, emitter)| match &*item.kind() {
            ItemKind::Lantern(lantern) => emitter.animated
                && emitter.col == lantern.color()
                && emitter.strength == lantern.strength()
                && emitter.flicker == lantern.flicker()
                && emitter.dir == lantern.dir,
            _ => false,
        });
    let position_json = position.map(|value| [value.x, value.y, value.z]);
    let local = position
        .map(|value| {
            let voxel = value.map(|component| component.floor() as i32);
            let under = voxel - Vec3::unit_z();
            json!({
                "block_under": block_at(client, under),
                "aimed_block": aimed_block(client, value, state.look_dir),
                "nearby_blocks": nearby_blocks(client, value),
                "loaded_doors": local_doors::observe(client, value),
                "nearby_interactable_sprites": nearby_interactables_json(client, state, value),
                "known_sites": known_sites_json(client, position),
                "known_site_total": client.markers().count(),
                "known_map_caves": map_cave_records_json(client.markers(), position),
                "known_map_cave_total": client.markers()
                    .filter(|marker| matches!(marker.kind, common::map::MarkerKind::Cave)).count(),
                "nearby_entities": nearby_entities(client, state, value),
                "nearby_mountable_volumes": nearby_mountable_volumes(client, state, value),
            })
        })
        .unwrap_or_else(|| {
            json!({
                "block_under": null,
                "aimed_block": null,
                "nearby_blocks": [],
                "loaded_doors": null,
                "nearby_interactable_sprites": [],
                "known_sites": known_sites_json(client, position),
                "known_site_total": client.markers().count(),
                "known_map_caves": map_cave_records_json(client.markers(), position),
                "known_map_cave_total": client.markers()
                    .filter(|marker| matches!(marker.kind, common::map::MarkerKind::Cave)).count(),
                "nearby_entities": [],
                "nearby_mountable_volumes": null,
            })
        });

    let mut snapshot = with_current_observation_in_liquid(
        json!({
            "protocol_version": PROTOCOL_VERSION,
            "observation_schema_version": OBSERVATION_SCHEMA_VERSION,
            "session_id": state.session_id,
            "tick": state.tick,
            "coordinate_frame": {
                "position": "world_xyz_meters",
                "movement": "world_xy",
                "z_axis": "up",
                "look": "unit_vector_xyz",
            },
            "identifier_scopes": {
                "entity_id": "session",
                "item_definition_id": "game_definition",
                "block_kind": "game_definition",
            },
            "self_identity": self_identity(client),
            "control_actor": continuous_control::actor_ref(client, state),
            "continuous_control": state.continuous.observation(),
            "combat_perception": combat_perception::observation(
                client, state, local["nearby_entities"].as_array().unwrap()),
            "local_geometry": local_motion::observation(client, state),
            "current_site": current_site_json(client),
            "position": position_json,
            "velocity": velocity,
            "look": look,
            "desired_look": [desired_look.x, desired_look.y, desired_look.z],
            "health": health,
            "energy": energy,
            "survival": survival_snapshot(client, state),
            "inventory": inventory_json(inventory.as_ref()),
            "character_state": character_state,
            "character_controls": {
                "lantern_equipped": lantern.is_some(),
                "matching_lantern_light_emitter": matching_lantern_light,
                "light_emitter_present": light.is_some(),
                "light_scope": "sampled_ECS_equipped_lantern_match_not_exclusive_source_proof",
                "glider_equipped": inventory.as_ref()
                    .and_then(|value| value.equipped(EquipSlot::Glider)).is_some(),
                "water_contact": continuous_control::water_contact(physics.as_ref()),
                "flight_eligible_now": continuous_control::flight_eligibility(client),
                "riding": client.is_riding(),
                "mounted_entity_uid": client.current::<common::link::Is<common::mounting::Rider>>()
                    .map(|rider| rider.mount.0.get()),
                "mounted_volume": client.current::<common::link::Is<common::mounting::VolumeRider>>()
                    .map(|rider| json!({
                        "kind": match rider.pos.kind {
                            Volume::Terrain => "terrain", Volume::Entity(_) => "entity",
                        },
                        "carrier_uid": match rider.pos.kind {
                            Volume::Terrain => None, Volume::Entity(uid) => Some(uid.0.get()),
                        },
                        "position": [rider.pos.pos.x, rider.pos.pos.y, rider.pos.pos.z],
                    })),
                "can_build": client.current::<comp::CanBuild>()
                    .map(|permission| json!({
                        "enabled": permission.enabled,
                        "assigned_area_count": permission.build_areas.len(),
                        "target_area_membership": null,
                        "scope": "sampled_permission_only_server_checks_exact_area",
                    })),
                "fixture_authoring": client.current::<comp::FixtureAuthoring>()
                    .map(|hold| json!({
                        "owner": hold.owner,
                        "position": [hold.position.0.x, hold.position.0.y, hold.position.0.z],
                        "restore_position": [hold.restore_position.0.x, hold.restore_position.0.y, hold.restore_position.0.z],
                        "restore_velocity": [hold.restore_velocity.0.x, hold.restore_velocity.0.y, hold.restore_velocity.0.z],
                        "scope": "server_authorized_fixture_administrator_only_not_grounded_evidence",
                    })),
            },
            "personal_map_marker": client.current::<comp::MapMarker>()
                .map(|marker| json!({"world_xy": [marker.0.x, marker.0.y],
                                     "scope": "observed_self_personal_marker"})),
            "social_and_trade": social_and_trade_snapshot(client),
            "wielding": client.is_wielding(),
            "held_input": state.held_input.map(|input| format!("{input:?}")),
            "break_block_pos": state.break_block_pos.map(|pos| [pos.x, pos.y, pos.z]),
            "on_ground": on_ground,
            "moving": moving,
            "current_action": state.active.as_ref().map(|action| json!({
                "action_id": action.action_id,
                "type": action.action_type,
                "status": "running",
                "started_tick": action.started_tick,
                "deadline_tick": action.deadline_tick,
            })),
            "block_under": local["block_under"],
            "aimed_block": local["aimed_block"],
            "nearby_blocks": local["nearby_blocks"],
            "loaded_doors": local["loaded_doors"],
            "nearby_interactable_sprites": local["nearby_interactable_sprites"],
            "known_recipes": known_recipes,
            "known_sites": local["known_sites"],
            "known_map_caves": local["known_map_caves"],
            "nearby_entities": local["nearby_entities"],
            "nearby_mountable_volumes": local["nearby_mountable_volumes"],
        }),
        physics.as_ref(),
    );
    snapshot["sequence_domain"] = json!({"kind": "client-update-counter", "session_id": state.session_id});
    snapshot["event_cursor"] = json!(state.next_event_seq - 1);
    snapshot["known_site_total"] = local["known_site_total"].clone();
    snapshot["known_map_cave_total"] = local["known_map_cave_total"].clone();
    snapshot["skill_tree"] = skill_snapshot(client);
    snapshot["auxiliary_abilities"] = auxiliary_ability_snapshot(client);
    snapshot["component_recipes"] = component_recipe_snapshot(client);
    snapshot["dialogue"] = dialogue_snapshot(state);
    snapshot["dialogue_markers"] = json!(state.interactions.markers);
    snapshot["last_trade_result"] = state.interactions.last_trade_result.clone();
    snapshot["rpg_interactions"] = native_interactions::capability();
    snapshot
}

fn horizontal_look(look_dir: Dir) -> Vec2<f32> {
    let horizontal = look_dir.to_vec().xy();
    if horizontal.magnitude_squared() > 0.0001 {
        horizontal.normalized()
    } else {
        Vec2::unit_y()
    }
}

/// Keep yaw, zero pitch — FPV demos should face the horizon by default.
pub fn flatten_look_dir(look_dir: Dir) -> Dir {
    let v = look_dir.to_vec();
    Dir::from_unnormalized(Vec3::new(v.x, v.y, 0.0)).unwrap_or(look_dir)
}

fn move_to_traversal_config(client: &Client, target: Vec3<f32>) -> (TraversalConfig, bool) {
    let physics = client.current::<comp::PhysicsState>();
    let on_ground = physics
        .as_ref()
        .is_some_and(|physics| physics.on_ground.is_some());
    let in_liquid = physics
        .as_ref()
        .is_some_and(|physics| physics.in_liquid().is_some());
    let body = client.current::<comp::Body>();
    let scale = client.current::<comp::Scale>().map_or(1.0, |scale| scale.0);

    (
        TraversalConfig {
            node_tolerance: scale * 1.5,
            slow_factor: body
                .as_ref()
                .map_or(0.0, |body| 1.0 - 1.0 / (1.0 + body.base_accel() * 0.01)),
            on_ground,
            in_liquid,
            // This is below the bridge's minimum arrival tolerance (0.1 m).
            // Chaser declining to steer therefore never establishes arrival.
            min_tgt_dist: CHASER_MIN_TARGET_DISTANCE,
            can_climb: body.as_ref().is_some_and(comp::Body::can_climb),
            can_fly: body.as_ref().is_some_and(|body| body.fly_thrust().is_some()),
            vectored_propulsion: body
                .as_ref()
                .is_some_and(comp::Body::vectored_propulsion),
            is_target_loaded: client
                .state()
                .terrain()
                .get(target.map(|component| component.floor() as i32))
                .is_ok(),
        },
        on_ground,
    )
}

fn base_inputs_for_tick(state: &BridgeState) -> ControllerInputs {
    ControllerInputs {
        look_dir: state.look_dir,
        move_dir: if sit_pending(state) { Vec2::zero() } else { state.persistent_move },
        move_z: if sit_pending(state) { 0.0 } else { state.continuous.vertical_input() },
        strafing: state.continuous.strafing(),
        break_block_pos: state.break_block_pos,
        ..ControllerInputs::default()
    }
}

pub fn inputs_for_tick(client: &mut Client, state: &mut BridgeState) -> ControllerInputs {
    continuous_control::tick(client, state);
    let mut inputs = base_inputs_for_tick(state);
    let mut request_jump = false;
    if let Some(active) = state.active.as_mut() {
        match &mut active.kind {
            ActiveKind::TimedMove {
                direction,
                speed,
                finish_tick,
            } => {
                inputs.move_dir = if state.tick < *finish_tick {
                    *direction * *speed
                } else {
                    Vec2::zero()
                };
            },
            ActiveKind::MoveTo {
                target,
                speed: action_speed,
                reached_tick,
                chaser,
                last_progress_tick,
                pending_ticks,
                consecutive_pending_ticks,
                planner_none_ticks,
                chaser_none_ticks,
                stuck_ever,
                stuck_ticks,
                last_bearing,
                max_path_length_reached,
                ..
            } => {
                inputs.move_dir = Vec2::zero();
                if reached_tick.is_none()
                    && let Some(position) = client.position()
                {
                    let velocity = client
                        .current::<comp::Vel>()
                        .map_or_else(Vec3::zero, |velocity| velocity.0);
                    let (traversal_config, on_ground) =
                        move_to_traversal_config(client, *target);
                    let terrain = client.state().terrain();
                    let time = *client
                        .state()
                        .ecs()
                        .read_resource::<common::resources::Time>();
                    let chase = chaser.chase(
                        &*terrain,
                        position,
                        velocity,
                        *target,
                        traversal_config,
                        &time,
                    );
                    *max_path_length_reached =
                        (*max_path_length_reached).max(chaser.state().0);
                    match chaser.state().1 {
                        PathState::Pending => {
                            *pending_ticks = pending_ticks.saturating_add(1);
                            *consecutive_pending_ticks =
                                consecutive_pending_ticks.saturating_add(1);
                            *last_progress_tick = state.tick;
                        },
                        PathState::None => {
                            *consecutive_pending_ticks = 0;
                            *planner_none_ticks = planner_none_ticks.saturating_add(1);
                        },
                        _ => {
                            *consecutive_pending_ticks = 0;
                            *planner_none_ticks = 0;
                        },
                    }
                    match chase {
                        Some((bearing, chase_speed, stuck)) => {
                            *last_bearing = Some(bearing);
                            if stuck {
                                *stuck_ever = true;
                                *stuck_ticks = stuck_ticks.saturating_add(1);
                            }
                            let speed = chase_speed * *action_speed;
                            inputs.move_dir = bearing
                                .xy()
                                .try_normalized()
                                .unwrap_or_else(Vec2::zero)
                                * speed;
                            inputs.move_z = bearing.z;
                            request_jump = on_ground
                                && (bearing.z > 1.5
                                    || (stuck
                                        && *stuck_ticks
                                            % STUCK_RECOVERY_JUMP_CADENCE_TICKS
                                            == 1));
                        },
                        // Chaser uses None for its own near-target check. The bridge
                        // sends no beeline fallback and only completes after its
                        // separate horizontal/vertical arrival test passes.
                        None => {
                            *chaser_none_ticks = chaser_none_ticks.saturating_add(1);
                        },
                    }
                }
            },
            ActiveKind::Jump { .. } => inputs.move_dir = Vec2::zero(),
            ActiveKind::Look { target } => {
                inputs.look_dir = *target;
                inputs.move_dir = Vec2::zero();
            },
            ActiveKind::Attack { .. }
            | ActiveKind::Mine { .. }
            | ActiveKind::HoldAbility { .. }
            | ActiveKind::Craft { .. }
            | ActiveKind::InteractWait { .. } => {},
        }
    }
    if request_jump {
        client.handle_input(InputKind::Jump, true, None, None);
        state.release_jump = true;
    }
    inputs
}

fn vec2_arg(value: &Value, field: &str) -> Result<Vec2<f32>, String> {
    let values = value
        .get(field)
        .and_then(Value::as_array)
        .ok_or_else(|| format!("{field} must be a two-number array"))?;
    if values.len() != 2 {
        return Err(format!("{field} must contain exactly two numbers"));
    }
    let x = values[0]
        .as_f64()
        .ok_or_else(|| format!("{field}[0] must be a number"))? as f32;
    let y = values[1]
        .as_f64()
        .ok_or_else(|| format!("{field}[1] must be a number"))? as f32;
    let vector = Vec2::new(x, y);
    if !vector.map(|component| component.is_finite()).reduce_and()
        || vector.magnitude_squared() < 0.0001
    {
        return Err(format!("{field} must be finite and non-zero"));
    }
    Ok(vector.normalized())
}

fn vec3_arg(value: &Value, field: &str) -> Result<Vec3<f32>, String> {
    let values = value
        .get(field)
        .and_then(Value::as_array)
        .ok_or_else(|| format!("{field} must be a three-number array"))?;
    if values.len() != 3 {
        return Err(format!("{field} must contain exactly three numbers"));
    }
    let vector = Vec3::new(
        values[0]
            .as_f64()
            .ok_or_else(|| format!("{field}[0] must be a number"))? as f32,
        values[1]
            .as_f64()
            .ok_or_else(|| format!("{field}[1] must be a number"))? as f32,
        values[2]
            .as_f64()
            .ok_or_else(|| format!("{field}[2] must be a number"))? as f32,
    );
    if !vector.map(|component| component.is_finite()).reduce_and() {
        return Err(format!("{field} must contain finite values"));
    }
    Ok(vector)
}

fn action_result(
    client: &Client,
    state: &BridgeState,
    action: &ActiveAction,
    status: &str,
    detail: Value,
) -> Value {
    let mut result = action_result_core(action, state.tick, status, detail);
    result["survival"] = json!(is_survival_action(&action.action_type).then(|| json!({
        "before": action.survival_before,
        "after": survival_snapshot(client, state),
    })));
    result["observation"] = observation(client, state);
    if let Some(origin) = state.visual_origins.get(&action.action_id) {
        result["visual_resolution"] = origin.clone();
    }
    result
}

/// Pure wire fields shared with offline native/Python terminal-contract tests.
fn action_result_core(action: &ActiveAction, finished_tick: u64, status: &str, detail: Value) -> Value {
    json!({
        "action_id": action.action_id,
        "action_type": action.action_type,
        "status": status,
        "started_tick": action.started_tick,
        "finished_tick": finished_tick,
        "detail": detail,
    })
}

fn craft_output_inventory_observation(
    output_definition_delta: i64,
    expected_output_amount: u64,
) -> (&'static str, u64) {
    let observed_output_amount = output_definition_delta.max(0) as u64;
    let output_shortfall = expected_output_amount.saturating_sub(observed_output_amount);
    let observation = if output_shortfall == 0 {
        "at_least_expected_output_definition_delta_observed"
    } else if observed_output_amount == 0 {
        "no_positive_output_definition_delta_observed"
    } else {
        "fewer_output_definitions_than_expected_observed"
    };
    (observation, output_shortfall)
}

#[expect(clippy::too_many_arguments)]
fn craft_result_detail(
    client: &Client,
    recipe: &str,
    requested_amount: u64,
    amount: u32,
    station: Option<Vec3<i32>>,
    required_sprite: Option<SpriteKind>,
    craft_request_sequence: u64,
    output_definition_id: &str,
    expected_output_amount: u64,
    start_inventory: &InventorySnapshot,
    craft_update_observed: bool,
) -> Value {
    let mut detail = craft_request_detail(recipe, requested_amount, amount);
    detail["station"] = json!(station.map(|position| [position.x, position.y, position.z]));
    detail["required_station_sprite"] =
        json!(required_sprite.map(|sprite| format!("{sprite:?}")));
    detail["craft_request_sequence"] = json!(craft_request_sequence);
    detail["output_definition_id"] = json!(output_definition_id);
    detail["expected_output_amount"] = json!(expected_output_amount);
    detail["craft_update_observed"] = json!(craft_update_observed);
    detail["confirmation"] = json!(if craft_update_observed {
        "A Craft inventory update was observed while this action was waiting; inventory updates do not carry an action id."
    } else {
        "No Craft inventory update eligible for this sent request was observed before the action deadline."
    });

    if let Some(end_inventory) = client
        .current::<comp::Inventory>()
        .as_ref()
        .map(|inventory| inventory_snapshot(inventory))
    {
        let delta = inventory_delta(&start_inventory.definition_counts, &end_inventory.definition_counts);
        let output_definition_delta = *delta.get(output_definition_id).unwrap_or(&0);
        let (output_inventory_observation, output_shortfall) =
            craft_output_inventory_observation(output_definition_delta, expected_output_amount);
        detail["inventory_delta"] = json!(delta);
        detail["free_slot_delta"] =
            json!(end_inventory.free_slots as i64 - start_inventory.free_slots as i64);
        detail["output_definition_delta"] = json!(output_definition_delta);
        detail["observed_output_amount"] =
            json!(output_definition_delta.max(0) as u64);
        detail["output_shortfall"] = json!(output_shortfall);
        detail["output_inventory_observation"] = json!(output_inventory_observation);
        detail["output_may_have_been_dropped"] =
            json!(craft_update_observed && output_shortfall > 0);
    } else {
        detail["inventory_delta"] = Value::Null;
        detail["free_slot_delta"] = Value::Null;
        detail["output_definition_delta"] = Value::Null;
        detail["observed_output_amount"] = Value::Null;
        detail["output_shortfall"] = Value::Null;
        detail["output_inventory_observation"] = json!("inventory_unavailable");
        detail["output_may_have_been_dropped"] = Value::Null;
    }

    if !craft_update_observed {
        detail["code"] = json!("craft_unconfirmed");
    }
    detail
}

fn cache_action(state: &mut BridgeState, action_id: String, signature: String, result: Value) {
    state
        .completed
        .insert(action_id.clone(), CachedAction { signature, result });
    state.completed_order.push_back(action_id);
    while state.completed_order.len() > MAX_COMPLETED_ACTIONS {
        if let Some(expired) = state.completed_order.pop_front() {
            state.completed.remove(&expired);
        }
    }
}

fn finish_active(client: &mut Client, state: &mut BridgeState, status: &str, mut detail: Value) {
    clear_control_effects(client, state);
    let Some(action) = state.active.take() else {
        return;
    };
    if let ActiveKind::InteractWait {
        mode: InteractWait::Pickup { uid, request_sequence, start_item_count, .. },
    } = &action.kind {
        detail["uid"] = json!(uid);
        detail["pickup_request_sequence"] = json!(request_sequence);
        detail["item_count_delta"] = json!(client.current::<comp::Inventory>()
            .as_ref().map(|inv| i64::from(inventory_item_count(inv)) - i64::from(*start_item_count)));
        detail["target_present_at_finish"] = json!(entity_by_uid(client, *uid).is_some());
        detail["inventory_and_presence_are_context_only"] = json!(true);
        detail["target_acquired"] = Value::Null;
    }
    if let ActiveKind::InteractWait {
        mode: InteractWait::UseItem {
            definition_id, start_definition_count, consumable, entered_use, ..
        },
    } = &action.kind {
        let inventory = client.current::<comp::Inventory>();
        let delta = inventory.as_ref().map(|inv| {
            i64::from(inventory_snapshot(inv).definition_counts
                .get(definition_id).copied().unwrap_or(0))
                - i64::from(*start_definition_count)
        });
        detail["item_definition_id"] = json!(definition_id);
        detail["definition_count_delta"] = json!(delta);
        detail["consumable"] = json!(consumable);
        detail["entered_use"] = json!(entered_use);
        detail["using_at_finish"] = json!(client.current::<comp::CharacterState>()
            .map(|cs| matches!(cs, comp::CharacterState::UseItem(_))));
    }
    state.persistent_move = Vec2::zero();
    let result = action_result(client, state, &action, status, detail);
    response(state, action.request_id, result.clone());
    cache_action(
        state,
        action.action_id.clone(),
        action.signature.clone(),
        result.clone(),
    );
    record_event(
        state,
        match status {
            "completed" => "action_completed",
            "cancelled" => "action_cancelled",
            _ => "action_failed",
        },
        result,
    );
}

pub fn cancel_active(client: &mut Client, state: &mut BridgeState, reason: &str) {
    continuous_control::stop(client, state, reason);
    if state.active.is_some() {
        finish_active(
            client,
            state,
            "cancelled",
            json!({
                "code": "action_cancelled",
                "reason": reason,
            }),
        );
    }
}

/// Distance used to judge whether a ground move has arrived.
///
/// The vertical axis is deliberately excluded. A walking character does not
/// choose its own height: it follows the terrain. Charging the height
/// difference to the move -- and the old weighting charged it double -- made
/// arrival depend on something the character cannot control, so a move that
/// landed within 2 cm of its target horizontally was still reported as
/// stalled whenever the ground there sat a few metres lower.
fn ground_distance(target: Vec3<f32>, position: Vec3<f32>) -> f32 {
    let delta = target - position;
    delta.xy().magnitude()
}

/// Ranged aim combines controller pitch with body heading (basic_ranged uses
/// the same merge_z). Upright wielding bodies deliberately never pitch toward
/// the requested point; comparing Ori directly with a steep 3D target can
/// therefore time out forever despite the correct applied look input.
fn applied_look_aligned(target: Dir, input: Dir, body: Dir) -> bool {
    input.dot(target.to_vec()) >= 0.95
        && input.merge_z(body).dot(target.to_vec()) >= 0.95
}

/// How far the ground may drop or rise under a target before it is treated as
/// a different place rather than the same place on a slope.
const VERTICAL_ARRIVAL_SLACK: f32 = 8.0;

fn actual_path_length_at_termination(
    client: &Client,
    last_path_position: Option<Vec3<f32>>,
    actual_path_length: f32,
) -> f32 {
    client
        .position()
        .zip(last_path_position)
        .map_or(actual_path_length, |(position, previous)| {
            actual_path_length + ground_distance(position, previous)
        })
}

#[expect(clippy::too_many_arguments)]
fn move_to_detail(
    client: &Client,
    current_tick: u64,
    started_tick: u64,
    start_position: Option<Vec3<f32>>,
    target: Vec3<f32>,
    remaining_distance: f32,
    chaser: &Chaser,
    max_path_length_reached: PathLength,
    pending_ticks: u64,
    consecutive_pending_ticks: u64,
    planner_none_ticks: u64,
    chaser_none_ticks: u64,
    stuck_ever: bool,
    stuck_ticks: u64,
    last_bearing: Option<Vec3<f32>>,
    actual_path_length: f32,
    last_best_distance_tick: u64,
    max_no_best_distance_improvement_ticks: u64,
    code: Option<&str>,
) -> Value {
    let (path_length, path_state) = chaser.state();
    let min_path_length = chaser.min_path_length();
    let edge_cost_mode = chaser.edge_cost_mode();
    let remaining_route_nodes = chaser.get_route().map(|route| {
        route
            .get_path()
            .nodes()
            .len()
            .saturating_sub(route.next_idx())
    });
    let physics = client.current::<comp::PhysicsState>();
    let on_ground = physics
        .as_ref()
        .map(|physics| physics.on_ground.is_some());
    let in_liquid = physics
        .as_ref()
        .map(|physics| physics.in_liquid().is_some());
    let straight_line_distance = start_position.map(|position| ground_distance(target, position));
    let actual_to_straight_path_ratio = straight_line_distance
        .filter(|distance| *distance > 0.0)
        .map(|distance| actual_path_length / distance);
    let max_no_best_distance_improvement_ticks = max_no_best_distance_improvement_ticks
        .max(current_tick.saturating_sub(last_best_distance_tick));
    let mut detail = json!({
        "remaining_distance": remaining_distance,
        "path_state": format!("{path_state:?}"),
        "path_length": format!("{path_length:?}"),
        "min_path_length": format!("{min_path_length:?}"),
        "max_path_length_reached": format!("{max_path_length_reached:?}"),
        "edge_cost_mode": edge_cost_mode.as_str(),
        "remaining_route_nodes": remaining_route_nodes,
        "stuck_ever": stuck_ever,
        "stuck_ticks": stuck_ticks,
        "last_bearing": last_bearing.map(|bearing| [bearing.x, bearing.y, bearing.z]),
        "on_ground": on_ground,
        "in_liquid": in_liquid,
        "action_ticks": current_tick.saturating_sub(started_tick),
        "straight_line_distance": straight_line_distance,
        "actual_path_length": actual_path_length,
        "actual_to_straight_path_ratio": actual_to_straight_path_ratio,
        "max_no_best_distance_improvement_ticks": max_no_best_distance_improvement_ticks,
        "pending_ticks": pending_ticks,
        "consecutive_pending_ticks": consecutive_pending_ticks,
        "path_state_none_ticks": planner_none_ticks,
        "chaser_none_ticks": chaser_none_ticks,
    });
    if let (Some(code), Some(object)) = (code, detail.as_object_mut()) {
        object.insert("code".to_owned(), Value::String(code.to_owned()));
    }
    detail
}

fn immediate_action(
    client: &Client,
    state: &mut BridgeState,
    request_id: Value,
    action_id: String,
    action_type: String,
    signature: String,
    detail: Value,
) {
    immediate_action_with_status(
        client,
        state,
        request_id,
        action_id,
        action_type,
        signature,
        "completed",
        detail,
    );
}

fn immediate_action_with_status(
    client: &Client,
    state: &mut BridgeState,
    request_id: Value,
    action_id: String,
    action_type: String,
    signature: String,
    status: &str,
    detail: Value,
) {
    let action = ActiveAction {
        request_id: request_id.clone(),
        action_id: action_id.clone(),
        action_type,
        signature: signature.clone(),
        started_tick: state.tick,
        deadline_tick: state.tick,
        start_position: client.position(),
        progress_position: client.position(),
        last_progress_tick: state.tick,
        survival_before: None,
        kind: ActiveKind::TimedMove {
            direction: Vec2::zero(),
            speed: 0.0,
            finish_tick: state.tick,
        },
    };
    let result = action_result(client, state, &action, status, detail);
    response(state, request_id, result.clone());
    cache_action(state, action_id, signature, result.clone());
    record_event(
        state,
        match status {
            "completed" => "action_completed",
            "cancelled" => "action_cancelled",
            _ => "action_failed",
        },
        result,
    );
}

fn begin_step(client: &mut Client, state: &mut BridgeState, request: Request) {
    let action = match request.args.get("action") {
        Some(Value::Object(action)) => action,
        _ => {
            error_response(
                state,
                request.id.clone(),
                "invalid_action",
                "args.action must be an object",
            );
            return;
        },
    };
    let Some(action_id) = action.get("action_id").and_then(Value::as_str) else {
        error_response(
            state,
            request.id,
            "invalid_action",
            "action_id must be a non-empty string",
        );
        return;
    };
    if action_id.is_empty() || action_id.len() > 128 {
        error_response(
            state,
            request.id,
            "invalid_action",
            "action_id must contain between 1 and 128 bytes",
        );
        return;
    }
    let action_id = action_id.to_owned();
    let Some(action_type) = action.get("type").and_then(Value::as_str) else {
        error_response(
            state,
            request.id,
            "invalid_action",
            "action.type must be a string",
        );
        return;
    };
    let action_type = action_type.to_owned();
    if action_type == "give_up" && request.args.as_object().is_some_and(|args|
        args.keys().any(|key| key != "action" && key != "preconditions" && key != "control_fence"))
    {
        error_response(state, request.id, "invalid_action", "give_up does not accept step options");
        return;
    }
    let signature = match step_signature(state, &request.id, &action_id, &request.args) {
        Ok(signature) => signature,
        Err(code) => {
            error_response(state, request.id, code, "action_id belongs to a visual submission");
            return;
        },
    };

    if state.continuous.is_active() && action_type != "stop" {
        error_response(state, request.id, "avatar_busy",
            "continuous controls own the avatar; release them before step");
        return;
    }

    if let Some(cached) = state.completed.get(&action_id).cloned() {
        if cached.signature != signature {
            error_response(
                state,
                request.id,
                "action_id_conflict",
                "action_id was already used with different action parameters",
            );
            return;
        }
        let mut result = cached.result;
        result["duplicate"] = Value::Bool(true);
        response(state, request.id, result);
        return;
    }
    if let Some(active) = state.active.as_ref() {
        if active.action_id == action_id {
            if active.signature != signature {
                error_response(
                    state,
                    request.id,
                    "action_id_conflict",
                    "action_id is running with different action parameters",
                );
                return;
            }
            response(
                state,
                request.id.clone(),
                json!({
                    "action_id": active.action_id,
                    "action_type": active.action_type,
                    "status": "running",
                    "duplicate": true,
                    "started_tick": active.started_tick,
                    "finished_tick": null,
                    "detail": {},
                    "observation": observation(client, state),
                }),
            );
        } else if action_type != "stop" {
            error_response(
                state,
                request.id.clone(),
                "avatar_busy",
                format!("action {} is still running", active.action_id),
            );
        }
        if action_type != "stop" {
            return;
        }
    }
    if let Some(expected) = request.args.get("preconditions") {
        if let Err(code) = operation_preconditions::check(client, state, expected) {
            error_response(state, request.id, code, code);
            return;
        }
    }

    if action_type == "stop" {
        cancel_active(client, state, "stopped by command");
        state.persistent_move = Vec2::zero();
        immediate_action(
            client,
            state,
            request.id,
            action_id,
            action_type,
            signature,
            json!({}),
        );
        return;
    }

    let speed = match number_arg(action, "speed", 1.0, 0.05, 1.0) {
        Ok(speed) => speed,
        Err(message) => {
            error_response(state, request.id, "invalid_action", message);
            return;
        },
    };
    let duration_ticks = match ticks_arg(
        &request.args,
        "duration_ticks",
        DEFAULT_ACTION_TICKS,
        TPS * 60,
    ) {
        Ok(ticks) => ticks,
        Err(message) => {
            error_response(state, request.id, "invalid_action", message);
            return;
        },
    };
    let timeout_ticks = match ticks_arg(
        &request.args,
        "timeout_ticks",
        DEFAULT_TIMEOUT_TICKS,
        TPS * 600,
    ) {
        Ok(ticks) => ticks,
        Err(message) => {
            error_response(state, request.id, "invalid_action", message);
            return;
        },
    };
    if action_type != "craft" && timeout_ticks < duration_ticks {
        error_response(
            state,
            request.id,
            "invalid_action",
            "timeout_ticks must be greater than or equal to duration_ticks",
        );
        return;
    }
    let look = horizontal_look(state.look_dir);

    let survival_before = is_survival_action(&action_type)
        .then(|| survival_snapshot(client, state));
    let kind = match action_type.as_str() {
        "move_forward" => ActiveKind::TimedMove {
            direction: look,
            speed,
            finish_tick: state.tick + duration_ticks,
        },
        "move_backward" => ActiveKind::TimedMove {
            direction: -look,
            speed,
            finish_tick: state.tick + duration_ticks,
        },
        "move_right" => ActiveKind::TimedMove {
            direction: Vec2::new(look.y, -look.x),
            speed,
            finish_tick: state.tick + duration_ticks,
        },
        "move_left" => ActiveKind::TimedMove {
            direction: Vec2::new(-look.y, look.x),
            speed,
            finish_tick: state.tick + duration_ticks,
        },
        "move_vector" => match vec2_arg(&Value::Object(action.clone()), "direction") {
            Ok(direction) => ActiveKind::TimedMove {
                direction,
                speed,
                finish_tick: state.tick + duration_ticks,
            },
            Err(message) => {
                error_response(state, request.id, "invalid_action", message);
                return;
            },
        },
        "move_to" => {
            let target = match vec3_arg(&Value::Object(action.clone()), "position") {
                Ok(target) => target,
                Err(message) => {
                    error_response(state, request.id, "invalid_action", message);
                    return;
                },
            };
            let tolerance = match number_arg(action, "tolerance", 1.0, 0.1, 10.0) {
                Ok(tolerance) => tolerance,
                Err(message) => {
                    error_response(state, request.id, "invalid_action", message);
                    return;
                },
            };
            if let Some(position) = client.position()
                && let Some(direction) = Dir::from_unnormalized(target - position)
            {
                state.look_dir = direction;
            }
            let distance = client
                .position()
                .map(|position| ground_distance(target, position))
                .unwrap_or(f32::MAX);
            let min_path_length = agent_min_path_length();
            let edge_cost_mode = agent_edge_cost_mode();
            ActiveKind::MoveTo {
                target,
                speed,
                tolerance,
                best_distance: distance,
                last_progress_tick: state.tick,
                last_best_distance_tick: state.tick,
                max_no_best_distance_improvement_ticks: 0,
                reached_tick: None,
                chaser: Box::new(
                    Chaser::default()
                        .with_min_path_length(min_path_length)
                        .with_edge_cost_mode(edge_cost_mode),
                ),
                pending_ticks: 0,
                consecutive_pending_ticks: 0,
                planner_none_ticks: 0,
                chaser_none_ticks: 0,
                stuck_ever: false,
                stuck_ticks: 0,
                last_bearing: None,
                last_path_position: client.position(),
                actual_path_length: 0.0,
                max_path_length_reached: min_path_length,
            }
        },
        "jump" | "wall_jump" => {
            if action_type == "wall_jump" {
                if client.current::<comp::PhysicsState>()
                    .is_none_or(|physics| physics.on_wall.is_none())
                    || !matches!(client.current::<comp::CharacterState>(),
                        Some(comp::CharacterState::Climb(_) | comp::CharacterState::Wallrun(_)))
                {
                    error_response(state, request.id, "wall_jump_ineligible",
                        "wall jump requires sampled wall contact and climb or wallrun state");
                    return;
                }
                client.handle_input(InputKind::WallJump, true, None, None);
                state.release_wall_jump = true;
            } else {
                client.handle_input(InputKind::Jump, true, None, None);
                state.release_jump = true;
            }
            ActiveKind::Jump {
                earliest_finish_tick: state.tick + duration_ticks.min(TPS),
                was_airborne: false,
            }
        },
        "look_at" => {
            let target = match vec3_arg(&Value::Object(action.clone()), "position") {
                Ok(target) => target,
                Err(message) => {
                    error_response(state, request.id, "invalid_action", message);
                    return;
                },
            };
            let Some(position) = client.position() else {
                error_response(
                    state,
                    request.id,
                    "state_unavailable",
                    "position unavailable",
                );
                return;
            };
            let Some(direction) = Dir::from_unnormalized(target - position) else {
                error_response(
                    state,
                    request.id,
                    "invalid_action",
                    "look_at target equals current position",
                );
                return;
            };
            state.look_dir = direction;
            ActiveKind::Look { target: direction }
        },
        "turn" => {
            let Some(degrees) = action.get("degrees").and_then(Value::as_f64) else {
                error_response(
                    state,
                    request.id,
                    "invalid_action",
                    "degrees must be a number",
                );
                return;
            };
            if !degrees.is_finite() || degrees.abs() > 3600.0 {
                error_response(
                    state,
                    request.id,
                    "invalid_action",
                    "degrees must be finite and between -3600 and 3600",
                );
                return;
            }
            let current = state.look_dir.to_vec();
            let radians = (degrees as f32).to_radians();
            let (sin, cos) = radians.sin_cos();
            let rotated = Vec3::new(
                current.x * cos - current.y * sin,
                current.x * sin + current.y * cos,
                current.z,
            );
            state.look_dir = Dir::from_unnormalized(rotated).unwrap_or_default();
            ActiveKind::Look {
                target: state.look_dir,
            }
        },
        "attack" | "attack_primary" | "attack_secondary" => {
            let input = if action_type == "attack_secondary" {
                InputKind::Secondary
            } else {
                InputKind::Primary
            };
            let target_uid = match action.get("target_uid") {
                None => None,
                Some(value) => {
                    let Some(uid) = value.as_u64() else {
                        error_response(
                            state,
                            request.id,
                            "invalid_action",
                            "target_uid must be a positive integer",
                        );
                        return;
                    };
                    Some(uid)
                },
            };
            let target_ref = match target_uid {
                Some(uid) => match continuous_control::observed_target_ref(client, state, uid) {
                    Ok(target) => Some(target),
                    Err(reason) => {
                        error_response(state, request.id, reason, "Target is not currently observed");
                        return;
                    },
                },
                None => None,
            };
            let target_aim = match target_ref.as_ref()
                .map(|target| continuous_control::tracked_target_aim(client, state, target))
                .transpose()
            {
                Ok(aim) => aim,
                Err(reason) => {
                    error_response(state, request.id, reason, "Target cannot be tracked");
                    return;
                },
            };
            if let Some((_, direction, _)) = target_aim { state.look_dir = direction; }
            let target_entity = target_aim.map(|aim| aim.0);
            let start_health = target_entity.and_then(|entity| health_of_entity(client, entity));
            client.handle_input(input, true, None, target_entity);
            state.held_input = Some(input);
            ActiveKind::Attack {
                input,
                finish_tick: state.tick + duration_ticks,
                saw_attack: false,
                target_uid,
                target_ref,
                start_health,
            }
        },
        "block" | "roll" => {
            let input = if action_type == "block" {
                InputKind::Block
            } else {
                InputKind::Roll
            };
            client.handle_input(input, true, None, None);
            state.held_input = Some(input);
            ActiveKind::HoldAbility {
                input,
                finish_tick: state.tick + duration_ticks,
            }
        },
        "toggle_wield" => {
            let want_wielding = match action.get("wield") {
                None => match client.is_wielding() {
                    Some(current) => !current,
                    None => {
                        error_response(
                            state,
                            request.id,
                            "state_unavailable",
                            "character state unavailable",
                        );
                        return;
                    },
                },
                Some(Value::Bool(value)) => *value,
                Some(_) => {
                    error_response(
                        state,
                        request.id,
                        "invalid_action",
                        "wield must be a boolean",
                    );
                    return;
                },
            };
            if client.is_wielding() != Some(want_wielding) {
                client.toggle_wield();
            }
            ActiveKind::InteractWait {
                mode: InteractWait::ToggleWield { want_wielding },
            }
        },
        "sit" => {
            if !sit_action_has_no_parameters(action) {
                error_response(
                    state,
                    request.id,
                    "invalid_action",
                    "sit does not accept parameters",
                );
                return;
            }
            let character = client.current::<comp::CharacterState>();
            let health = client.current::<comp::Health>();
            let outcome = begin_sit_with(state, character.as_ref(), health.as_ref(), |control| {
                match control {
                    SitControl::Release(input) => client.handle_input(input, false, None, None),
                    SitControl::Toggle => client.toggle_sit(),
                }
            });
            if let Some((status, detail)) = outcome {
                immediate_action_with_status(
                    client, state, request.id, action_id, action_type, signature, status, detail,
                );
                return;
            }
            ActiveKind::InteractWait { mode: InteractWait::Sit }
        },
        "give_up" => {
            let before = survival_before.as_ref().expect("give_up survival snapshot");
            if let Err(reason) = give_up::begin(action, before, || client.give_up()) {
                error_response(state, request.id, "give_up_not_sent", reason);
                return;
            }
            ActiveKind::InteractWait { mode: InteractWait::GiveUp }
        },
        "respawn" => {
            if action
                .keys()
                .any(|field| field != "action_id" && field != "type")
            {
                error_response(
                    state,
                    request.id,
                    "invalid_action",
                    "respawn does not accept parameters",
                );
                return;
            }
            if !client.respawn() {
                immediate_action_with_status(
                    client,
                    state,
                    request.id,
                    action_id,
                    action_type,
                    signature,
                    "refused",
                    json!({
                        "code": "respawn_not_sent",
                        "reason": "respawn was not sent because the local character is not dead",
                        "sent": false,
                        "observed_alive": true,
                    }),
                );
                return;
            }
            ActiveKind::InteractWait {
                mode: InteractWait::Respawn,
            }
        },
        "craft" => {
            let recipe_key = match action.get("recipe").and_then(Value::as_str) {
                Some(recipe) => recipe,
                None => {
                    error_response(
                        state,
                        request.id,
                        "invalid_action",
                        "recipe must be a string",
                    );
                    return;
                },
            };
            let (requested_amount, amount) = match craft_amount_arg(action) {
                Ok(amount) => amount,
                Err(message) => {
                    error_response(state, request.id, "invalid_action", message);
                    return;
                },
            };
            let recipe = {
                let manifest = client.state().ecs().read_resource::<RecipeBookManifest>();
                manifest.get(recipe_key).cloned()
            };
            let Some(recipe) = recipe else {
                immediate_action_with_status(
                    client,
                    state,
                    request.id,
                    action_id,
                    action_type,
                    signature,
                    "failed",
                    craft_failure_detail(
                        recipe_key,
                        requested_amount,
                        amount,
                        "craft_recipe_unknown",
                        "The recipe key is not present in the recipe manifest.",
                    ),
                );
                return;
            };
            let known = match client.current::<comp::Inventory>() {
                Some(inventory) => inventory.recipe_is_known(recipe_key),
                None => {
                    immediate_action_with_status(
                        client,
                        state,
                        request.id,
                        action_id,
                        action_type,
                        signature,
                        "failed",
                        craft_failure_detail(
                            recipe_key,
                            requested_amount,
                            amount,
                            "state_unavailable",
                            "The character inventory is unavailable.",
                        ),
                    );
                    return;
                },
            };
            if !known {
                immediate_action_with_status(
                    client,
                    state,
                    request.id,
                    action_id,
                    action_type,
                    signature,
                    "failed",
                    craft_failure_detail(
                        recipe_key,
                        requested_amount,
                        amount,
                        "craft_recipe_not_known",
                        "The character does not know this recipe.",
                    ),
                );
                return;
            }

            let supplied_station = match craft_station_arg(action) {
                Ok(station) => station,
                Err(message) => {
                    error_response(state, request.id, "invalid_action", message);
                    return;
                },
            };
            let required_sprite = recipe.craft_sprite;
            let station = if let Some(required_sprite) = required_sprite {
                let Some(position) = client.position() else {
                    immediate_action_with_status(
                        client,
                        state,
                        request.id,
                        action_id,
                        action_type,
                        signature,
                        "failed",
                        craft_failure_detail(
                            recipe_key,
                            requested_amount,
                            amount,
                            "state_unavailable",
                            "The character position is unavailable.",
                        ),
                    );
                    return;
                };
                let station = match supplied_station {
                    Some(station) => {
                        let block = client.state().terrain().get(station).ok().copied();
                        let actual_sprite = block.and_then(|block| block.get_sprite());
                        if actual_sprite != Some(required_sprite) {
                            let mut detail = craft_failure_detail(
                                recipe_key,
                                requested_amount,
                                amount,
                                "craft_station_mismatch",
                                "The supplied station voxel does not contain the required sprite.",
                            );
                            detail["station"] =
                                json!([station.x, station.y, station.z]);
                            detail["required_station_sprite"] =
                                json!(format!("{required_sprite:?}"));
                            detail["actual_station_sprite"] =
                                json!(actual_sprite.map(|sprite| format!("{sprite:?}")));
                            detail["station_terrain_loaded"] = json!(block.is_some());
                            immediate_action_with_status(
                                client,
                                state,
                                request.id,
                                action_id,
                                action_type,
                                signature,
                                "failed",
                                detail,
                            );
                            return;
                        }
                        station
                    },
                    None => {
                        let candidates = nearby_interactable_sprites(
                            client,
                            position,
                            CRAFT_STATION_SCAN_RADIUS,
                            Some(required_sprite),
                        );
                        state.interactable_sprite_scan = Some(InteractableSpriteScan {
                            tick: state.tick,
                            position,
                            radius: CRAFT_STATION_SCAN_RADIUS,
                            sprites: candidates.clone(),
                        });
                        match candidates
                            .into_iter()
                            .find(|candidate| candidate.sprite == required_sprite)
                            .map(|candidate| candidate.voxel)
                        {
                            Some(station) => station,
                            None => {
                                let mut detail = craft_failure_detail(
                                    recipe_key,
                                    requested_amount,
                                    amount,
                                    "craft_station_not_found",
                                    "No matching station was found in loaded cached terrain; unloaded chunks are skipped, so this does not prove none exists in the world.",
                                );
                                detail["required_station_sprite"] =
                                    json!(format!("{required_sprite:?}"));
                                detail["search_radius_voxels"] = json!(CRAFT_STATION_SCAN_RADIUS);
                                detail["search_voxel_bound"] =
                                    json!((CRAFT_STATION_SCAN_RADIUS * 2 + 1).pow(3));
                                immediate_action_with_status(
                                    client,
                                    state,
                                    request.id,
                                    action_id,
                                    action_type,
                                    signature,
                                    "failed",
                                    detail,
                                );
                                return;
                            },
                        }
                    },
                };
                let Some(distance) = craft_station_distance(client, station) else {
                    immediate_action_with_status(
                        client,
                        state,
                        request.id,
                        action_id,
                        action_type,
                        signature,
                        "failed",
                        craft_failure_detail(
                            recipe_key,
                            requested_amount,
                            amount,
                            "state_unavailable",
                            "The character position is unavailable.",
                        ),
                    );
                    return;
                };
                if !craft_station_in_range(distance) {
                    let mut detail = craft_failure_detail(
                        recipe_key,
                        requested_amount,
                        amount,
                        "craft_station_out_of_range",
                        "The required station is outside the server pickup range.",
                    );
                    detail["station"] = json!([station.x, station.y, station.z]);
                    detail["required_station_sprite"] = json!(format!("{required_sprite:?}"));
                    detail["distance"] = json!(distance);
                    detail["range_predicate"] = json!(
                        "client reproduction of the server cylinder-to-block-centre minimum distance"
                    );
                    detail["range_threshold"] = json!(MAX_PICKUP_RANGE);
                    immediate_action_with_status(
                        client,
                        state,
                        request.id,
                        action_id,
                        action_type,
                        signature,
                        "failed",
                        detail,
                    );
                    return;
                }
                Some(station)
            } else {
                None
            };

            let (slots, start_inventory) = match client.current::<comp::Inventory>() {
                Some(inventory) => match recipe.inventory_contains_ingredients(&inventory, amount) {
                    Ok(slots) => (slots, inventory_snapshot(&inventory)),
                    Err(missing) => {
                        let mut detail = craft_failure_detail(
                            recipe_key,
                            requested_amount,
                            amount,
                            "craft_missing_ingredients",
                            "The current inventory does not satisfy the recipe inputs.",
                        );
                        detail["unsatisfied_inputs"] = missing_ingredients_json(&missing);
                        immediate_action_with_status(
                            client,
                            state,
                            request.id,
                            action_id,
                            action_type,
                            signature,
                            "failed",
                            detail,
                        );
                        return;
                    },
                },
                None => {
                    immediate_action_with_status(
                        client,
                        state,
                        request.id,
                        action_id,
                        action_type,
                        signature,
                        "failed",
                        craft_failure_detail(
                            recipe_key,
                            requested_amount,
                            amount,
                            "state_unavailable",
                            "The character inventory is unavailable.",
                        ),
                    );
                    return;
                },
            };
            let craft_sprite = match (station, required_sprite) {
                (Some(station), Some(sprite)) => Some((VolumePos::terrain(station), sprite)),
                _ => None,
            };
            let craft_request_sequence = state.next_craft_request_sequence;
            let Some(next_craft_request_sequence) = craft_request_sequence.checked_add(1) else {
                immediate_action_with_status(
                    client,
                    state,
                    request.id,
                    action_id,
                    action_type,
                    signature,
                    "failed",
                    craft_failure_detail(
                        recipe_key,
                        requested_amount,
                        amount,
                        "craft_sequence_exhausted",
                        "The bridge cannot safely send another craft request because its request sequence is exhausted.",
                    ),
                );
                return;
            };
            if !client.craft_recipe(recipe_key, slots, craft_sprite, amount) {
                let mut detail = craft_failure_detail(
                    recipe_key,
                    requested_amount,
                    amount,
                    "craft_send_rejected",
                    "The local craft preflight refused to send the request; this is not a server refusal.",
                );
                detail["station"] =
                    json!(station.map(|position| [position.x, position.y, position.z]));
                detail["required_station_sprite"] =
                    json!(required_sprite.map(|sprite| format!("{sprite:?}")));
                immediate_action_with_status(
                    client,
                    state,
                    request.id,
                    action_id,
                    action_type,
                    signature,
                    "failed",
                    detail,
                );
                return;
            }
            state.next_craft_request_sequence = next_craft_request_sequence;
            state
                .pending_craft_request_sequences
                .push_back(craft_request_sequence);
            ActiveKind::Craft {
                recipe: recipe_key.to_owned(),
                requested_amount,
                amount,
                station,
                required_sprite,
                craft_request_sequence,
                output_definition_id: recipe.output.0.id().to_owned(),
                expected_output_amount: u64::from(recipe.output.1) * u64::from(amount),
                start_inventory,
                saw_craft_update: false,
            }
        },
        "pickup" => {
            let target = match action.get("uid") {
                Some(value) => {
                    let Some(uid) = value.as_u64() else {
                        error_response(
                            state,
                            request.id,
                            "invalid_action",
                            "uid must be a positive integer",
                        );
                        return;
                    };
                    let Some(entity) = entity_by_uid(client, uid) else {
                        error_response(
                            state,
                            request.id,
                            "invalid_action",
                            format!("uid {uid} not found"),
                        );
                        return;
                    };
                    if client
                        .state()
                        .read_storage::<PickupItem>()
                        .get(entity)
                        .is_none()
                    {
                        error_response(
                            state,
                            request.id,
                            "invalid_action",
                            format!("uid {uid} is not a pickup item"),
                        );
                        return;
                    }
                    (entity, uid)
                },
                None => match nearest_pickup(client, MAX_PICKUP_RANGE) {
                    Some((entity, uid, _)) => (entity, uid),
                    None => {
                        error_response(
                            state,
                            request.id,
                            "invalid_action",
                            "no pickable item within range",
                        );
                        return;
                    },
                },
            };
            let inventory = client.current::<comp::Inventory>();
            let start_item_count = inventory
                .as_ref()
                .map(|inv| inventory_item_count(inv))
                .unwrap_or(0);
            if client.is_dead() {
                error_response(state, request.id, "pickup_send_rejected",
                    "local client does not send pickup while dead; not a server refusal");
                return;
            }
            let (definition_id, amount) = {
                let state = client.state();
                let pickups = state.read_storage::<PickupItem>();
                let item = pickups.get(target.0).expect("validated pickup");
                (json!(item.item().item_definition_id().to_owned()), item.amount())
            };
            let (request_sequence, retired) = state.pickup_ledger.register(
                pickup_feedback::PendingPickup {
                    sequence: 0,
                    uid: target.1,
                    definition_id: definition_id.clone(),
                    amount,
                    request_id: request.id.clone(),
                    action_id: action_id.clone(),
                    sent_tick: state.tick,
                },
            );
            if let Some(retired) = retired {
                record_event(state, "pickup_correlation_history_summarized", json!({
                    "unresolved_request": retired,
                    "future_association": "target_and_item_scoped_uncertainty_not_a_retry_veto",
                    "summary_representation": "256_target_and_item_hash_partitioned_inclusive_ranges",
                }));
            }
            record_event(state, "pickup_request_registered", json!({
                "uid": target.1, "sequence": request_sequence,
                "request_id": request.id, "action_id": action_id, "sent_tick": state.tick,
                "definition_id": definition_id, "amount": amount,
                "send_requested": true,
            }));
            client.pick_up(target.0);
            ActiveKind::InteractWait {
                mode: InteractWait::Pickup {
                    uid: target.1,
                    request_sequence,
                    start_item_count,
                    feedback: None,
                },
            }
        },
        "collect" => {
            let position = match voxel_arg(action, "position") {
                Ok(position) => position,
                Err(message) => {
                    error_response(state, request.id, "invalid_action", message);
                    return;
                },
            };
            let Some(block) = client.state().terrain().get(position).ok().copied() else {
                error_response(
                    state,
                    request.id,
                    "invalid_action",
                    "collect target block is unavailable",
                );
                return;
            };
            if !block.is_collectible() {
                error_response(
                    state,
                    request.id,
                    "invalid_action",
                    "target block is not collectible",
                );
                return;
            }
            let start_item_count = client
                .current::<comp::Inventory>()
                .as_ref()
                .map(|inv| inventory_item_count(inv))
                .unwrap_or(0);
            // Collected is shared with sprite collection. Do not later credit
            // its delayed reply to an entity pickup. Read only the deterministic
            // default Item spec; do not sample random loot or simulate inventory.
            let definition = if client.state().terrain().sprite_cfg_at(position)
                .and_then(|cfg| cfg.loot_table.as_ref()).is_none()
            {
                match block.get_sprite().and_then(|s| s.default_loot_spec()).flatten() {
                    Some(common::lottery::LootSpec::Item(id)) => Some(json!({"Simple": id})),
                    _ => None,
                }
            } else { None };
            state.pickup_ledger.register_other_collection(action_id.clone(), definition.clone());
            record_event(state, "pickup_competing_collection_registered", json!({
                "request_id": request.id, "action_id": action_id, "sent_tick": state.tick,
                "position": [position.x, position.y, position.z], "definition_id": definition,
                "scope": if definition.is_some() { "single_default_item" } else { "opaque_loot" },
                "retention": "bounded_256_records_then_item_scoped_summary_or_opaque_flag",
            }));
            client.collect_block(position);
            ActiveKind::InteractWait {
                mode: InteractWait::Collect {
                    position,
                    start_item_count,
                    start_collectible: true,
                },
            }
        },
        "mine" => {
            let voxel = match voxel_arg(action, "position") {
                Ok(position) => position,
                Err(message) => {
                    error_response(state, request.id, "invalid_action", message);
                    return;
                },
            };
            let Some(block) = client.state().terrain().get(voxel).ok().copied() else {
                error_response(
                    state,
                    request.id,
                    "invalid_action",
                    "mine target block is unavailable",
                );
                return;
            };
            if block.kind().is_air() {
                error_response(
                    state,
                    request.id,
                    "invalid_action",
                    "cannot mine air",
                );
                return;
            }
            let break_pos = voxel.map(|component| component as f32 + 0.5);
            state.break_block_pos = Some(break_pos);
            if let Some(position) = client.position()
                && let Some(direction) = Dir::from_unnormalized(break_pos - position)
            {
                state.look_dir = direction;
            }
            client.handle_input(InputKind::Primary, true, Some(break_pos), None);
            state.held_input = Some(InputKind::Primary);
            ActiveKind::Mine {
                voxel,
                finish_tick: state.tick + duration_ticks,
                start_kind: block.kind().to_string(),
            }
        },
        "use_item" => {
            let inv_slot = match inv_slot_from_action(action) {
                Ok(slot) => slot,
                Err(message) => {
                    error_response(state, request.id, "invalid_action", message);
                    return;
                },
            };
            let (start_item_count, definition_id, start_definition_count, consumable) = {
                let inventory = client.current::<comp::Inventory>();
                match use_item_reference(inventory.as_ref(), inv_slot) {
                    Ok(reference) => reference,
                    Err(message) => {
                        error_response(state, request.id, "invalid_action", message);
                        return;
                    },
                }
            };
            client.use_slot(Slot::Inventory(inv_slot));
            ActiveKind::InteractWait {
                mode: InteractWait::UseItem {
                    start_item_count,
                    definition_id,
                    start_definition_count,
                    consumable,
                    entered_use: false,
                },
            }
        },
        "configure_auxiliary_ability" => {
            if action.keys().any(|key| !matches!(key.as_str(),
                "type" | "action_id" | "slot" | "ability"))
            {
                error_response(state, request.id, "invalid_action", "unknown ability field");
                return;
            }
            let Some(slot) = action.get("slot").and_then(Value::as_u64)
                .and_then(|slot| usize::try_from(slot).ok())
            else {
                error_response(state, request.id, "invalid_action", "ability slot must be a non-negative index");
                return;
            };
            let ability: comp::ability::AuxiliaryAbility =
                match action.get("ability").cloned().map(serde_json::from_value).transpose() {
                    Ok(Some(ability)) => ability,
                    _ => {
                        error_response(state, request.id, "invalid_action", "invalid auxiliary ability");
                        return;
                    },
                };
            let observation = auxiliary_ability_snapshot(client);
            if !observation["active_set"].as_array().is_some_and(|set| slot < set.len())
                || (ability != comp::ability::AuxiliaryAbility::Empty
                    && !observation["available"].as_array().is_some_and(|available|
                        available.contains(&json!(ability))))
            {
                error_response(state, request.id, "ability_ineligible",
                    "selected slot or ability is not in the current active loadout");
                return;
            }
            client.change_ability(slot, ability);
            ActiveKind::InteractWait { mode: InteractWait::Ability { slot, ability } }
        },
        "unlock_skill" => {
            if action.keys().any(|key| !matches!(key.as_str(),
                "type" | "action_id" | "skill"))
            {
                error_response(state, request.id, "invalid_action", "unknown skill field");
                return;
            }
            let skill: comp::skills::Skill = match action.get("skill").cloned()
                .map(serde_json::from_value).transpose()
            {
                Ok(Some(skill)) => skill,
                _ => {
                    error_response(state, request.id, "invalid_action", "skill must be a valid typed skill variant");
                    return;
                },
            };
            let selected = action.get("skill");
            if !skill_snapshot(client)["groups"].as_array()
                .is_some_and(|groups| groups.iter().any(|group|
                    group["unlockable_now"].as_array().is_some_and(|available|
                        available.iter().any(|candidate| Some(candidate) == selected))))
            {
                error_response(state, request.id, "skill_ineligible",
                    "selected skill is not currently unlockable with sampled points/prerequisites");
                return;
            }
            let Some(skills) = client.current::<comp::SkillSet>() else {
                error_response(state, request.id, "skill_state_unavailable", "skillset unavailable");
                return;
            };
            let Some(group) = skill.skill_group_kind() else {
                error_response(state, request.id, "skill_ineligible", "skill has no native group");
                return;
            };
            let before_level = skills.skill_level(skill).unwrap_or(0);
            let before_points = skills.available_sp(group);
            let cost = skills.skill_cost(skill);
            client.unlock_skill(skill);
            ActiveKind::InteractWait {
                mode: InteractWait::Skill { skill, before_level, before_points, cost },
            }
        },
        "read_sign" => {
            if action.keys().any(|key| !matches!(key.as_str(),
                "type" | "action_id" | "voxel" | "expected_sprite"))
            {
                error_response(state, request.id, "invalid_action", "unknown sign read field");
                return;
            }
            let voxel = match voxel_arg(action, "voxel") {
                Ok(voxel) => voxel,
                Err(message) => {
                    error_response(state, request.id, "invalid_action", message);
                    return;
                },
            };
            let terrain = client.state().terrain();
            let Some(block) = terrain.get(voxel).ok() else {
                error_response(state, request.id, "terrain_unknown", "sign voxel is not loaded");
                return;
            };
            let sprite = block.get_sprite();
            if sprite.is_none_or(|sprite|
                action.get("expected_sprite").and_then(Value::as_str)
                    != Some(format!("{sprite:?}").as_str()))
                || !craft_station_distance(client, voxel).is_some_and(craft_station_in_range)
            {
                error_response(state, request.id, "sign_changed",
                    "selected sign sprite or reading range no longer matches current terrain");
                return;
            }
            let content = sprite.and_then(|sprite|
                sprite.content(terrain.sprite_cfg_at(voxel).cloned()));
            let Some(content) = content else {
                error_response(state, request.id, "sign_content_unavailable",
                    "current loaded sprite has no readable configured content");
                return;
            };
            immediate_action(client, state, request.id, action_id, action_type, signature,
                json!({"observed_content": content, "world_effect": null,
                       "scope": "current_loaded_terrain_sprite_cfg"}));
            return;
        },
        "dialogue_action" => {
            if action.keys().any(|key| !matches!(key.as_str(),
                "type" | "action_id" | "operation" | "sender_uid"
                | "dialogue_id" | "tag" | "response_id"))
            {
                error_response(state, request.id, "invalid_action", "unknown dialogue action field");
                return;
            }
            let Some((sender, dialogue)) = state.dialogue.as_ref() else {
                error_response(state, request.id, "dialogue_unavailable",
                    "no current server dialogue event is selected");
                return;
            };
            if action.get("sender_uid").and_then(Value::as_u64) != Some(sender.0.get())
                || action.get("dialogue_id") != Some(&json!(dialogue.id))
            {
                error_response(state, request.id, "dialogue_changed",
                    "selected sender or dialogue ID differs from latest server dialogue event");
                return;
            }
            let Some(position) = client.position() else {
                error_response(state, request.id, "actor_state_unavailable", "position unavailable");
                return;
            };
            let sender_uid = sender.0.get();
            if native_interactions::entity_reference(client, state, sender_uid)
                != state.interactions.speaker
            {
                error_response(state, request.id, "dialogue_sender_changed",
                    "speaker lifecycle differs from the received dialogue");
                return;
            }
            if client.is_dead() || !nearby_entities(client, state, position).iter()
                .any(|row| row["uid"].as_u64() == Some(sender_uid)
                    && row["distance"].as_f64().is_some_and(|distance|
                        distance < common::consts::MAX_INTERACT_RANGE as f64))
            {
                error_response(state, request.id, "dialogue_sender_unavailable",
                    "speaker no longer observed nearby or actor is dead");
                return;
            }
            let Some(entity) = entity_by_uid(client, sender_uid) else {
                error_response(state, request.id, "dialogue_sender_unavailable", "speaker unavailable");
                return;
            };
            let operation = action.get("operation").and_then(Value::as_str);
            let kind = match (operation, &dialogue.kind) {
                (Some("ack"), common::rtsim::DialogueKind::Statement { tag, .. })
                    if action.get("tag").and_then(Value::as_u64) == Some(*tag as u64)
                        && !action.contains_key("response_id") =>
                    common::rtsim::DialogueKind::Ack { tag: *tag },
                (Some("respond"), common::rtsim::DialogueKind::Question { tag, responses, .. })
                    if action.get("tag").and_then(Value::as_u64) == Some(*tag as u64) => {
                    let Some((response_id, response)) = action.get("response_id")
                        .and_then(Value::as_u64).and_then(|id| responses.iter()
                            .find(|(candidate, _)| *candidate as u64 == id))
                    else {
                        error_response(state, request.id, "dialogue_response_unknown",
                            "selected response is not an offered choice in the latest question");
                        return;
                    };
                    common::rtsim::DialogueKind::Response {
                        tag: *tag, response: response.clone(), response_id: *response_id,
                    }
                },
                _ => {
                    error_response(state, request.id, "invalid_action",
                        "dialogue action must match a current statement tag or offered question choice");
                    return;
                },
            };
            client.perform_dialogue(entity, common::rtsim::Dialogue {
                id: dialogue.id, kind, private_settlement: None,
            });
            state.dialogue = None;
            immediate_action(client, state, request.id, action_id, action_type, signature,
                json!({"submitted_to_server": true, "dialogue_advance_confirmed": null}));
            return;
        },
        "context_interact" => {
            if action.keys().any(|key| !matches!(key.as_str(),
                "type" | "action_id" | "operation" | "target"
                | "voxel" | "expected_sprite" | "enabled"))
            {
                error_response(state, request.id, "invalid_action", "unknown interaction field");
                return;
            }
            if client.current::<comp::Health>().is_none_or(|health| health.is_dead) {
                error_response(state, request.id, "actor_unavailable", "interact requires a living actor");
                return;
            }
            let operation = action.get("operation").and_then(Value::as_str);
            match operation {
                Some("collect" | "toggle_world_light") if action.get("target").is_none() => {
                    let voxel = match voxel_arg(action, "voxel") {
                        Ok(voxel) => voxel,
                        Err(message) => {
                            error_response(state, request.id, "invalid_action", message);
                            return;
                        },
                    };
                    let Some(block) = client.state().terrain().get(voxel).ok().copied() else {
                        error_response(state, request.id, "terrain_unknown",
                            "selected interaction voxel is not currently loaded");
                        return;
                    };
                    let expected = action.get("expected_sprite").and_then(Value::as_str);
                    if expected.is_none() || block.get_sprite()
                        .is_none_or(|sprite| Some(format!("{sprite:?}").as_str()) != expected)
                        || !craft_station_distance(client, voxel).is_some_and(craft_station_in_range)
                    {
                        error_response(state, request.id, "interaction_target_changed",
                            "sprite or range no longer matches selected loaded interaction voxel");
                        return;
                    }
                    if operation == Some("collect") && !action.contains_key("enabled")
                        && block.is_directly_collectible()
                    {
                        client.collect_block(voxel);
                    } else if operation == Some("toggle_world_light")
                        && let Some(enabled) = action.get("enabled").and_then(Value::as_bool)
                        && block.get_sprite().is_some_and(|sprite|
                            sprite.category() == common::terrain::sprite::Category::Lamp)
                        && let Ok(common::terrain::sprite::LightEnabled(current)) = block.get_attr()
                        && enabled != current
                    {
                        client.toggle_sprite_light(common::mounting::VolumePos {
                            kind: Volume::Terrain, pos: voxel,
                        }, enabled);
                    } else {
                        error_response(state, request.id, "interaction_ineligible",
                            "selected block is not directly collectible or its light state cannot change");
                        return;
                    }
                },
                Some("talk" | "help_downed" | "activate_portal")
                    if action.get("voxel").is_none()
                    && action.get("expected_sprite").is_none()
                    && action.get("enabled").is_none() => {
                    let Some(position) = client.position() else {
                        error_response(state, request.id, "actor_state_unavailable", "position unavailable");
                        return;
                    };
                    let Some(target_value) = action.get("target") else {
                        error_response(state, request.id, "invalid_action", "target UID/lifecycle required");
                        return;
                    };
                    let target: continuous_control::EntityRef = match serde_json::from_value::<continuous_control::EntityRef>(target_value.clone()) {
                        Ok(target) if target.uid != 0 => target,
                        _ => {
                            error_response(state, request.id, "invalid_action", "invalid target UID/lifecycle");
                            return;
                        },
                    };
                    if !nearby_entities(client, state, position).iter()
                        .any(|row| row["control_target"] == *target_value
                            && row["distance"].as_f64().is_some_and(|distance| distance <= MAX_PICKUP_RANGE as f64))
                    {
                        error_response(state, request.id, "interaction_target_changed",
                            "target is not the current nearby UID/lifecycle in interaction range");
                        return;
                    }
                    let Some(entity) = entity_by_uid(client, target.uid) else {
                        error_response(state, request.id, "interaction_target_changed", "target entity unavailable");
                        return;
                    };
                    let body = client.state().read_storage::<comp::Body>().get(entity).copied();
                    let target_downed = client.state().read_storage::<comp::Health>()
                        .get(entity).is_some_and(|health|
                            !health.is_dead && health.has_consumed_death_protection());
                    if operation == Some("talk") && body.is_some_and(|body| body.is_humanoid())
                        && !client.player_list().keys().any(|uid| uid.0.get() == target.uid)
                    {
                        client.npc_interact(entity);
                    } else if operation == Some("activate_portal")
                        && body.is_some_and(|body| body.is_portal())
                    {
                        client.activate_portal(entity);
                    } else if operation == Some("help_downed")
                        && target_downed
                    {
                        client.help_downed(entity);
                    } else {
                        error_response(state, request.id, "interaction_ineligible",
                            "target type or downed state does not match selected interaction");
                        return;
                    }
                },
                _ => {
                    error_response(state, request.id, "invalid_action", "invalid contextual interaction fields");
                    return;
                },
            }
            immediate_action(client, state, request.id, action_id, action_type, signature,
                json!({"operation": operation, "submitted_to_server": true,
                       "world_effect_confirmed": null}));
            return;
        },
        "craft_primary_component" => {
            if action.keys().any(|key| !matches!(key.as_str(),
                "type" | "action_id" | "toolkind" | "material_slot"
                | "modifier_slot" | "station"))
            {
                error_response(state, request.id, "invalid_action", "unknown component recipe field");
                return;
            }
            if client.is_dead() {
                error_response(state, request.id, "actor_dead", "crafting requires a living actor");
                return;
            }
            let toolkind: comp::item::tool::ToolKind = match action.get("toolkind").cloned()
                .map(serde_json::from_value).transpose()
            {
                Ok(Some(kind)) => kind,
                _ => {
                    error_response(state, request.id, "invalid_action", "unknown tool kind");
                    return;
                },
            };
            let Some(material) = action.get("material_slot").and_then(Value::as_u64)
                .filter(|slot| *slot <= u32::MAX as u64)
            else {
                error_response(state, request.id, "invalid_action", "material_slot must be packed uint32");
                return;
            };
            let modifier = action.get("modifier_slot").and_then(Value::as_u64);
            if action.get("modifier_slot").is_some() && modifier.is_none_or(|slot| slot > u32::MAX as u64) {
                error_response(state, request.id, "invalid_action", "modifier_slot must be packed uint32");
                return;
            }
            let material = material as u32;
            let material = InvSlotId::new((material >> 16) as u16, material as u16);
            let modifier = modifier.map(|packed| {
                let packed = packed as u32;
                InvSlotId::new((packed >> 16) as u16, packed as u16)
            });
            let Some(inventory) = client.current::<comp::Inventory>() else {
                error_response(state, request.id, "inventory_unavailable", "inventory unavailable");
                return;
            };
            let id = |slot: InvSlotId| inventory.get(slot)
                .and_then(|item| item.item_definition_id().itemdef_id().map(str::to_owned));
            let Some(material_id) = id(material) else {
                error_response(state, request.id, "material_unavailable", "material slot no longer occupied");
                return;
            };
            let modifier_id = modifier.and_then(id);
            if modifier.is_some() && modifier_id.is_none() {
                error_response(state, request.id, "modifier_unavailable", "modifier slot no longer occupied");
                return;
            }
            let key = common::recipe::ComponentKey {
                toolkind, material: material_id, modifier: modifier_id,
            };
            let Some(recipe) = client.component_recipe_book().get(&key) else {
                error_response(state, request.id, "recipe_unknown", "no current component recipe for selected items");
                return;
            };
            if !inventory.recipe_is_known(&recipe.recipe_book_key) {
                error_response(state, request.id, "recipe_not_known",
                    "component recipe not known to actor inventory");
                return;
            }
            let additional = match recipe.inventory_contains_additional_ingredients(&inventory) {
                Ok(slots) => slots,
                Err(_) => {
                    error_response(state, request.id, "ingredients_unavailable",
                        "additional component ingredients are not currently available");
                    return;
                },
            };
            let station = match (recipe.craft_sprite, action.get("station")) {
                (None, None) => None,
                (Some(required), Some(_)) => {
                    let voxel = match voxel_arg(action, "station") {
                        Ok(voxel) => voxel,
                        Err(message) => {
                            error_response(state, request.id, "invalid_action", message);
                            return;
                        },
                    };
                    if client.state().terrain().get(voxel).ok()
                        .and_then(|block| block.get_sprite()) != Some(required)
                        || !craft_station_distance(client, voxel).is_some_and(craft_station_in_range)
                    {
                        error_response(state, request.id, "station_unavailable",
                            "recipe requires matching loaded station within pickup range");
                        return;
                    }
                    Some(VolumePos { kind: Volume::Terrain, pos: voxel })
                },
                _ => {
                    error_response(state, request.id, "station_mismatch",
                        "station must be present iff this component recipe requires one");
                    return;
                },
            };
            drop(inventory);
            client.craft_modular_weapon_component(toolkind, material, modifier, additional, station);
            immediate_action(client, state, request.id, action_id, action_type, signature,
                json!({"submitted_to_server": true, "component_craft_confirmed": null}));
            return;
        },
        "craft_component_action" => {
            if action.keys().any(|key| !matches!(key.as_str(),
                "type" | "action_id" | "operation" | "station"
                | "slot" | "other_slot" | "equip_slot"))
            {
                error_response(state, request.id, "invalid_action", "unknown component crafting field");
                return;
            }
            if client.is_dead() {
                error_response(state, request.id, "actor_dead", "crafting requires a living actor");
                return;
            }
            let operation = action.get("operation").and_then(Value::as_str);
            let station = match voxel_arg(action, "station") {
                Ok(voxel) => voxel,
                Err(message) => {
                    error_response(state, request.id, "invalid_action", message);
                    return;
                },
            };
            let expected_sprite = match operation {
                Some("salvage") => SpriteKind::DismantlingBench,
                Some("repair") => SpriteKind::RepairBench,
                Some("modular_weapon") => SpriteKind::CraftingBench,
                _ => {
                    error_response(state, request.id, "invalid_action", "unknown crafting operation");
                    return;
                },
            };
            if client.state().terrain().get(station).ok()
                .and_then(|block| block.get_sprite()) != Some(expected_sprite)
                || !craft_station_distance(client, station).is_some_and(craft_station_in_range)
            {
                error_response(state, request.id, "station_unavailable",
                    "selected station must be loaded, matching, and within server interaction range");
                return;
            }
            let Some(inventory) = client.current::<comp::Inventory>() else {
                error_response(state, request.id, "inventory_unavailable", "inventory unavailable");
                return;
            };
            let result = match operation {
                Some("salvage") if action.get("other_slot").is_none()
                    && action.get("equip_slot").is_none() => {
                    let slot = inv_slot_from_action(action);
                    match slot {
                        Ok(slot) if inventory.get(slot).is_some() => Ok((Some(slot), None, None)),
                        Ok(_) => Err("selected salvage slot is empty".to_owned()),
                        Err(message) => Err(message),
                    }
                },
                Some("repair") if action.get("other_slot").is_none() => {
                    if let Some(equip_name) = action.get("equip_slot").and_then(Value::as_str) {
                        if action.get("slot").is_some() {
                            Err("repair requires one inventory or equipment slot".to_owned())
                        } else {
                            match parse_equip_slot(equip_name) {
                                Some(equip) if inventory.equipped(equip).is_some() =>
                                    Ok((None, None, Some(equip))),
                                _ => Err("selected equipment slot is empty or invalid".to_owned()),
                            }
                        }
                    } else {
                        match inv_slot_from_action(action) {
                            Ok(slot) if inventory.get(slot).is_some() => Ok((Some(slot), None, None)),
                            Ok(_) => Err("selected repair slot is empty".to_owned()),
                            Err(message) => Err(message),
                        }
                    }
                },
                Some("modular_weapon") if action.get("equip_slot").is_none() => {
                    let a = inv_slot_from_action(action);
                    let b = action.get("other_slot").and_then(Value::as_u64)
                        .filter(|slot| *slot <= u32::MAX as u64)
                        .map(|packed| {
                            let packed = packed as u32;
                            InvSlotId::new((packed >> 16) as u16, packed as u16)
                        });
                    match (a, b) {
                        (Ok(a), Some(b)) if a != b
                            && inventory.get(a).is_some() && inventory.get(b).is_some() =>
                                Ok((Some(a), Some(b), None)),
                        _ => Err("modular weapon requires two different occupied component slots".to_owned()),
                    }
                },
                _ => Err("craft operation fields are invalid".to_owned()),
            };
            let (slot, other, equip) = match result {
                Ok(selected) => selected,
                Err(message) => {
                    error_response(state, request.id, "invalid_action", message);
                    return;
                },
            };
            drop(inventory);
            let location = common::mounting::VolumePos {
                kind: Volume::Terrain, pos: station,
            };
            let submitted = match operation {
                Some("salvage") => client.salvage_item(slot.unwrap(), location),
                Some("repair") => client.repair_item(equip.map(Slot::Equip)
                    .unwrap_or_else(|| Slot::Inventory(slot.unwrap())), location),
                Some("modular_weapon") =>
                    client.craft_modular_weapon(slot.unwrap(), other.unwrap(), Some(location)),
                _ => unreachable!(),
            };
            if !submitted {
                error_response(state, request.id, "craft_precondition_failed",
                    "selected item(s) are no longer valid for this crafting operation");
                return;
            }
            immediate_action(client, state, request.id, action_id, action_type, signature,
                json!({"operation": operation, "submitted_to_server": true,
                       "craft_effect_confirmed": null}));
            return;
        },
        "inventory_action" => {
            if action.keys().any(|key| !matches!(key.as_str(),
                "type" | "action_id" | "operation" | "slot" | "other_slot"
                | "equip_slot" | "sort_order"))
            {
                error_response(state, request.id, "invalid_action", "unknown inventory action field");
                return;
            }
            if client.is_dead() {
                error_response(state, request.id, "actor_dead", "inventory action requires a living actor");
                return;
            }
            let operation = action.get("operation").and_then(Value::as_str);
            let inventory = client.current::<comp::Inventory>();
            let selected = |key: &str| -> Result<InvSlotId, String> {
                let packed = action.get(key).and_then(Value::as_u64)
                    .filter(|value| *value <= u32::MAX as u64)
                    .ok_or_else(|| format!("{key} must be an observed packed inventory slot"))? as u32;
                let slot = InvSlotId::new((packed >> 16) as u16, packed as u16);
                if !inventory.as_ref().is_some_and(|inv|
                    inv.slots_with_id().any(|(available, _)| available == slot))
                {
                    return Err(format!("{key} is not a current inventory slot"));
                }
                Ok(slot)
            };
            let result = match operation {
                Some("swap" | "split_swap") if action.get("other_slot").is_some()
                    && action.get("slot").is_some()
                    && action.get("equip_slot").is_none()
                    && action.get("sort_order").is_none() => {
                    let a = selected("slot");
                    let b = selected("other_slot");
                    match (a, b) {
                        (Ok(a), Ok(b)) if a != b
                            && inventory.as_ref().is_some_and(|inv| inv.get(a).is_some()
                                || inv.get(b).is_some()) =>
                            Ok((Some(a), Some(b), None)),
                        (Ok(_), Ok(_)) => Err("different slots with an item are required".to_owned()),
                        (Err(message), _) | (_, Err(message)) => Err(message),
                    }
                },
                Some("unequip") if action.get("equip_slot").is_some()
                    && action.get("slot").is_some()
                    && action.get("other_slot").is_none()
                    && action.get("sort_order").is_none() => {
                    let equip = action.get("equip_slot").and_then(Value::as_str)
                        .and_then(parse_equip_slot)
                        .ok_or_else(|| "invalid equipment slot".to_owned());
                    match (selected("slot"), equip) {
                        (Ok(slot), Ok(equip)) if inventory.as_ref().is_some_and(|inv|
                            inv.equipped(equip).is_some() && inv.get(slot).is_none()) =>
                            Ok((Some(slot), None, Some(equip))),
                        (Ok(_), Ok(_)) => Err("equipment must exist and destination slot be empty".to_owned()),
                        (Err(message), _) | (_, Err(message)) => Err(message),
                    }
                },
                Some("sort") if action.get("slot").is_none()
                    && action.get("other_slot").is_none()
                    && action.get("equip_slot").is_none() => {
                    if action.get("sort_order").and_then(Value::as_str)
                        .is_some_and(|order| matches!(order,
                            "Name" | "Quality" | "Category" | "Tag" | "Amount"))
                        && inventory.is_some()
                    {
                        Ok((None, None, None))
                    } else { Err("sort requires an available inventory and a known order".to_owned()) }
                },
                _ => Err("inventory operation or fields are invalid".to_owned()),
            };
            let (a, b, equip) = match result {
                Ok(selection) => selection,
                Err(message) => {
                    error_response(state, request.id, "invalid_inventory_action", message);
                    return;
                },
            };
            drop(inventory);
            match operation {
                Some("swap") => client.swap_slots(Slot::Inventory(a.unwrap()), Slot::Inventory(b.unwrap())),
                Some("split_swap") => client.split_swap_slots(Slot::Inventory(a.unwrap()), Slot::Inventory(b.unwrap())),
                Some("unequip") => client.swap_slots(Slot::Equip(equip.unwrap()), Slot::Inventory(a.unwrap())),
                Some("sort") => {
                    use common::comp::inventory::InventorySortOrder;
                    let order = match action.get("sort_order").and_then(Value::as_str) {
                        Some("Name") => InventorySortOrder::Name,
                        Some("Quality") => InventorySortOrder::Quality,
                        Some("Category") => InventorySortOrder::Category,
                        Some("Tag") => InventorySortOrder::Tag,
                        Some("Amount") => InventorySortOrder::Amount,
                        _ => unreachable!(),
                    };
                    client.sort_inventory(order);
                },
                _ => unreachable!(),
            }
            immediate_action(client, state, request.id, action_id, action_type, signature,
                json!({"submitted_to_server": true, "inventory_effect_confirmed": null,
                       "operation": operation}));
            return;
        },
        "drop" => {
            let split = match action.get("split") {
                None => false,
                Some(Value::Bool(value)) => *value,
                Some(_) => {
                    error_response(state, request.id, "invalid_action", "split must be boolean");
                    return;
                },
            };
            let slot = if action.get("slot").is_some()
                || action.get("loadout_idx").is_some()
                || action.get("slot_idx").is_some()
            {
                match inv_slot_from_action(action) {
                    Ok(slot) => Slot::Inventory(slot),
                    Err(message) => {
                        error_response(state, request.id, "invalid_action", message);
                        return;
                    },
                }
            } else if let Some(name) = action.get("equip_slot").and_then(Value::as_str) {
                let Some(equip_slot) = parse_equip_slot(name) else {
                    error_response(
                        state,
                        request.id,
                        "invalid_action",
                        format!("unknown equip_slot: {name}"),
                    );
                    return;
                };
                Slot::Equip(equip_slot)
            } else {
                error_response(
                    state,
                    request.id,
                    "invalid_action",
                    "drop requires slot or equip_slot",
                );
                return;
            };
            let inventory = client.current::<comp::Inventory>();
            let start_item_count = inventory
                .as_ref()
                .map(|inv| inventory_item_count(inv))
                .unwrap_or(0);
            let start_free_slots = inventory
                .as_ref()
                .map(|inv| inv.free_slots())
                .unwrap_or(0);
            if split && !matches!(slot, Slot::Inventory(_)) {
                error_response(state, request.id, "invalid_action",
                    "split drop requires an observed inventory slot, not equipment");
                return;
            }
            if split { client.split_drop_slot(slot); }
            else { client.drop_slot(slot); }
            ActiveKind::InteractWait {
                mode: InteractWait::Drop {
                    start_item_count,
                    start_free_slots,
                },
            }
        },
        "equip" => {
            let inv_slot = match inv_slot_from_action(action) {
                Ok(slot) => slot,
                Err(message) => {
                    error_response(state, request.id, "invalid_action", message);
                    return;
                },
            };
            let equip_name = action
                .get("equip_slot")
                .and_then(Value::as_str)
                .unwrap_or("active_mainhand");
            let Some(equip_slot) = parse_equip_slot(equip_name) else {
                error_response(
                    state,
                    request.id,
                    "invalid_action",
                    format!("unknown equip_slot: {equip_name}"),
                );
                return;
            };
            let Some(expected_item_id) = client
                .current::<comp::Inventory>()
                .as_ref()
                .and_then(|inv| inv.get(inv_slot))
                .map(|item| match item.item_definition_id().to_owned() {
                    common::comp::inventory::item::ItemDefinitionIdOwned::Simple(id) => id,
                    other => format!("{other:?}"),
                })
            else {
                error_response(
                    state,
                    request.id,
                    "invalid_action",
                    "inventory slot is empty",
                );
                return;
            };
            client.swap_slots(Slot::Inventory(inv_slot), Slot::Equip(equip_slot));
            ActiveKind::InteractWait {
                mode: InteractWait::Equip {
                    equip_slot,
                    expected_item_id,
                },
            }
        },
        "swap_weapons" => {
            client.swap_loadout();
            immediate_action(
                client,
                state,
                request.id,
                action_id,
                action_type,
                signature,
                json!({ "swapped": true }),
            );
            return;
        },
        "chat_channel" => {
            if action.keys().any(|key| !matches!(key.as_str(),
                "type" | "action_id" | "channel" | "message"))
            {
                error_response(state, request.id, "invalid_action", "unknown channel chat field");
                return;
            }
            let message = match chat_message_arg(action) {
                Ok(message) if !message.trim().is_empty() => message,
                Ok(_) => {
                    error_response(state, request.id, "invalid_action", "message must be nonempty");
                    return;
                },
                Err(message) => {
                    error_response(state, request.id, "invalid_action", message);
                    return;
                },
            };
            let Some(channel) = action.get("channel").and_then(Value::as_str)
                .filter(|channel| matches!(*channel, "group" | "region" | "say" | "world"))
            else {
                error_response(state, request.id, "invalid_action", "unknown ordinary chat channel");
                return;
            };
            if channel == "group" && client.group_info().is_none() {
                error_response(state, request.id, "group_unavailable",
                    "group channel requires current group membership");
                return;
            }
            client.send_command(channel.to_owned(), vec![message.to_owned()]);
            immediate_action(client, state, request.id, action_id, action_type, signature,
                json!({"channel": channel, "submitted_to_server": true,
                       "delivery_confirmed": null,
                       "server_may_change_default_chat_mode": true}));
            return;
        },
        "chat" => {
            let message = match chat_message_arg(action) {
                Ok(message) => message,
                Err(message) => {
                    error_response(state, request.id, "invalid_action", message);
                    return;
                },
            };
            client.send_chat(message.to_owned());
            immediate_action(
                client,
                state,
                request.id,
                action_id,
                action_type,
                signature,
                json!({ "message": message }),
            );
            return;
        },
        "private_chat" => {
            let (recipient, message) = match private_chat_args(action) {
                Ok(value) => value,
                Err(message) => {
                    error_response(state, request.id, "invalid_action", message);
                    return;
                },
            };
            client.send_command("tell".to_owned(), vec![recipient.to_owned(), message.to_owned()]);
            immediate_action(
                client, state, request.id, action_id, action_type, signature,
                json!({"channel": "tell", "recipient_alias": recipient,
                       "submitted_to_server": true, "delivery_confirmed": null}),
            );
            return;
        },
        "character_action" => {
            let kind = match action.get("kind").and_then(Value::as_str) {
                Some(kind) if matches!(kind, "lantern" | "sneak" | "crawl"
                                       | "dance" | "glide" | "greet" | "cancel_climb") => kind,
                _ => {
                    error_response(state, request.id, "invalid_action", "unknown character action");
                    return;
                },
            };
            if action.keys().any(|key| !matches!(key.as_str(), "type" | "action_id" | "kind" | "enabled"))
                || matches!(kind, "greet" | "cancel_climb") && action.contains_key("enabled")
                || !matches!(kind, "greet" | "cancel_climb")
                    && !action.get("enabled").is_some_and(Value::is_boolean)
            {
                error_response(state, request.id, "invalid_action", "invalid character action fields");
                return;
            }
            let enabled = action.get("enabled").and_then(Value::as_bool);
            if kind == "lantern" {
                if client.current::<comp::Inventory>().as_ref()
                    .and_then(|inventory| inventory.equipped(EquipSlot::Lantern)).is_none()
                {
                    error_response(state, request.id, "lantern_not_equipped", "no equipped lantern");
                    return;
                }
                if enabled == Some(true) && client.is_dead() {
                    error_response(state, request.id, "actor_dead", "dead actor cannot light a lantern");
                    return;
                }
                if enabled == Some(true) { client.enable_lantern(); }
                else { client.disable_lantern(); }
            } else if kind == "glide" && enabled == Some(true)
                && client.current::<comp::Inventory>().as_ref()
                    .and_then(|inventory| inventory.equipped(EquipSlot::Glider)).is_none()
            {
                error_response(state, request.id, "glider_not_equipped", "no equipped glider");
                return;
            } else if kind == "greet" {
                client.utter(comp::UtteranceKind::Greeting);
            } else {
                let Some(character) = client.current::<comp::CharacterState>() else {
                    error_response(state, request.id, "state_unavailable", "character state unavailable");
                    return;
                };
                let current = match kind {
                    "sneak" => character.is_stealthy(),
                    "crawl" => matches!(character, comp::CharacterState::Crawl),
                    "dance" => matches!(character, comp::CharacterState::Dance),
                    "glide" => matches!(character, comp::CharacterState::GlideWield(_)
                        | comp::CharacterState::Glide(_)),
                    "cancel_climb" => matches!(character, comp::CharacterState::Climb(_)),
                    _ => unreachable!(),
                };
                if kind == "cancel_climb" {
                    if !current {
                        error_response(state, request.id, "not_climbing", "climb state not observed");
                        return;
                    }
                    client.cancel_climb();
                } else if enabled != Some(current) {
                    match kind {
                        "sneak" => client.toggle_sneak(),
                        "crawl" => client.toggle_crawl(),
                        "dance" => client.toggle_dance(),
                        "glide" => client.toggle_glide(),
                        _ => unreachable!(),
                    }
                }
            }
            immediate_action(
                client, state, request.id, action_id, action_type, signature,
                json!({"kind": kind, "requested_enabled": enabled,
                       "submitted_to_server": true, "state_confirmed": false}),
            );
            return;
        },
        "map_marker" => {
            let operation = action.get("operation").and_then(Value::as_str);
            let change = match map_marker_request(action) {
                Ok(change) => change,
                Err(message) => {
                    error_response(state, request.id, "invalid_action", message);
                    return;
                },
            };
            client.map_marker_event(change);
            immediate_action(
                client, state, request.id, action_id, action_type, signature,
                json!({"operation": operation, "submitted_to_server": true,
                       "persisted_or_observed": false}),
            );
            return;
        },
        "build_block" => {
            let pos = match integer_voxel_arg(action, "position") {
                Ok(pos) => pos,
                Err(message) => {
                    error_response(state, request.id, "invalid_action", message);
                    return;
                },
            };
            let Some(health) = client.current::<comp::Health>() else {
                error_response(state, request.id, "actor_state_unavailable", "actor health unavailable");
                return;
            };
            if health.is_dead {
                error_response(state, request.id, "actor_dead", "dead actor cannot build");
                return;
            }
            let permission = client.current::<comp::CanBuild>()
                .map(|value| (value.enabled, value.build_areas.len()));
            let current = match real_terrain_block(&client.state().terrain(), pos) {
                Ok(current) => current,
                Err(code) => {
                    error_response(state, request.id, code,
                        "cannot build outside the actual world; synthetic Air is not writable terrain");
                    return;
                },
            };
            let new_block = match build_request(action, permission, current) {
                Ok(block) => block,
                Err(message) => {
                    error_response(state, request.id, "build_precondition_failed", message);
                    return;
                },
            };
            if let Some(block) = new_block { client.place_block(pos, block); }
            else { client.remove_block(pos); }
            immediate_action(
                client, state, request.id, action_id, action_type, signature,
                json!({"position": [pos.x, pos.y, pos.z],
                       "permission_enabled": true,
                       "assigned_area_count": permission.unwrap().1,
                       "server_area_membership": null,
                       "server_mutation_confirmed": null,
                       "submitted_to_server": true}),
            );
            return;
        },
        "companion_action" => {
            if action.keys().any(|key| !matches!(key.as_str(),
                "type" | "action_id" | "operation" | "target" | "volume_kind"
                | "volume_pos" | "stay"))
            {
                error_response(state, request.id, "invalid_action", "unknown companion action field");
                return;
            }
            let Some(position) = client.position() else {
                error_response(state, request.id, "actor_state_unavailable", "actor position unavailable");
                return;
            };
            let Some(actor_uid) = client.uid() else {
                error_response(state, request.id, "actor_state_unavailable", "actor UID unavailable");
                return;
            };
            if client.current::<comp::Health>().is_none_or(|health| health.is_dead) {
                error_response(state, request.id, "actor_unavailable", "actor health missing or dead");
                return;
            }
            let operation = action.get("operation").and_then(Value::as_str);
            let target = if let Some(value) = action.get("target") {
                let target: continuous_control::EntityRef =
                    match serde_json::from_value(value.clone()) {
                        Ok(target) => target,
                        Err(_) => {
                            error_response(state, request.id, "invalid_action", "invalid target UID and lifecycle");
                            return;
                        },
                    };
                let observed = nearby_entities(client, state, position).iter()
                    .any(|row| row["control_target"] == *value);
                if !observed || target.uid == 0 {
                    error_response(state, request.id, "companion_target_changed",
                        "target UID/lifecycle is not currently observed nearby");
                    return;
                }
                let Some(entity) = entity_by_uid(client, target.uid) else {
                    error_response(state, request.id, "companion_target_changed", "target entity missing");
                    return;
                };
                Some(entity)
            } else { None };
            if client.is_riding() && matches!(operation, Some("mount_entity" | "mount_volume")) {
                error_response(state, request.id, "already_riding", "unmount before changing mounts");
                return;
            }
            match operation {
                Some("unmount") if target.is_none() && !action.contains_key("volume_kind")
                    && !action.contains_key("volume_pos") && !action.contains_key("stay") =>
                {
                    if !client.is_riding() {
                        error_response(state, request.id, "not_riding", "actor has no rider link");
                        return;
                    }
                    client.unmount();
                },
                Some("mount_entity") if let Some(entity) = target
                    && !action.contains_key("volume_kind")
                    && !action.contains_key("volume_pos") && !action.contains_key("stay") =>
                {
                    let eligible = {
                        let ecs = client.state().ecs();
                        let positions = ecs.read_storage::<comp::Pos>();
                        let bodies = ecs.read_storage::<comp::Body>();
                        let masses = ecs.read_storage::<comp::Mass>();
                        let owned = ecs.read_storage::<comp::Alignment>().get(entity)
                            .is_some_and(|alignment| matches!(alignment,
                                comp::Alignment::Owned(owner) if *owner == actor_uid));
                        let mountable = bodies.get(entity).zip(masses.get(entity))
                            .is_some_and(|(body, mass)| common::comp::pet::is_mountable(
                                body, mass, bodies.get(client.entity()), masses.get(client.entity())));
                        owned && mountable && positions.get(entity).is_some_and(|target_pos|
                            position.distance_squared(target_pos.0) < MAX_MOUNT_RANGE.powi(2))
                            && ecs.read_storage::<common::link::Is<common::mounting::Mount>>()
                                .get(entity).is_none()
                    };
                    if !eligible {
                        error_response(state, request.id, "mount_ineligible",
                            "observed target is not a nearby owned mountable unoccupied entity");
                        return;
                    }
                    client.mount(entity);
                },
                Some("pet_stay") if let (Some(entity), Some(stay)) =
                    (target, action.get("stay").and_then(Value::as_bool))
                    && !action.contains_key("volume_kind") && !action.contains_key("volume_pos") =>
                {
                    let eligible = {
                        let ecs = client.state().ecs();
                        let positions = ecs.read_storage::<comp::Pos>();
                        let owned = ecs.read_storage::<comp::Alignment>().get(entity)
                            .is_some_and(|alignment| matches!(alignment,
                                comp::Alignment::Owned(owner) if *owner == actor_uid));
                        owned && positions.get(entity).is_some_and(|target_pos|
                            position.distance_squared(target_pos.0) < MAX_MOUNT_RANGE.powi(2))
                            && ecs.read_storage::<common::link::Is<common::mounting::Mount>>()
                                .get(entity).is_none()
                    };
                    if !eligible {
                        error_response(state, request.id, "pet_not_owned_or_nearby",
                            "pet is not owned, within server range or unmounted");
                        return;
                    }
                    client.set_pet_stay(entity, stay);
                },
                Some("mount_volume") if !action.contains_key("stay") => {
                    let volume_position = match integer_voxel_arg(action, "volume_pos") {
                        Ok(pos) => pos,
                        Err(message) => {
                            error_response(state, request.id, "invalid_action", message);
                            return;
                        },
                    };
                    let volume = match action.get("volume_kind").and_then(Value::as_str) {
                        Some("terrain") if target.is_none() =>
                            VolumePos::terrain(volume_position),
                        Some("entity") if let Some(carrier) = target
                            && let Some(uid) = client.state().read_storage::<Uid>().get(carrier) =>
                            VolumePos::entity(volume_position, *uid),
                        _ => {
                            error_response(state, request.id, "invalid_action",
                                "volume requires terrain or observed entity carrier");
                            return;
                        },
                    };
                    let valid_seat = {
                        let ecs = client.state().ecs();
                        let id_maps = ecs.read_resource::<IdMaps>();
                        let positions = ecs.read_storage::<comp::Pos>();
                        let orientations = ecs.read_storage::<comp::Ori>();
                        let colliders = ecs.read_storage::<comp::Collider>();
                        volume.get_mount_mat(&client.state().terrain(), &id_maps,
                            |entity| positions.get(entity).copied()
                                .zip(orientations.get(entity).copied()), &colliders)
                            .is_some_and(|(mat, _)| position.distance_squared(
                                mat.mul_point(Vec3::zero())) < MAX_SPRITE_MOUNT_RANGE.powi(2))
                    };
                    if !valid_seat {
                        error_response(state, request.id, "volume_seat_ineligible",
                            "exact loaded seat is not mountable within server range");
                        return;
                    }
                    client.mount_volume(volume);
                },
                _ => {
                    error_response(state, request.id, "invalid_action",
                        "invalid companion operation, target or fields");
                    return;
                },
            }
            immediate_action(
                client, state, request.id, action_id, action_type, signature,
                json!({"operation": operation, "submitted_to_server": true,
                       "server_link_or_pet_state_confirmed": null}),
            );
            return;
        },
        "social_action" => {
            if action.keys().any(|key| !matches!(key.as_str(),
                "type" | "action_id" | "operation" | "target_uid"
                | "target_alias" | "inviter_uid" | "invite_kind" | "target"))
            {
                error_response(state, request.id, "invalid_action", "unknown social action field");
                return;
            }
            let operation = action.get("operation").and_then(Value::as_str);
            let uid = client.uid();
            if uid.is_none() || client.current::<comp::Health>().is_none_or(|health| health.is_dead) {
                error_response(state, request.id, "actor_unavailable", "actor UID/health unavailable or dead");
                return;
            }
            let target_uid = action.get("target_uid").and_then(Value::as_u64);
            let inviter_uid = action.get("inviter_uid").and_then(Value::as_u64);
            let alias = action.get("target_alias").and_then(Value::as_str);
            let kind = action.get("invite_kind").and_then(Value::as_str);
            let selected = target_uid.and_then(|selected| client.player_list().iter()
                .find(|(id, _)| id.0.get() == selected).map(|(id, info)| (*id, info.player_alias.clone())));
            match operation {
                Some("invite_trade") if inviter_uid.is_none() && kind.is_none()
                    && alias.is_none() && target_uid.is_none() => {
                    let Some(target) = action.get("target") else {
                        error_response(state, request.id, "invalid_action",
                            "NPC trade requires observed target UID/lifecycle");
                        return;
                    };
                    let Some(position) = client.position() else {
                        error_response(state, request.id, "actor_unavailable", "position unavailable");
                        return;
                    };
                    let Some(selected) = nearby_entities(client, state, position).iter()
                        .find(|row| row["control_target"] == *target
                            && row["distance"].as_f64().is_some_and(|distance|
                                distance <= common::consts::MAX_TRADE_RANGE as f64))
                        .and_then(|row| row["uid"].as_u64())
                        .and_then(NonZeroU64::new).map(Uid)
                    else {
                        error_response(state, request.id, "trade_target_changed",
                            "trade target is not a currently observed nearby UID/lifecycle");
                        return;
                    };
                    if client.is_trading() || client.pending_invites().contains(&selected) {
                        error_response(state, request.id, "trade_already_pending",
                            "a trade or invitation is already pending");
                        return;
                    }
                    client.send_invite(selected, comp::invite::InviteKind::Trade);
                },
                Some("invite_group" | "invite_trade") if inviter_uid.is_none() && kind.is_none()
                    && !action.contains_key("target")
                    && selected.as_ref().is_some_and(|(_, name)| Some(name.as_str()) == alias)
                    && alias.is_some_and(|name| !name.is_empty()) =>
                {
                    let selected = selected.unwrap().0;
                    if Some(selected) == uid || client.pending_invites().contains(&selected) {
                        error_response(state, request.id, "invite_ineligible",
                            "cannot invite self or repeat a currently pending invitation");
                        return;
                    }
                    if operation == Some("invite_trade") && client.is_trading() {
                        error_response(state, request.id, "trade_already_pending",
                            "decline or settle current trade before inviting another");
                        return;
                    }
                    client.send_invite(selected, if operation == Some("invite_group") {
                        comp::invite::InviteKind::Group
                    } else { comp::invite::InviteKind::Trade });
                },
                Some("respond_accept" | "respond_decline")
                    if target_uid.is_none() && alias.is_none() && !action.contains_key("target") =>
                {
                    let Some((inviter, started, lifetime, incoming_kind)) = client.invite() else {
                        error_response(state, request.id, "no_current_invite", "no pending invite to respond to");
                        return;
                    };
                    if inviter_uid != Some(inviter.0.get())
                        || kind != Some(match incoming_kind {
                            comp::invite::InviteKind::Group => "Group",
                            comp::invite::InviteKind::Trade => "Trade",
                        })
                        || lifetime <= started.elapsed()
                    {
                        error_response(state, request.id, "invite_changed_or_expired",
                            "selected inviter/kind no longer matches an unexpired invite");
                        return;
                    }
                    if operation == Some("respond_accept") { client.accept_invite(); }
                    else { client.decline_invite(); }
                },
                Some("leave_group") if target_uid.is_none() && inviter_uid.is_none()
                    && kind.is_none() && alias.is_none() && !action.contains_key("target") =>
                {
                    if client.group_info().is_none() {
                        error_response(state, request.id, "not_in_group", "no current group");
                        return;
                    }
                    client.leave_group();
                },
                Some("kick_member" | "assign_leader") if inviter_uid.is_none()
                    && kind.is_none() && alias.is_none() && !action.contains_key("target") =>
                {
                    let Some((_, leader)) = client.group_info() else {
                        error_response(state, request.id, "not_in_group", "no current group");
                        return;
                    };
                    if Some(leader) != uid {
                        error_response(state, request.id, "not_group_leader",
                            "only the current group leader can select member management");
                        return;
                    }
                    let Some(target) = target_uid.and_then(|selected| client.group_members()
                        .iter().find(|(member, role)| member.0.get() == selected
                            && **role == common::comp::group::Role::Member).map(|(member, _)| *member))
                    else {
                        error_response(state, request.id, "group_member_changed",
                            "selected UID is not a current non-pet group member");
                        return;
                    };
                    if Some(target) == uid {
                        error_response(state, request.id, "invalid_action", "cannot target the group leader");
                        return;
                    }
                    if operation == Some("kick_member") { client.kick_from_group(target); }
                    else { client.assign_group_leader(target); }
                },
                _ => {
                    error_response(state, request.id, "invalid_action", "invalid social operation or fields");
                    return;
                },
            }
            immediate_action(
                client, state, request.id, action_id, action_type, signature,
                json!({"operation": operation, "submitted_to_server": true,
                       "server_invite_or_group_change_confirmed": null}),
            );
            return;
        },
        "trade_action" => {
            if action.keys().any(|key| !matches!(key.as_str(),
                "type" | "action_id" | "operation" | "trade_id"
                | "counterparty_uid" | "phase" | "slot" | "quantity" | "ours"))
            {
                error_response(state, request.id, "invalid_action", "unknown trade action field");
                return;
            }
            let Some(actor_uid) = client.uid() else {
                error_response(state, request.id, "actor_unavailable", "actor UID unavailable");
                return;
            };
            let Some((id, trade, _)) = client.pending_trade().as_ref() else {
                error_response(state, request.id, "no_pending_trade", "no current trade");
                return;
            };
            let ours = match selected_trade_party(action, actor_uid, *id, trade) {
                Ok(ours) => ours,
                Err(message) => {
                    error_response(state, request.id, "trade_identity_or_phase_changed", message);
                    return;
                },
            };
            let operation = action.get("operation").and_then(Value::as_str);
            let own_offer = match action.get("ours") {
                None => true,
                Some(Value::Bool(ours)) => *ours,
                _ => {
                    error_response(state, request.id, "invalid_action", "ours must be boolean");
                    return;
                },
            };
            let selected_party = if own_offer { ours } else { 1 - ours };
            let trade_action = match operation {
                Some("accept") if !action.contains_key("slot") && !action.contains_key("quantity")
                    && !action.contains_key("ours") =>
                    common::trade::TradeAction::Accept(trade.phase),
                Some("decline") if !action.contains_key("slot") && !action.contains_key("quantity")
                    && !action.contains_key("ours") =>
                    common::trade::TradeAction::Decline,
                Some("add_item" | "remove_item") if trade.phase == common::trade::TradePhase::Mutate =>
                {
                    let slot = match inv_slot_from_action(action) {
                        Ok(slot) => slot,
                        Err(message) => {
                            error_response(state, request.id, "invalid_action", message);
                            return;
                        },
                    };
                    let Some(quantity) = action.get("quantity").and_then(Value::as_u64)
                        .filter(|amount| *amount > 0 && *amount <= u32::MAX as u64)
                    else {
                        error_response(state, request.id, "invalid_action",
                            "trade quantity must be a positive u32");
                        return;
                    };
                    let amount = quantity as u32;
                    if operation == Some("add_item") {
                        let inventories = client.inventories();
                        let available = entity_by_uid(client, trade.parties[selected_party].0.get())
                            .and_then(|entity| inventories.get(entity))
                            .and_then(|inventory| inventory.get(slot))
                            .map(|item| item.amount());
                        if available.is_none_or(|available| available
                            < trade.offers[selected_party].get(&slot).copied().unwrap_or(0)
                                .saturating_add(amount))
                        {
                            error_response(state, request.id, "trade_slot_unavailable",
                                "selected party slot lacks the additional offered amount");
                            return;
                        }
                        common::trade::TradeAction::AddItem { item: slot, quantity: amount, ours: own_offer }
                    } else {
                        if trade.offers[selected_party].get(&slot).is_none_or(|offered| *offered < amount) {
                            error_response(state, request.id, "trade_offer_changed",
                                "selected party offer no longer contains this quantity");
                            return;
                        }
                        common::trade::TradeAction::RemoveItem { item: slot, quantity: amount, ours: own_offer }
                    }
                },
                _ => {
                    error_response(state, request.id, "invalid_action",
                        "trade action fields/phase do not match an offer or accept/decline");
                    return;
                },
            };
            client.perform_trade_action(trade_action);
            immediate_action(
                client, state, request.id, action_id, action_type, signature,
                json!({"operation": operation, "submitted_to_server": true,
                       "trade_completion_confirmed": null, "phase_accepted_by_server": null}),
            );
            return;
        },
        "remove_owned_invulnerability" => {
            let snapshot = survival_snapshot(client, state);
            match owned_invulnerability_removal(action, &self_identity(client), &snapshot["buffs"]) {
                Ok(present) => {
                    if present {
                        client.remove_buff(comp::BuffKind::Invulnerability);
                    }
                    immediate_action(client, state, request.id, action_id, action_type,
                        signature, json!({ "removal_sent": present, "effect_confirmed": false }));
                },
                Err(message) => error_response(state, request.id, "ownership_refused", message),
            }
            return;
        },
        "server_command" => {
            let (name, args) = match server_command_args(action) {
                Ok(value) => value,
                Err(message) => {
                    error_response(state, request.id, "invalid_action", message);
                    return;
                },
            };
            client.send_command(name.to_owned(), args.clone());
            immediate_action(
                client,
                state,
                request.id,
                action_id,
                action_type,
                signature,
                json!({ "name": name, "args": args }),
            );
            return;
        },
        _ => {
            error_response(
                state,
                request.id,
                "unsupported_action",
                format!("unsupported action: {action_type}"),
            );
            return;
        },
    };

    let active = ActiveAction {
        request_id: request.id,
        action_id: action_id.clone(),
        action_type: action_type.clone(),
        signature,
        started_tick: state.tick,
        deadline_tick: if action_type == "respawn" {
            state.tick + RESPAWN_OBSERVATION_TICKS
        } else if action_type == "craft" {
            state.tick + timeout_ticks
        } else {
            state.tick + timeout_ticks.max(duration_ticks)
        },
        start_position: client.position(),
        progress_position: client.position(),
        last_progress_tick: state.tick,
        survival_before,
        kind,
    };
    state.active = Some(active);
    record_event(
        state,
        "action_started",
        json!({
            "action_id": action_id,
            "action_type": action_type,
            "status": "running",
        }),
    );
}

pub fn update_active(client: &mut Client, state: &mut BridgeState) {
    let tracked_attack = state.active.as_ref().and_then(|active| match &active.kind {
        ActiveKind::Attack { target_ref: Some(target), .. } =>
            Some(continuous_control::tracked_target_aim(client, state, target)),
        _ => None,
    });
    match tracked_attack {
        Some(Ok((_, direction, _))) => state.look_dir = direction,
        Some(Err(reason)) => {
            finish_active(client, state, "failed", json!({
                "code": reason, "target_tracking": "retired_invalid_target_input",
                "hit_success": null,
            }));
            return;
        },
        None => {},
    }
    let give_up_after = state.active.as_ref().and_then(|active|
        matches!(active.kind, ActiveKind::InteractWait { mode: InteractWait::GiveUp })
            .then(|| survival_snapshot(client, state)));
    let outcome = {
        let Some(active) = state.active.as_mut() else {
            return;
        };
        let started_tick = active.started_tick;
        let start_position = active.start_position;
        if state.tick.saturating_sub(started_tick) >= MAX_MOVE_TO_TICKS
            && let ActiveKind::MoveTo {
                target,
                best_distance,
                chaser,
                max_path_length_reached,
                pending_ticks,
                consecutive_pending_ticks,
                planner_none_ticks,
                chaser_none_ticks,
                stuck_ever,
                stuck_ticks,
                last_bearing,
                last_path_position,
                actual_path_length,
                last_best_distance_tick,
                max_no_best_distance_improvement_ticks,
                ..
            } = &active.kind
        {
            let remaining_distance = client
                .position()
                .map(|position| ground_distance(*target, position))
                .unwrap_or(*best_distance);
            let actual_path_length = actual_path_length_at_termination(
                client,
                *last_path_position,
                *actual_path_length,
            );
            Some((
                "failed",
                move_to_detail(
                    client,
                    state.tick,
                    started_tick,
                    start_position,
                    *target,
                    remaining_distance,
                    chaser,
                    *max_path_length_reached,
                    *pending_ticks,
                    *consecutive_pending_ticks,
                    *planner_none_ticks,
                    *chaser_none_ticks,
                    *stuck_ever,
                    *stuck_ticks,
                    *last_bearing,
                    actual_path_length,
                    *last_best_distance_tick,
                    *max_no_best_distance_improvement_ticks,
                    Some("move_to_max_ticks"),
                ),
            ))
        } else if let Some(after) = give_up_after.as_ref() {
            give_up::outcome(active.survival_before.as_ref().unwrap_or(&Value::Null),
                             after, state.tick >= active.deadline_tick)
        } else if state.tick >= active.deadline_tick {
            match &active.kind {
                ActiveKind::MoveTo {
                    target,
                    best_distance,
                    chaser,
                    max_path_length_reached,
                    pending_ticks,
                    consecutive_pending_ticks,
                    planner_none_ticks,
                    chaser_none_ticks,
                    stuck_ever,
                    stuck_ticks,
                    last_bearing,
                    last_path_position,
                    actual_path_length,
                    last_best_distance_tick,
                    max_no_best_distance_improvement_ticks,
                    ..
                } => {
                    let remaining_distance = client
                        .position()
                        .map(|position| ground_distance(*target, position))
                        .unwrap_or(*best_distance);
                    let actual_path_length = actual_path_length_at_termination(
                        client,
                        *last_path_position,
                        *actual_path_length,
                    );
                    Some((
                        "failed",
                        move_to_detail(
                            client,
                            state.tick,
                            started_tick,
                            start_position,
                            *target,
                            remaining_distance,
                            chaser,
                            *max_path_length_reached,
                            *pending_ticks,
                            *consecutive_pending_ticks,
                            *planner_none_ticks,
                            *chaser_none_ticks,
                            *stuck_ever,
                            *stuck_ticks,
                            *last_bearing,
                            actual_path_length,
                            *last_best_distance_tick,
                            *max_no_best_distance_improvement_ticks,
                            Some("action_timeout"),
                        ),
                    ))
                },
                ActiveKind::Craft {
                    recipe,
                    requested_amount,
                    amount,
                    station,
                    required_sprite,
                    craft_request_sequence,
                    output_definition_id,
                    expected_output_amount,
                    start_inventory,
                    saw_craft_update,
                } => Some((
                    if *saw_craft_update {
                        "completed"
                    } else {
                        "failed"
                    },
                    craft_result_detail(
                        client,
                        recipe,
                        *requested_amount,
                        *amount,
                        *station,
                        *required_sprite,
                        *craft_request_sequence,
                        output_definition_id,
                        *expected_output_amount,
                        start_inventory,
                        *saw_craft_update,
                    ),
                )),
                ActiveKind::InteractWait {
                    mode: InteractWait::Pickup { uid, request_sequence, feedback, .. },
                } => pickup_feedback::wait_outcome(*uid, *request_sequence, feedback, true),
                ActiveKind::InteractWait {
                    mode: InteractWait::Sit,
                } => sit_outcome(
                    client.current::<comp::CharacterState>().as_ref(),
                    client.current::<comp::Health>().as_ref(),
                    true,
                ),
                ActiveKind::InteractWait {
                    mode: InteractWait::Skill { skill, before_level, before_points, cost },
                } => native_interactions::skill_readback(client.current::<comp::SkillSet>().as_ref(),
                    *skill, *before_level, *before_points, *cost, true),
                ActiveKind::InteractWait {
                    mode: InteractWait::Ability { slot, ability },
                } => native_interactions::ability_readback(&auxiliary_ability_snapshot(client),
                    *slot, *ability, true),
                ActiveKind::InteractWait {
                    mode: InteractWait::Respawn,
                } => Some((
                    "failed",
                    json!({
                        "code": "respawn_not_observed_alive",
                        "sent": true,
                        "observed_alive": false,
                    }),
                )),
                _ => Some(("failed", json!({ "code": "action_timeout" }))),
            }
        } else {
            match &mut active.kind {
                ActiveKind::TimedMove { finish_tick, .. } => {
                    if state.tick >= *finish_tick {
                        let distance = active
                            .start_position
                            .zip(client.position())
                            .map(|(from, to)| (to - from).magnitude());
                        // Short duration_ticks can end before STALL_TICKS; treat
                        // near-zero displacement as a failed move rather than success.
                        if distance.is_some_and(|value| value < 0.05) {
                            Some((
                                "failed",
                                json!({
                                    "code": "stalled",
                                    "distance_moved": distance,
                                }),
                            ))
                        } else {
                            Some(("completed", json!({ "distance_moved": distance })))
                        }
                    } else if let Some(position) = client.position() {
                        let progressed = active
                            .progress_position
                            .is_none_or(|previous| (position - previous).xy().magnitude() >= 0.1);
                        if progressed {
                            active.progress_position = Some(position);
                            active.last_progress_tick = state.tick;
                            None
                        } else if state.tick.saturating_sub(active.last_progress_tick) >= STALL_TICKS
                        {
                            let distance = active
                                .start_position
                                .map(|start| (position - start).magnitude());
                            Some((
                                "failed",
                                json!({
                                    "code": "stalled",
                                    "distance_moved": distance,
                                }),
                            ))
                        } else {
                            None
                        }
                    } else {
                        None
                    }
                },
                ActiveKind::Jump {
                    earliest_finish_tick,
                    was_airborne,
                } => {
                    let on_ground = client
                        .current::<comp::PhysicsState>()
                        .is_some_and(|physics| physics.on_ground.is_some());
                    if !on_ground {
                        *was_airborne = true;
                    }
                    if *was_airborne && on_ground && state.tick >= *earliest_finish_tick {
                        Some(("completed", json!({ "landed": true })))
                    } else if state.tick >= *earliest_finish_tick + TPS * 3 {
                        Some(("failed", json!({ "code": "jump_not_completed" })))
                    } else {
                        None
                    }
                },
                ActiveKind::MoveTo {
                    target,
                    tolerance,
                    best_distance,
                    last_progress_tick,
                    last_best_distance_tick,
                    max_no_best_distance_improvement_ticks,
                    reached_tick,
                    chaser,
                    max_path_length_reached,
                    pending_ticks,
                    consecutive_pending_ticks,
                    planner_none_ticks,
                    chaser_none_ticks,
                    stuck_ever,
                    stuck_ticks,
                    last_bearing,
                    last_path_position,
                    actual_path_length,
                    ..
                } => {
                    if let Some(reached) = *reached_tick {
                        if state.tick > reached {
                            let actual_path_length = actual_path_length_at_termination(
                                client,
                                *last_path_position,
                                *actual_path_length,
                            );
                            Some((
                                "completed",
                                move_to_detail(
                                    client,
                                    state.tick,
                                    started_tick,
                                    start_position,
                                    *target,
                                    *best_distance,
                                    chaser,
                                    *max_path_length_reached,
                                    *pending_ticks,
                                    *consecutive_pending_ticks,
                                    *planner_none_ticks,
                                    *chaser_none_ticks,
                                    *stuck_ever,
                                    *stuck_ticks,
                                    *last_bearing,
                                    actual_path_length,
                                    *last_best_distance_tick,
                                    *max_no_best_distance_improvement_ticks,
                                    None,
                                ),
                            ))
                        } else {
                            None
                        }
                    } else if let Some(position) = client.position() {
                        if let Some(previous) = *last_path_position {
                            *actual_path_length += ground_distance(position, previous);
                        }
                        *last_path_position = Some(position);
                        let distance = ground_distance(*target, position);
                        let vertical_gap = (target.z - position.z).abs();
                        let no_best_distance_improvement_ticks =
                            state.tick.saturating_sub(*last_best_distance_tick);
                        *max_no_best_distance_improvement_ticks = (*max_no_best_distance_improvement_ticks)
                            .max(no_best_distance_improvement_ticks);
                        if distance <= *tolerance && vertical_gap <= VERTICAL_ARRIVAL_SLACK {
                            *best_distance = distance;
                            *reached_tick = Some(state.tick);
                            None
                        } else if *pending_ticks >= MAX_CUMULATIVE_PENDING_TICKS {
                            Some((
                                "failed",
                                move_to_detail(
                                    client,
                                    state.tick,
                                    started_tick,
                                    start_position,
                                    *target,
                                    distance,
                                    chaser,
                                    *max_path_length_reached,
                                    *pending_ticks,
                                    *consecutive_pending_ticks,
                                    *planner_none_ticks,
                                    *chaser_none_ticks,
                                    *stuck_ever,
                                    *stuck_ticks,
                                    *last_bearing,
                                    *actual_path_length,
                                    *last_best_distance_tick,
                                    *max_no_best_distance_improvement_ticks,
                                    Some("planner_pending_timeout"),
                                ),
                            ))
                        } else if *planner_none_ticks >= PLANNER_NONE_TICKS {
                            Some((
                                "failed",
                                move_to_detail(
                                    client,
                                    state.tick,
                                    started_tick,
                                    start_position,
                                    *target,
                                    distance,
                                    chaser,
                                    *max_path_length_reached,
                                    *pending_ticks,
                                    *consecutive_pending_ticks,
                                    *planner_none_ticks,
                                    *chaser_none_ticks,
                                    *stuck_ever,
                                    *stuck_ticks,
                                    *last_bearing,
                                    *actual_path_length,
                                    *last_best_distance_tick,
                                    *max_no_best_distance_improvement_ticks,
                                    Some("planner_no_path"),
                                ),
                            ))
                        } else if no_best_distance_improvement_ticks
                            >= MAX_NO_BEST_DISTANCE_IMPROVEMENT_TICKS
                        {
                            Some((
                                "failed",
                                move_to_detail(
                                    client,
                                    state.tick,
                                    started_tick,
                                    start_position,
                                    *target,
                                    distance,
                                    chaser,
                                    *max_path_length_reached,
                                    *pending_ticks,
                                    *consecutive_pending_ticks,
                                    *planner_none_ticks,
                                    *chaser_none_ticks,
                                    *stuck_ever,
                                    *stuck_ticks,
                                    *last_bearing,
                                    *actual_path_length,
                                    *last_best_distance_tick,
                                    *max_no_best_distance_improvement_ticks,
                                    Some("target_distance_stalled"),
                                ),
                            ))
                        } else {
                            let distance_improved = distance + 0.05 < *best_distance;
                            let position_moved = active.progress_position.is_none_or(|previous| {
                                ground_distance(position, previous) > 0.05
                            });
                            if distance_improved {
                                *best_distance = distance;
                                *last_best_distance_tick = state.tick;
                            }
                            if distance_improved || position_moved {
                                active.progress_position = Some(position);
                                *last_progress_tick = state.tick;
                                None
                            } else if state.tick.saturating_sub(*last_progress_tick) >= STALL_TICKS
                            {
                                Some((
                                    "failed",
                                    move_to_detail(
                                        client,
                                        state.tick,
                                        started_tick,
                                        start_position,
                                        *target,
                                        distance,
                                        chaser,
                                        *max_path_length_reached,
                                        *pending_ticks,
                                        *consecutive_pending_ticks,
                                        *planner_none_ticks,
                                        *chaser_none_ticks,
                                        *stuck_ever,
                                        *stuck_ticks,
                                        *last_bearing,
                                        *actual_path_length,
                                        *last_best_distance_tick,
                                        *max_no_best_distance_improvement_ticks,
                                        Some("stalled"),
                                    ),
                                ))
                            } else {
                                None
                            }
                        }
                    } else {
                        None
                    }
                },
                ActiveKind::Look { target } => {
                    let aligned = client.current::<comp::Controller>()
                        .zip(client.current::<comp::Ori>())
                        .is_some_and(|(controller, orientation)| {
                            applied_look_aligned(*target, controller.inputs.look_dir,
                                                 orientation.look_dir())
                        });
                    if aligned {
                        let look = target.to_vec();
                        Some(("completed", json!({ "look": [look.x, look.y, look.z] })))
                    } else {
                        None
                    }
                },
                ActiveKind::Attack {
                    input,
                    finish_tick,
                    saw_attack,
                    target_uid,
                    target_ref: _,
                    start_health,
                } => {
                    if client
                        .current::<comp::CharacterState>()
                        .is_some_and(|cs| cs.is_attack())
                    {
                        *saw_attack = true;
                    }
                    if state.tick >= *finish_tick {
                        let end_health = (*target_uid)
                            .and_then(|uid| entity_by_uid(client, uid))
                            .and_then(|entity| health_of_entity(client, entity));
                        Some((
                            "completed",
                            json!({
                                "input": format!("{input:?}"),
                                "saw_attack": *saw_attack,
                                "target_uid": *target_uid,
                                "start_health": *start_health,
                                "end_health": end_health,
                            }),
                        ))
                    } else {
                        None
                    }
                },
                ActiveKind::Mine {
                    voxel,
                    finish_tick,
                    start_kind,
                } => {
                    let current = client.state().terrain().get(*voxel).ok().copied();
                    let current_kind = current.as_ref().map(|block| block.kind().to_string());
                    let broken = current.as_ref().is_none_or(|block| {
                        block.kind().is_air() || block.kind().to_string() != *start_kind
                    });
                    if broken {
                        Some((
                            "completed",
                            json!({
                                "position": [voxel.x, voxel.y, voxel.z],
                                "start_kind": start_kind,
                                "end_kind": current_kind,
                            }),
                        ))
                    } else if state.tick >= *finish_tick {
                        Some((
                            "failed",
                            json!({
                                "code": "mine_incomplete",
                                "position": [voxel.x, voxel.y, voxel.z],
                                "kind": current_kind,
                            }),
                        ))
                    } else {
                        None
                    }
                },
                ActiveKind::HoldAbility { input, finish_tick } => {
                    if state.tick >= *finish_tick {
                        Some(("completed", json!({ "input": format!("{input:?}") })))
                    } else {
                        None
                    }
                },
                ActiveKind::Craft {
                    recipe,
                    requested_amount,
                    amount,
                    station,
                    required_sprite,
                    craft_request_sequence,
                    output_definition_id,
                    expected_output_amount,
                    start_inventory,
                    saw_craft_update,
                } => {
                    if *saw_craft_update {
                        Some((
                            "completed",
                            craft_result_detail(
                                client,
                                recipe,
                                *requested_amount,
                                *amount,
                                *station,
                                *required_sprite,
                                *craft_request_sequence,
                                output_definition_id,
                                *expected_output_amount,
                                start_inventory,
                                true,
                            ),
                        ))
                    } else {
                        None
                    }
                },
                ActiveKind::InteractWait { mode } => match mode {
                    InteractWait::Skill { skill, before_level, before_points, cost } =>
                        native_interactions::skill_readback(client.current::<comp::SkillSet>().as_ref(),
                            *skill, *before_level, *before_points, *cost, false),
                    InteractWait::Ability { slot, ability } =>
                        native_interactions::ability_readback(&auxiliary_ability_snapshot(client),
                            *slot, *ability, false),
                    InteractWait::Sit => sit_outcome(
                        client.current::<comp::CharacterState>().as_ref(),
                        client.current::<comp::Health>().as_ref(),
                        false,
                    ),
                    InteractWait::Pickup {
                        uid, request_sequence, feedback, ..
                    } => pickup_feedback::wait_outcome(*uid, *request_sequence, feedback, false),
                    InteractWait::Collect {
                        position,
                        start_item_count,
                        start_collectible,
                    } => {
                        let inventory = client.current::<comp::Inventory>();
                        let item_count = inventory
                            .as_ref()
                            .map(|inv| inventory_item_count(inv))
                            .unwrap_or(0);
                        let still_collectible = client
                            .state()
                            .terrain()
                            .get(*position)
                            .ok()
                            .is_some_and(|block| block.is_collectible());
                        if item_count > *start_item_count
                            || (*start_collectible && !still_collectible)
                        {
                            Some((
                                "completed",
                                json!({
                                    "position": [position.x, position.y, position.z],
                                    "item_count_delta": item_count as i64
                                        - *start_item_count as i64,
                                }),
                            ))
                        } else {
                            None
                        }
                    },
                    InteractWait::UseItem {
                        start_item_count,
                        definition_id,
                        start_definition_count,
                        consumable,
                        entered_use,
                    } => {
                        let using = client
                            .current::<comp::CharacterState>()
                            .is_some_and(|cs| matches!(cs, comp::CharacterState::UseItem(_)));
                        if using {
                            *entered_use = true;
                        }
                        let inventory = client.current::<comp::Inventory>();
                        let item_count = inventory.as_ref().map(|inv| inventory_item_count(inv));
                        let definition_count = inventory.as_ref().map(|inv|
                            inventory_snapshot(inv).definition_counts
                                .get(definition_id).copied().unwrap_or(0));
                        let definition_delta = definition_count.map(|count|
                            i64::from(count) - i64::from(*start_definition_count));
                        // Wait for the native use state to finish when observed. Food may
                        // apply after >5 seconds. Count decreases are evidence, not healing.
                        if use_item_completion_observed(*entered_use, using, definition_delta) {
                            Some((
                                "completed",
                                json!({
                                    "item_count_delta": item_count.map(|count|
                                        i64::from(count) - i64::from(*start_item_count)),
                                    "item_definition_id": definition_id,
                                    "definition_count_delta": definition_delta,
                                    "consumable": consumable,
                                    "entered_use": entered_use,
                                    "using_at_finish": using,
                                }),
                            ))
                        } else if !*consumable && state.tick.saturating_sub(active.started_tick) >= TPS {
                            Some((
                                "completed",
                                json!({
                                    "item_count_delta": item_count.map(|count|
                                        i64::from(count) - i64::from(*start_item_count)),
                                    "item_definition_id": definition_id,
                                    "definition_count_delta": definition_delta,
                                    "consumable": consumable,
                                    "entered_use": entered_use,
                                    "assumed_applied": true,
                                }),
                            ))
                        } else {
                            None
                        }
                    },
                    InteractWait::ToggleWield { want_wielding } => {
                        if client.is_wielding() == Some(*want_wielding) {
                            Some(("completed", json!({ "wielding": *want_wielding })))
                        } else {
                            None
                        }
                    },
                    InteractWait::GiveUp => unreachable!("give_up is evaluated before deadline branching"),
                    InteractWait::Respawn => {
                        let observed_alive = client
                            .current::<comp::Health>()
                            .is_some_and(|health| !health.is_dead);
                        if observed_alive {
                            Some((
                                "completed",
                                json!({
                                    "sent": true,
                                    "observed_alive": true,
                                }),
                            ))
                        } else {
                            None
                        }
                    },
                    InteractWait::Drop {
                        start_item_count,
                        start_free_slots,
                    } => {
                        let inventory = client.current::<comp::Inventory>();
                        let item_count = inventory
                            .as_ref()
                            .map(|inv| inventory_item_count(inv))
                            .unwrap_or(0);
                        let free_slots = inventory
                            .as_ref()
                            .map(|inv| inv.free_slots())
                            .unwrap_or(0);
                        if item_count < *start_item_count || free_slots > *start_free_slots {
                            Some((
                                "completed",
                                json!({
                                    "item_count_delta": item_count as i64
                                        - *start_item_count as i64,
                                }),
                            ))
                        } else {
                            None
                        }
                    },
                    InteractWait::Equip {
                        equip_slot,
                        expected_item_id,
                    } => {
                        if equipped_item_id(client, *equip_slot).as_deref()
                            == Some(expected_item_id.as_str())
                        {
                            Some((
                                "completed",
                                json!({
                                    "equip_slot": format!("{equip_slot:?}"),
                                    "item_id": expected_item_id,
                                }),
                            ))
                        } else {
                            None
                        }
                    },
                },
            }
        }
    };

    if let Some((status, detail)) = outcome {
        finish_active(client, state, status, detail);
    }
}

fn route_waypoint_notification(state: &mut BridgeState, event: &Event) {
    if let Event::Notification(crate::UserNotification::WaypointUpdated { location_name }) = event {
        record_event(state, "waypoint_saved", json!({ "location_name": location_name }));
    }
}

fn route_inventory_update(state: &mut BridgeState, updates: &[comp::InventoryUpdateEvent]) {
    use common::comp::inventory::item::ItemDesc;
    let update_tick = state.tick;
    let current = state.active.as_ref().and_then(|active| match &active.kind {
        ActiveKind::InteractWait { mode: InteractWait::Pickup { uid, request_sequence, .. } } =>
            Some((*request_sequence, *uid)),
        _ => None,
    });
    let other = state.active.as_ref().and_then(|active| match &active.kind {
        ActiveKind::InteractWait { mode: InteractWait::Collect { .. } } =>
            Some(active.action_id.as_str()),
        _ => None,
    });
    if let Some(evidence) = state.pickup_ledger.observe_other_collection(other, updates) {
        record_event(state, "pickup_competing_collection_resolved", evidence);
    }
    for update in updates {
        // One entity request produces one collection OR refusal. Do not settle
        // early on a collection when the batch itself contradicts that premise.
        let batch_conflict = if let comp::InventoryUpdateEvent::Collected(item) = update {
            updates.iter().filter(|other| match other {
                comp::InventoryUpdateEvent::Collected(other) =>
                    other.item_definition_id() == item.item_definition_id(),
                _ => false,
            }).count() != 1 || updates.iter().any(|other| match other {
                comp::InventoryUpdateEvent::EntityCollectFailed { entity, .. } =>
                    current.is_some_and(|(_, uid)| uid == entity.0.get()),
                _ => false,
            })
        } else { false };
        let Some((evidence, candidates)) = state.pickup_ledger.evidence_for(
            update, current.map(|(sequence, _)| sequence), batch_conflict,
        ) else {
            continue;
        };
        if let Some(active) = state.active.as_mut()
            && let ActiveKind::InteractWait {
                mode: InteractWait::Pickup { request_sequence, feedback, .. },
            } = &mut active.kind
            && candidates.contains(request_sequence)
            && (feedback.is_none() || evidence["association"] == "unique_pending_target")
        {
            let mut detail = pickup_feedback::terminal_detail(*request_sequence, &evidence);
            if let Some(previous) = feedback {
                detail["preceding_pickup_feedback"] = previous["pickup_feedback"].clone();
            }
            *feedback = Some(detail);
        }
        // Includes late/unmatched/ambiguous evidence; never silently drop a
        // receipt because its action has already timed out or been cancelled.
        record_event(state, "pickup_inventory_update", evidence);
    }
    for _ in updates
        .iter()
        .filter(|update| matches!(update, comp::InventoryUpdateEvent::Craft))
    {
        let Some(craft_request_sequence) =
            state.pending_craft_request_sequences.pop_front()
        else {
            continue;
        };
        if let Some(active) = state.active.as_mut()
            && active.started_tick < update_tick
            && let ActiveKind::Craft {
                craft_request_sequence: active_craft_request_sequence,
                saw_craft_update,
                ..
            } = &mut active.kind
            && *active_craft_request_sequence == craft_request_sequence
        {
            *saw_craft_update = true;
        }
    }
}

pub fn handle_request(client: &mut Client, state: &mut BridgeState, request: Request) -> bool {
    if !request
        .id
        .as_str()
        .is_some_and(|id| !id.is_empty() && id.len() <= 128)
    {
        error_response(
            state,
            request.id,
            "invalid_request_id",
            "id must be a string containing between 1 and 128 bytes",
        );
        return true;
    }
    if request.protocol_version.as_deref() != Some(PROTOCOL_VERSION) {
        let received = request.protocol_version.as_deref().unwrap_or("<missing>");
        error_response(
            state,
            request.id,
            "protocol_mismatch",
            format!("expected protocol version {PROTOCOL_VERSION}, received {received}"),
        );
        return true;
    }
    if !request.args.is_object() {
        error_response(
            state,
            request.id,
            "invalid_request",
            "args must be an object",
        );
        return true;
    }

    if matches!(request.cmd.as_str(), "step" | "stop" | "move_forward" | "jump")
        && let Err(code) = continuous_control::legacy_fence(
            state, &request.args, &continuous_control::actor_ref(client, state))
    {
        error_response(state, request.id, code, "native protection retired this proposal's actuator generation");
        return true;
    }
    match request.cmd.as_str() {
        "set_control_state" | "release_control_state" => {
            match continuous_control::handle(
                client, state, &request.args, request.cmd == "release_control_state",
            ) {
                Ok(result) => response(state, request.id, result),
                Err(code) => error_response(state, request.id, code, code),
            }
        },
        "step" => begin_step(client, state, request),
        "observe" => response(state, request.id, observation(client, state)),
        "inspect_map" => match player_map::inspect(client, state, &request.args) {
            Ok(result) => response(state, request.id, result),
            Err((code, message)) => error_response(state, request.id, code, message),
        },
        "goal_recipes" => match goal_recipes::query(client, &request.args) {
            Ok(result) => response(state, request.id, result),
            Err((code, message)) => error_response(state, request.id, code, message),
        },
        "inspect_terrain" => match inspect_terrain(client, state, &request.args) {
            Ok(result) => response(state, request.id, result),
            Err((code, message)) => error_response(state, request.id, code, message),
        },
        "health" => {
            let health = client.current::<comp::Health>().map(|value| {
                json!({
                    "current": value.current(),
                    "maximum": value.maximum(),
                    "fraction": value.fraction(),
                    "dead": value.is_dead,
                })
            });
            response(state, request.id, json!({ "health": health }));
        },
        "events" => {
            if request.args.get("after_seq").is_some() {
                let Some(after_seq) = request.args.get("after_seq").and_then(Value::as_u64) else {
                    error_response(
                        state,
                        request.id,
                        "invalid_request",
                        "after_seq must be a non-negative integer",
                    );
                    return true;
                };
                let limit = request
                    .args
                    .get("limit")
                    .and_then(Value::as_u64)
                    .unwrap_or(MAX_EVENTS as u64);
                if limit == 0 || limit > MAX_EVENTS as u64 {
                    error_response(
                        state,
                        request.id,
                        "invalid_request",
                        format!("limit must be between 1 and {MAX_EVENTS}"),
                    );
                    return true;
                }
                response(
                    state,
                    request.id,
                    event_batch(state, after_seq, limit as usize),
                );
            } else {
                let events = state.events.drain(..).collect::<Vec<_>>();
                response(state, request.id, json!(events));
            }
        },
        "move_forward" => {
            if sit_pending(state) || state.continuous.is_active() {
                error_response(state, request.id, "avatar_busy", "avatar controls are owned");
                return true;
            }
            let speed = request
                .args
                .get("speed")
                .and_then(Value::as_f64)
                .unwrap_or(1.0)
                .clamp(0.0, 1.0) as f32;
            state.persistent_move = horizontal_look(state.look_dir) * speed;
            response(
                state,
                request.id,
                json!({ "accepted": true, "legacy": true }),
            );
        },
        "stop" => {
            cancel_active(client, state, "stopped by legacy command");
            state.persistent_move = Vec2::zero();
            response(
                state,
                request.id,
                json!({ "accepted": true, "legacy": true }),
            );
        },
        "jump" => {
            if sit_pending(state) || state.continuous.is_active() {
                error_response(state, request.id, "avatar_busy", "avatar controls are owned");
                return true;
            }
            client.handle_input(InputKind::Jump, true, None, None);
            state.release_jump = true;
            response(
                state,
                request.id,
                json!({ "accepted": true, "legacy": true }),
            );
        },
        "ping" => response(
            state,
            request.id,
            json!({
                "protocol_version": PROTOCOL_VERSION,
                "session_id": state.session_id,
                "tick": state.tick,
            }),
        ),
        "close" => {
            continuous_control::shutdown(client, state, "bridge closed");
            response(state, request.id, json!({ "closed": true }));
            return false;
        },
        other => error_response(
            state,
            request.id,
            "unsupported_command",
            format!("unsupported command: {other}"),
        ),
    }
    true
}

/// Identity and ownership only: safe to associate with an ordinary player frame.
pub fn actuator_identity(client: &Client, state: &BridgeState) -> Value {
    let controls = state.continuous.observation();
    json!({
        "actor": continuous_control::actor_ref(client, state),
        "active_action_id": state.active.as_ref().map(|action| &action.action_id),
        "control": {
            "owner": controls["owner"],
            "generation": controls["generation"],
            "sequence": controls["sequence"],
            "retired": controls["retired"],
            "protection_generation": controls["protection_generation"],
        },
    })
}

/// GUI selections use the same native action trackers, not a second actuator.
pub fn visual_fence(client: &Client, state: &BridgeState, args: &Value) -> Result<(), &'static str> {
    continuous_control::legacy_fence(state, args, &continuous_control::actor_ref(client, state))?;
    if state.continuous.is_active() || state.active.is_some() {
        return Err("avatar_busy");
    }
    if client.is_dead() { return Err("actor_unavailable"); }
    Ok(())
}

pub fn visual_duplicate(state: &BridgeState, action_id: &str, args: &Value) -> Result<bool, &'static str> {
    match state.visual_submissions.get(action_id) {
        Some(previous) if previous.args != *args => Err("action_id_conflict"),
        Some(_) => Ok(true),
        None if state.completed.contains_key(action_id)
            || state.active.as_ref().is_some_and(|active| active.action_id == action_id) =>
                Err("action_id_conflict"),
        None => Ok(false),
    }
}

pub fn reserve_visual_submission(state: &mut BridgeState, request: &Request) {
    let action_id = request.args["action"]["action_id"].as_str().expect("validated visual action ID");
    assert!(visual_duplicate(state, action_id, &request.args) == Ok(false));
    state.visual_submissions.insert(action_id.to_owned(), VisualSubmission {
        args: request.args.clone(), refusal: std::cell::OnceCell::new(),
    });
}

pub fn visual_replay(state: &BridgeState, request: &Request) -> Option<Value> {
    let action_id = request.args["action"]["action_id"].as_str()?;
    let submission = state.visual_submissions.get(action_id)?;
    if let Some(error) = submission.refusal.get() {
        return Some(json!({"type": "response", "protocol_version": PROTOCOL_VERSION,
            "session_id": state.session_id, "tick": state.tick, "id": request.id,
            "ok": false, "result": null, "error": error}));
    }
    let mut result = state.completed.get(action_id)?.result.clone();
    result["duplicate"] = json!(true);
    Some(json!({"type": "response", "protocol_version": PROTOCOL_VERSION,
        "session_id": state.session_id, "tick": state.tick, "id": request.id,
        "ok": true, "result": result, "error": null}))
}

fn step_signature(state: &BridgeState, request_id: &Value, action_id: &str, args: &Value)
    -> Result<String, &'static str>
{
    if let Some(submission) = state.visual_submissions.get(action_id) {
        if state.visual_dispatch.as_ref().is_some_and(|(active_id, active_request)|
            active_id == action_id && active_request == request_id)
        {
            return Ok(action_signature(&submission.args));
        }
        return Err("action_id_conflict");
    }
    Ok(action_signature(args))
}

pub fn dispatch_visual_action(client: &mut Client, state: &mut BridgeState,
                              mut request: Request, action: Value) {
    let action_id = request.args["action"]["action_id"].as_str().expect("validated visual action ID").to_owned();
    assert!(state.visual_submissions.contains_key(&action_id));
    assert!(state.visual_dispatch.is_none());
    state.visual_origins.insert(action_id.to_owned(), json!({
        "original_args": request.args, "action": action,
        "source": "native-hud-hit-test-v1",
    }));
    state.visual_dispatch = Some((action_id, request.id.clone()));
    request.args["action"] = action;
    handle_request(client, state, request);
    state.visual_dispatch = None;
}

pub fn complete_visual_ui(client: &mut Client, state: &mut BridgeState, request: Request,
                          status: &str, code: &str) {
    let action_id = request.args["action"]["action_id"].as_str().expect("validated visual action ID").to_owned();
    let signature = action_signature(&request.args);
    immediate_action_with_status(client, state, request.id, action_id, "visual_input".into(),
                                 signature, status, json!({"code": code, "gameplay_success": null}));
}

pub fn shutdown(client: &mut Client, state: &mut BridgeState, reason: &str) {
    continuous_control::shutdown(client, state, reason);
}

/// Run once after the owning client's update, before its events reach the HUD.
pub fn after_client_tick(client: &mut Client, state: &mut BridgeState, events: &[Event]) {
    state.tick += 1;
    let actor = continuous_control::actor_ref(client, state);
    if state.interactions.actor != actor {
        state.dialogue = None;
        state.interactions = native_interactions::Ledger {
            actor, ..Default::default()
        };
    }
    release_jump_with(state, |input| client.handle_input(input, false, None, None));
    for event in events {
        match event {
            Event::Notification(_) => route_waypoint_notification(state, event),
            Event::Chat(message) => record_event(
                state, "chat", chat_event_data_with_sender_identity(client, message),
            ),
            Event::Dialogue(sender, dialogue) => {
                let speaker = native_interactions::entity_reference(client, state, sender.0.get());
                let data = state.interactions.receive_dialogue(
                    *sender, speaker, dialogue, state.tick);
                record_event(state, "dialogue", data);
                if matches!(dialogue.kind, common::rtsim::DialogueKind::End) {
                    if state.dialogue.as_ref().is_some_and(|(active, prompt)|
                        active == sender && prompt.id == dialogue.id)
                    {
                        state.dialogue = None;
                    }
                } else if matches!(dialogue.kind, common::rtsim::DialogueKind::Statement { .. }
                    | common::rtsim::DialogueKind::Question { .. })
                {
                    state.dialogue = Some((*sender, dialogue.clone()));
                }
            },
            Event::TradeComplete { result, trade } => {
                let mut data = native_interactions::trade_result(result, trade);
                data["parties"] = json!(trade.parties.map(|uid|
                    native_interactions::entity_reference(client, state, uid.0.get())));
                state.interactions.last_trade_result = data.clone();
                record_event(state, "trade_complete", data);
            },
            Event::InventoryUpdated(updates) => route_inventory_update(state, updates),
            Event::Outcome(outcome) => {
                if let Some(mut data) = server_health_change_data(outcome) {
                    for (uid_key, ref_key) in [("target_uid", "target"), ("contributor_uid", "contributor")] {
                        data[ref_key] = data[uid_key].as_u64().map_or(Value::Null, |uid|
                            native_interactions::entity_reference(client, state, uid));
                    }
                    data["native_contributor"] = match outcome {
                        common::outcome::Outcome::HealthChange { info, .. } => json!(info.by),
                        _ => Value::Null,
                    };
                    data["attribution_limit"] = json!("server_reported_source;not_exclusive_net_healing;missing_lifecycle_is_unknown");
                    record_event(state, "combat_health_change", data);
                }
            },
            Event::Disconnect => {
                shutdown(client, state, "server disconnected");
                record_event(state, "disconnected", json!({}));
                return;
            },
            _ => {},
        }
    }
    continuous_control::protection_tick(client, state);
    update_active(client, state);
}

pub fn run() -> Result<(), String> {
    let _ = agent_min_path_length();
    let _ = agent_edge_cost_mode();
    let config = Config::from_env_and_args();
    let runtime = Arc::new(Runtime::new().map_err(|error| error.to_string())?);
    let address = ConnectionArgs::Tcp {
        prefer_ipv6: false,
        hostname: config.server.clone(),
    };
    let mut server_info = None;
    let mut client = runtime
        .block_on(Client::new(
            address,
            Arc::clone(&runtime),
            &mut server_info,
            &config.username,
            &config.password,
            None,
            |_| true,
            &|_| {},
            |_| {},
            Default::default(),
            ClientType::Game,
        ))
        .map_err(|error| format!("failed to connect to {}: {error:?}", config.server))?;

    let dt = Duration::from_secs_f64(1.0 / TPS as f64);
    let alias = enter_world(&mut client, &config, dt)?;
    let initial_look = client
        .current::<comp::Ori>()
        .map(|orientation| orientation.look_dir())
        .map(flatten_look_dir)
        .unwrap_or_default();
    let mut state = BridgeState {
        session_id: config.session_id,
        tick: 0,
        look_dir: initial_look,
        persistent_move: Vec2::zero(),
        active: None,
        continuous: continuous_control::Controls::default(),
        completed: HashMap::new(),
        completed_order: VecDeque::new(),
        visual_origins: HashMap::new(),
        visual_submissions: HashMap::new(),
        visual_dispatch: None,
        events: VecDeque::new(),
        next_event_seq: 1,
        next_craft_request_sequence: 1,
        pending_craft_request_sequences: VecDeque::new(),
        pickup_ledger: pickup_feedback::PickupLedger::default(),
        interactable_sprite_scan: None,
        release_jump: false,
        release_wall_jump: false,
        dialogue: None,
        interactions: native_interactions::Ledger::default(),
        held_input: None,
        break_block_pos: None,
    };
    emit_ready_with_uid(
        &state.session_id,
        state.tick,
        &alias,
        client.uid().map(|uid| uid.0.get()),
    );

    let (sender, receiver) = mpsc::sync_channel::<InputMessage>(256);
    thread::spawn(move || {
        for line in io::stdin().lock().lines() {
            match line {
                Ok(line) => {
                    if sender.send(InputMessage::Line(line)).is_err() {
                        break;
                    }
                },
                Err(_) => break,
            }
        }
        let _ = sender.send(InputMessage::Closed);
    });

    let mut clock = Clock::new(dt);
    loop {
        while let Ok(message) = receiver.try_recv() {
            let InputMessage::Line(line) = message else {
                continuous_control::shutdown(&mut client, &mut state, "controller input closed");
                record_event(&mut state, "controller_disconnected", json!({}));
                return Ok(());
            };
            let request = match serde_json::from_str::<Request>(&line) {
                Ok(request) => request,
                Err(error) => {
                    error_response(&state, Value::Null, "invalid_json", error.to_string());
                    continue;
                },
            };
            if !handle_request(&mut client, &mut state, request) {
                return Ok(());
            }
        }

        let inputs = inputs_for_tick(&mut client, &mut state);
        let events = tick_once(&mut client, inputs, clock.game_dt())?;
        after_client_tick(&mut client, &mut state, &events);
        if events.iter().any(|event| matches!(event, Event::Disconnect)) {
            return Err("server disconnected".to_owned());
        }
        clock.tick();
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn applied_look_uses_controller_pitch_and_body_heading() {
        use super::{Dir, Vec3, applied_look_aligned};
        // Exact failing T08 displacement: actual feet are a metre above the
        // authored start after body contact. Horizontal Ori can never pass the
        // old full-3D dot product, no matter how many ticks are allowed.
        let target = Dir::from_unnormalized(Vec3::new(5.0, -0.1123, -2.2)).unwrap();
        let body = target.to_horizontal().unwrap();
        assert!(body.dot(target.to_vec()) < 0.95);
        assert!(applied_look_aligned(target, target, body));
        assert!(!applied_look_aligned(target, body, body)); // Pitch not applied.
        assert!(!applied_look_aligned(target, target, -body)); // Heading not applied.
        for z in [-10.0, 0.0, 10.0] {
            let look = Dir::from_unnormalized(Vec3::new(1.0, 0.0, z)).unwrap();
            assert!(applied_look_aligned(look, look, Dir::right()));
            assert!(!applied_look_aligned(look, -look, Dir::right()));
        }
        for z in [-1.0, 1.0] {
            let vertical = Dir::from_unnormalized(Vec3::new(0.0, 0.0, z)).unwrap();
            assert!(applied_look_aligned(vertical, vertical, Dir::forward()));
        }
    }

    #[test]
    fn fixture_authoring_terrain_rejects_synthetic_out_of_world_air_even_when_cached() {
        use common::{terrain::{MapSizeLg, TerrainChunk, TerrainChunkMeta, TerrainGrid},
                     vol::{ReadVol, WriteVol}};
        use std::sync::Arc;
        let air = super::Block::air(super::SpriteKind::Empty);
        let empty = Arc::new(TerrainChunk::new(0, air, air, TerrainChunkMeta::void()));
        let mut terrain = TerrainGrid::new(MapSizeLg::new(vek::Vec2::new(7,7)).unwrap(), empty.clone()).unwrap();
        let outside = vek::Vec3::new(18700,10800,500);
        assert!(terrain.get(outside).unwrap().kind().is_air());
        assert!(terrain.set(outside, super::Block::new(super::BlockKind::Rock, vek::Rgb::new(1,2,3))).is_err());
        // Reproduce load_chunks inserting a clone of its synthetic default.
        terrain.insert(TerrainGrid::chunk_key(outside), empty.clone());
        assert!(terrain.contains_key_real(TerrainGrid::chunk_key(outside)));
        assert_eq!(super::real_terrain_block(&terrain, outside).unwrap_err(),
                   "terrain_position_out_of_world_bounds");
        let inside = vek::Vec3::new(64,64,500);
        assert!(super::real_terrain_block(&terrain, inside).unwrap().is_none());
        terrain.insert(TerrainGrid::chunk_key(inside), empty);
        assert!(super::real_terrain_block(&terrain, inside).unwrap().unwrap().kind().is_air());
    }
    #[test]
    fn server_outcomes_preserve_independent_events_even_with_same_instance() {
        use common::{
            DamageSource,
            combat::{AttackSource, DamageContributor},
            outcome::{HealthChangeInfo, Outcome},
            uid::Uid,
        };
        let uid = |n| Uid(std::num::NonZeroU64::new(n).unwrap());
        let event = |amount: f32, contributor: Option<u64>| Outcome::HealthChange {
            pos: vek::Vec3::new(1.0, 2.0, 3.0),
            info: HealthChangeInfo {
                amount, target: uid(7),
                by: contributor.map(|n| DamageContributor::Solo(uid(n))),
                cause: Some(DamageSource::Attack(AttackSource::Melee)),
                instance: 42, precise: true,
            },
        };
        let mut bridge = super::BridgeState::new("offline", common::util::Dir::default());
        for outcome in [event(-3.0, Some(3)), event(-2.0, Some(4)), event(2.0, None)] {
            let data = super::server_health_change_data(&outcome).unwrap();
            super::record_event(&mut bridge, "combat_health_change", data);
        }
        assert_eq!(bridge.events.len(), 3);
        let rows = bridge.events.iter().collect::<Vec<_>>();
        assert_eq!(rows[0]["seq"], 1);
        assert_eq!(rows[1]["seq"], 2);
        assert_eq!(rows[0]["data"]["instance"], rows[1]["data"]["instance"]);
        assert_eq!(rows[0]["data"]["contributor_uid"], 3);
        assert_eq!(rows[1]["data"]["contributor_uid"], 4);
        assert_eq!(rows[2]["data"]["amount"], 2.0);
        assert!(super::server_health_change_data(
            &Outcome::Death { pos: vek::Vec3::zero() }).is_none());
    }

    #[test]
    fn protected_navigation_capability_is_explicit() {
        let ready = super::ready_event("offline", 1, "fixture", Some(1));
        assert_eq!(ready["capabilities"]["remove_owned_invulnerability"]["schema_version"], "1.0");
        assert_eq!(ready["capabilities"]["remove_owned_invulnerability"]["requires_exact_ids"], true);
    }

    #[test]
    fn protected_navigation_removal_preserves_unowned_and_other_buffs() {
        let identity = json!({"uid": 7, "alias": "fixture", "native_character_id": 1373});
        let action = json!({"type": "remove_owned_invulnerability",
                            "expected_self": identity, "owned_ids": ["owned"]});
        let action = action.as_object().unwrap();
        let other = json!({"id": "other", "kind": "Regeneration"});
        let own = json!({"id": "owned", "kind": "Invulnerability"});
        let foreign = json!({"id": "foreign", "kind": "Invulnerability"});
        assert_eq!(super::owned_invulnerability_removal(action, &identity, &json!([own, other])), Ok(true));
        assert_eq!(super::owned_invulnerability_removal(action, &identity, &json!([other])), Ok(false));
        assert!(super::owned_invulnerability_removal(action, &identity, &json!([own, foreign])).is_err());
        assert!(super::owned_invulnerability_removal(action, &json!({"uid": 8}), &json!([own])).is_err());
        assert!(super::owned_invulnerability_removal(action, &identity, &Value::Null).is_err());
    }
    use super::*;

    fn visual_request(id: &str) -> Request {
        Request { id: json!(id), protocol_version: Some(PROTOCOL_VERSION.into()), cmd: "step".into(),
            args: json!({"action": {"type": "visual_input", "action_id": "visual-test",
                                   "frame_id": "frame", "gesture": "interact"},
                         "control_fence": {"generation": 2}}) }
    }

    #[test]
    fn visual_terminal_refusal_replays_exact_error_not_unknown() {
        let mut state = state();
        let request = visual_request("first");
        reserve_visual_submission(&mut state, &request);
        state.visual_dispatch = Some(("visual-test".into(), request.id.clone()));
        error_response(&state, request.id, "interaction_target_changed", "target left native range");
        state.visual_dispatch = None;
        assert!(state.active.is_none() && state.completed.is_empty());
        let duplicate = visual_request("duplicate");
        assert_eq!(visual_duplicate(&state, "visual-test", &duplicate.args), Ok(true));
        let replay = visual_replay(&state, &duplicate).unwrap();
        assert_eq!(replay["ok"], false);
        assert_eq!(replay["result"], Value::Null);
        assert_eq!(replay["id"], "duplicate");
        assert_eq!(replay["error"], json!({"code": "interaction_target_changed",
                                         "message": "target left native range"}));
        let mut changed = duplicate.args.clone();
        changed["action"]["frame_id"] = json!("new-frame");
        assert_eq!(visual_duplicate(&state, "visual-test", &changed), Err("action_id_conflict"));
        assert!(visual_replay(&state, &duplicate).unwrap()["error"] == replay["error"]);
    }

    #[test]
    fn visual_pending_and_missing_receipt_do_not_become_retry_safe() {
        let mut state = state();
        let request = visual_request("first");
        reserve_visual_submission(&mut state, &request);
        assert_eq!(visual_duplicate(&state, "visual-test", &request.args), Ok(true));
        assert!(visual_replay(&state, &request).is_none());
        state.active = Some(ActiveAction { action_id: "visual-test".into(), ..sit_active() });
        assert!(visual_replay(&state, &request).is_none());
        state.active = None;
        assert!(visual_replay(&state, &request).is_none());
        assert_eq!(step_signature(&state, &json!("raw"), "visual-test", &json!({})),
                   Err("action_id_conflict"));
    }

    #[test]
    fn visual_and_ordinary_ids_conflict_active_completed_and_reserved_both_directions() {
        let mut state = state();
        let request = visual_request("visual");
        state.active = Some(ActiveAction { action_id: "visual-test".into(), ..sit_active() });
        assert_eq!(visual_duplicate(&state, "visual-test", &request.args), Err("action_id_conflict"));
        state.active = None;
        cache_action(&mut state, "visual-test".into(), "ordinary".into(), json!({}));
        assert_eq!(visual_duplicate(&state, "visual-test", &request.args), Err("action_id_conflict"));
        state.completed.clear();
        reserve_visual_submission(&mut state, &request);
        let canonical = json!({"action": {"type": "use_item", "slot": 0, "action_id": "visual-test"}});
        assert_eq!(step_signature(&state, &json!("raw"), "visual-test", &canonical),
                   Err("action_id_conflict"));
        state.active = Some(ActiveAction { action_id: "visual-test".into(), ..sit_active() });
        assert!(step_signature(&state, &json!("raw"), "visual-test", &canonical).is_err());
        state.active = None;
        cache_action(&mut state, "visual-test".into(), action_signature(&canonical), json!({"status": "completed"}));
        assert!(step_signature(&state, &json!("raw"), "visual-test", &canonical).is_err());
        state.completed.clear();
        assert!(step_signature(&state, &json!("raw"), "visual-test", &canonical).is_err());
    }

    #[test]
    fn visual_canonical_signature_retains_original_args_and_completed_replay() {
        let mut state = state();
        let request = visual_request("first");
        reserve_visual_submission(&mut state, &request);
        state.visual_dispatch = Some(("visual-test".into(), request.id.clone()));
        let canonical = json!({"action": {"type": "use_item", "slot": 0, "action_id": "visual-test"}});
        let signature = step_signature(&state, &request.id, "visual-test", &canonical).unwrap();
        assert_eq!(signature, action_signature(&request.args));
        assert_ne!(signature, action_signature(&canonical));
        state.visual_dispatch = None;
        for status in ["completed", "failed", "cancelled"] {
            cache_action(&mut state, "visual-test".into(), signature.clone(),
                json!({"action_id": "visual-test", "action_type": "use_item", "status": status}));
            let replay = visual_replay(&state, &visual_request("duplicate")).unwrap();
            assert_eq!(replay["ok"], true);
            assert_eq!(replay["result"]["duplicate"], true);
            assert_eq!(replay["result"]["status"], status);
            assert_eq!(replay["result"]["action_type"], "use_item");
        }
        let ordinary = json!({"action": {"type": "wait", "action_id": "ordinary"}});
        assert_eq!(step_signature(&state, &json!("ordinary"), "ordinary", &ordinary).unwrap(),
                   action_signature(&ordinary));
    }

    #[test]
    fn selected_trade_party_fences_actor_counterparty_id_and_phase() {
        use common::trade::{PendingTrade, TradeId, TradePhase};
        let actor = Uid(NonZeroU64::new(7).unwrap());
        let other = Uid(NonZeroU64::new(8).unwrap());
        let stranger = Uid(NonZeroU64::new(9).unwrap());
        let id: TradeId = serde_json::from_value(json!(12)).unwrap();
        let trade = PendingTrade {
            parties: [other, actor],
            offers: Default::default(),
            phase: TradePhase::Review,
            accept_flags: [false; 2],
        };
        let request = json!({"trade_id": 12, "counterparty_uid": 8, "phase": "Review"});
        let action = request.as_object().unwrap();
        assert_eq!(selected_trade_party(action, actor, id, &trade), Ok(1));
        assert!(selected_trade_party(action, stranger, id, &trade).is_err());
        assert!(selected_trade_party(action, actor,
            serde_json::from_value(json!(13)).unwrap(), &trade).is_err());
        let changed = PendingTrade { phase: TradePhase::Complete, ..trade.clone() };
        assert!(selected_trade_party(action, actor, id, &changed).is_err());
        let wrong_party = json!({"trade_id": 12, "counterparty_uid": 9, "phase": "Review"});
        assert!(selected_trade_party(wrong_party.as_object().unwrap(), actor, id, &trade).is_err());
    }

    #[test]
    fn build_is_native_available_but_requires_current_permission_area_and_loaded_kind() {
        let request = json!({"type": "build_block", "operation": "place",
            "position": [1, 2, 3], "expected_kind": "Air",
            "new_kind": "Rock", "color_rgb": [8, 9, 10]});
        let action = request.as_object().unwrap();
        let empty = Block::empty();
        assert_eq!(build_request(action, None, Some(empty)),
            Err("build permission state unavailable"));
        assert_eq!(build_request(action, Some((false, 1)), Some(empty)),
            Err("actor does not have enabled CanBuild permission"));
        assert_eq!(build_request(action, Some((true, 0)), Some(empty)),
            Err("actor has no assigned build areas"));
        assert_eq!(build_request(action, Some((true, 1)), None),
            Err("target voxel is not loaded"));
        let rock = Block::new(BlockKind::Rock, Rgb::new(8, 9, 10));
        assert_eq!(build_request(action, Some((true, 1)), Some(rock)),
            Err("target voxel kind changed since observation"));
        assert_eq!(build_request(action, Some((true, 1)), Some(empty)),
            Ok(Some(rock)));

        let remove = json!({"type": "build_block", "operation": "remove",
            "position": [1, 2, 3], "expected_kind": "Rock"});
        assert_eq!(build_request(remove.as_object().unwrap(), Some((true, 1)), Some(rock)),
            Ok(None));
        assert!(build_request(remove.as_object().unwrap(), Some((true, 1)), Some(empty)).is_err());
        let malformed = json!({"type": "build_block", "operation": "place",
            "position": [1, 2, 3], "expected_kind": "Air", "new_kind": "Lava",
            "color_rgb": [8, 9, 10]});
        assert!(build_request(malformed.as_object().unwrap(), Some((true, 1)), Some(empty)).is_err());
    }

    #[test]
    fn give_up_is_discovered_as_model_selected_survival_action() {
        let ready = ready_event("test", 0, "agent", None);
        assert_eq!(ready["capabilities"]["give_up"]["parameters"], json!([]));
        assert_eq!(ready["capabilities"]["give_up"]["automatic"], false);
        assert!(is_survival_action("give_up"));
    }
    use common::assets::AssetExt;

    fn state() -> BridgeState {
        BridgeState {
            session_id: "test-session".to_owned(),
            tick: 0,
            look_dir: Dir::default(),
            persistent_move: Vec2::zero(),
            active: None,
            continuous: continuous_control::Controls::default(),
            completed: HashMap::new(),
            completed_order: VecDeque::new(),
            visual_origins: HashMap::new(),
            visual_submissions: HashMap::new(),
            visual_dispatch: None,
            events: VecDeque::new(),
            next_event_seq: 1,
            next_craft_request_sequence: 1,
            pending_craft_request_sequences: VecDeque::new(),
            pickup_ledger: pickup_feedback::PickupLedger::default(),
            interactable_sprite_scan: None,
            release_jump: false,
            release_wall_jump: false,
            dialogue: None,
            interactions: native_interactions::Ledger::default(),
            held_input: None,
            break_block_pos: None,
        }
    }

    fn sit_active() -> ActiveAction {
        ActiveAction {
            request_id: json!("sit-request"),
            action_id: "sit-action".to_owned(),
            action_type: "sit".to_owned(),
            signature: "sit-signature".to_owned(),
            started_tick: 0,
            deadline_tick: 180,
            start_position: None,
            progress_position: None,
            last_progress_tick: 0,
            survival_before: None,
            kind: ActiveKind::InteractWait { mode: InteractWait::Sit },
        }
    }

    fn sit_test_health() -> comp::Health {
        comp::Health::new(comp::body::quadruped_medium::Body {
            species: comp::body::quadruped_medium::Species::Wolf,
            body_type: comp::body::quadruped_medium::BodyType::Female,
        }.into())
    }

    #[test]
    fn sit_is_zero_argument_and_advertised() {
        let action = json!({"action_id": "sit-1", "type": "sit"});
        assert!(sit_action_has_no_parameters(action.as_object().unwrap()));
        for parameter in ["speed", "position", "wield", "location_name"] {
            let mut invalid = action.clone();
            invalid[parameter] = Value::Null;
            assert!(!sit_action_has_no_parameters(invalid.as_object().unwrap()));
        }
        let ready = ready_event("test", 0, "agent", None);
        assert_eq!(ready["capabilities"]["sit"]["parameters"], json!([]));
        assert_eq!(ready["capabilities"]["sit"]["idempotent"], true);
        assert_eq!(ready["capabilities"]["sit"]["checkpoint_save_confirmation"], false);
        assert_eq!(ready["capabilities"]["waypoint_saved"]["event"], true);
        assert_eq!(ready["capabilities"]["waypoint_saved"]["coordinates"], false);
        assert_eq!(ready["capabilities"]["waypoint_saved"]["absence_is_failure"], false);
    }

    #[test]
    fn sit_already_sitting_releases_controls_without_toggling() {
        let health = sit_test_health();
        let mut state = state();
        state.held_input = Some(InputKind::Primary);
        state.release_jump = true;
        state.break_block_pos = Some(Vec3::one());
        state.persistent_move = Vec2::one();
        let mut controls = Vec::new();
        let (status, detail) = begin_sit_with(
            &mut state, Some(&comp::CharacterState::Sit), Some(&health),
            |control| controls.push(control),
        ).unwrap();
        assert_eq!(status, "completed");
        assert_eq!(detail["observed_sitting"], true);
        assert!(detail.get("checkpoint_saved").is_none());
        assert_eq!(controls, vec![SitControl::Release(InputKind::Primary), SitControl::Release(InputKind::Jump)]);
        assert_eq!(state.held_input, None);
        assert!(!state.release_jump);
        assert_eq!(state.break_block_pos, None);
        assert_eq!(state.persistent_move, Vec2::zero());
        controls.clear();
        assert!(begin_sit_with(
            &mut state, Some(&comp::CharacterState::Sit), Some(&health),
            |control| controls.push(control),
        ).is_some());
        assert!(controls.is_empty());
    }

    #[test]
    fn sit_releases_every_owned_held_input_before_one_toggle() {
        let health = sit_test_health();
        let character = comp::CharacterState::Idle(Default::default());
        for input in [InputKind::Primary, InputKind::Secondary, InputKind::Block, InputKind::Roll] {
            let mut state = state();
            state.held_input = Some(input);
            state.release_jump = true;
            state.persistent_move = Vec2::one();
            state.break_block_pos = Some(Vec3::one());
            let mut controls = Vec::new();
            assert!(begin_sit_with(&mut state, Some(&character), Some(&health),
                |control| controls.push(control)).is_none());
            assert_eq!(controls, vec![SitControl::Release(input), SitControl::Release(InputKind::Jump), SitControl::Toggle]);
            assert_eq!(state.persistent_move, Vec2::zero());
            assert_eq!(state.break_block_pos, None);
            assert!(state.held_input.is_none());
            assert!(!state.release_jump);
        }
    }

    #[test]
    fn sit_pending_owns_zero_movement_without_changing_other_action_inputs() {
        let mut state = state();
        state.persistent_move = Vec2::new(0.5, 0.25);
        let ordinary = base_inputs_for_tick(&state);
        assert_eq!(ordinary.move_dir, state.persistent_move);
        state.active = Some(sit_active());
        assert!(sit_pending(&state));
        let sitting = base_inputs_for_tick(&state);
        assert_eq!(sitting.move_dir, Vec2::zero());
        assert_eq!(sitting.move_z, 0.0);
        assert_eq!(sitting.look_dir, ordinary.look_dir);
        state.active.as_mut().unwrap().kind = ActiveKind::InteractWait { mode: InteractWait::Respawn };
        assert!(!sit_pending(&state));
        assert_eq!(base_inputs_for_tick(&state).move_dir, ordinary.move_dir);
    }

    #[test]
    fn sit_wait_only_completes_on_exact_observed_state_before_deadline() {
        let health = sit_test_health();
        let idle = comp::CharacterState::Idle(Default::default());
        assert!(sit_outcome(Some(&idle), Some(&health), false).is_none());
        assert!(sit_outcome(Some(&comp::CharacterState::Crawl), Some(&health), false).is_none());
        let (status, detail) = sit_outcome(Some(&comp::CharacterState::Sit), Some(&health), false).unwrap();
        assert_eq!(status, "completed");
        assert_eq!(detail["character_state"], "Sit");
        for character in [&idle, &comp::CharacterState::Sit] {
            let (status, detail) = sit_outcome(Some(character), Some(&health), true).unwrap();
            assert_eq!(status, "failed");
            assert_eq!(detail["code"], "action_timeout");
            let result = action_result_core(&sit_active(), 180, status, detail);
            assert_eq!(result["status"], "failed");
            assert_eq!(result["finished_tick"], 180);
        }
    }

    #[test]
    fn sit_dead_downed_and_unavailable_fail_without_toggle_on_start_or_wait() {
        let mut dead = sit_test_health();
        dead.is_dead = true;
        let mut downed = sit_test_health();
        downed.can_have_death_protection = true;
        downed.death_protection = false;
        for (character, health, code) in [
            (Some(comp::CharacterState::Sit), Some(&dead), "character_dead"),
            (Some(comp::CharacterState::Crawl), Some(&downed), "character_downed"),
            (None, Some(&downed), "state_unavailable"),
            (Some(comp::CharacterState::Sit), None, "state_unavailable"),
        ] {
            let mut controls = Vec::new();
            let (status, detail) = begin_sit_with(&mut state(), character.as_ref(), health,
                |control| controls.push(control)).unwrap();
            assert_eq!(status, "failed");
            assert_eq!(detail["code"], code);
            assert!(controls.is_empty());
            for expired in [false, true] {
                let (status, detail) = sit_outcome(character.as_ref(), health, expired).unwrap();
                assert_eq!(status, "failed");
                assert_eq!(detail["code"], code);
            }
        }
    }

    #[test]
    fn waypoint_notification_forwards_each_payload_with_existing_envelope() {
        let mut state = state();
        state.tick = 42;
        for name in ["First camp", "第二处"] {
            route_waypoint_notification(&mut state, &Event::Notification(
                crate::UserNotification::WaypointUpdated { location_name: name.to_owned() },
            ));
        }
        assert_eq!(state.events.len(), 2);
        for (index, name) in ["First camp", "第二处"].into_iter().enumerate() {
            assert_eq!(state.events[index], json!({
                "type": "event", "protocol_version": PROTOCOL_VERSION,
                "session_id": "test-session", "tick": 42, "seq": index + 1,
                "sequence_domain": {
                    "kind": "client-update-counter", "session_id": "test-session",
                },
                "event": "waypoint_saved", "data": { "location_name": name },
            }));
        }
    }

    #[test]
    fn waypoint_notification_ignores_unrelated_events_and_never_completes_sit() {
        let mut state = state();
        state.active = Some(sit_active());
        route_waypoint_notification(&mut state, &Event::SetViewDistance(8));
        route_waypoint_notification(&mut state, &Event::DisconnectionNotification(1));
        assert!(state.events.is_empty());
        assert_eq!(state.next_event_seq, 1);
        route_waypoint_notification(&mut state, &Event::Notification(
            crate::UserNotification::WaypointUpdated { location_name: String::new() },
        ));
        assert_eq!(state.events[0]["data"], json!({"location_name": ""}));
        assert!(sit_pending(&state));
        assert!(state.completed.is_empty());
    }

    fn test_recipe(inputs: Vec<(RecipeInput, u32, bool)>) -> Recipe {
        Recipe {
            output: (
                std::sync::Arc::<common::comp::inventory::item::ItemDef>::load_expect_cloned(
                    "common.items.crafting_ing.twigs",
                ),
                1,
            ),
            inputs,
            craft_sprite: None,
        }
    }

    fn test_item_input(item: &str) -> RecipeInput {
        RecipeInput::Item(
            std::sync::Arc::<common::comp::inventory::item::ItemDef>::load_expect_cloned(item),
        )
    }

    fn test_inventory_with(item: &str, amount: u32) -> comp::Inventory {
        let mut item = Item::new_from_asset_expect(item);
        item.set_amount(amount).unwrap();
        let mut inventory = comp::Inventory::with_empty();
        inventory.push(item).unwrap();
        inventory
    }

    #[test]
    fn survival_use_reference_rejects_empty_and_tracks_definition_not_container() {
        let slot = InvSlotId::new(0, 0);
        assert!(use_item_reference(None, slot).is_err());
        assert!(use_item_reference(Some(&comp::Inventory::with_empty()), slot).is_err());
        let inventory = test_inventory_with("common.items.consumable.potion_minor", 2);
        let (total, id, count, consumable) = use_item_reference(Some(&inventory), slot).unwrap();
        assert_eq!(total, 2);
        assert_eq!(id, "common.items.consumable.potion_minor");
        assert_eq!(count, 2);
        assert!(consumable);
        assert!(use_item_reference(Some(&inventory), InvSlotId::new(65535, 65535)).is_err());
        // A returned vial is not a retained potion, even when total count is unchanged.
        let after = test_inventory_with("common.items.crafting_ing.empty_vial", 2);
        assert_eq!(inventory_item_count(&after), total);
        assert!(!inventory_snapshot(&after).definition_counts.contains_key(&id));
    }

    #[test]
    fn survival_use_wait_does_not_complete_during_food_buildup_or_use() {
        assert!(!use_item_completion_observed(false, false, Some(0)));
        assert!(!use_item_completion_observed(true, true, Some(0)));
        assert!(!use_item_completion_observed(true, true, Some(-1)));
        assert!(!use_item_completion_observed(false, false, None));
        assert!(!use_item_completion_observed(false, false, Some(1)));
        assert!(use_item_completion_observed(true, false, Some(0)));
        assert!(use_item_completion_observed(false, false, Some(-1)));
    }

    #[test]
    fn survival_observes_asset_mechanism_and_all_equipment_slots() {
        let apple = item_json(&Item::new_from_asset_expect("common.items.food.apple"));
        assert_eq!(apple["consumable"]["kind"], "Food");
        assert_eq!(apple["consumable"]["buildup_seconds"], 1.0);
        assert_eq!(apple["consumable"]["effects"]["One"]["Buff"]["kind"], "Saturation");
        let potion = item_json(&Item::new_from_asset_expect("common.items.consumable.potion_minor"));
        assert_eq!(potion["consumable"]["effects"]["All"][0]["Buff"]["kind"], "Potion");
        assert_eq!(potion["consumable"]["effects"]["All"][1]["Buff"]["kind"], "PotionSickness");
        assert!(!potion["consumable"]["container"].is_null());
        let equipment = equipment_json(&comp::Inventory::with_empty());
        assert_eq!(equipment.as_object().unwrap().len(), 22);
        assert!(equipment.as_object().unwrap().values().all(Value::is_null));
        for name in equipment.as_object().unwrap().keys() {
            assert!(parse_equip_slot(name).is_some());
        }
        assert_eq!(inventory_json(None), Value::Null);
    }

    #[test]
    fn survival_buff_time_captured_case_requires_initial_server_sync() {
        use common::{comp::buff::{Buff, BuffData, BuffKind, BuffSource}, resources::{Secs, Time}};
        let mut ecs = specs::World::new();
        ecs.insert(Time(0.872119747));
        let mut buffs = comp::Buffs::default();
        buffs.insert(Buff {
            kind: BuffKind::Invulnerability,
            data: BuffData::new(7386780.0, Some(Secs(180.0))),
            cat_ids: vec![], start_time: Time(39.73068386400001),
            end_time: Some(Time(219.730683864)), effects: vec![], source: BuffSource::Command,
        }, Time(39.73068386400001));
        // Exercise the same ECS-reading projection that survival_snapshot uses,
        // NOT just buffs_json with an already-correct time supplied by the test.
        let snapshot = survival_buff_snapshot(&ecs, Some(&buffs));
        println!("captured_case_before_initial_time_sync={snapshot}");
        assert_eq!(snapshot["buffs"][0]["selected"], true);
        assert!(snapshot["buffs"][0]["active"].is_null(), "{snapshot}");
        assert!(snapshot["buffs"][0]["remaining_seconds"].is_null(), "{snapshot}");
    }

    #[test]
    fn survival_buff_time_pipeline_uses_packet_epoch_for_active_delayed_and_expired_buffs() {
        use common::{comp::buff::{Buff, BuffData, BuffKind, BuffSource},
                     resources::{ProgramTime, Secs, Time, TimeOfDay}};
        let mut ecs = specs::World::new();
        ecs.insert(Time(0.872119747));
        ecs.insert(TimeOfDay(80_000.0));
        ecs.insert(ProgramTime(600.0));
        crate::time_sync::initialize(&mut ecs);
        let make = |start, end| {
            let mut buffs = comp::Buffs::default();
            buffs.insert(Buff {
                kind: BuffKind::Invulnerability,
                data: BuffData::new(7386780.0, Some(Secs(180.0))),
                cat_ids: vec![], start_time: Time(start), end_time: Some(Time(end)),
                effects: vec![], source: BuffSource::Command,
            }, Time(start));
            buffs
        };
        let captured = make(39.73068386400001, 219.730683864);
        let unsynced = survival_buff_snapshot(&ecs, Some(&captured));
        assert_eq!(unsynced["clock"]["synchronized"], false);
        assert!(unsynced["buffs"][0]["active"].is_null());
        // Replay a server physics-Time packet, not a buff-derived offset.
        assert_eq!(crate::time_sync::apply_server_time(&ecs, Time(40.0)), 1.0);
        let synced = survival_buff_snapshot(&ecs, Some(&captured));
        assert_eq!(synced["clock"]["synchronized"], true);
        assert_eq!(synced["clock"]["now"], 40.0);
        assert_eq!(synced["buffs"][0]["active"], true);
        assert!((synced["buffs"][0]["remaining_seconds"].as_f64().unwrap() - 179.730683864).abs() < 1e-10);
        assert_eq!(synced["buffs"][0]["starts_in_seconds"], 0.0);
        // A genuinely delayed selected buff is NOT active in the synchronized clock.
        let delayed = survival_buff_snapshot(&ecs, Some(&make(60.0, 240.0)));
        assert_eq!(delayed["buffs"][0]["selected"], true);
        assert_eq!(delayed["buffs"][0]["active"], false);
        assert_eq!(delayed["buffs"][0]["starts_in_seconds"], 20.0);
        assert_eq!(delayed["buffs"][0]["remaining_seconds"], 200.0);
        let expired = survival_buff_snapshot(&ecs, Some(&make(-160.0, 20.0)));
        assert_eq!(expired["buffs"][0]["selected"], true);
        assert_eq!(expired["buffs"][0]["active"], false);
        assert_eq!(expired["buffs"][0]["remaining_seconds"], 0.0);
        // Existing elapsed physics time drives expiration, not the unrelated clocks.
        ecs.write_resource::<Time>().0 = 219.730683864;
        assert_eq!(survival_buff_snapshot(&ecs, Some(&captured))["buffs"][0]["active"], false);
    }

    #[test]
    fn survival_buff_time_projection_is_wired_to_production_snapshot() {
        let source = include_str!("mod.rs");
        let body = source.split_once("fn survival_snapshot(").unwrap().1
            .split_once("fn is_survival_action(").unwrap().0;
        assert!(body.contains("survival_buff_snapshot(client.state().ecs(), buffs.as_ref())"));
        assert!(body.contains("\"buffs\": buff_snapshot[\"buffs\"]"));
        assert!(!body.contains("read_resource::<common::resources::Time>"));
    }

    #[test]
    fn wall_jump_is_a_distinct_one_tick_input_released_on_cleanup() {
        let mut state = BridgeState::new("walljump-offline", Dir::from_unnormalized(Vec3::unit_x()).unwrap());
        state.release_wall_jump = true;
        let mut released = Vec::new();
        release_jump_with(&mut state, |input| released.push(input));
        assert_eq!(released, vec![InputKind::WallJump]);
        assert!(!state.release_wall_jump);
        release_jump_with(&mut state, |input| released.push(input));
        assert_eq!(released, vec![InputKind::WallJump]);
    }

    #[test]
    fn survival_buffs_distinguish_absent_empty_queued_and_stacking() {
        use common::{comp::buff::{Buff, BuffData, BuffKind, BuffSource}, resources::{Secs, Time}};
        assert_eq!(buffs_json(None, Some(0.0)), Value::Null);
        let mut buffs = comp::Buffs::default();
        assert_eq!(buffs_json(Some(&buffs), Some(0.0)), json!([]));
        let make = |kind, strength, start| Buff {
            kind, data: BuffData::new(strength, Some(Secs(5.0))),
            cat_ids: vec![], start_time: Time(start), end_time: Some(Time(start + 5.0)),
            effects: vec![], source: BuffSource::Item,
        };
        buffs.insert(make(BuffKind::Saturation, 2.0, 0.0), Time(0.0));
        buffs.insert(make(BuffKind::Saturation, 2.0, 0.0), Time(0.0));
        buffs.insert(make(BuffKind::PotionSickness, 0.33, 0.0), Time(0.0));
        buffs.insert(make(BuffKind::PotionSickness, 0.33, 0.0), Time(0.0));
        let snapshot = buffs_json(Some(&buffs), Some(1.0));
        let items = snapshot.as_array().unwrap();
        assert_eq!(items.iter().filter(|b| b["kind"] == "Saturation").count(), 2);
        assert_eq!(items.iter().filter(|b| b["kind"] == "Saturation" && b["active"] == true).count(), 1);
        assert!(items.iter().any(|b| b["starts_in_seconds"].as_f64().unwrap() > 0.0));
        assert_eq!(items.iter().filter(|b| b["kind"] == "PotionSickness" && b["active"] == true && b["stacks"] == true).count(), 2);
    }

    fn test_known_recipe_record(
        recipe: &str,
        satisfied: Option<bool>,
        missing_inputs: usize,
    ) -> KnownRecipeRecord {
        KnownRecipeRecord {
            recipe: recipe.to_owned(),
            value: json!({ "recipe": recipe }),
            satisfied,
            missing_inputs,
        }
    }

    #[test]
    fn current_observation_in_liquid_preserves_unknown_air_and_liquid_states() {
        let omitted = json!({});
        assert!(omitted.get("in_liquid").is_none());

        let absent = with_current_observation_in_liquid(json!({}), None);
        assert!(absent.as_object().unwrap().contains_key("in_liquid"));
        assert_eq!(absent.get("in_liquid"), Some(&Value::Null));

        let no_fluid = comp::PhysicsState::default();
        let no_fluid = with_current_observation_in_liquid(json!({}), Some(&no_fluid));
        assert_eq!(no_fluid.get("in_liquid"), Some(&json!(false)));

        let air = comp::PhysicsState {
            in_fluid: Some(comp::Fluid::Air {
                vel: comp::Vel(Vec3::zero()),
                elevation: 0.0,
            }),
            ..comp::PhysicsState::default()
        };
        let air = with_current_observation_in_liquid(json!({}), Some(&air));
        assert_eq!(air.get("in_liquid"), Some(&json!(false)));

        for depth in [0.0, 1.25] {
            let liquid = comp::PhysicsState {
                in_fluid: Some(comp::Fluid::Liquid {
                    kind: comp::fluid_dynamics::LiquidKind::Water,
                    vel: comp::Vel(Vec3::zero()),
                    depth,
                }),
                ..comp::PhysicsState::default()
            };
            let liquid = with_current_observation_in_liquid(json!({}), Some(&liquid));
            assert_eq!(liquid.get("in_liquid"), Some(&json!(true)));
        }
    }

    fn terrain_query_with(
        actor_position: Vec3<f32>,
        target_position: Vec3<f32>,
        cells: &BTreeMap<(i32, i32, i32), TerrainCellSample>,
    ) -> Value {
        let mut state = state();
        state.tick = 17;
        loaded_terrain_support_query(
            &state,
            actor_position,
            json!({"entity_id": 9, "uid": 11, "alias": "agent"}),
            json!({
                "health": {"current": 100.0, "maximum": 100.0, "dead": false},
                "in_liquid": false,
                "on_ground": true,
                "moving": false,
                "velocity": [0.0, 0.0, 0.0],
                "character_state": "Idle",
                "held_input": null,
                "current_action": null,
            }),
            target_position,
            Some((0.45, 0.0, 1.8)),
            |position| {
                cells
                    .get(&(position.x, position.y, position.z))
                    .copied()
                    .unwrap_or(TerrainCellSample::Loaded(Block::empty()))
            },
        )
    }

    #[test]
    fn terrain_query_enforces_the_existing_move_envelope_at_its_boundaries() {
        let actor = Vec3::new(0.0, 0.0, 5.0);
        assert_eq!(
            terrain_query_target(&json!({"position": [10.0, 0.0, 7.0]}), actor).unwrap(),
            Vec3::new(10.0, 0.0, 7.0),
        );
        assert!(
            terrain_query_target(&json!({"position": [10.001, 0.0, 5.0]}), actor)
                .is_err()
        );
        assert!(
            terrain_query_target(&json!({"position": [0.0, 0.0, 7.001]}), actor)
                .is_err()
        );
        assert!(terrain_query_target(
            &json!({"position": [1.0, 2.0, 5.0], "radius": 3}),
            actor,
        )
        .is_err());
    }

    #[test]
    fn terrain_query_body_clearance_rejects_recorded001_neighbor_not_just_rounding() {
        let rock = TerrainCellSample::Loaded(Block::new(
            BlockKind::Rock, vek::Rgb::new(1, 1, 1)));
        let cells = BTreeMap::from([
            ((26583, 4892, 143), rock),
            ((26582, 4892, 143), rock),
            ((26582, 4893, 144), rock),
        ]);
        // The submitted Python point rounds to this native f32 coordinate.
        let rounded = 26582.999808624492_f64 as f32;
        assert_eq!(rounded, 26583.0);
        // A representable point immediately below the boundary still overlaps.
        for x in [rounded, f32::from_bits(rounded.to_bits() - 1)] {
            let query = terrain_query_with(
                Vec3::new(x, 4892.8154296875, 144.0),
                Vec3::new(x, 4892.8154296875, 144.0), &cells);
            let candidate = &query["support_candidates"][0];
            assert_eq!(candidate["foot_clearance"]["clear"], true);
            assert_eq!(candidate["head_clearance"]["clear"], true);
            assert_eq!(candidate["body_clearance"]["known"], true);
            assert_eq!(candidate["body_clearance"]["clear"], false);
            assert_eq!(candidate["occupiable"], false);
        }
    }

    #[test]
    fn terrain_query_body_clearance_preserves_recorded006_007_valid_edge_controls() {
        // Exact recorded targets; a controlled level Rock support environment.
        // This is a geometry regression, not a replay of the entire old world.
        for point in [
            Vec3::<f32>::new(26577.490234375, 4915.50439453125, 144.0),
            Vec3::new(26584.587890625, 4935.21435546875, 144.0),
            Vec3::new(26572.28125, 4900.9892578125, 144.0),
            Vec3::new(26552.3984375, 4898.10986328125, 144.0),
        ] {
            let column = point.xy().map(|v| v.floor() as i32);
            let cells = BTreeMap::from([((column.x, column.y, 143),
                TerrainCellSample::Loaded(Block::new(BlockKind::Rock, vek::Rgb::new(1, 1, 1))))]);
            let query = terrain_query_with(point, point, &cells);
            assert_eq!(query["support_candidates"][0]["occupiable"], true, "{point:?}");
            assert_eq!(query["support_candidates"][0]["body_clearance"]["clear"], true);
        }
    }

    #[test]
    fn terrain_query_body_clearance_preserves_valid_elevated_fountain() {
        let fountain = Block::empty().with_sprite(SpriteKind::FountainArabic);
        assert_eq!(fountain.solid_height(), 2.4);
        let cells = BTreeMap::from([((26586, 4904, 144),
            TerrainCellSample::Loaded(fountain))]);
        let point = Vec3::new(26586.94140625, 4904.61767578125, 146.39999389648438);
        let query = terrain_query_with(point, point, &cells);
        assert_eq!(query["support_candidates"][0]["standing_position"],
                   json!([point.x, point.y, point.z]));
        assert_eq!(query["support_candidates"][0]["occupiable"], true);
    }

    #[test]
    fn terrain_query_body_clearance_requires_neighbor_load_and_actual_actor_height() {
        let rock = TerrainCellSample::Loaded(Block::new(
            BlockKind::Rock, vek::Rgb::new(1, 1, 1)));
        for sample in [TerrainCellSample::Unloaded, TerrainCellSample::Unknown, rock] {
            let cells = BTreeMap::from([((0, 0, 0), rock), ((-1, 0, 2), sample)]);
            let query = terrain_query_with(
                Vec3::new(0.1, 0.5, 1.0), Vec3::new(0.1, 0.5, 1.0), &cells);
            assert_eq!(query["support_candidates"][0]["occupiable"], false);
        }
        let mut sample = |_: Vec3<i32>| TerrainCellSample::Loaded(Block::empty());
        assert_eq!(terrain_body_clearance(Vec3::zero(), None, &mut sample)["clear"], false);
        let mut sample = |p: Vec3<i32>| if p == Vec3::new(-1, 0, 3) {
            rock
        } else { TerrainCellSample::Loaded(Block::empty()) };
        assert_eq!(terrain_body_clearance(Vec3::new(0.1, 0.5, 1.0),
            Some((0.45, 0.0, 2.95)), &mut sample)["clear"], false);
    }

    #[test]
    fn terrain_query_reports_support_geometry_and_known_clearance() {
        let support_position = Vec3::new(10, 0, 4);
        let cells = BTreeMap::from([(
            (support_position.x, support_position.y, support_position.z),
            TerrainCellSample::Loaded(Block::new(
                common::terrain::BlockKind::Grass,
                vek::Rgb::new(11, 102, 35),
            )),
        )]);
        let query = terrain_query_with(
            Vec3::new(0.0, 0.0, 5.0),
            Vec3::new(10.0, 0.25, 5.0),
            &cells,
        );

        assert_eq!(query["status"], "complete");
        assert_eq!(query["binding"]["tick"], 17);
        assert_eq!(query["request"]["target_voxel_column"], json!([10, 0]));
        assert_eq!(query["support_candidates"].as_array().unwrap().len(), 1);
        let candidate = &query["support_candidates"][0];
        assert_eq!(candidate["standing_position"], json!([10.0, 0.25, 5.0]));
        assert_eq!(candidate["support"]["kind"], "Grass");
        assert_eq!(candidate["support"]["solid"], true);
        assert_eq!(candidate["support"]["liquid"], false);
        assert_eq!(candidate["foot_clearance"]["known"], true);
        assert_eq!(candidate["foot_clearance"]["clear"], true);
        assert_eq!(candidate["head_clearance"]["known"], true);
        assert_eq!(candidate["head_clearance"]["clear"], true);
        assert_eq!(candidate["occupiable"], true);
    }

    #[test]
    fn terrain_query_never_certifies_lava_support_or_clearance_as_occupiable() {
        let lava_block = Block::new(
            common::terrain::BlockKind::Lava,
            vek::Rgb::new(255, 32, 0),
        );
        assert!(lava_block.kind().is_filled());
        assert!(!lava_block.is_solid());
        assert!(!lava_block.is_liquid());
        let lava = TerrainCellSample::Loaded(lava_block);
        let grass = TerrainCellSample::Loaded(Block::new(
            common::terrain::BlockKind::Grass,
            vek::Rgb::new(11, 102, 35),
        ));
        let lava_support = terrain_query_with(
            Vec3::zero(),
            Vec3::zero(),
            &BTreeMap::from([((0, 0, -1), lava)]),
        );
        let lava_clearance = terrain_query_with(
            Vec3::zero(),
            Vec3::zero(),
            &BTreeMap::from([((0, 0, -1), grass), ((0, 0, 0), lava)]),
        );

        assert_eq!(lava_support["support_candidates"].as_array().unwrap().len(), 1);
        assert_eq!(lava_support["support_candidates"][0]["support"]["kind"], "Lava");
        assert_eq!(lava_support["support_candidates"][0]["support_usable"], false);
        assert_eq!(lava_support["support_candidates"][0]["occupiable"], false);
        assert_eq!(
            lava_clearance["support_candidates"][0]["foot_clearance"]["clear"],
            false
        );
        assert_eq!(lava_clearance["support_candidates"][0]["occupiable"], false);
    }

    #[test]
    fn terrain_query_rejects_every_native_on_ground_debuff_sprite() {
        let grass = TerrainCellSample::Loaded(Block::new(
            common::terrain::BlockKind::Grass,
            vek::Rgb::new(11, 102, 35),
        ));
        for sprite in [
            SpriteKind::EnsnaringVines,
            SpriteKind::EnsnaringWeb,
            SpriteKind::SeaUrchin,
            SpriteKind::HaniwaTrap,
            SpriteKind::IronSpike,
            SpriteKind::HaniwaTrapTriggered,
            SpriteKind::HotSurface,
            SpriteKind::IceSpike,
            SpriteKind::FireBlock,
        ] {
            let hazard = Block::air(sprite);
            assert!(
                terrain_block_is_hazardous(hazard),
                "{sprite:?} must remain tied to its native on-ground debuff"
            );

            let support_query = terrain_query_with(
                Vec3::new(0.0, 0.0, hazard.solid_height()),
                Vec3::new(0.0, 0.0, hazard.solid_height()),
                &BTreeMap::from([((0, 0, 0), TerrainCellSample::Loaded(hazard))]),
            );
            assert_eq!(
                support_query["support_candidates"][0]["support_usable"],
                false,
                "{sprite:?} must not be usable support"
            );
            assert_eq!(
                support_query["support_candidates"][0]["occupiable"],
                false,
                "{sprite:?} must not be occupiable support"
            );

            let clearance_query = terrain_query_with(
                Vec3::zero(),
                Vec3::zero(),
                &BTreeMap::from([
                    ((0, 0, -1), grass),
                    ((0, 0, 0), TerrainCellSample::Loaded(hazard)),
                ]),
            );
            assert_eq!(
                clearance_query["support_candidates"][0]["foot_clearance"]["clear"],
                false,
                "{sprite:?} must block clearance"
            );
            assert_eq!(
                clearance_query["support_candidates"][0]["occupiable"],
                false,
                "{sprite:?} clearance must not be occupiable"
            );
        }
    }

    #[test]
    fn terrain_overlap_boundaries_keep_self_contact_and_low_geometry_open() {
        let grass = Block::new(
            common::terrain::BlockKind::Grass,
            vek::Rgb::new(11, 102, 35),
        );
        let low_geometry = Block::air(SpriteKind::LillyPads);

        assert!(!terrain_block_overlaps_span(-1, grass, 0.0, 1.0));
        assert!(!terrain_block_overlaps_span(0, low_geometry, 0.5, 1.5));
    }

    #[test]
    fn terrain_query_rejects_representable_overlap_at_large_signed_heights() {
        let grass = TerrainCellSample::Loaded(Block::new(
            common::terrain::BlockKind::Grass,
            vek::Rgb::new(11, 102, 35),
        ));
        for standing_height in [10_000_000.0_f32, -10_000_000.0_f32] {
            let support_z = (standing_height - 1.0) as i32;
            let occupied_z = standing_height as i32;
            let actor = Vec3::new(0.0, 0.0, standing_height);
            let safe_query = terrain_query_with(
                actor,
                actor,
                &BTreeMap::from([((0, 0, support_z), grass)]),
            );
            let safe_candidate = safe_query["support_candidates"]
                .as_array()
                .unwrap()
                .iter()
                .find(|candidate| {
                    candidate["standing_height"].as_f64()
                        == Some(f64::from(standing_height))
                })
                .unwrap();
            assert_eq!(safe_candidate["foot_clearance"]["clear"], true);
            assert_eq!(safe_candidate["occupiable"], true);

            let occupied_query = terrain_query_with(
                actor,
                actor,
                &BTreeMap::from([
                    ((0, 0, support_z), grass),
                    ((0, 0, occupied_z), grass),
                ]),
            );
            let occupied_candidate = occupied_query["support_candidates"]
                .as_array()
                .unwrap()
                .iter()
                .find(|candidate| {
                    candidate["standing_height"].as_f64()
                        == Some(f64::from(standing_height))
                })
                .unwrap();
            assert_eq!(occupied_candidate["foot_clearance"]["clear"], false);
            assert_eq!(occupied_candidate["occupiable"], false);
        }
    }

    #[test]
    fn terrain_query_clearance_includes_lower_protruding_sprite_contributors() {
        let grass = TerrainCellSample::Loaded(Block::new(
            common::terrain::BlockKind::Grass,
            vek::Rgb::new(11, 102, 35),
        ));
        for (support_z, sprite) in [
            (-2, SpriteKind::TallCactus),
            (-5, SpriteKind::TerracottaStatue),
        ] {
            let protruding = TerrainCellSample::Loaded(Block::air(sprite));
            let query = terrain_query_with(
                Vec3::zero(),
                Vec3::zero(),
                &BTreeMap::from([
                    ((0, 0, support_z), protruding),
                    ((0, 0, -1), grass),
                ]),
            );
            let candidate = query["support_candidates"]
                .as_array()
                .unwrap()
                .iter()
                .find(|candidate| candidate["support"]["kind"] == "Grass")
                .unwrap();
            let contributor_z = candidate["foot_clearance"]["cells"]
                .as_array()
                .unwrap()
                .iter()
                .map(|cell| cell["position"][2].as_i64().unwrap())
                .collect::<Vec<_>>();

            assert!(contributor_z.contains(&i64::from(support_z)));
            assert_eq!(candidate["foot_clearance"]["clear"], false);
            assert_eq!(candidate["occupiable"], false);
        }
    }

    #[test]
    fn terrain_query_scan_includes_the_tallest_current_sprite_support() {
        let statue = Block::air(SpriteKind::TerracottaStatue);
        assert_eq!(
            statue.solid_height(),
            TERRAIN_QUERY_MAX_SUPPORT_HEIGHT_METRES
        );
        let cells = BTreeMap::from([((0, 0, -5), TerrainCellSample::Loaded(statue))]);

        let query = terrain_query_with(Vec3::zero(), Vec3::new(0.0, 0.0, 0.29), &cells);
        let candidates = query["support_candidates"].as_array().unwrap();

        assert_eq!(candidates.len(), 1);
        assert_eq!(candidates[0]["support"]["sprite"], "TerracottaStatue");
        let reported_height = candidates[0]["support"]["solid_height"].as_f64().unwrap();
        assert!(
            (reported_height - f64::from(TERRAIN_QUERY_MAX_SUPPORT_HEIGHT_METRES)).abs()
                < 1.0e-6
        );
        assert_eq!(candidates[0]["support_usable"], false);
        assert_eq!(candidates[0]["occupiable"], false);
        assert_eq!(
            query["vertical_scan"]["max_physics_support_solid_height_metres"],
            f64::from(Block::MAX_HEIGHT)
        );
    }

    #[test]
    fn terrain_query_never_treats_unloaded_or_unknown_clearance_as_open() {
        let support = TerrainCellSample::Loaded(Block::new(
            common::terrain::BlockKind::Grass,
            vek::Rgb::new(11, 102, 35),
        ));
        let unloaded_cells = BTreeMap::from([
            ((0, 0, -1), support),
            ((0, 0, 0), TerrainCellSample::Unloaded),
        ]);
        let unknown_cells = BTreeMap::from([
            ((0, 0, -1), support),
            ((0, 0, -5), TerrainCellSample::Unknown),
        ]);

        let unloaded = terrain_query_with(Vec3::zero(), Vec3::zero(), &unloaded_cells);
        let unknown = terrain_query_with(Vec3::zero(), Vec3::zero(), &unknown_cells);

        assert_eq!(unloaded["status"], "unloaded_cells");
        assert_eq!(unloaded["support_candidates"][0]["foot_clearance"]["known"], false);
        assert_eq!(unloaded["support_candidates"][0]["occupiable"], false);
        assert_eq!(unknown["status"], "unknown_cells");
        assert_eq!(unknown["support_candidates"][0]["foot_clearance"]["known"], false);
        assert_eq!(unknown["support_candidates"][0]["occupiable"], false);
    }

    #[test]
    fn terrain_query_enumerates_multiple_support_levels_without_selecting_one() {
        let rock = TerrainCellSample::Loaded(Block::new(
            common::terrain::BlockKind::Rock,
            vek::Rgb::zero(),
        ));
        let cells = BTreeMap::from([
            ((0, 0, -1), rock),
            ((0, 0, 2), rock),
        ]);

        let query = terrain_query_with(
            Vec3::new(0.0, 0.0, 1.0),
            Vec3::new(0.0, 0.0, 1.0),
            &cells,
        );
        let candidates = query["support_candidates"].as_array().unwrap();

        assert_eq!(query["status"], "complete");
        assert_eq!(candidates.len(), 2);
        assert_eq!(candidates[0]["standing_height"], 0.0);
        assert_eq!(candidates[1]["standing_height"], 3.0);
        assert_eq!(candidates[0]["occupiable"], true);
        assert_eq!(candidates[1]["occupiable"], true);
        assert_eq!(query["semantics"]["route_or_traversal_certificate"], false);
    }

    #[test]
    fn terrain_query_distinguishes_loaded_window_without_support_candidate() {
        let query = terrain_query_with(Vec3::zero(), Vec3::zero(), &BTreeMap::new());

        assert_eq!(query["status"], "no_candidate_in_window");
        assert_eq!(query["support_candidates"], json!([]));
        assert_eq!(query["vertical_scan"]["all_cells_loaded"], true);
    }

    #[test]
    fn action_signature_ignores_object_key_order() {
        let first = json!({
            "duration_ticks": 60,
            "action": {"type": "move_forward", "speed": 0.5, "action_id": "a"},
        });
        let second = json!({
            "action": {"action_id": "a", "speed": 0.5, "type": "move_forward"},
            "duration_ticks": 60,
        });
        assert_eq!(action_signature(&first), action_signature(&second));
    }

    #[test]
    fn marker_wpos_is_already_world_meters() {
        let wpos = Vec2::new(18_703.9, 10_800.0);
        assert_eq!(marker_wpos_to_world_xy(wpos), wpos);
    }

    #[test]
    fn minimum_path_length_parser_defaults_to_small() {
        assert_eq!(parse_min_path_length(None), PathLength::Small);
        assert_eq!(parse_min_path_length(Some("")), PathLength::Small);
        assert_eq!(parse_min_path_length(Some(" \t ")), PathLength::Small);
        assert_eq!(parse_min_path_length(Some("small")), PathLength::Small);
        assert_eq!(parse_min_path_length(Some("SMALL")), PathLength::Small);
        assert_eq!(parse_min_path_length(Some(" small ")), PathLength::Small);
        assert_eq!(parse_min_path_length(Some("medium")), PathLength::Medium);
        assert_eq!(parse_min_path_length(Some("long")), PathLength::Long);
        assert_eq!(parse_min_path_length(Some("longest")), PathLength::Longest);
        assert_eq!(parse_min_path_length(Some("huge")), PathLength::Small);
    }

    #[test]
    fn edge_cost_mode_parser_defaults_to_legacy_behavior() {
        assert_eq!(parse_edge_cost_mode(None), EdgeCostMode::LegacyTierDerived);
        assert_eq!(parse_edge_cost_mode(Some("")), EdgeCostMode::LegacyTierDerived);
        assert_eq!(
            parse_edge_cost_mode(Some(" \t ")),
            EdgeCostMode::LegacyTierDerived
        );
        assert_eq!(
            parse_edge_cost_mode(Some("default")),
            EdgeCostMode::LegacyTierDerived
        );
        assert_eq!(parse_edge_cost_mode(Some("ON")), EdgeCostMode::ForceOn);
        assert_eq!(parse_edge_cost_mode(Some(" off ")), EdgeCostMode::ForceOff);
        assert_eq!(
            parse_edge_cost_mode(Some("huge")),
            EdgeCostMode::LegacyTierDerived
        );
    }

    #[test]
    fn known_sites_are_sorted_by_horizontal_distance_and_capped() {
        let sites = (0..(MAX_KNOWN_SITES + 2)).map(|index| KnownSite {
            kind: "Town".to_owned(),
            name: Some(index.to_string()),
            wpos: Vec2::new((MAX_KNOWN_SITES + 1 - index) as f32, 0.0),
        });

        let sites = known_site_records_json(sites, Some(Vec3::zero()));

        assert_eq!(sites.len(), MAX_KNOWN_SITES);
        assert_eq!(sites[0]["position"], json!([0.0, 0.0, null]));
        assert_eq!(sites[0]["horizontal_distance"], 0.0);
        assert_eq!(
            sites[MAX_KNOWN_SITES - 1]["horizontal_distance"],
            (MAX_KNOWN_SITES - 1) as f32
        );
    }

    #[test]
    fn map_projection_keeps_site_less_cave_and_native_marker_semantics() {
        use common::map::{Marker, MarkerKind};
        let cave = Marker::at(Vec2::new(-96.5, 64.25)).with_kind(MarkerKind::Cave);
        let town = Marker::at(Vec2::new(32.0, 0.0))
            .with_kind(MarkerKind::Town)
            .with_site_id(Some(7))
            .with_label(common_i18n::Content::Plain("Native town".to_owned()));
        assert!(cave.site.is_none());
        let markers = [cave, town];
        let records = map_marker_records_json(markers.iter(), Some(Vec3::new(0.0, 0.0, 900.0)));
        assert_eq!(records.len(), 2);
        assert_eq!(records[0]["kind"], "Town");
        assert_eq!(records[0]["name"], "Native town");
        assert_eq!(records[0]["position"], json!([32.0, 0.0, null]));
        assert_eq!(records[0]["horizontal_distance"], 32.0);
        assert_eq!(records[1]["kind"], "Cave");
        assert_eq!(records[1]["name"], Value::Null);
        assert_eq!(records[1]["map_id"], "Cave@-96.5,64.25");
        assert_eq!(records[1]["position"], json!([-96.5, 64.25, null]));
        let caves = map_cave_records_json(markers.iter(), None);
        assert_eq!(caves.len(), 1);
        assert_eq!(caves[0]["position"], json!([-96.5, 64.25, null]));
        assert_eq!(caves[0]["map_id"], records[1]["map_id"]);
        assert_eq!(caves[0]["horizontal_distance"], Value::Null);
    }

    #[test]
    fn map_projection_caves_remain_available_behind_twelve_nearer_landmarks() {
        use common::map::{Marker, MarkerKind};
        let mut markers = (0..MAX_KNOWN_SITES)
            .rev()
            .map(|index| Marker::at(Vec2::new(index as f32, 0.0))
                .with_kind(MarkerKind::Town)
                .with_site_id(Some(index as u64)))
            .collect::<Vec<_>>();
        markers.extend((0..6).rev().map(|index| {
            Marker::at(Vec2::new(100.0 + index as f32, 0.0)).with_kind(MarkerKind::Cave)
        }));
        let ordinary = map_marker_records_json(markers.iter(), Some(Vec3::zero()));
        assert_eq!(ordinary.len(), MAX_KNOWN_SITES);
        for (index, record) in ordinary.iter().enumerate() {
            assert_eq!(record["kind"], "Town");
            assert_eq!(record["position"], json!([index as f32, 0.0, null]));
        }
        let caves = map_cave_records_json(markers.iter(), Some(Vec3::zero()));
        assert_eq!(caves.len(), MAX_KNOWN_MAP_CAVES);
        for (index, record) in caves.iter().enumerate() {
            assert_eq!(record["kind"], "Cave");
            assert_eq!(record["position"], json!([100.0 + index as f32, 0.0, null]));
            assert_eq!(record["horizontal_distance"], 100.0 + index as f32);
        }
    }

    #[test]
    fn event_batch_reports_retention_gap() {
        let mut state = state();
        state.events.push_back(json!({ "seq": 4, "event": "test" }));
        state.next_event_seq = 5;
        let batch = event_batch(&state, 1, 256);
        assert_eq!(batch["oldest_seq"], 4);
        assert_eq!(batch["next_seq"], 5);
        assert_eq!(batch["truncated"], true);
    }

    #[test]
    fn numeric_arguments_are_rejected_instead_of_clamped() {
        let action = json!({ "speed": 2.0 });
        let object = action.as_object().unwrap();
        assert!(number_arg(object, "speed", 1.0, 0.05, 1.0).is_err());
        assert!(ticks_arg(&json!({ "duration_ticks": 0 }), "duration_ticks", 60, 600).is_err());
    }

    #[test]
    fn craft_amount_is_positive_and_has_a_reportable_cap() {
        let default = json!({});
        assert_eq!(craft_amount_arg(default.as_object().unwrap()), Ok((1, 1)));

        let zero = json!({ "amount": 0 });
        assert!(craft_amount_arg(zero.as_object().unwrap()).is_err());

        let fractional = json!({ "amount": 1.5 });
        assert!(craft_amount_arg(fractional.as_object().unwrap()).is_err());

        let over_cap = json!({ "amount": MAX_CRAFT_AMOUNT + 1 });
        assert_eq!(
            craft_amount_arg(over_cap.as_object().unwrap()),
            Ok((u64::from(MAX_CRAFT_AMOUNT) + 1, MAX_CRAFT_AMOUNT))
        );
    }

    #[test]
    fn craft_station_requires_integer_voxel_coordinates() {
        let station = json!({ "station": [-2, 3, 4] });
        assert_eq!(
            integer_voxel_arg(station.as_object().unwrap(), "station"),
            Ok(Vec3::new(-2, 3, 4))
        );

        let fractional = json!({ "station": [-2.5, 3, 4] });
        assert!(integer_voxel_arg(fractional.as_object().unwrap(), "station").is_err());
    }

    #[test]
    fn craft_station_range_uses_the_server_strict_threshold() {
        let station = Vec3::zero();
        let station_center = Vec3::broadcast(0.5);
        let inside = find_dist::Cylinder {
            center: station_center - Vec3::new(4.999, 0.0, 0.0),
            radius: 0.0,
            height: 0.0,
        };
        assert!(craft_station_in_range(craft_station_distance_from_cylinder(
            inside, station
        )));

        let at_limit = find_dist::Cylinder {
            center: station_center - Vec3::new(MAX_PICKUP_RANGE, 0.0, 0.0),
            radius: 0.0,
            height: 0.0,
        };
        assert!(!craft_station_in_range(craft_station_distance_from_cylinder(
            at_limit, station
        )));
    }

    #[test]
    fn inventory_delta_reports_added_removed_and_changed_definitions() {
        let mut start = BTreeMap::new();
        start.insert("common.items.log.wood".to_owned(), 1);
        start.insert("common.items.crafting_ing.twigs".to_owned(), 4);
        let mut end = BTreeMap::new();
        end.insert("common.items.crafting_ing.twigs".to_owned(), 8);
        end.insert("common.items.mineral.stone".to_owned(), 1);

        let delta = inventory_delta(&start, &end);
        assert_eq!(delta["common.items.log.wood"], -1);
        assert_eq!(delta["common.items.crafting_ing.twigs"], 4);
        assert_eq!(delta["common.items.mineral.stone"], 1);
        assert_eq!(delta.len(), 3);
    }

    #[test]
    fn compound_item_identity_uses_its_simple_base_definition() {
        let compound = common::comp::inventory::item::ItemDefinitionId::Compound {
            simple_base: "common.items.weapons.sword.zweihander",
            components: Vec::new(),
        };

        assert_eq!(
            item_definition_id_key(compound),
            "common.items.weapons.sword.zweihander"
        );
    }

    #[test]
    fn craft_output_observation_distinguishes_none_partial_and_all_output() {
        assert_eq!(
            craft_output_inventory_observation(0, 10),
            ("no_positive_output_definition_delta_observed", 10)
        );

        let (observation, shortfall) = craft_output_inventory_observation(1, 10);
        assert_eq!(
            observation,
            "fewer_output_definitions_than_expected_observed"
        );
        assert_eq!(shortfall, 9);
        assert_ne!(observation, "at_least_expected_output_definition_delta_observed");

        assert_eq!(
            craft_output_inventory_observation(10, 10),
            ("at_least_expected_output_definition_delta_observed", 0)
        );
    }

    #[test]
    fn known_recipes_are_satisfied_first_and_deterministically_ordered() {
        let first = known_recipe_records_json(
            4,
            vec![
                test_known_recipe_record("many_missing", Some(false), 2),
                test_known_recipe_record("zeta_satisfied", Some(true), 0),
                test_known_recipe_record("few_missing", Some(false), 1),
                test_known_recipe_record("alpha_satisfied", Some(true), 0),
            ],
        );
        let second = known_recipe_records_json(
            4,
            vec![
                test_known_recipe_record("alpha_satisfied", Some(true), 0),
                test_known_recipe_record("few_missing", Some(false), 1),
                test_known_recipe_record("zeta_satisfied", Some(true), 0),
                test_known_recipe_record("many_missing", Some(false), 2),
            ],
        );

        assert_eq!(first, second);
        assert_eq!(
            first["items"]
                .as_array()
                .unwrap()
                .iter()
                .map(|item| item["recipe"].as_str().unwrap())
                .collect::<Vec<_>>(),
            vec![
                "alpha_satisfied",
                "zeta_satisfied",
                "few_missing",
                "many_missing",
            ]
        );
    }

    #[test]
    fn known_recipe_reports_partial_concrete_item_shortfall() {
        let item = "common.items.crafting_ing.twigs";
        let recipe = test_recipe(vec![(test_item_input(item), 2, false)]);
        let inventory = test_inventory_with(item, 1);

        let record = known_recipe_record(
            "two_twigs",
            Some(SpriteKind::SpinningWheel),
            &recipe,
            Some(&inventory),
        );

        assert_eq!(record.satisfied, Some(false));
        assert_eq!(
            record.value["missing"],
            json!([{ "item": item, "amount": 1 }])
        );
        assert_eq!(record.value["station"], "SpinningWheel");
    }

    #[test]
    fn known_recipe_reports_tag_and_list_inputs_as_unknown() {
        let item = "common.items.crafting_ing.twigs";
        let inventory = test_inventory_with(item, 2);
        let tag_recipe = test_recipe(vec![(
            RecipeInput::Tag(common::comp::inventory::item::ItemTag::Food),
            2,
            false,
        )]);
        let same_tag_recipe = test_recipe(vec![(
            RecipeInput::TagSameItem(common::comp::inventory::item::ItemTag::Food),
            2,
            false,
        )]);
        let list_recipe = test_recipe(vec![(
            RecipeInput::ListSameItem(vec![
                std::sync::Arc::<common::comp::inventory::item::ItemDef>::load_expect_cloned(item),
            ]),
            2,
            false,
        )]);

        let tag = known_recipe_record("tag", None, &tag_recipe, Some(&inventory));
        let same_tag = known_recipe_record("same_tag", None, &same_tag_recipe, Some(&inventory));
        let list = known_recipe_record("list", None, &list_recipe, Some(&inventory));

        assert_eq!(tag.satisfied, None);
        assert_eq!(tag.value["inputs"][0]["kind"], "tag");
        assert_eq!(tag.value["missing"], json!([{ "item": "tag:Food", "amount": 2 }]));
        assert_eq!(same_tag.satisfied, None);
        assert_eq!(same_tag.value["inputs"][0]["kind"], "tag_same_item");
        assert_eq!(
            same_tag.value["missing"],
            json!([{ "item": "tag_same_item:Food", "amount": 2 }])
        );
        assert_eq!(list.satisfied, None);
        assert_eq!(list.value["inputs"][0]["kind"], "list_same_item");
        assert_eq!(
            list.value["missing"],
            json!([{ "item": "list_same_item:[common.items.crafting_ing.twigs]", "amount": 2 }])
        );
    }

    #[test]
    fn known_recipe_truncation_reports_total_and_omitted() {
        let total = MAX_KNOWN_RECIPES + 2;
        let recipes = (0..total)
            .map(|index| test_known_recipe_record(&format!("recipe_{index:02}"), Some(false), 1))
            .collect();

        let recipes = known_recipe_records_json(total, recipes);

        assert_eq!(recipes["total"], json!(total));
        assert_eq!(recipes["omitted"], 2);
        assert_eq!(recipes["items"].as_array().unwrap().len(), MAX_KNOWN_RECIPES);
    }

    #[test]
    fn craft_inventory_update_is_routed_to_the_waiting_craft_action() {
        let mut state = state();
        state.tick = 1;
        state.pending_craft_request_sequences.push_back(1);
        state.active = Some(ActiveAction {
            request_id: Value::Null,
            action_id: "craft".to_owned(),
            action_type: "craft".to_owned(),
            signature: String::new(),
            started_tick: 0,
            deadline_tick: 1,
            start_position: None,
            progress_position: None,
            last_progress_tick: 0,
            survival_before: None,
            kind: ActiveKind::Craft {
                recipe: "twig".to_owned(),
                requested_amount: 1,
                amount: 1,
                station: None,
                required_sprite: None,
                craft_request_sequence: 1,
                output_definition_id: "common.items.crafting_ing.twigs".to_owned(),
                expected_output_amount: 1,
                start_inventory: InventorySnapshot {
                    definition_counts: BTreeMap::new(),
                    free_slots: 0,
                },
                saw_craft_update: false,
            },
        });

        route_inventory_update(&mut state, &[comp::InventoryUpdateEvent::Craft]);

        let ActiveKind::Craft {
            saw_craft_update, ..
        } = &state.active.as_ref().unwrap().kind
        else {
            panic!("expected active craft action");
        };
        assert!(*saw_craft_update);
    }

    #[test]
    fn late_craft_update_for_predecessor_is_not_credited_to_active_craft() {
        let mut state = state();
        state.tick = 11;
        state.pending_craft_request_sequences.extend([1, 2]);
        state.active = Some(ActiveAction {
            request_id: Value::Null,
            action_id: "second-craft".to_owned(),
            action_type: "craft".to_owned(),
            signature: String::new(),
            started_tick: 10,
            deadline_tick: 20,
            start_position: None,
            progress_position: None,
            last_progress_tick: 10,
            survival_before: None,
            kind: ActiveKind::Craft {
                recipe: "twig".to_owned(),
                requested_amount: 1,
                amount: 1,
                station: None,
                required_sprite: None,
                craft_request_sequence: 2,
                output_definition_id: "common.items.crafting_ing.twigs".to_owned(),
                expected_output_amount: 1,
                start_inventory: InventorySnapshot {
                    definition_counts: BTreeMap::new(),
                    free_slots: 0,
                },
                saw_craft_update: false,
            },
        });

        route_inventory_update(&mut state, &[comp::InventoryUpdateEvent::Craft]);

        let ActiveKind::Craft {
            saw_craft_update, ..
        } = &state.active.as_ref().unwrap().kind
        else {
            panic!("expected active craft action");
        };
        assert!(!*saw_craft_update);
        assert_eq!(
            state
                .pending_craft_request_sequences
                .iter()
                .copied()
                .collect::<Vec<_>>(),
            vec![2]
        );
    }

    #[test]
    fn chat_message_requires_a_bounded_string() {
        let valid = json!({ "message": "hello" });
        assert_eq!(chat_message_arg(valid.as_object().unwrap()), Ok("hello"));

        let non_string = json!({ "message": 1 });
        assert!(chat_message_arg(non_string.as_object().unwrap()).is_err());

        let too_long = json!({ "message": "x".repeat(MAX_CHAT_MESSAGE_BYTES + 1) });
        assert!(chat_message_arg(too_long.as_object().unwrap()).is_err());
    }

    #[test]
    fn server_command_requires_bounded_name_and_string_arguments() {
        let valid = json!({ "name": "make_npc", "args": ["common.entity.wild.aggressive.wolf", "1"] });
        assert_eq!(
            server_command_args(valid.as_object().unwrap()),
            Ok(("make_npc", vec!["common.entity.wild.aggressive.wolf".to_owned(), "1".to_owned()]))
        );
        assert!(server_command_args(json!({ "name": "", "args": [] }).as_object().unwrap()).is_err());
        assert!(server_command_args(json!({ "name": "make npc", "args": [] }).as_object().unwrap()).is_err());
        assert!(server_command_args(json!({ "name": "make_npc", "args": [1] }).as_object().unwrap()).is_err());
        assert!(server_command_args(
            json!({ "name": "make_npc", "args": ["x".repeat(MAX_SERVER_COMMAND_ARG_BYTES + 1)] })
                .as_object()
                .unwrap()
        )
        .is_err());
    }

    #[test]
    fn command_feedback_retains_localized_content_without_translation() {
        let content = comp::Content::localized("fixture-command-denied")
            .with_arg("command", "buff")
            .with_arg("required", 7_u64);
        for kind in [comp::ChatType::CommandError, comp::ChatType::CommandInfo] {
            let message: comp::ChatMsg = kind.into_msg(content.clone());
            let data = chat_event_data(&message);
            assert_eq!(data["content"], json!(content));
            assert_eq!(data["content"]["Localized"]["key"], "fixture-command-denied");
            assert_eq!(data["content"]["Localized"]["args"]["command"]["Content"]["Plain"], "buff");
            assert_eq!(data["content"]["Localized"]["args"]["required"]["Nat"], 7);
            assert_eq!(data["text"], Value::Null);
            assert_eq!(data["content_kind"], "localized");
        }
    }

    #[test]
    fn command_feedback_retains_content_variants_without_expanding_private_chat() {
        for content in [
            comp::Content::Plain("plain command reply".to_owned()),
            comp::Content::Key("fixture-key".to_owned()),
            comp::Content::with_attr("fixture-key", "fixture-attr"),
            comp::Content::WithFallback(
                Box::new(comp::Content::localized("fixture-localized")),
                Box::new(comp::Content::Plain("fallback".to_owned())),
            ),
        ] {
            let message: comp::ChatMsg = comp::ChatType::CommandInfo.into_msg(content.clone());
            let data = chat_event_data(&message);
            assert_eq!(data["content"], json!(content));
            assert_eq!(data["text"], json!(content.as_plain()));
            let uid = Uid(NonZeroU64::new(7).unwrap());
            let private: comp::ChatMsg = comp::ChatType::Tell(uid, uid).into_msg(content.clone());
            assert!(chat_event_data(&private).get("content").is_none());
        }
    }

    #[test]
    fn private_chat_is_a_typed_single_recipient_server_command_not_public_chat() {
        let action = json!({"type":"private_chat", "recipient":"player_two",
                            "message":"private words", "action_id":"only-this-command"});
        assert_eq!(private_chat_args(action.as_object().unwrap()),
                   Ok(("player_two", "private words")));
        for invalid in [
            json!({"recipient":"two players", "message":"private words"}),
            json!({"recipient":"player_two", "message":" "}),
            json!({"recipient":"player_two\n", "message":"private words"}),
        ] {
            assert!(private_chat_args(invalid.as_object().unwrap()).is_err());
        }
    }

    #[test]
    fn chat_event_data_preserves_plain_content_and_metadata() {
        let uid = Uid(NonZeroU64::new(7).unwrap());
        let message: comp::ChatMsg = comp::ChatType::World(uid).into_plain_msg("hello");
        assert_eq!(
            chat_event_data(&message),
            json!({
                "kind": "world",
                "sender_uid": 7,
                "text": "hello",
                "content_kind": "plain",
            })
        );

        let localized: comp::ChatMsg =
            comp::ChatType::Meta.into_msg(comp::Content::localized("test-key"));
        let data = chat_event_data(&localized);
        assert_eq!(data["kind"], "meta");
        assert_eq!(data["sender_uid"], Value::Null);
        assert_eq!(data["text"], Value::Null);
        assert_eq!(data["content_kind"], "localized");
    }

    #[test]
    fn body_identity_includes_species_below_the_legacy_body_family() {
        let body: comp::Body = comp::body::quadruped_medium::Body {
            species: comp::body::quadruped_medium::Species::Wolf,
            body_type: comp::body::quadruped_medium::BodyType::Female,
        }
        .into();
        let identity = body_identity(&body);
        assert_eq!(identity["QuadrupedMedium"]["species"], "Wolf");
    }

    #[test]
    fn ready_event_includes_own_uid_or_null() {
        let ready = ready_event("test-session", 3, "agent", Some(7));
        assert_eq!(ready["uid"], 7);
        assert_eq!(ready["protocol_version"], PROTOCOL_VERSION);
        assert_eq!(
            ready["observation_schema_version"],
            OBSERVATION_SCHEMA_VERSION
        );
        assert_eq!(
            ready["capabilities"]["inspect_terrain"]["schema_version"],
            TERRAIN_QUERY_SCHEMA_VERSION
        );
        assert_eq!(
            ready["capabilities"]["inspect_terrain"]["read_only"],
            true
        );

        let ready_without_uid = ready_event("test-session", 3, "agent", None);
        assert_eq!(ready_without_uid["uid"], Value::Null);
    }

    #[test]
    fn self_identity_record_includes_uid_and_nulls_when_unavailable() {
        assert_eq!(
            self_identity_record(42, Some(7), Some("agent")),
            json!({"entity_id": 42, "uid": 7, "alias": "agent"})
        );
        assert_eq!(
            self_identity_record(42, None, None),
            json!({"entity_id": 42, "uid": null, "alias": null})
        );
    }
}
