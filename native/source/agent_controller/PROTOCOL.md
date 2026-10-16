# Veloren Agent Bridge Protocol 0.2

Bridge 使用 stdin/stdout 上的 NDJSON。每一行必须是一个完整 JSON 对象，stdout 不得输出普通日志。

## 请求

```json
{
  "id": "request-1",
  "protocol_version": "0.2",
  "cmd": "observe",
  "args": {}
}
```

`id` 标识一次传输请求。`action_id` 标识游戏动作；二者用途不同。`id`、`protocol_version`、`cmd` 和对象类型的 `args` 均为必填字段，ID 长度为 1–128 字节。

## 响应

```json
{
  "type": "response",
  "protocol_version": "0.2",
  "session_id": "agent-1234",
  "tick": 100,
  "id": "request-1",
  "ok": true,
  "result": {},
  "error": null
}
```

协议错误使用 `ok=false`。动作执行失败仍是一个成功处理的请求，使用 ActionResult 的 `status=failed` 和 `detail.code` 表示。

## Step

```json
{
  "id": "request-2",
  "protocol_version": "0.2",
  "cmd": "step",
  "args": {
    "action": {
      "action_id": "episode-1-action-1",
      "type": "move_forward",
      "speed": 1.0
    },
    "duration_ticks": 60,
    "timeout_ticks": 180
  }
}
```

Bridge 在动作达到终止条件后响应。结果状态为 `completed`、`failed` 或 `cancelled`。重复提交完全相同的动作参数会返回带 `duplicate=true` 的缓存结果，不会再次执行；复用 `action_id` 但动作参数、持续时间或超时不同会返回 `action_id_conflict`。一次只允许一个进行中的动作。

数值参数不会被静默截断：`speed` 必须在 `[0.05, 1.0]`，`duration_ticks` 和 `timeout_ticks` 必须是正整数，且超时不得短于持续时间。非法参数返回 `invalid_action`。

支持的动作：

- `move_forward`、`move_backward`、`move_left`、`move_right`
- `move_vector`，参数 `direction: [x, y]`
- `move_to`，参数 `position: [x, y, z]`、`tolerance`
- `look_at`，参数 `position: [x, y, z]`
- `turn`，参数 `degrees`，正数表示 XY 平面逆时针
- `jump`
- `stop`
- `sit`，无动作参数（仅 `action_id` 和 `type`）；幂等地坐下，不是切换坐/站
- `attack` / `attack_primary` / `attack_secondary`，可选 `target_uid`；按住主/副攻击 `duration_ticks`
- `block`、`roll`，按住对应输入 `duration_ticks`
- `toggle_wield`，可选 `wield: bool`（缺省则切换）
- `pickup`，可选 `uid`（缺省最近可拾取物）
- `collect`，参数 `position: [x, y, z]`（体素整数坐标）
- `mine`，参数 `position: [x, y, z]`；设置 `break_block_pos` 并按住主攻击
- `use_item`，参数 `slot` 或 `loadout_idx`+`slot_idx`
- `drop`，参数库存 `slot`/`loadout_idx`+`slot_idx`，或 `equip_slot`
- `equip`，将库存槽交换到 `equip_slot`（默认 `active_mainhand`）
- `swap_weapons`，切换主副手武器组

`equip_slot` / `drop.equip_slot` 可用名称：`active_mainhand`、`active_offhand`、`inactive_mainhand`、`inactive_offhand`、`lantern`、`glider`，以及护甲位 `head`、`neck`、`shoulders`、`chest`、`hands`、`ring1`、`ring2`、`back`、`belt`、`legs`、`feet`、`tabard`、`bag1`–`bag4`。

`move_to` 在到达容差范围时完成，在超时或连续两秒没有足够进展时失败。定时移动若连续两秒没有至少 0.1 米的位置进展，也会以 `detail.code=stalled` 失败。

交互类动作以库存/方块/角色状态变化为完成条件；采矿在方块被破坏或种类变化时完成，否则在 `duration_ticks` 结束时以 `mine_incomplete` 失败。无合适世界目标时，上层应把 `pickup`/`collect`/`mine` 视为环境依赖动作，而不是协议错误。

`stop` 会先将当前动作终结为 `cancelled`（`detail.code=action_cancelled`），再完成自身。Bridge 关闭、控制器 stdin 关闭或游戏服务器断线时也会取消在途动作。

### Native `give_up`

`{"cmd":"step","args":{"action":{"type":"give_up","action_id":"unique-id"}}}`
uses the ordinary protocol version and request ID envelope. No action
parameters or step timing overrides are accepted. Native admission requires
the current alive self to have consumed death protection and be in Crawl,
with a known actor/session identity. Only an explicit caller selects GiveUp.

