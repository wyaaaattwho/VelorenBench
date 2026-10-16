//! NOTE: Some of these arguments are used by airshipper, so those needs to be
//! kept fairly stable (probably with some sort of migration period if we need
//! to modify the name or semantics).
//!
//! The arguments used by airshipper are:
//! * `server`
//!
//! Airshipper should only use arguments listed above! Since we will not try to
//! be careful about their stability otherwise.
//!
//! Likewise Airshipper should only use the following subcommands:
//! * `ListWgpuBackends`
use std::str::FromStr;

use clap::{Parser, Subcommand};
use common_net::msg::ClientType;

#[derive(Parser, Clone)]
pub struct Args {
    /// Value to auto-fill into the server field.
    ///
    /// This allows passing in server selection performed in airshipper.
    #[clap(short, long)]
    pub server: Option<String>,

    /// The [`ClientType`] voxygen will use to initialize the client.
    ///
    /// The only supported values are currently `game` and `silent_spectator`,
    /// the latter one only being usable by moderators.
    #[clap(short, long, env = "VELOREN_CLIENT_TYPE", default_value_t = VoxygenClientType(ClientType::Game))]
    pub client_type: VoxygenClientType,

    /// Attach stdin/stdout native protocol to the ordinary player's existing client.
    #[clap(long)]
    pub agent_bridge: bool,

    /// Directory for immutable, locally retrievable PNG frame attachments.
    #[clap(long, requires = "agent_bridge", required_if_eq("agent_bridge", "true"))]
    pub agent_frame_dir: Option<std::path::PathBuf>,

    #[clap(long, requires = "agent_bridge", default_value = "structured",
           value_parser = ["structured", "image", "hybrid"])]
    pub agent_observation: String,

    /// Offline empty-password login through the normal menu (never loads credentials).
    #[clap(long, requires_all = ["agent_bridge", "server"])]
    pub agent_offline_user: Option<String>,

    /// Exact character alias to select through the normal character menu.
    #[clap(long, requires = "agent_offline_user")]
    pub agent_character: Option<String>,

    /// Explicitly create a missing character for an isolated offline engineering world.
    #[clap(long, requires = "agent_character")]
    pub agent_create_character: bool,

    #[clap(subcommand)]
    pub command: Option<Commands>,
}

#[derive(Subcommand, Clone)]
pub enum Commands {
    /// List available wgpu backends. This is called by Airshipper to show a
    /// dropbox of available backends.
    ListWgpuBackends,
    /// List available wgpu devices. This is called by Airshipper to show a
    /// dropbox of available devices.
    ListWgpuDevices,
}

#[derive(Clone)]
pub struct VoxygenClientType(pub ClientType);

impl FromStr for VoxygenClientType {
    type Err = String;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        Ok(Self(match s.to_lowercase().as_str() {
            "game" => ClientType::Game,
            "silent_spectator" => ClientType::SilentSpectator,
            c_type => return Err(format!("Invalid client type for voxygen: {c_type}")),
        }))
    }
}

impl std::fmt::Display for VoxygenClientType {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", match self.0 {
            ClientType::Game => "game",
            ClientType::ChatOnly => "chat_only",
            ClientType::SilentSpectator => "silent_spectator",
            ClientType::Bot { .. } => "bot",
        })
    }
}
