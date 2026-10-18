use super::*;
use crate::{ServerConstants, sys::terrain::SpawnEntityData};
use common::{
    LoadoutBuilder,
    calendar::Calendar,
    comp::{
        self, Body, Item, Presence, PresenceKind,
        inventory::trade_pricing::TradePricing,
        item::{ItemDefinitionIdOwned, Quality},
        slot::ArmorSlot,
    },
    event::{CreateNpcEvent, CreateShipEvent, DeleteEvent, EventBus, NpcBuilder},
    generation::{BodyBuilder, EntityConfig, EntityInfo},
    resources::{DeltaTime, Time, TimeOfDay},
    rtsim::{Actor, NpcId, Role, RtSimEntity},
    slowjob::SlowJobPool,
    terrain::CoordinateConversions,
    trade::{Good, SiteInformation},
    uid::{IdMaps, Uid},
    util::Dir,
    weather::WeatherGrid,
};
use common_ecs::{Job, Origin, Phase, System};
use rand::RngExt;
use rtsim::{
    ai::NpcSystemData,
    data::{
        Npc, Sites,
        npc::{Profession, SimulationMode},
    },
};
use specs::{Entities, Join, LendJoin, Read, ReadExpect, ReadStorage, WriteExpect, WriteStorage};
use std::{
    ops::Range,
    sync::{Arc, Mutex},
    time::Duration,
};
use tracing::error;

pub fn trader_loadout(
    loadout_builder: LoadoutBuilder,
    economy: Option<&SiteInformation>,
    permitted: impl FnMut(Good) -> bool,
    mut permitted_quality: impl FnMut(Quality) -> bool,
    coin_range: Range<f32>,
) -> LoadoutBuilder {
    let rng = &mut rand::rng();
    let mut backpack = Item::new_from_asset_expect("common.items.armor.misc.back.backpack");
    let mut bag1 = Item::new_from_asset_expect("common.items.armor.misc.bag.sturdy_red_backpack");
    let mut bag2 = Item::new_from_asset_expect("common.items.armor.misc.bag.sturdy_red_backpack");
    let mut bag3 = Item::new_from_asset_expect("common.items.armor.misc.bag.sturdy_red_backpack");
    let mut bag4 = Item::new_from_asset_expect("common.items.armor.misc.bag.sturdy_red_backpack");
    let slots = backpack.slots().len() + 4 * bag1.slots().len();
    let mut stockmap: hashbrown::HashMap<Good, f32> = economy
        .map(|e| e.unconsumed_stock.clone().into_iter().collect())
        .unwrap_or_default();
    // modify stock for better gameplay
    stockmap
        .entry(Good::Ingredients)
        .and_modify(|e| {
            *e = e.max(10_000.0);
            *e *= 100_000.0;
            *e = e.min(2_000_000.0);
        })
        .or_insert(1_000_000.0);

    // economy isn't economying sometimes
    stockmap
        .entry(Good::Wood)
        .and_modify(|e| {
            *e = e.max(10_000.0);
            *e *= 100_000.0;
            *e = e.min(2_000_000.0);
        })
        .or_insert(1_000_000.0);

    // econsim doesn't produce recipes at all
    stockmap
        .entry(Good::Recipe)
        .and_modify(|e| *e = e.max(10_000.0))
        .or_insert(10_000.0);

    // TODO: currently econsim spends all its food on population, resulting in none
    // for the players to buy; the `.max` is temporary to ensure that there's some
    // food for sale at every site, to be used until we have some solution like NPC
    // houses as a limit on econsim population growth
    stockmap
        .entry(Good::Food)
        .and_modify(|e| *e = e.max(10_000.0))
        .or_insert(10_000.0);
    // Reduce amount of potions so merchants do not oversupply potions.
    // TODO: Maybe remove when merchants and their inventories are rtsim?
    // Note: Likely without effect now that potions are counted as food
    stockmap
        .entry(Good::Potions)
        .and_modify(|e| *e = e.powf(0.25));
    stockmap
        .entry(Good::Coin)
        .and_modify(|e| *e = e.min(rng.random_range(coin_range)));

    // assume roughly 10 merchants sharing a town's stock (other logic for coins)
    stockmap
        .iter_mut()
        .filter(|(good, _amount)| **good != Good::Coin)
        .for_each(|(_good, amount)| *amount *= 0.1);
    // Fill bags with stuff according to unclaimed stock
    let ability_map = &comp::tool::AbilityMap::load().read();
    let msm = &comp::item::MaterialStatManifest::load().read();

    let mut allow_item = |n: ItemDefinitionIdOwned, a: &u32| -> Option<Item> {
        let i = Item::new_from_item_definition_id(n.as_ref(), ability_map, msm).ok();
        if !permitted_quality(i.as_ref()?.quality()) {
            return None;
        }
        i.map(|mut i| {
            i.set_amount(*a)
                .map_err(|_| tracing::error!("merchant loadout amount failure"))
                .ok();
            i
        })
    };

    let mut wares: Vec<Item> =
        TradePricing::random_items(&mut stockmap, slots as u32, true, true, 16, permitted)
            .into_iter()
            .filter_map(|(n, a)| allow_item(n, &a))
            .collect();
    sort_wares(&mut wares);
    transfer(&mut wares, &mut backpack);
    transfer(&mut wares, &mut bag1);
    transfer(&mut wares, &mut bag2);
    transfer(&mut wares, &mut bag3);
    transfer(&mut wares, &mut bag4);

    loadout_builder
        .back(Some(backpack))
        .bag(ArmorSlot::Bag1, Some(bag1))
        .bag(ArmorSlot::Bag2, Some(bag2))
        .bag(ArmorSlot::Bag3, Some(bag3))
        .bag(ArmorSlot::Bag4, Some(bag4))
}