`ready.capabilities.give_up` advertises this action. A send is not success:
the action waits for a fresh same-actor/session observation of death and
reports `give_up_observed_dead` only then. This is interval evidence, not
exclusive causal attribution. External revival reports
`give_up_no_longer_downed`; absent death at the ordinary action deadline
reports `give_up_death_not_observed` with timeout status. Identity change,
missing/removed actor (including possible hardcore removal), disconnect or
cancellation never establishes successful death/recovery. No respawn is
chained; the existing dead-only respawn action remains separate.

### Native `sit`

```json
{"id":"sit-1","protocol_version":"0.2","cmd":"step","args":{"action":{"action_id":"sit-1","type":"sit"},"duration_ticks":1,"timeout_ticks":180}}
```

`sit` releases bridge-owned held inputs (including a pending jump), mining input
and continuous movement. If the observed character is already exactly `Sit`, it
completes without toggling to stand. Otherwise it sends the ordinary native sit
toggle once and waits for observed `CharacterState::Sit`, under the normal
`timeout_ticks` deadline. Movement remains zero while pending; legacy movement
and jump commands return `avatar_busy` during that wait. Existing single-action
ownership still applies: use `stop` to cancel an in-flight action before `sit`.

Completion reports `detail.observed_sitting=true`; it does **not** report that a
checkpoint was saved. Dead/downed/unavailable state returns `status=failed` with
`detail.code=character_dead`, `character_downed`, or `state_unavailable`.
If native state does not enter `Sit` before the deadline (including native state
refusal), the action fails with `action_timeout`, never assumed success.
`detail.character_state` records the observed state, or null if unavailable.
`ready.capabilities.sit` advertises this idempotent zero-argument action.

## 观测

`observe` 当前返回 `observation_schema_version=1.4`，并遵循以下规则：

- 世界 XYZ 米制位置、速度、真实视线方向、目标视线方向、tick 和 session
- 当前/最大生命、能量及比例
- inventory（含 `mainhand_tool`、槽位 `slot`/`loadout_idx`/`slot_idx`）、稳定 item definition ID、数量和当前主手
- character state、`wielding`、`held_input`、`break_block_pos` 与 on-ground 状态
- 脚下方块、视线方块和局部方块（含 `collectible` / `mine_tool` / `sprite`）
- 32 米内至多 32 个实体（含 `uid`、`pickup`、`pickable`）
- 当前动作状态

`coordinate_frame` 描述坐标约定：移动使用世界 XY，Z 轴向上，`look` 为真实单位方向向量，`desired_look` 为 Bridge 当前控制目标。`identifier_scopes` 说明实体 ID 仅在当前 session 内稳定，而物品定义 ID 和方块类型属于游戏定义级标识。`uid` 为会话内实体稳定标识，适合作为 `pickup`/`attack` 目标。

状态暂不可用时，单值字段使用 `null`；集合字段使用空数组。特别地，物理状态不可用时 `on_ground` 和 `moving` 为 `null`，不能解释成 `false`。`aimed_block` / `block_under` 在地形未加载或视线未命中时也可能为 `null`。

方块使用 `BlockKind` 的稳定名称。物品 ID 使用 Veloren 的结构化 `ItemDefinitionIdOwned`，区分 Simple、Modular 和 Compound。

## 事件

动作会发出 `action_started`、`action_completed`、`action_failed` 或 `action_cancelled` 事件。每个事件都有 session 内递增的 `seq`。

`waypoint_saved` forwards a native server `WaypointSaved` notification through
the normal event envelope (`protocol_version`, `session_id`, `tick`, `seq`).
Its data is exactly `{"location_name":"<native notification name>"}`; it does not
contain inferred coordinates or bind the save to an action. The client preserves
each notification's own name, including when several arrive in one tick.
`ready.capabilities.waypoint_saved` advertises support.

This event is positive save evidence only. Absence is **not** failure: native
notifications may be omitted for unnamed locations and are debounced. Sitting,
entering a Town, generic interaction or proximity alone is not save evidence.
Nearby `CampfireLit` bodies remain candidates, not definitive `WaypointArea`
discovery. No save-radius, physics, game-rule or persistence change is made.

`events` 不带参数时保持旧行为：返回并清空当前事件队列。可视化和可靠消费者应使用非破坏式游标读取：

```json
{"id":"events-1","protocol_version":"0.2","cmd":"events","args":{"after_seq":120,"limit":100}}
```

