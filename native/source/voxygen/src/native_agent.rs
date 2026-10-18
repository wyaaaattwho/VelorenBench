//! Ordinary-player protocol host. It owns no Client and never connects to a server.
use client::{Client, agent_protocol::{self as protocol, BridgeState, Request}};
use common::comp;
use crate::session::interactable::Interactables;
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, OpenOptions},
    io::{BufRead, Cursor, Write},
    path::{Path, PathBuf},
    sync::{Arc, atomic::{AtomicBool, Ordering}, mpsc},
    time::{SystemTime, UNIX_EPOCH},
};

pub const FRAME_SCHEMA: &str = "1.0";
mod visual_ui;

enum Input {
    Line(String),
    Closed,
    Failed(String),
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FrameArgs {
    schema_version: String,
    expected_session_id: String,
    actor: Value,
}

pub struct NativeAgent {
    pub state: BridgeState,
    receiver: mpsc::Receiver<Input>,
    frame_dir: PathBuf,
    pending_frame: Option<Value>,
    in_flight: Arc<AtomicBool>,
    frame_sequence: u64,
    dispatch_sequence: u64,
    last_dispatched_request_id: Value,
    last_actuator_dispatch: Value,
    image_only: bool,
    visual: visual_ui::VisualUi,
}

fn host_ns() -> Result<String, String> {
    SystemTime::now().duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos().to_string()).map_err(|error| error.to_string())
}

fn selected(value: &Value, fields: &[&str]) -> Value {
    let mut result = serde_json::Map::new();
    for key in fields {
        if let Some(value) = value.get(*key) {
            result.insert((*key).to_owned(), value.clone());
        }
    }
    Value::Object(result)
}

/// Applies to every native emission, including late receipts, events and errors.
pub fn project_image_output(value: &Value) -> Value {
    let mut out = selected(value, &[
        "type", "protocol_version", "session_id", "tick", "id", "ok", "event", "seq",
        "uid", "character", "observation_schema_version",
    ]);
    out["visibility_profile"] = json!("image-receipts-v1");
    if let Some(error) = value.get("error") {
        out["error"] = if error.is_null() { Value::Null } else { selected(error, &["code"]) };
    }
    for field in ["result", "data"] {
        let Some(payload) = value.get(field) else { continue };
        out[field] = if payload.is_null() {
            Value::Null
        } else if payload.get("frame_schema_version") == Some(&json!(FRAME_SCHEMA)) {
            // Only the fixed renderer readiness/frame builders produce this payload.
            payload.clone()
        } else {
            selected(payload, &[
                "action_id", "action_type", "status", "started_tick", "finished_tick",
                "duplicate", "accepted", "closed", "session_id", "protocol_version",
                "owner", "generation", "sequence", "retired", "released",
                "actor", "accepted_tick", "applied_tick", "gameplay_success",
            ])
        };
    }
    out
}

fn image_command_allowed(request: &Request) -> bool {
    match request.cmd.as_str() {
        "frame" | "ui" | "ping" | "close" | "release_control_state" => true,
        "step" => matches!(request.args["action"]["type"].as_str(),
            Some("move_forward" | "move_backward" | "move_left" | "move_right" |
                 "move_vector" | "jump" | "sit" | "stop" | "toggle_wield" | "visual_input")),
        "set_control_state" => {
            request.args.get("target").is_none_or(Value::is_null)
                && request.args.get("destination").is_none_or(Value::is_null)
                && request.args.get("movement_intent").is_none_or(Value::is_null)
        },
        _ => false,
    }
}

pub fn frame_fence(args: &Value, session: &str, actor: &Value) -> Result<(), &'static str> {
    let args: FrameArgs = serde_json::from_value(args.clone()).map_err(|_| "invalid_frame_args")?;
    if args.schema_version != FRAME_SCHEMA { return Err("frame_schema_mismatch"); }
    if args.expected_session_id != session { return Err("stale_session"); }
    if actor.is_null() || args.actor != *actor { return Err("stale_actor"); }
    Ok(())
}