fn sort_wares(bag: &mut [Item]) {
    use common::comp::item::TagExampleInfo;

    bag.sort_by(|a, b| {
        a.quality()
            .cmp(&b.quality())
        // sort by kind
        .then(
            Ord::cmp(
                a.tags().first().map_or("", |tag| tag.name()),
                b.tags().first().map_or("", |tag| tag.name()),
            )
        )
        // sort by name
        // TODO: figure out the better way here
        .then(#[expect(deprecated)] Ord::cmp(&a.legacy_name(), &b.legacy_name()))
    });
}

fn transfer(wares: &mut Vec<Item>, bag: &mut Item) {
    let capacity = bag.slots().len();
    for (s, w) in bag
        .slots_mut()
        .iter_mut()
        .zip(wares.drain(0..wares.len().min(capacity)))
    {
        *s = Some(w);
    }
}

fn humanoid_config(profession: &Profession) -> &'static str {
    match profession {
        Profession::Farmer => "common.entity.village.farmer",
        Profession::Hunter => "common.entity.village.hunter",
        Profession::Herbalist => "common.entity.village.herbalist",
        Profession::Captain => "common.entity.village.captain",
        Profession::Merchant => "common.entity.village.merchant",
        Profession::Guard => "common.entity.village.guard",
        Profession::Adventurer(rank) => match rank {
            0 => "common.entity.world.traveler0",
            1 => "common.entity.world.traveler1",
            2 => "common.entity.world.traveler2",
            3 => "common.entity.world.traveler3",
            _ => {
                error!(
                    "Tried to get configuration for invalid adventurer rank {}",
                    rank
                );
                "common.entity.world.traveler3"
            },
        },
        Profession::Blacksmith => "common.entity.village.blacksmith",
        Profession::Chef => "common.entity.village.chef",
        Profession::Alchemist => "common.entity.village.alchemist",
        Profession::Pirate(leader) => match leader {
            false => "common.entity.spot.pirate",
            true => "common.entity.spot.buccaneer",
        },
        Profession::Cultist => "common.entity.dungeon.cultist.cultist",
    }
}