响应包含 `events`、`oldest_seq`、`next_seq`、`truncated` 和 `has_more`。当请求游标早于 Bridge 当前保留的最旧事件时，`truncated=true`，消费者应记录数据缺口并从返回的最旧事件继续；`has_more=true` 时应以本批最后一个事件的 `seq` 继续读取。Bridge 最多保留 256 个事件。

## Read-only normal player map (`inspect_map`, schema 1.0)

`ready.capabilities.inspect_map` advertises this on-demand query. It uses only
the already connected `Client::world_data().lod_base`, `markers()` and `pois()`;
no additional connection, terrain/resource scan, movement or route planning.

`args` may contain `bounds: [min_x,min_y,max_x,max_y]` in native horizontal
block coordinates, `cell_stride` (integer 1..65535 native cells per symbol axis),
and `marker_offset` (non-negative pagination offset). Empty args gives world
extent and an automatic overview. Python `InspectMapSkill` converts horizontal
coordinates to Agent `[x,z]` with the shared vector converters.

Responses include world/requested/actual coverage bounds, clipping, cell and
symbol scale, at most 48x48 character `rows`, a legend, and 16 labeled markers
per page with total/next offset. Bounds are min-inclusive/max-exclusive and
snapped outward to map cells; last row/column can cover fewer cells. Raster
columns increase native x, rows increase native y (unflipped row-major).
Map completeness and unknown cells are explicit; alpha is ignored, as in the
native renderer. Exact RGB road 55/41/35, bridge 128/128/128, water predicate
`r == 0 && b > 102 && g < 77`, other colors are visual land.

Symbols encode presence unions, using alphabet `0123456789abcdefghijklmnopqrstuv`
and bit values land=1, water=2, road=4, bridge=8, unknown=16. Every underlying
pixel contributes, so aggregation never votes out a narrow water barrier.
Mixed tiles do not specify feature order, connections or precise positions;
crop and use `cell_stride=1` for the original map pixels. Neither those pixels
nor marker locations certify collision, depth, slope or physical passability.
Road/bridge colors overlay terrain: underlying water is unknown. Towns sort
first, then deterministic marker pages within coverage; labels are bounded with
explicit truncation/localization flags and never imply known altitude.

Invalid arguments, fully off-map views, and explicit-resolution views exceeding
48x48 return `invalid_request`, `map_out_of_bounds`, or `map_view_too_large`.
Partially off-map views clip visibly. There is no request/session/action quota,
automatic itinerary, or map attachment to ordinary observations.

## 兼容命令

### Continuous control capability (schema 1.0)

Ready advertises `capabilities.continuous_control`. This is a separate native
motor path, not concurrent `step` requests. `set_control_state` atomically
replaces the full group:

```json
{"owner":"jev","generation":1,"sequence":1,"expected_session_id":"observed-session","actor":{"uid":7,"lifecycle":"observed-opaque-value"},"movement":[0.6,0.8],"target":{"uid":8,"lifecycle":"observed-opaque-value"},"primary":true,"block":false}
```

Use `observe.control_actor` and `nearby_entities[].control_target` unchanged.
Movement is a world-XY vector of magnitude at most one (Python world-XZ).
All original fields are required: `[0,0]`, `null`, `false`, `false` explicitly neutralize
movement, tracking, primary and block. Optional `facing` is `auto`, `travel`, or
`track`, advertised in `facing_modes`. Auto faces travel without a target and
tracks with one. Travel follows the selected local movement; track requires an
observed target and permits independent strafe/backward movement. Neutral
movement without a target preserves the last look.
Held primary/block follow normal native equipment, energy, collision and
animation rules; no auto-equip, target choice, unrequested pursuit, switching or hit guarantee.

Optional `movement_intent` is advertised through `movement_intents`:
`["approach","retreat","left","right","destination"]`. Omitted/null intent
preserves world-vector behavior and requires omitted/null `destination`.
Semantic movement requires `movement:[0,0]` to avoid ambiguous mixed commands.
Approach/retreat/left/right require the selected observed target and no
destination. Each tick computes a unit horizontal bearing toward/away from that
target, or its left/right perpendicular relative to facing it. Coincident
horizontal positions give zero movement. All existing target lifecycle,
nearby-range and LOS checks still apply; ordinary target/actor motion changes
the bearing, not identity validity.