fn renderer_ready(session: &str, identity: &Value, observation: &str) -> Value {
    json!({
        "type": "event", "event": "native_renderer_ready",
        "protocol_version": protocol::PROTOCOL_VERSION, "session_id": session,
        "data": {
            "frame_schema_version": FRAME_SCHEMA,
            "actor": identity["actor"], "actuator": identity,
            "client_type": "game", "same_client": true,
            "observation_profile": observation,
            "camera_profile": "ordinary-player", "hud_profile": "normal-voxygen",
            "native_tick": null, "tick_semantics": "client-update-counter-not-server-tick",
            "ui_panels": ["world", "inventory", "equipment", "map", "crafting", "skills", "trade"],
            "visual_target_addressing": "native-visual-ui/1",
            "visual_ui": visual_ui::capabilities(),
            "dialogue_trade_addressing": "ordinary native HUD pixels; task qualification separate",
        },
    })
}

impl NativeAgent {
    pub fn attach(client: &Client, args: &crate::cli::Args) -> Result<Self, String> {
        if !matches!(client.client_type(), client::ClientType::Game)
            || !client.presence().is_some_and(|presence| presence.controlling_char())
        {
            return Err("native renderer bridge requires an ordinary controlling player".into());
        }
        let dir = args.agent_frame_dir.as_ref().ok_or("--agent-frame-dir required")?;
        fs::create_dir_all(dir).map_err(|error| format!("frame directory: {error}"))?;
        let frame_dir = fs::canonicalize(dir).map_err(|error| error.to_string())?;
        let session_id = std::env::var("VELOREN_SESSION_ID")
            .unwrap_or(format!("voxygen-{}-{}", std::process::id(), host_ns()?));
        if session_id.is_empty() { return Err("empty VELOREN_SESSION_ID".into()); }
        let look = client.current::<comp::Ori>().map(|ori| ori.look_dir()).unwrap_or_default();
        let state = BridgeState::new(session_id, look);
        let (sender, receiver) = mpsc::sync_channel(256);
        std::thread::Builder::new().name("native-agent-input".into()).spawn(move || {
            for line in std::io::stdin().lock().lines() {
                let message = match line {
                    Ok(line) => Input::Line(line),
                    Err(error) => Input::Failed(error.to_string()),
                };
                let failed = matches!(message, Input::Failed(_));
                if sender.send(message).is_err() || failed { return; }
            }
            let _ = sender.send(Input::Closed);
        }).map_err(|error| error.to_string())?;
        let identity = protocol::actuator_identity(client, &state);
        let alias = client.current::<comp::Stats>().and_then(|stats| match stats.name {
            comp::Content::Plain(name) => Some(name), _ => None,
        });
        protocol::emit_ready_with_uid(&state.session_id, state.tick, alias.as_deref().unwrap_or("unknown"),
                                      client.uid().map(|uid| uid.0.get()));
        protocol::emit(&renderer_ready(&state.session_id, &identity, &args.agent_observation));
        Ok(Self {
            state, receiver, frame_dir, pending_frame: None,
            in_flight: Arc::new(AtomicBool::new(false)), frame_sequence: 0,
            dispatch_sequence: 0, last_dispatched_request_id: Value::Null,
            last_actuator_dispatch: Value::Null,
            image_only: args.agent_observation == "image",
            visual: visual_ui::VisualUi::default(),
        })
    }

    fn error(&self, id: Value, code: &str, message: impl Into<String>) {
        protocol::emit(&json!({
            "type": "response", "protocol_version": protocol::PROTOCOL_VERSION,
            "session_id": self.state.session_id, "id": id, "ok": false,
            "result": null, "error": {"code": code, "message": message.into()},
        }));
    }