fn loadout_default(
    loadout: LoadoutBuilder,
    _economy: Option<&SiteInformation>,
    _time: Option<&(TimeOfDay, Calendar)>,
) -> LoadoutBuilder {
    loadout
}

fn merchant_loadout(
    loadout_builder: LoadoutBuilder,
    economy: Option<&SiteInformation>,
    _time: Option<&(TimeOfDay, Calendar)>,
) -> LoadoutBuilder {
    trader_loadout(
        loadout_builder,
        economy,
        |good| {
            !matches!(
                good,
                Good::Ingredients | Good::Tools | Good::Armor | Good::Wood
            )
        },
        |quality| matches!(quality, Quality::Low | Quality::Common | Quality::Moderate),
        1000.0..3000.0,
    )
}

fn farmer_loadout(
    loadout_builder: LoadoutBuilder,
    economy: Option<&SiteInformation>,
    _time: Option<&(TimeOfDay, Calendar)>,
) -> LoadoutBuilder {
    trader_loadout(
        loadout_builder,
        economy,
        |good| matches!(good, Good::Food | Good::Coin),
        |quality| matches!(quality, Quality::Low | Quality::Common | Quality::Moderate),
        500.0..1400.0,
    )
}

fn herbalist_loadout(
    loadout_builder: LoadoutBuilder,
    economy: Option<&SiteInformation>,
    _time: Option<&(TimeOfDay, Calendar)>,
) -> LoadoutBuilder {
    trader_loadout(
        loadout_builder,
        economy,
        |good| {
            matches!(
                good,
                Good::Potions | Good::Stone | Good::Wood | Good::Ingredients | Good::Coin
            )
        },
        |quality| matches!(quality, Quality::Low | Quality::Common | Quality::Moderate),
        500.0..1400.0,
    )
}

fn chef_loadout(
    loadout_builder: LoadoutBuilder,
    economy: Option<&SiteInformation>,
    _time: Option<&(TimeOfDay, Calendar)>,
) -> LoadoutBuilder {
    trader_loadout(
        loadout_builder,
        economy,
        |good| matches!(good, Good::Food | Good::Coin),
        |quality| matches!(quality, Quality::Low | Quality::Common | Quality::Moderate),
        500.0..1400.0,
    )
}

fn blacksmith_loadout(
    loadout_builder: LoadoutBuilder,
    economy: Option<&SiteInformation>,
    _time: Option<&(TimeOfDay, Calendar)>,
) -> LoadoutBuilder {
    trader_loadout(
        loadout_builder,
        economy,
        |good| matches!(good, Good::Armor | Good::Coin),
        |quality| matches!(quality, Quality::Low | Quality::Common | Quality::Moderate),
        500.0..1400.0,
    )
}

fn hunter_loadout(
    loadout_builder: LoadoutBuilder,
    economy: Option<&SiteInformation>,
    _time: Option<&(TimeOfDay, Calendar)>,
) -> LoadoutBuilder {
    trader_loadout(
        loadout_builder,
        economy,
        |good| matches!(good, Good::Tools | Good::Coin),
        |quality| matches!(quality, Quality::Low | Quality::Common | Quality::Moderate),
        500.0..1400.0,
    )
}

fn alchemist_loadout(
    loadout_builder: LoadoutBuilder,
    economy: Option<&SiteInformation>,
    _time: Option<&(TimeOfDay, Calendar)>,
) -> LoadoutBuilder {
    trader_loadout(
        loadout_builder,
        economy,
        |good| {
            matches!(
                good,
                Good::Potions | Good::Stone | Good::Wood | Good::Ingredients | Good::Coin
            )
        },
        |quality| matches!(quality, Quality::Low | Quality::Common | Quality::Moderate),
        500.0..1400.0,
    )
}