`movement_intent:"destination"` requires `destination:[x,y,z]` in native
world coordinates (Python accepts GameAgent XYZ via its existing coordinate
adapter). The advertised `local_route` contract uses loaded terrain to
search deterministic cardinal, support-height-aware local segments using the
shared `common::astar::Astar`. Every node has a loaded solid support surface
and checked standing clearance. An uphill step within the engine's 1 m
automatic block-hop envelope or a supported descent within the ground-snap
envelope is a walk candidate, not an instruction to jump or proof that physics
completed the elevation change. The follower measures actual waypoint height
before advancing. Larger steps require an explicitly model-selected action
and are not silently planned as ordinary walking. It follows the selected goal
across ticks under this same owner; a long destination is approached in local
legs toward a reachable directional frontier about 12 m away rather than
requiring one projected voxel to be clear.
For a finite destination, its loaded support column must offer a reachable
height and a collision-checked connector to the exact requested coordinate;
grid-center proximity alone does not establish a route. Measured arrival
rechecks that connector against current terrain, including near voxel corners.
The search examines at most a 20-voxel local radius and 2048 nodes per leg.
These are local computation/coverage bounds, not game-session allowances.
Unloaded terrain is unknown, not air; unsupported elevation, missing support,
hazards, no path and exhausted local coverage are distinct failure reasons.
The 1 m vertical tolerance applies at the actual destination, never to the
whole route from the starting elevation. Candidate walk envelopes cannot
certify dynamic collision, successful hop, floating platforms or complex
jump/climb traversal.
No route is installed on a rejected update. Altitude does not command
jumping/flying.
An optional selected target can still independently own aim/attack, and remains
subject to the same validity checks; look/aim never rotates world-space route
travel. Same owner/actor/goal renewal preserves route progress, while new
ownership/goal, protection and release fence it. No autojump, unknown-world
exploration, strategic destination selection or automatic target switching
is performed. Observation control metadata includes the selected intent and point.
Each full replacement clears an omitted semantic intent; it is not a patch.

The local motor checks loaded terrain for the unmounted actor's effective
physics-capped collision cylinder, solid block/sprite collision direction and
next-step support. Point/voxel colliders are explicitly unsupported by this
walk contract.
The unmounted actor uses the effective terrain collision cylinder: bounding
radius capped at 0.45 m before scaling, scale capped at 10, standing base at
zero and top clamped to 1.2-2.95 m before scaling. This avoids rejecting a
locally clear step using the larger full capsule-prism enclosing radius.
Steered ground mounts retain their conservative full carrier enclosure,
including any collider extent below its origin.
Segments use the physics-capped body AABB/voxel overlap rather than distant
side columns. Next-step support is checked beneath every actually overlapping
footprint column, not just the center voxel. A route can start on verified
overlapping support when its center voxel is Air; its initial footprint still
has to pass loaded obstruction, hazard and unknown-terrain checks. The
observation's `self_column.support` describes only the center column and is
not a claim that the actor has no other footprint contact.
Unknown terrain is not air. Obstruction, hazardous/unsupported footing,
nonprogress and material lateral deviation interrupt movement without waiting
for a remote model. A selected side/backward detour is measured against its own
segment, never against monotonically decreasing mission-goal distance.

The `character_action` step accepts typed `kind` (`lantern`, `sneak`, `crawl`,
`dance`, `glide`, `greet`, `cancel_climb`) and explicit `enabled` only for
toggleable modes. An equipped lantern/glider and observed climb state are
checked when applicable. `character_controls` reports equipped items and a
light emitter matching the equipped lantern parameters; a generic emitter is
not exclusive lantern-on proof. An immediate step completion means submitted
input, not observed game-effect completion.

`private_chat` submits only the server's ordinary `tell` command to one
validated player alias; it does not prefix public chat with an address.
Command submission is not recipient delivery. A `drop` step with
`split:true` uses the server's half-stack (rounded-down) inventory action;
the server has no arbitrary-count world-drop event.