    /// Returns false only on explicit close, input failure or EOF.
    pub fn poll(&mut self, client: &mut Client, hud: &mut crate::hud::Hud,
                talk: &Interactables) -> bool {
        while let Ok(input) = self.receiver.try_recv() {
            let line = match input {
                Input::Line(line) => line,
                Input::Failed(error) => {
                    self.error(Value::Null, "controller_input_failed", error);
                    protocol::shutdown(client, &mut self.state, "controller input failed");
                    return false;
                },
                Input::Closed => {
                    self.error(Value::Null, "controller_disconnected", "stdin closed");
                    protocol::shutdown(client, &mut self.state, "controller input closed");
                    return false;
                },
            };
            let request = match protocol::parse_request_line(&line) {
                Ok(request) => request,
                Err(error) => { self.error(Value::Null, "invalid_json", error); continue; },
            };
            if !request.id.as_str().is_some_and(|id| !id.is_empty() && id.len() <= 128)
                || request.protocol_version.as_deref() != Some(protocol::PROTOCOL_VERSION)
                || !request.args.is_object()
            {
                // Preserve the original request envelope validators verbatim.
                protocol::handle_request(client, &mut self.state, request);
                continue;
            }
            if self.image_only && !image_command_allowed(&request) {
                self.error(request.id, "unsupported_in_image_profile",
                    "this action needs visual addressing that is not implemented");
                continue;
            }
            if request.cmd == "step" && matches!(
                request.args["action"]["type"].as_str(), Some("server_command" | "chat"))
            {
                self.error(request.id, "unsupported_in_ordinary_profile",
                    "server commands and unfiltered chat are not exposed by this renderer seam");
                continue;
            }
            self.dispatch_sequence += 1;
            self.last_dispatched_request_id = request.id.clone();
            if matches!(request.cmd.as_str(), "step" | "set_control_state" | "release_control_state") {
                self.last_actuator_dispatch = json!({
                    "request_id": request.id, "requested_action_id": request.args["action"]["action_id"],
                    "dispatch_sequence": self.dispatch_sequence,
                    "acceptance": "consult-native-receipts-not-inferred-from-dispatch",
                });
            }
            match request.cmd.as_str() {
                "step" if request.args["action"]["type"] == "visual_input" => {
                    if let Err((request, code)) = self.visual.submit(client, hud, &mut self.state, request, talk) {
                        self.error(request.id, code, code);
                    }
                },
                "frame" => {
                    let actor = protocol::actuator_identity(client, &self.state)["actor"].clone();
                    if let Err(code) = frame_fence(&request.args, &self.state.session_id, &actor) {
                        self.error(request.id, code, code);
                    } else if self.pending_frame.is_some() || self.in_flight.load(Ordering::Acquire) {
                        self.error(request.id, "frame_busy", "readback already outstanding");
                    } else {
                        self.pending_frame = Some(request.id);
                    }
                },
                "ui" => {
                    let actor = protocol::actuator_identity(client, &self.state)["actor"].clone();
                    let mut fence = request.args.clone();
                    let panel = fence.as_object_mut().and_then(|args| args.remove("panel"));
                    if let Err(code) = frame_fence(&fence, &self.state.session_id, &actor) {
                        self.error(request.id, code, code);
                    } else if let Some(panel) = panel.and_then(|panel| panel.as_str().map(str::to_owned)) {
                        match hud.native_panel(&panel) {
                            Ok(()) => protocol::emit(&json!({
                                "type": "response", "protocol_version": protocol::PROTOCOL_VERSION,
                                "session_id": self.state.session_id, "id": request.id,
                                "ok": true, "result": {"accepted": true}, "error": null,
                            })),
                            Err(code) => self.error(request.id, code, code),
                        }
                    } else {
                        self.error(request.id, "invalid_ui_panel", "panel string required");
                    }
                },
                _ => if !protocol::handle_request(client, &mut self.state, request) { return false; },
            }
        }
        true
    }

    pub fn prepare_frame(&mut self, client: &Client, hud: &crate::hud::Hud,
                         camera: &crate::scene::camera::Camera,
                         renderer: &mut crate::render::Renderer, talk: &Interactables) {
        let Some(id) = self.pending_frame.take() else { return };
        let begin = match host_ns() {
            Ok(begin) => begin,
            Err(error) => { self.error(id, "capture_clock_failed", error); return; },
        };
        self.frame_sequence += 1;
        let frame_id = format!("{}-{}", std::process::id(), self.frame_sequence);
        let identity = protocol::actuator_identity(client, &self.state);
        let visual = self.visual.capture(client, hud, talk, &frame_id, identity.clone(),
                                         renderer.resolution().into_array());
        let orientation = camera.get_orientation();
        let mut metadata = json!({
            "frame_schema_version": FRAME_SCHEMA, "frame_id": frame_id,
            "session_id": self.state.session_id, "actor": identity["actor"],
            "camera": {"profile": "ordinary-player", "mode": format!("{:?}", camera.get_mode()),
                       "orientation_radians": [orientation.x, orientation.y, orientation.z]},
            "hud": hud.native_profile(), "native_tick": null,
            "visual_ui": visual,
            "client_update_counter": self.state.tick,
            "capture_host": {"clock": "unix-realtime-nanoseconds-as-decimal-string",
                             "begin_ns": begin, "readback_complete_ns": null},
            "correlation": {
                "synchronization": "same-client-render-loop-not-server-barrier",
                "dispatch_sequence": self.dispatch_sequence,
                "last_dispatched_request_id": self.last_dispatched_request_id,
                "last_actuator_dispatch": self.last_actuator_dispatch,
                "actuator_at_render_submission": identity,
                "action_effect_ordering": "unconfirmed",
            },
        });
        let path = self.frame_dir.join(format!("frame-{frame_id}.png"));
        let session = self.state.session_id.clone();
        self.in_flight.store(true, Ordering::Release);
        let in_flight = Arc::clone(&self.in_flight);
        renderer.create_screenshot(move |result| {
            let result = result.and_then(|image| {
                metadata["capture_host"]["readback_complete_ns"] = json!(host_ns()?);
                metadata["image"] = save_png(&path, image)?;
                Ok(metadata)
            });
            let (ok, result, error) = match result {
                Ok(result) => (true, result, Value::Null),
                Err(error) => (false, Value::Null,
                    json!({"code": "frame_readback_failed", "message": error})),
            };
            protocol::emit(&json!({
                "type": "response", "protocol_version": protocol::PROTOCOL_VERSION,
                "session_id": session, "id": id, "ok": ok, "result": result, "error": error,
            }));
            in_flight.store(false, Ordering::Release);
        });
    }

