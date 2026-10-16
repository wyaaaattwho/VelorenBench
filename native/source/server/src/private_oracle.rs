//! Opt-in, private engineering evidence. Never sent to a player connection.
use serde_json::json;
pub(crate) use common::private_outcomes::{enabled, event, snapshot};
use std::sync::atomic::{AtomicBool, Ordering};

static FIXTURE_CREATED: AtomicBool = AtomicBool::new(false);

pub(crate) fn resident_fixture(
    data: &mut rtsim::Data, position: vek::Vec3<f32>, index: world::IndexRef,
) {
    use common::{comp, rtsim::{Actor, Personality, Profession, Role}};
    use rand::SeedableRng;
    let Some(profile) = std::env::var_os("VELOREN_PRIVATE_NATIVE_FIXTURE") else { return };
    let outcomes = profile == "outcomes-message-v1" || profile == "outcomes-slay-v1"
        || profile == "outcomes-healing-v1";
    assert!(profile == "resident-smoke-v1" || outcomes, "unknown private native fixture profile");
    assert!(enabled(), "private native fixture requires durable private oracle evidence");
    if FIXTURE_CREATED.swap(true, Ordering::SeqCst) { return; }
    let mut rng = rand::rngs::StdRng::seed_from_u64(20261015);
    let home = data.sites.iter().filter(|(_, site)|
        site.world_site.is_some_and(|id| index.sites.get(id).trade_information(id).is_some()))
        .min_by_key(|(_, site)| site.wpos.as_::<f32>().distance_squared(position.xy()) as u64)
        .map(|(id, _)| id);
    let mut residents = Vec::new();
    let professions = if outcomes {
        vec![Profession::Farmer, Profession::Farmer]
    } else { vec![Profession::Farmer, Profession::Merchant] };
    for (index, profession) in professions.into_iter().enumerate() {
        let npc = rtsim::data::Npc::new(
            20261015 + index as u32,
            position + vek::Vec3::new(2.0 + index as f32, 0.0, 0.0),
            comp::Body::Humanoid(comp::humanoid::Body::random_with(
                &mut rng, &comp::humanoid::Species::Human)),
            Role::Civilised(Some(profession)),
        ).with_personality(Personality::random_good(&mut rng)).with_home(home);
        let actor = Actor::Npc(data.spawn_npc(npc));
        residents.push(json!({
            "actor": actor, "profession": format!("{profession:?}"),
            "rtsim_uid": actor.npc().and_then(|id| data.npcs.get(id)).map(|npc| npc.uid),
            "home": home,
        }));
    }
    if profile == "outcomes-slay-v1" {
        let mut monster = rtsim::data::Npc::new(
            20261099, position + vek::Vec3::new(220.0, 0.0, 0.0),
            comp::Body::BipedLarge(comp::biped_large::Body::random_with(
                &mut rng, &comp::biped_large::Species::Ogre)), Role::Monster,
        );
        monster.health_fraction = 0.03;
        let actor = Actor::Npc(data.spawn_npc(monster));
        residents.push(json!({
            "actor": actor, "role": "Monster",
            "rtsim_uid": actor.npc().and_then(|id| data.npcs.get(id)).map(|npc| npc.uid),
            "initial_health_fraction": 0.03,
            "scope": "real_native_RTSim_supported_Ogre;private_initial_injury_not_player_kill",
        }));
    }
    event("private_resident_fixture", json!({
        "profile": profile.to_str(), "residents": residents,
        "scope": "private_engineering_setup;real_RTSim_Npc;no_quest_created_or_accepted",
        "home_scope": "nearest_existing_native_economy_or_none;no_invented_prices;merchant_may_refuse",
    }));
}