`map_marker` updates or removes only the ordinary player's personal marker
using signed native world-XY map coordinates. `observe.personal_map_marker`
reports the synced own marker when present. A submitted update is not proof
of server persistence, shared-group propagation, collision safety or travel.
Continuous control also advertises `secondary` in `held_inputs`. An explicitly
selected motor owner may press it with movement/aim and later replace/release
the intent. The held-input observation is not ability readiness, hit or effect
confirmation.
Continuous `vertical_input: swim_up|swim_down` writes the ordinary controller
`move_z` while the actor's current sampled physics fluid is water. Missing
physics state, air and lava refuse installation; leaving water retires the
owner on the next tick. `character_controls.water_contact` is tri-state and
does not certify safe water elsewhere. Stop/replacement clears vertical input.
Horizontal swimming uses sampled loaded actor-volume water, hazard and
collision checks; it does not treat water as a walk route or skip unknown
cells. A walk-only destination route and swimming cannot be combined.
`fly` is an owned continuous held input and is admitted only for a sampled
flying-capable actor or a steered capable mount. Loaded flight movement checks
the actor volume for obstacles, unknown cells and hazards, not ground support;
it is not a walk route or flight-completion receipt. The capability is
available even when the current ordinary body is ineligible.
`vertical_input=fly_up|fly_down` requires `fly=true`, while swim input
requires sampled water and rejects simultaneous flight. Loaded vertical
swim/flight checks the full actor volume at the next 0.05 m step; unknown,
hazardous or obstructed cells are refused. `speed` scales the owned motor
within [0,1]; `auxiliary` holds an actually configured ability slot by
index, refusing empty or disappeared slots and releasing on replacement,
Stop or loss of ownership. A configured slot and a held press do not prove
ability effect. `wall_jump` is separate from `jump`, requires sampled wall
contact and climb/wallrun state and releases the pressed input after a tick.
The separate `jump=true` continuous held input may be installed alongside
horizontal motion under one owner. An airborne same-owner update can change
direction while Jump stays pressed; releasing Jump preserves horizontal
steering until a separate Stop or replacement. Release/replacement/Stop sends
Jump=false. The controlled body's sampled physics ground contact, not whether
Jump is held, chooses supported walking versus loaded airborne clearance.
Neither input admission nor a held Jump grants an extra jump or proves
takeoff/landing. The ordinary `jump` action remains a standalone pulse.
`observe.local_geometry.movement_context` identifies sampled `walk`,
`airborne_glide`, `steered_mount`, `steered_vehicle`, volume `passenger` or
direct-mount `entity_passenger` (with real carrier UID/entity or seat identity
where relevant); missing body/shape data carries an
unavailable reason. A currently active airborne
`Glide` state and glider equipment permit loaded horizontal air checks without
granting humanoid flight or reclassifying `GlideWield` as active gliding.
Steered mount checks use the carrier pose and full scaled capsule extents,
including negative lower bounds. A controller seat on an entity voxel carrier
uses the native volume and orientation to enclose the scaled hull, with
loaded terrain, hazard and collision checks rather than rider-floor support;
physics alone decides propulsion and altitude. `movement_context.shape` and,
for large voxel volumes, summary directional checks expose what was sampled
without claiming exact hull occupancy. A different carrier UID/entity/seat,
dead carrier, or unavailable shape retires/refuses steering. A volume passenger
seat or valid free-will direct entity mount may still hold rider
combat/ability/look inputs but cannot steer its carrier. Missing mount body or
invalid/dead link is not interpreted as passenger eligibility.
Native destination routes remain unmounted walk-only.
Advertised `explicit_look_direction=true` admits a finite nonzero native
Z-up `look_direction=[x,y,z]` alongside owned movement when target is null
and facing mode is auto. This holds actual glider pitch; no invented glide
thrust or guaranteed ascent is reported. The caller converts Y-up to Z-up
once. The input is rejected if it conflicts with target tracking.
`build_block` uses the normal client's `PlaceBlock`/`BreakBlock` requests;
CanBuild enabled, assigned areas and loaded exact target kind are checked
before sending. The server decides exact build-area membership; the immediate
request receipt does not assert that a block changed. This does not grant
CanBuild, relax server permissions or expose privileged commands.
The `character_controls`, `nearby_mountable_volumes`, `social_and_trade`,
`skill_tree`, `auxiliary_abilities`, `component_recipes`, `dialogue` and
inventory `empty_slots` observations describe current actor capabilities,
eligible targets and server events, not permission to bypass preconditions.
Selected `companion_action` and `context_interact` requests recheck exact
UID/lifecycle or loaded volume/voxel. `social_action` validates invites and
membership; `trade_action` fences trade ID, counterparty and phase and
changes only the actor's offer. `inventory_action`, `craft_component_action`,
`craft_primary_component`, `configure_auxiliary_ability` and `unlock_skill`
recheck observed slots, recipes, stations and skillset before ordinary
server submissions; requests do not certify persistent game effects.
`dialogue_action` selects only an offered choice from a recorded server
question or acknowledges its current statement. A submitted response
retires the pending choice without assuming NPC acceptance. `read_sign`
samples current loaded terrain sprite configuration, without writing the
world. `chat_channel` sends one whitelisted ordinary server channel command
(group requires membership); it may update subsequent default chat mode.
Private tell is distinct and neither path confirms recipient delivery.
Horizontal proximity within 0.5 m and vertical proximity within 1 m produces
`control_motion_arrived`; horizontal proximity at inaccessible elevation is
`control_route_vertical_unsupported`, not arrival.

