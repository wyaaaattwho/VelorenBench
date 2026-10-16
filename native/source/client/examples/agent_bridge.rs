//! Headless NDJSON agent bridge (protocol host without a game window).
//!
//! Shared protocol/state machine lives in `veloren_client::agent_protocol`.

use serde_json::json;
use veloren_client::agent_protocol::{self, PROTOCOL_VERSION};

fn main() {
    if let Err(error) = agent_protocol::run() {
        agent_protocol::emit(&json!({
            "type": "event",
            "event": "fatal",
            "protocol_version": PROTOCOL_VERSION,
            "error": error,
        }));
        std::process::exit(1);
    }
}