fn profession_extra_loadout(
    profession: Option<&Profession>,
) -> fn(
    LoadoutBuilder,
    Option<&SiteInformation>,
    time: Option<&(TimeOfDay, Calendar)>,
) -> LoadoutBuilder {
    match profession {
        Some(Profession::Merchant) => merchant_loadout,
        Some(Profession::Farmer) => farmer_loadout,
        Some(Profession::Herbalist) => herbalist_loadout,
        Some(Profession::Chef) => chef_loadout,
        Some(Profession::Blacksmith) => blacksmith_loadout,
        Some(Profession::Alchemist) => alchemist_loadout,
        Some(Profession::Hunter) => hunter_loadout,
        _ => loadout_default,
    }
}

fn profession_agent_mark(profession: Option<&Profession>) -> Option<comp::agent::Mark> {
    match profession {
        Some(
            Profession::Merchant
            | Profession::Farmer
            | Profession::Herbalist
            | Profession::Chef
            | Profession::Blacksmith
            | Profession::Hunter
            | Profession::Alchemist,
        ) => Some(comp::agent::Mark::Merchant),
        Some(Profession::Guard) => Some(comp::agent::Mark::Guard),
        _ => None,
    }
}

fn get_npc_entity_info(
    npc: &Npc,
    sites: &Sites,
    index: IndexRef,
    time: Option<&(TimeOfDay, Calendar)>,
) -> EntityInfo {
    let pos = comp::Pos(npc.wpos);

    let mut rng = npc.rng(Npc::PERM_ENTITY_CONFIG);
    if let Some(profession) = npc.profession() {
        let economy = npc.home.and_then(|home| {
            let site = sites.get(home)?.world_site?;
            index.sites.get(site).trade_information(site)
        });

        let config_asset = humanoid_config(&profession);

        let entity_config = EntityConfig::from_asset_expect_owned(config_asset)
            .with_body(BodyBuilder::Exact(npc.body));
        EntityInfo::at(pos.0)
            .with_entity_config(entity_config, Some(config_asset), &mut rng, time)
            .with_alignment(
                if matches!(profession, Profession::Cultist | Profession::Pirate(_)) {
                    comp::Alignment::Enemy
                } else {
                    comp::Alignment::Npc
                },
            )
            .with_economy(economy.as_ref())
            .with_lazy_loadout(profession_extra_loadout(Some(&profession)))
            .with_alias(npc.get_name())
            .with_agent_mark(profession_agent_mark(Some(&profession)))
    } else {
        let config_asset = match npc.body {
            Body::BirdLarge(body) => match body.species {
                comp::bird_large::Species::Phoenix => "common.entity.wild.aggressive.phoenix",
                comp::bird_large::Species::Cockatrice => "common.entity.wild.aggressive.cockatrice",
                comp::bird_large::Species::Roc => "common.entity.wild.aggressive.roc",
                comp::bird_large::Species::CloudWyvern => {
                    "common.entity.wild.aggressive.cloudwyvern"
                },
                comp::bird_large::Species::FlameWyvern => {
                    "common.entity.wild.aggressive.flamewyvern"
                },
                comp::bird_large::Species::FrostWyvern => {
                    "common.entity.wild.aggressive.frostwyvern"
                },
                comp::bird_large::Species::SeaWyvern => "common.entity.wild.aggressive.seawyvern",
                comp::bird_large::Species::WealdWyvern => {
                    "common.entity.wild.aggressive.wealdwyvern"
                },
            },
            Body::BipedLarge(body) => match body.species {
                comp::biped_large::Species::Ogre => "common.entity.wild.aggressive.ogre",
                comp::biped_large::Species::Cyclops => "common.entity.wild.aggressive.cyclops",
                comp::biped_large::Species::Wendigo => "common.entity.wild.aggressive.wendigo",
                comp::biped_large::Species::Werewolf => "common.entity.wild.aggressive.werewolf",
                comp::biped_large::Species::Cavetroll => "common.entity.wild.aggressive.cave_troll",
                comp::biped_large::Species::Mountaintroll => {
                    "common.entity.wild.aggressive.mountain_troll"
                },
                comp::biped_large::Species::Swamptroll => {
                    "common.entity.wild.aggressive.swamp_troll"
                },
                comp::biped_large::Species::Blueoni => "common.entity.wild.aggressive.blue_oni",
                comp::biped_large::Species::Redoni => "common.entity.wild.aggressive.red_oni",
                comp::biped_large::Species::Tursus => "common.entity.wild.aggressive.tursus",
                comp::biped_large::Species::Gigasfrost => {
                    "common.entity.world.world_bosses.gigas_frost"
                },
                comp::biped_large::Species::Gigasfire => {
                    "common.entity.world.world_bosses.gigas_fire"
                },
                species => unimplemented!("rtsim spawning for {:?}", species),
            },
            body => unimplemented!("rtsim spawning for {:?}", body),
        };
        let entity_config = EntityConfig::from_asset_expect_owned(config_asset)
            .with_body(BodyBuilder::Exact(npc.body));

        EntityInfo::at(pos.0).with_entity_config(entity_config, Some(config_asset), &mut rng, time)
    }
}