`continuous_control.motion_feedback` contains status, segment origin/direction,
sample tick and scope. On local interruption `last_receipt.status` is
`motion_interrupted` with a `control_motion_*` or `control_route_*` reason.
The same native route tick advances the measured segment and sets
`motion_feedback.status` on interruption; a receipt reason alone is not the
Python executive's arrival or blockage signal. Native offline transition
fixtures supply measured poses and loaded terrain to this production tick
logic; they are not a physics simulation or live arrival evidence.
`continuous_control.local_route` contains goal, owner-scoped route ID and
revision, next segment, last physically verified progress, blocked edge/cause,
local coverage, earliest retained entry and a compact recent breadcrumb preview
(up to 64 retained internally, eight recent in observations; truncation explicit).
Breadcrumbs require fresh reverse validation; drops and dynamic changes are
not declared reversible.
Rejected route proposals expose `local_route_failure` on the next observation,
including blocked edge/coverage where available. Movement is neutralized;
valid owned tracking and held defense/primary remain as explicitly requested.
The caller chooses a new waypoint, turn, jump, strafe or recovery tool. These
checks do not guarantee dynamic collision safety or a reachable room exit.
Native animations/momentum may continue after input release. Invalid proposed
updates fail before mutating or releasing the prior valid owner.

Owner generations increase globally per bridge session, starting at 1.
A higher generation preempts; within a generation the owner must match and
sequence must strictly increase. `release_control_state` takes only the six
identity/ownership fields, neutralizes controls and retires its generation.
It may fence a newer generation whose first update is still in flight.
Resuming after release/failure requires a higher generation. Stale updates
and releases cannot alter the newer owner. Actor lifecycle and session must
match. The opaque lifecycle includes bridge incarnation and local entity
generation, not position; reusing a configured session ID after restart does
not revive old references.

Every tick recalculates aim at the selected observed body's center from the
actor eye position. The target must remain in the ordinary nearby projection
(32 m, nearest 32 records); a loaded-terrain solid-voxel ray must be clear.
This conservative occlusion test is not visual rendering or projectile
collision prediction. Missing, dead, replaced, occluded targets or invalid
actors release the whole group and emit `continuous_control_failed`.
Ordinary target movement does not stale its identity.

`observe.continuous_control` reports active/retired ownership and last receipt.
Set receipts use `status=applied`, releases `status=released`; both carry
owner/generation/sequence, actor, session, accepted/applied tick and
`gameplay_success=null`. Applied means desired inputs installed by the native
request handler, not a completed game tick, hit, arrival or animation.
Failure events/observations carry explicit reason codes. Read observations
remain available while controls persist. No watchdog expires controls merely
because model inference is pending.

Continuous controls reject a running legacy step with `avatar_busy`, and active
continuous ownership rejects conflicting legacy steps/move_forward/jump.
Explicit legacy/step Stop, disconnect and close release continuous ownership.
Stop/release do not zero physical velocity or cancel native animations.

### Automatic combat and local geometry observations (extension 1.0)

`observe.combat_perception` and `observe.local_geometry` are fixed versioned
extensions. Each has `binding:{session_id,tick,actor,frame}`, where `actor`
equals `control_actor` and frame is `veloren_world_xyz_meters` (native Z-up).
Consumers must convert positions, directions and voxel axes exactly once.
Missing components/cells are null/unknown, never fabricated free space.

Combat `self` contains geometry, orientation, current state/phase/timer,
`last_health_change`, and resolved `primary`/`guard`. Health provenance is
`synced_health.last_change`: amount, cause, attack source, by UID/kind, instance,
client-local sync time and observation tick. The native client rewrites that
time on sync; it is not server event time. Only the last change survives.
Instances group attack changes, not individual hit events. The protection
tracker seeds the first sample, session/actor/lifecycle changes, and observations
after death/downed-state reset without reacting. Only an advancing same-session,
same-actor sample with net health decrease and an unseen negative synced
last-change record can trigger protection. The duplicate key includes the
reported amount, local receipt time, cause, contributor, precision and instance;
instance alone is never a deduplication key. Distinct synced changes may share
an attack instance. Receipt time alone does not establish a new hit. This
conservative rule cannot reconstruct coalesced hits or a damage history.
Only negative `Attack` damage with a non-self contributor names an attacker.
Buff/falling/ambiguous damage never substitutes the nearest entity.
`attribution_scope` explicitly limits that contributor to the reported last
change, not the entire net HP decrease. `instance_semantics` and
`freshness_basis` retain these qualifications in the observation.
The wire discriminator is named `client_local_sync_time`; `precise`, `by_kind`
and `by_uid` are also present. This is a normalized projection, not the complete
Rust HealthChange record: contributor group IDs and Buff subtypes are not
included.