    pub fn finish_ui(&mut self, client: &mut Client, hud: &crate::hud::Hud,
                     events: &mut Vec<crate::hud::Event>, talk: &Interactables) {
        self.visual.finish(client, hud, &mut self.state, events, talk);
    }
}

impl Drop for NativeAgent {
    fn drop(&mut self) {
        if let Some(id) = self.pending_frame.take() {
            self.error(id, "frame_cancelled_before_render", "session retired before render");
        }
    }
}

pub fn save_png(path: &Path, image: image::RgbImage) -> Result<Value, String> {
    let (width, height) = image.dimensions();
    let mut encoded = Cursor::new(Vec::new());
    image::DynamicImage::ImageRgb8(image).write_to(&mut encoded, image::ImageFormat::Png)
        .map_err(|error| error.to_string())?;
    let bytes = encoded.into_inner();
    let hash = hex::encode(Sha256::digest(&bytes));
    let mut file = OpenOptions::new().write(true).create_new(true).open(path)
        .map_err(|error| format!("{}: {error}", path.display()))?;
    file.write_all(&bytes).and_then(|()| file.sync_all()).map_err(|error| error.to_string())?;
    Ok(json!({"transport": "local-file", "path": path, "sha256": hash,
              "mime_type": "image/png", "byte_length": bytes.len(),
              "width": width, "height": height, "model_attachment_delivered": false}))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn production_ready_and_frame_capabilities_reach_public_schema() {
        let identity = json!({"actor": {"uid":1,"lifecycle":"life"}, "active_action_id":null,
            "control":{"owner":"","generation":0,"sequence":0,"retired":false,"protection_generation":0}});
        // These are the actual builders used by attach and frame capture.
        let ready = renderer_ready("s", &identity, "image");
        let frame = visual_ui::frame_metadata("frame");
        if let Some(path) = std::env::var_os("VELORENBENCH_VISUAL_CAPABILITY_EVIDENCE") {
            let bytes = serde_json::to_vec_pretty(&json!({
                "ready_event":ready, "frame_visual_ui":frame,
                "scope":"production builders; not native task/effect qualification",
            })).unwrap();
            OpenOptions::new().write(true).create_new(true).open(path).unwrap()
                .write_all(&bytes).unwrap();
        }
        assert_eq!(ready["data"]["visual_ui"]["modifiers"]["left_click"], json!(["shift","ctrl"]));
        for field in ["schema", "action_profile", "gestures", "modifiers", "modifier_scope"] {
            assert_eq!(ready["data"]["visual_ui"][field], frame[field]);
        }
        assert!(ready["data"]["visual_ui"].get("ui_state_id").is_none());
        assert_eq!(frame["ui_state_id"], "frame");
        for field in ["state", "inventory", "dialogue", "position", "actor"] {
            assert!(ready["data"]["visual_ui"].get(field).is_none());
            assert!(frame.get(field).is_none());
        }
        assert_eq!(project_image_output(&ready)["data"]["visual_ui"], ready["data"]["visual_ui"]);
    }

    #[test]
    fn frame_fences_exact_session_actor_lifecycle_and_schema() {
        let actor = json!({"uid": 7, "lifecycle": "incarnation:Entity(1, 1)"});
        let args = json!({"schema_version": FRAME_SCHEMA, "expected_session_id": "s", "actor": actor});
        assert!(frame_fence(&args, "s", &actor).is_ok());
        assert_eq!(frame_fence(&args, "other", &actor), Err("stale_session"));
        assert_eq!(frame_fence(&args, "s", &json!({"uid": 7, "lifecycle": "reused"})), Err("stale_actor"));
        let mut invalid = args.clone();
        invalid["extra"] = json!(true);
        assert_eq!(frame_fence(&invalid, "s", &actor), Err("invalid_frame_args"));
        invalid = args;
        invalid["schema_version"] = json!("0");
        assert_eq!(frame_fence(&invalid, "s", &actor), Err("frame_schema_mismatch"));
    }

    #[test]
    fn image_projection_covers_late_results_events_and_errors() {
        let secret = json!({"position": [1, 2, 3], "judge": true});
        let action = json!({"action_id": "a", "status": "completed", "observation": secret,
                            "survival": secret, "detail": secret});
        for field in ["result", "data"] {
            let mut envelope = json!({"type": "response", "error": {"code": "bad", "message": secret}});
            envelope[field] = action.clone();
            let projected = project_image_output(&envelope);
            assert_eq!(projected[field]["action_id"], "a");
            assert!(projected[field].get("observation").is_none());
            assert!(!projected.to_string().contains("judge"));
        }
        let error = json!({"ok": false, "result": null, "error": {"code": "stale_actor"}});
        assert!(project_image_output(&error)["result"].is_null());
    }

    #[test]
    fn image_actions_do_not_resolve_hidden_targets_or_world_destinations() {
        let request = |cmd: &str, args: Value| Request {
            id: json!("r"), protocol_version: Some(protocol::PROTOCOL_VERSION.into()),
            cmd: cmd.into(), args,
        };
        assert!(image_command_allowed(&request("step", json!({"action": {"type": "sit"}}))));
        assert!(!image_command_allowed(&request("step", json!({"action": {"type": "server_command"}}))));
        assert!(!image_command_allowed(&request("observe", json!({}))));
        assert!(!image_command_allowed(&request("set_control_state", json!({"target": {"uid": 1}}))));
    }

    #[test]
    fn image_profile_does_not_expose_new_structured_rpg_payloads_or_actions() {
        let private = json!({
            "text": "rpg-visible-structured-only", "responses": [1, 2],
            "dialogue_markers": [1], "site_prices": [7], "committed": true,
            "skill_tree": [3], "private_oracle": "never-model-evidence",
        });
        for event in ["dialogue", "trade_complete", "combat_health_change"] {
            let projected = project_image_output(&json!({
                "type": "event", "event": event, "data": private,
            }));
            assert_eq!(projected["data"], json!({}));
            assert!(!projected.to_string().contains("structured-only"));
            assert!(!projected.to_string().contains("never-model-evidence"));
        }
        for action in ["dialogue_action", "trade_action", "social_action", "unlock_skill",
                       "configure_auxiliary_ability", "context_interact"] {
            assert!(!image_command_allowed(&Request {
                id: json!("hidden"), protocol_version: Some(protocol::PROTOCOL_VERSION.into()),
                cmd: "step".into(), args: json!({"action":{"type":action}}),
            }));
        }
    }

    #[test]
    fn immutable_png_attachment_is_retrievable_and_hashes_exact_bytes() {
        let dir = std::env::temp_dir().join(format!("native-frame-test-{}-{}", std::process::id(), host_ns().unwrap()));
        fs::create_dir(&dir).unwrap();
        let path = dir.join("frame.png");
        let image = image::RgbImage::new(3, 2);
        let receipt = save_png(&path, image.clone()).unwrap();
        let bytes = fs::read(&path).unwrap();
        assert_eq!(receipt["sha256"], hex::encode(Sha256::digest(&bytes)));
        assert_eq!(image::load_from_memory(&bytes).unwrap().to_rgb8(), image);
        assert!(save_png(&path, image).is_err());
        fs::remove_file(&path).unwrap();
        fs::remove_dir(&dir).unwrap();
    }
}