#[derive(Default)]
pub struct Sys;
impl<'a> System<'a> for Sys {
    type SystemData = (
        Entities<'a>,
        Read<'a, DeltaTime>,
        Read<'a, Time>,
        Read<'a, TimeOfDay>,
        Read<'a, EventBus<CreateShipEvent>>,
        Read<'a, EventBus<CreateNpcEvent>>,
        Read<'a, EventBus<DeleteEvent>>,
        WriteExpect<'a, RtSim>,
        ReadExpect<'a, Arc<world::World>>,
        ReadExpect<'a, world::IndexOwned>,
        ReadExpect<'a, SlowJobPool>,
        ReadStorage<'a, comp::Pos>,
        ReadStorage<'a, RtSimEntity>,
        ReadStorage<'a, Uid>,
        WriteStorage<'a, comp::Agent>,
        ReadStorage<'a, Presence>,
        ReadExpect<'a, Calendar>,
        Read<'a, IdMaps>,
        ReadExpect<'a, ServerConstants>,
        ReadExpect<'a, WeatherGrid>,
        WriteStorage<'a, comp::Inventory>,
        WriteExpect<'a, comp::gizmos::RtsimGizmos>,
        ReadExpect<'a, comp::tool::AbilityMap>,
        ReadExpect<'a, comp::item::MaterialStatManifest>,
        (ReadStorage<'a, comp::Body>, ReadStorage<'a, comp::Health>,
         ReadStorage<'a, comp::Alignment>, ReadStorage<'a, comp::Group>,
         ReadStorage<'a, comp::PhysicsState>),
    );

    const NAME: &'static str = "rtsim::tick";
    const ORIGIN: Origin = Origin::Server;
    const PHASE: Phase = Phase::Create;

    fn run(
        _job: &mut Job<Self>,
        (
            entities,
            dt,
            time,
            time_of_day,
            create_ship_events,
            create_npc_events,
            delete_events,
            mut rtsim,
            world,
            index,
            slow_jobs,
            positions,
            rtsim_entities,
            uids,
            mut agents,
            presences,
            calendar,
            id_maps,
            server_constants,
            weather_grid,
            inventories,
            rtsim_gizmos,
            ability_map,
            msm,
            (bodies, healths, alignments, groups, physics),
        ): Self::SystemData,
    ) {
        let mut create_ship_emitter = create_ship_events.emitter();
        let mut create_npc_emitter = create_npc_events.emitter();
        let mut delete_emitter = delete_events.emitter();
        let rtsim = &mut *rtsim;
        let calendar_data = (*time_of_day, (*calendar).clone());

        // Set up rtsim inputs
        {
            let mut data = rtsim.state.data_mut();

            // Update time of day
            data.time_of_day = *time_of_day;
            if let Some((_, position)) = (&presences, &positions).join()
                .find(|(presence, _)| matches!(presence.kind, PresenceKind::Character(_)))
            {
                crate::private_oracle::resident_fixture(&mut data, position.0, index.as_index_ref());
            }

            // Update character map (i.e: so that rtsim knows where players are)
            // TODO: Other entities too like animals? Or do we now care about that?
            data.npcs.character_map.clear();
            for (presence, wpos) in (&presences, &positions).join() {
                if let PresenceKind::Character(character) = &presence.kind {
                    let chunk_pos = wpos.0.xy().as_().wpos_to_cpos();
                    data.npcs
                        .character_map
                        .entry(chunk_pos)
                        .or_default()
                        .push((*character, wpos.0));
                }
            }
        }

        let combat_equipment = if crate::private_oracle::enabled() {
            (&entities, &uids, &inventories).join().map(|(_, uid, inventory)|
                (uid.0.get(), inventory.equipped_items_with_slot().map(|(slot, item)|
                    serde_json::json!({"slot":slot,"item":item.item_definition_id().to_owned()}))
                    .collect::<Vec<_>>())).collect::<std::collections::BTreeMap<_, _>>()
        } else { Default::default() };

        // Tick rtsim
        rtsim.state.tick(
            &mut NpcSystemData {
                positions: positions.clone(),
                id_maps,
                server_constants,
                weather_grid,
                inventories: Mutex::new(inventories),
                rtsim_gizmos,
                ability_map,
                msm,
            },
            &world,
            index.as_index_ref(),
            *time_of_day,
            *time,
            dt.0,
        );

        // Perform a save if required
        if rtsim
            .last_saved
            .is_none_or(|ls| ls.elapsed() > Duration::from_secs(60))
        {
            // TODO: Use slow jobs
            let _ = slow_jobs;
            rtsim.save(/* &slow_jobs, */ false);
        }

        let chunk_states = rtsim.state.resource::<ChunkStates>();
        let data = &mut *rtsim.state.data_mut();
        if crate::private_oracle::enabled() {
            if data.architect.last_maintenance_tick.is_none()
                || data.architect.last_maintenance_tick == Some(data.tick)
            {
                crate::private_oracle::snapshot("rtsim_population", serde_json::json!({
                    "rtsim_tick": data.tick,
                    "clock": "server RTSim tick/time-of-day; not a client-update counter",
                    "time_of_day": data.time_of_day.0,
                    "allocated_population": data.architect.population.total(),
                    "wanted_population": data.architect.wanted_population.total(),
                    "queued_respawns": data.architect.deaths.len(),
                    "settlement_bootstrap": rtsim::rule::architect::settlement_population_readiness(
                        &data.architect, data.time_of_day),
                    "scope": "private initialization evidence; not NPC eligibility or model information",
                }));
            }
            let mut loaded = (&entities, &rtsim_entities, &uids).join().map(|(entity, npc, uid)| {
                serde_json::json!({
                    "actor": Actor::Npc(*npc), "uid": uid.0.get(),
                    "server_entity": format!("{entity:?}"),
                    "rtsim_uid": data.npcs.get(*npc).map(|npc| npc.uid),
                    "server_position": positions.get(entity).map(|pos| pos.0),
                    "rtsim_position": data.npcs.get(*npc).map(|npc| npc.wpos),
                    "civilised": data.npcs.get(*npc).map(|npc| matches!(&npc.role, Role::Civilised(_))),
                    "body": bodies.get(entity).map(|body| body.to_string()),
                    "alive": healths.get(entity).is_some_and(|health| !health.is_dead && health.current() > 0.0),
                    "on_ground": physics.get(entity).map(|state| state.on_ground.is_some()),
                    "existence": "loaded;not_death_evidence",
                })
            }).collect::<Vec<_>>();
            loaded.extend((&entities, &presences, &uids).join().filter_map(|(entity, presence, uid)| {
                match presence.kind {
                    PresenceKind::Character(character) => Some(serde_json::json!({
                        "actor": Actor::Character(character), "uid": uid.0.get(),
                        "server_entity": format!("{entity:?}"),
                    })),
                    _ => None,
                }
            }));
            loaded.sort_by_key(|row| row["uid"].as_u64());
            let mut combat_bindings = (&entities, &uids, &healths).join().map(|(entity, uid, health)| {
                serde_json::json!({
                    "uid": uid.0.get(), "server_entity": format!("{entity:?}"),
                    "body": bodies.get(entity), "alignment": alignments.get(entity),
                    "group": groups.get(entity), "health": health.current(),
                    "maximum_health": health.maximum(), "dead": health.is_dead,
                    "equipment": combat_equipment.get(&uid.0.get()),
                })
            }).collect::<Vec<_>>();
            combat_bindings.sort_by_key(|row| row["uid"].as_u64());
            crate::private_oracle::snapshot("combat_entity_bindings", serde_json::json!({
                "bindings": combat_bindings,
                "scope": "private_native_UID_entity_generation_equipment_Group_health;absence_not_death",
            }));
            crate::private_oracle::snapshot("loaded_actor_bindings", serde_json::json!({
                "bindings": loaded,
                "lifecycle_scope": "server ECS;join client lifecycle by UID within this server/run;not_equal_strings",
                "absence": "unloaded_or_removed;not_player_kill",
            }));
            let mut quests = data.quests.iter().map(|(id, quest)| {
                let mut actors = quest.get_related_actors().into_iter().map(|actor| {
                    serde_json::json!({
                        "actor": actor,
                        "rtsim_exists": actor.npc().map(|npc| data.npcs.get(npc).is_some()),
                        "rtsim_uid": actor.npc().and_then(|npc| data.npcs.get(npc)).map(|npc| npc.uid),
                        "rtsim_health_fraction": actor.npc().and_then(|npc| data.npcs.get(npc))
                            .map(|npc| npc.health_fraction),
                    })
                }).collect::<Vec<_>>();
                actors.sort_by_key(|actor| actor.to_string());
                serde_json::json!({
                    "quest_id": id, "quest": quest, "resolution": quest.resolution(),
                    "actors": actors,
                    "reward_arrival": null,
                    "reward_scope": "resolution_deposit_is_not_player_receipt;join_dialogue_item_transfer",
                })
            }).collect::<Vec<_>>();
            quests.sort_by_key(|row| row["quest_id"].as_u64());
            crate::private_oracle::snapshot("quest_states", serde_json::json!(quests));
        }

        let mut create_event = |id: NpcId, npc: &Npc, steering: Option<NpcBuilder>| match npc.body {
            Body::Ship(body) => {
                create_ship_emitter.emit(CreateShipEvent {
                    pos: comp::Pos(npc.wpos),
                    ori: comp::Ori::from(Dir::new(npc.dir.with_z(0.0))),
                    ship: body,
                    rtsim_entity: Some(id),
                    driver: steering,
                });
            },
            _ => {
                let entity_info = get_npc_entity_info(
                    npc,
                    &data.sites,
                    index.as_index_ref(),
                    Some(&calendar_data),
                );

                let (mut npc_builder, pos) = SpawnEntityData::from_entity_info(entity_info)
                    .into_npc_data_inner()
                    .expect("Entity loaded from assets cannot be special")
                    .to_npc_builder();

                if let Some(agent) = &mut npc_builder.agent {
                    agent.rtsim_outbox = Some(Default::default());
                }

                if let Some(health) = &mut npc_builder.health {
                    health.set_fraction(npc.health_fraction);
                }

                create_npc_emitter.emit(CreateNpcEvent {
                    pos,
                    ori: comp::Ori::from(Dir::new(npc.dir.with_z(0.0))),
                    npc: npc_builder.with_rtsim(id).with_rider(steering),
                });
            },
        };

        // Load in mounted npcs and their riders
        for mount in data.npcs.mounts.iter_mounts() {
            let mount_npc = data.npcs.npcs.get_mut(mount).expect("This should exist");
            let chunk = mount_npc.wpos.xy().as_::<i32>().wpos_to_cpos();

            if matches!(mount_npc.mode, SimulationMode::Simulated)
                && chunk_states.0.get(chunk).is_some_and(|c| c.is_some())
            {
                mount_npc.mode = SimulationMode::Loaded;

                let mut actor_info = |actor: Actor| {
                    let npc_id = actor.npc()?;
                    let npc = data.npcs.npcs.get_mut(npc_id)?;
                    if matches!(npc.mode, SimulationMode::Simulated) {
                        npc.mode = SimulationMode::Loaded;
                        let entity_info = get_npc_entity_info(
                            npc,
                            &data.sites,
                            index.as_index_ref(),
                            Some(&calendar_data),
                        );

                        let mut npc_builder = SpawnEntityData::from_entity_info(entity_info)
                            .into_npc_data_inner()
                            // EntityConfig can't represent Waypoints at all
                            // as of now, and if someone will try to spawn
                            // rtsim waypoint it is definitely error.
                            .expect("Entity loaded from assets cannot be special")
                            .to_npc_builder()
                            .0
                            .with_rtsim(npc_id);

                        if let Some(agent) = &mut npc_builder.agent {
                            agent.rtsim_outbox = Some(Default::default());
                        }

                        Some(npc_builder)
                    } else {
                        error!("Npc is loaded but vehicle is unloaded");
                        None
                    }
                };

                let steerer = data
                    .npcs
                    .mounts
                    .get_steerer_link(mount)
                    .and_then(|link| actor_info(link.rider));

                let mount_npc = data.npcs.npcs.get(mount).expect("This should exist");
                create_event(mount, mount_npc, steerer);
            }
        }

        // Load in NPCs
        for (npc_id, npc) in data.npcs.npcs.iter_mut() {
            let chunk = npc.wpos.xy().as_::<i32>().wpos_to_cpos();

            // Load the NPC into the world if it's in a loaded chunk and is not already
            // loaded
            if matches!(npc.mode, SimulationMode::Simulated)
                && chunk_states.0.get(chunk).is_some_and(|c| c.is_some())
                // Riding npcs will be spawned by the vehicle.
                && data.npcs.mounts.get_mount_link(npc_id).is_none()
            {
                npc.mode = SimulationMode::Loaded;
                create_event(npc_id, npc, None);
            }
        }

        // Synchronise rtsim NPC with entity data
        for (entity, pos, rtsim_entity, agent) in (
            &entities,
            &positions,
            &rtsim_entities,
            (&mut agents).maybe(),
        )
            .join()
        {
            if let Some(npc) = data.npcs.get_mut(*rtsim_entity) {
                match npc.mode {
                    SimulationMode::Loaded => {
                        // Update rtsim NPC state
                        npc.wpos = pos.0;

                        // Update entity state
                        if let Some(agent) = agent {
                            agent.rtsim_controller.personality = npc.personality;
                            agent.rtsim_controller.look_dir = npc.controller.look_dir;
                            agent.rtsim_controller.activity = npc.controller.activity;
                            agent
                                .rtsim_controller
                                .actions
                                .extend(std::mem::take(&mut npc.controller.actions));
                            if let Some(rtsim_outbox) = &mut agent.rtsim_outbox {
                                npc.inbox.append(rtsim_outbox);
                            }
                        }
                    },
                    SimulationMode::Simulated => {
                        delete_emitter.emit(DeleteEvent(entity));
                    },
                }
            }
        }
    }
}