Primary/Guard use own ActiveAbilities, equipped item configuration, skills,
stance/combo and current state. Supported primary kinds are BasicMelee and
ComboMelee2, with all resolved per-strike range, half-angle and timings.
Unresolved contextual adjustments and unsupported abilities remain explicitly
unknown. Durations are ability timer seconds; `attack_speed_modifier` describes
the dynamic clock, not a promised wall-clock deadline. The starter sword
resolves two strikes, range 3 and half-angle 45 degrees; its item speed 0.75
scales the bare ability timings. Guard reports energy, filters, strength,
half-angle and actual BasicBlock state separately from a pressed input.
`mitigating_now` remains null: state entry does not prove a blocked hit.
Supported active melee state also exposes its actual strike, `exhausted` flag
and completed-strike count, not just the currently resolved equipment ability.

`nearby_combat` is joined to the existing bounded nearby projection solely by
exact `target:{uid,lifecycle}`. It adds geometry, orientation, alignment,
variant/phase/timer and optional input target hint. Alignment is not present
aggression; input target hints are non-authoritative. No server Agent target
is accessed. Phase semantics are variant-specific: BasicRanged emits on
Recover, so Recover is not universally harmless. The narrow pre-hit assessment
below never authorizes an automatic retaliatory/proactive attack.

`combat_perception.pre_hit` has its own current native binding and assessment
rows keyed by `source_entity:{uid,lifecycle}` from the same nearby projection.
Only `status:"eligible_observed_potential_threat"` is an eligible pre-hit cue;
missing state, health, body/orientation, target hint or attack clock, other-target
hints, nonpositive damage, unblockable/unsupported constructors, failed geometry
and blocked/unloaded LOS have explicit non-eligible statuses. Unsupported types
(including BasicRanged in Recover) are unknown, not safe.

Supported cues require a live BasicMelee or ComboMelee2 source, its actual
unexhausted active strike in Buildup/Action, a positive blockable melee damage
constructor, and `input_attr.target_entity` naming the current actor UID.
That hint remains non-authoritative. The source's actual facing, body scales,
range/half-angle, this fork's vertical envelope, and loaded source-eye to
actor-center terrain ray must qualify. Action with emission due but
`exhausted:false` remains a potential cue; Recover/exhausted is outside this
specific pre-emission window, not a universal harmless-state rule.
`confirmed_attacker:false` and `attacker_uid:null` prevent treating the cue as
damage attribution. No alignment/proximity test or server Agent target is used.

Local geometry contains `geometry:{origin,facing_xy,radius_m,height_m,
directions,cells,self_column,coverage,limitations}`. Directions are forward,
left, right and back relative to actual facing. Each lists its exact sampled
XY columns and inclusive native Z interval, including the actual next-step
footprint, using the same next-step checks as local_motion. Each direction
lists the deduplicated union of lookahead columns and all swept-step
footprint columns, not just the lookahead. `self_column`
reports sampled body clearance, overhead and solid/liquid support. Every
unique cell in the listed domain appears once, with `known_free`, `solid`,
`liquid` or `unknown` occupancy; coverage counts include unknown cells.
These bounded samples are not a complete route, escape, safe walking
over liquid, or guaranteed jump. No whole-map scan is added.

### Tick-local protection and explicit selected melee

Protection runs immediately after synchronized native ticks and before input
assembly, independently of LLM/Jev delivery. Fresh damage or an eligible new
pre-hit cue cancels any running
step with a receipt, releases Primary and other queued inputs, neutralizes
movement and retires the same continuous-control generation. Its owner is
`native_protection`; observation/events expose the retired generation, cancelled
action, evidence, guard request, actual BasicBlock state and settlement.
Environmental damage only neutralizes unsafe controls. For compatible attack
or unknown damage, a supported guard may be requested with honest directional
limits. A visible exact attributed UID/lifecycle or the exact evidenced
potential-threat source supplies guard facing. After-hit source loss/occlusion
falls back to current-facing, target-free protection; pre-hit source/evidence
loss retires the pre-hit protection instead.

When Idle, protection may request wield using equipped item timing. A
non-interruptible BasicMelee/ComboMelee2 may finish its current strike before
guard entry is attempted; the wait is bounded by that native animation's
remaining duration and current attack-speed clock, not an inference quota.
Unsupported state/energy/attack filters settle explicitly. After-hit guard is a
single locally applied input pulse; the next native tick reports actual state
and releases Block. Pre-hit guard can remain held only while that same
evidenced unexhausted strike remains eligible. Its deadline is anchored at
first eligibility to the remaining native time-to-emission, synchronized attack
clock, and three native phase-transition ticks; repeated samples do not renew
it. Loss, expiry or failure to observe actual BasicBlock settles/releases it.
The requested versus actual-state receipts remain distinct. Momentum/animation
may continue. Fresh model decisions can
replace the protective owner, and protection never locks the avatar in guard.

There is one protective intervention per locally observed strike episode.
Fresh owner takeover/Stop does not rearm that same episode. A confirmed local
state transition to a new strike may rearm; timer rewinds and visibility gaps
alone do not. These are local episode keys, not server attack IDs; missed
transitions can conservatively miss attacks. No inference timeout, retry
budget, proactive target selection or whole-fight policy is added. The
protection-generation handshake below is identical for after-hit and pre-hit
interventions.

After the first protection, `step`, legacy `stop`, `move_forward` and `jump`
must include `args.control_fence:{generation,expected_session_id,actor}` from
the current observation. Missing/stale session, actor or generation is rejected
as `stale_control_owner` before mutation. Fresh continuous updates/releases
still require a generation greater than the retired generation and existing
actor/session fences. Work Stop/Pause is not operator shutdown: future fresh
danger may protect again. `close`, input disconnect and server disconnect
disable protection and release the actuator. Older hosts must implement this
handshake before using this bridge; no silent legacy bypass is provided.
Continuous update/release requests also carry
`expected_protection_generation` equal to the observed
`continuous_control.protection_generation` (initially zero). After protection
this field is mandatory even for a higher owner generation: an old in-flight
proposal may have reserved a higher generation before the interruption.
The protection generation remains unchanged by ordinary host updates.

`set_control_state` optionally accepts `melee:"primary"` with `primary:true`,
`block:false`, an exact selected target and auto/track facing. This explicitly
gates primary on the actual resolved ability, current actor orientation,
scaled radii/range, this fork's vertical wedge and the existing loaded-terrain
LOS/target-lifecycle checks. It continues tracking and coordinating that input
under the same owner; it never selects a target, auto-chases or chooses tactics.
Omitting `melee` preserves ordinary raw primary semantics.
`continuous_control.melee_feedback` distinguishes out-of-range, outside-angle,
vertical mismatch, unsupported/unknown ability, action/recovery and eligible
geometry. Ongoing selected melee can retain the input to allow its native combo;
recovery is not reported as immediate readiness. A geometry envelope and
center-ray visibility are not the server's multi-ray wall test or guaranteed
damage, legality, hit, or task success.

### Offline production-serializer fixture

The native test
`agent_protocol::combat_perception::tests::export_native_serializer_fixture`
can write compact JSON when `VELOREN_NATIVE_SERIALIZER_FIXTURE` names an output
file. Run with the normal offline `veloren-client`/`bin_bot` test recipe. It
uses the same production serializers as observe, real native component
fixtures, actual starter-sword resolution and pure owner/geometry helpers;
it creates no connected Client. The bundle contains the requested capability
values, combat/local geometry/control sections, melee feedback, pre-hit
assessment and two same-instance health records with different local sync times.
Pre-hit LOS is supplied by an offline mock. The control fixture represents
the protection-start substage before pressing guard, not a gameplay trace.

### Observation-bound operation commits

`capabilities.action_preconditions.schema_version="1.0"` advertises optional
`step.args.preconditions`. The normal broker-enabled Python adapter supplies
`session_id` and the observed `actor`; inventory operations additionally pin
`slot: {reference: {slot: ...}, item: ...}` (or the native split-slot/equipment
reference). Craft pins the observed inventory; UID interactions pin the
observed target lifecycle. The native handler compares these immediately before
a new dispatch, without releasing another control owner on rejection.
Slot comparison includes the public item definition and amount, not a claimed
hidden persistent item-instance UUID. Previously completed/running action IDs
retain existing bounded-cache idempotency semantics. Legacy callers without
preconditions retain their wire interface. Stop is ownership cleanup; GiveUp
retains its parameterless action and native downed-state boundary while accepting
the same optional actor commit metadata.

Changed session/actor/slot/inventory/target returns an
`action_{session,actor,slot,inventory,target}_changed` error. These exact
precondition rejections establish no dispatch for that request; transport
timeouts and missing cache entries do not establish no effect.

旧版 `move_forward`、`stop` 和 `jump` 命令继续保留，但响应标记 `legacy=true`。新代码应使用 `step`。
