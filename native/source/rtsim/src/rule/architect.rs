use common::{
    comp::{self, Body},
    resources::TimeOfDay,
    rtsim::{Actor, Personality, Profession, Role},
    terrain::CoordinateConversions,
};
use rand::{
    RngExt, rng,
    seq::{IndexedRandom, IteratorRandom},
};
use world::{CONFIG, IndexRef, World, sim::SimChunk, site::SiteKind};

use crate::{
    Data, EventCtx, OnTick, RtState,
    data::{
        Npc,
        architect::{Death, TrackedPopulation},
    },
    event::OnDeath,
};

use super::{Rule, RuleError};

/// How many ticks the architect skips.
///
/// We don't need to run it every tick.
const ARCHITECT_TICK_SKIP: u64 = 32;
/// Min spawn delay, in ingame time.
const MIN_SPAWN_DELAY: f64 = 60.0 * 60.0 * 24.0;
/// For monsters that respawn in chunks, how many chunks should we try each
/// respawn.
const RESPAWN_ATTEMPTS: usize = 30;

pub struct Architect;

impl Rule for Architect {
    fn start(rtstate: &mut RtState) -> Result<Self, RuleError> {
        rtstate.bind(on_death);
        rtstate.bind(architect_tick);

        Ok(Self)
    }
}

fn on_death(ctx: EventCtx<Architect, OnDeath>) {
    let data = &mut *ctx.state.data_mut();

    if let Actor::Npc(npc_id) = ctx.event.actor
        && let Some(npc) = data.npcs.get(npc_id)
    {
        data.architect.on_death(npc, data.time_of_day);
    }
}

fn architect_tick(ctx: EventCtx<Architect, OnTick>) {
    if !ctx.event.tick.is_multiple_of(ARCHITECT_TICK_SKIP) {
        return;
    }

    let tod = ctx.event.time_of_day;

    let data = &mut *ctx.state.data_mut();

    let mut rng = rng();
    let mut count_to_spawn = rng.random_range(1..20);

    let pop = data.architect.population.clone();
    'outer: for (pop, count) in pop
        .iter()
        .zip(data.architect.wanted_population.iter())
        .filter(|((_, current), (_, wanted))| current < wanted)
        .map(|((pop, current), (_, wanted))| (pop, wanted - current))
    {
        for _ in 0..count {
            let (body, role) = match pop {
                TrackedPopulation::Adventurers => (
                    Body::Humanoid(comp::humanoid::Body::random()),
                    Role::Civilised(Some(Profession::Adventurer(rng.random_range(0..=3)))),
                ),
                TrackedPopulation::Merchants => (
                    Body::Humanoid(comp::humanoid::Body::random()),
                    Role::Civilised(Some(Profession::Merchant)),
                ),
                TrackedPopulation::Guards => (
                    Body::Humanoid(comp::humanoid::Body::random()),
                    Role::Civilised(Some(Profession::Guard)),
                ),
                TrackedPopulation::Captains => (
                    Body::Humanoid(comp::humanoid::Body::random()),
                    Role::Civilised(Some(Profession::Captain)),
                ),
                TrackedPopulation::OtherTownNpcs => (
                    Body::Humanoid(comp::humanoid::Body::random()),
                    Role::Civilised(Some(match rng.random_range(0..10) {
                        0 => Profession::Hunter,
                        1 => Profession::Blacksmith,
                        2 => Profession::Chef,
                        3 => Profession::Alchemist,
                        4..=5 => Profession::Herbalist,
                        _ => Profession::Farmer,
                    })),
                ),
                TrackedPopulation::Pirates => (
                    Body::Humanoid(comp::humanoid::Body::random()),
                    Role::Civilised(Some(Profession::Pirate(false))),
                ),
                TrackedPopulation::PirateCaptains => (
                    Body::Humanoid(comp::humanoid::Body::random()),
                    Role::Civilised(Some(Profession::Pirate(true))),
                ),
                TrackedPopulation::Cultists => (
                    Body::Humanoid(comp::humanoid::Body::random()),
                    Role::Civilised(Some(Profession::Cultist)),
                ),
                TrackedPopulation::GigasFrost => (
                    Body::BipedLarge(comp::biped_large::Body::random_with(
                        &mut rng,
                        &comp::biped_large::Species::Gigasfrost,
                    )),
                    Role::Monster,
                ),
                TrackedPopulation::GigasFire => (
                    Body::BipedLarge(comp::biped_large::Body::random_with(
                        &mut rng,
                        &comp::biped_large::Species::Gigasfire,
                    )),
                    Role::Monster,
                ),
                TrackedPopulation::OtherMonsters => {
                    let species = [
                        comp::biped_large::Species::Ogre,
                        comp::biped_large::Species::Cyclops,
                        comp::biped_large::Species::Wendigo,
                        comp::biped_large::Species::Cavetroll,
                        comp::biped_large::Species::Mountaintroll,
                        comp::biped_large::Species::Swamptroll,
                        comp::biped_large::Species::Blueoni,
                        comp::biped_large::Species::Redoni,
                        comp::biped_large::Species::Tursus,
                    ]
                    .choose(&mut rng)
                    .unwrap();

                    (
                        Body::BipedLarge(comp::biped_large::Body::random_with(&mut rng, species)),
                        Role::Monster,
                    )
                },
                TrackedPopulation::CloudWyvern => (
                    Body::BirdLarge(comp::bird_large::Body::random_with(
                        &mut rng,
                        &comp::bird_large::Species::CloudWyvern,
                    )),
                    Role::Wild,
                ),
                TrackedPopulation::FrostWyvern => (
                    Body::BirdLarge(comp::bird_large::Body::random_with(
                        &mut rng,
                        &comp::bird_large::Species::FrostWyvern,
                    )),
                    Role::Wild,
                ),
                TrackedPopulation::SeaWyvern => (
                    Body::BirdLarge(comp::bird_large::Body::random_with(
                        &mut rng,
                        &comp::bird_large::Species::SeaWyvern,
                    )),
                    Role::Wild,
                ),
                TrackedPopulation::FlameWyvern => (
                    Body::BirdLarge(comp::bird_large::Body::random_with(
                        &mut rng,
                        &comp::bird_large::Species::FlameWyvern,
                    )),
                    Role::Wild,
                ),
                TrackedPopulation::WealdWyvern => (
                    Body::BirdLarge(comp::bird_large::Body::random_with(
                        &mut rng,
                        &comp::bird_large::Species::WealdWyvern,
                    )),
                    Role::Wild,
                ),
                TrackedPopulation::Phoenix => (
                    Body::BirdLarge(comp::bird_large::Body::random_with(
                        &mut rng,
                        &comp::bird_large::Species::Phoenix,
                    )),
                    Role::Wild,
                ),
                TrackedPopulation::Roc => (
                    Body::BirdLarge(comp::bird_large::Body::random_with(
                        &mut rng,
                        &comp::bird_large::Species::Roc,
                    )),
                    Role::Wild,
                ),
                TrackedPopulation::Cockatrice => (
                    Body::BirdLarge(comp::bird_large::Body::random_with(
                        &mut rng,
                        &comp::bird_large::Species::Cockatrice,
                    )),
                    Role::Wild,
                ),
                TrackedPopulation::Other => continue 'outer,
            };

            let fake_death = Death {
                time: TimeOfDay(tod.0 - MIN_SPAWN_DELAY),
                body,
                role,
                faction: None,
            };

            data.architect.population.on_spawn(&fake_death);

            data.architect.deaths.push_front(fake_death);
        }

        count_to_spawn += count;
    }

    // @perf: Could reuse previous allocation here.
    let mut failed_spawn = Vec::new();

    while count_to_spawn > 0 && let Some(death) = next_spawn(&mut data.architect, tod) {
        if spawn_npc(data, ctx.world, ctx.index, &death) {
            count_to_spawn -= 1;
        } else {
            failed_spawn.push(death);
        }
    }

    for death in failed_spawn.into_iter().rev() {
        data.architect.deaths.push_front(death);
    }
    data.architect.last_maintenance_tick = Some(ctx.event.tick);
}

fn waiting_for_respawn(death: &Death, now: TimeOfDay) -> bool {
    // Initial entries are backdated using this exact cutoff. Adding the delay
    // back can round above now and strand the otherwise immediately due queue.
    death.time.0 > now.0 - MIN_SPAWN_DELAY
}

fn next_spawn(architect: &mut crate::data::architect::Architect, now: TimeOfDay) -> Option<Death> {
    while let Some(death) = architect.deaths.pop_front() {
        if architect.population.of_death(&death) > architect.wanted_population.of_death(&death) {
            architect.population.on_death(&death);
            continue;
        }
        if waiting_for_respawn(&death, now) {
            architect.deaths.push_front(death);
            return None;
        }
        return Some(death);
    }
    None
}

#[cfg(test)]
mod population_bootstrap_tests {
    use super::*;
    use rand::SeedableRng;
    use crate::data::architect::Architect as PopulationArchitect;

    fn death(time: f64) -> Death {
        Death {
            time: TimeOfDay(time),
            body: Body::Humanoid(comp::humanoid::Body::random_with(
                &mut rand::rngs::StdRng::seed_from_u64(0), &comp::humanoid::Species::Human)),
            role: Role::Civilised(Some(Profession::Farmer)), faction: None,
        }
    }

    fn allocated(entry: Death) -> PopulationArchitect {
        let mut architect = PopulationArchitect::default();
        architect.wanted_population.add(TrackedPopulation::OtherTownNpcs, 1);
        architect.population.on_spawn(&entry);
        architect.deaths.push_front(entry);
        architect
    }

    #[test]
    fn generated_immediately_eligible_entry_reaches_real_queue_drain() {
        assert_eq!(MIN_SPAWN_DELAY, 86400.0);
        let now = TimeOfDay(f64::from_bits(0x40dfaa6666666667));
        let entry = death(now.0 - MIN_SPAWN_DELAY);
        // The observed arithmetic risk is not a native-world frequency claim.
        assert!(entry.time.0 + MIN_SPAWN_DELAY > now.0);
        let mut architect = allocated(entry);
        assert_eq!(architect.population.total(), 1);
        assert_eq!(architect.wanted_population.total(), 1);
        assert!(next_spawn(&mut architect, now).is_some(),
                "optimistic population allocation must not strand the generated queue entry");
        assert!(architect.deaths.is_empty());
    }

    #[test]
    fn real_cutoff_queue_boundary_is_before_equal_after_without_epsilon() {
        let now = TimeOfDay(f64::from_bits(0x40dfaa6666666667));
        let cutoff = now.0 - MIN_SPAWN_DELAY;
        for (time, due) in [(cutoff.next_down(), true), (cutoff, true), (cutoff.next_up(), false)] {
            let mut architect = allocated(death(time));
            assert_eq!(next_spawn(&mut architect, now).is_some(), due);
            assert_eq!(architect.deaths.len(), usize::from(!due));
            assert_eq!(architect.population.total(), 1);
        }
    }

    #[test]
    fn allocated_counts_are_not_bootstrap_completion_and_failed_spawns_remain_pending() {
        let now = TimeOfDay(f64::from_bits(0x40dfaa6666666667));
        let mut architect = allocated(death(now.0 - MIN_SPAWN_DELAY));
        assert!(!settlement_population_readiness(&architect, now).ready);
        architect.last_maintenance_tick = Some(32);
        let pending = settlement_population_readiness(&architect, now);
        assert_eq!(pending.allocated_deficit, 0);
        assert_eq!(pending.eligible_pending, 1);
        assert!(!pending.ready);
        let entry = next_spawn(&mut architect, now).unwrap();
        // Same queue restoration used by architect_tick after spawn_npc fails.
        architect.deaths.push_front(entry);
        assert!(!settlement_population_readiness(&architect, now).ready);
        assert!(next_spawn(&mut architect, now).is_some());
        assert!(settlement_population_readiness(&architect, now).ready);
        architect.on_death(&Npc::new(1, vek::Vec3::zero(), death(0.0).body,
                                    Role::Civilised(Some(Profession::Farmer))), now);
        assert!(settlement_population_readiness(&architect, now).ready,
                "an ordinary future respawn must retain its native delay");
        assert!(next_spawn(&mut architect, now).is_none());
    }

    #[test]
    fn first_maintenance_and_actual_population_deficit_are_required() {
        let now = TimeOfDay(32400.0);
        let mut architect = PopulationArchitect::default();
        assert!(!settlement_population_readiness(&architect, now).ready);
        architect.last_maintenance_tick = Some(32);
        architect.wanted_population.add(TrackedPopulation::OtherTownNpcs, 1);
        assert_eq!(settlement_population_readiness(&architect, now).allocated_deficit, 1);
        assert!(!settlement_population_readiness(&architect, now).ready);
        let entry = death(now.0 - MIN_SPAWN_DELAY);
        architect.population.on_spawn(&entry);
        architect.deaths.push_front(entry);
        architect.population.on_spawn(&death(0.0)); // Existing excess-population discard.
        assert!(next_spawn(&mut architect, now).is_none());
        assert_eq!(architect.population.total(), 1);
        assert!(settlement_population_readiness(&architect, now).ready);
    }
}

/// Opt-in owner evidence can distinguish allocated counts from actual spawning.
/// This says nothing about a particular NPC's later footing or interaction range.
#[derive(serde::Serialize)]
pub struct SettlementPopulationReadiness {
    pub last_maintenance_tick: Option<u64>,
    pub allocated_deficit: u32,
    pub eligible_pending: usize,
    pub ready: bool,
}

pub fn settlement_population_readiness(
    architect: &crate::data::architect::Architect, now: TimeOfDay,
) -> SettlementPopulationReadiness {
    let settlement = |kind: &TrackedPopulation| matches!(kind, TrackedPopulation::Adventurers
        | TrackedPopulation::Merchants | TrackedPopulation::Guards
        | TrackedPopulation::Captains | TrackedPopulation::OtherTownNpcs);
    let allocated_deficit = architect.population.iter().zip(architect.wanted_population.iter())
        .filter(|((kind, _), _)| settlement(kind))
        .map(|((_, current), (_, wanted))| wanted.saturating_sub(current)).sum();
    let eligible_pending = architect.deaths.iter().filter(|death|
        settlement(&TrackedPopulation::from_body_and_role(&death.body, &death.role))
        && !waiting_for_respawn(death, now)).count();
    SettlementPopulationReadiness {
        last_maintenance_tick: architect.last_maintenance_tick,
        allocated_deficit, eligible_pending,
        ready: architect.last_maintenance_tick.is_some() && allocated_deficit == 0 && eligible_pending == 0,
    }
}

fn randomize_body(body: Body, rng: &mut impl RngExt) -> Body {
    let mut random_humanoid = || {
        let species = comp::humanoid::ALL_SPECIES.choose(rng).unwrap();
        Body::Humanoid(comp::humanoid::Body::random_with(rng, species))
    };
    match body {
        Body::Humanoid(_) => random_humanoid(),
        body => body,
    }
}

fn role_personality(rng: &mut impl RngExt, role: &Role) -> Personality {
    match role {
        Role::Civilised(profession) => match profession {
            Some(Profession::Guard | Profession::Merchant | Profession::Captain) => {
                Personality::random_good(rng)
            },
            Some(Profession::Cultist | Profession::Pirate(_)) => Personality::random_evil(rng),
            None
            | Some(
                Profession::Farmer
                | Profession::Chef
                | Profession::Hunter
                | Profession::Blacksmith
                | Profession::Alchemist
                | Profession::Herbalist
                | Profession::Adventurer(_),
            ) => Personality::random(rng),
        },
        Role::Wild => Personality::random(rng),
        Role::Monster => Personality::random_evil(rng),
        Role::Vehicle => Personality::default(),
    }
}

fn spawn_anywhere(
    data: &mut Data,
    world: &World,
    death: &Death,
    rng: &mut impl RngExt,
    body: Body,
    personality: Personality,
) {
    let mut attempt = |check: bool| {
        let cpos = world
            .sim()
            .map_size_lg()
            .chunks()
            .map(|s| rng.random_range(0..s as i32));

        // TODO: If we had access to `ChunkStates` here we could make sure
        // these aren't getting respawned in loaded chunks.
        if let Some(chunk) = world.sim().get(cpos)
            && (!check || !chunk.is_underwater())
        {
            let wpos = cpos.cpos_to_wpos_center();
            let wpos = wpos.as_().with_z(world.sim().get_surface_alt_approx(wpos));

            data.spawn_npc(
                Npc::new(rng.random(), wpos, body, death.role.clone())
                    .with_personality(personality),
            );
            return true;
        }

        false
    };
    for _ in 0..RESPAWN_ATTEMPTS {
        if attempt(true) {
            return;
        }
    }
    attempt(false);
}

fn spawn_at_plot(
    data: &mut Data,
    world: &World,
    index: IndexRef,
    death: &Death,
    rng: &mut impl RngExt,
    body: Body,
    personality: Personality,
    match_plot: impl Fn(&Data, common::rtsim::SiteId, &world::site::Plot) -> bool,
) -> bool {
    let sites = &index.sites;
    let data_ref = &*data;
    let match_plot = &match_plot;
    if let Some((id, site, plot)) = data
        .sites
        .iter()
        .filter(|(_, site)| !site.is_loaded())
        .filter_map(|(id, site)| Some((id, site.world_site?)))
        .flat_map(|(id, world_site)| {
            let world_site = sites.get(world_site);
            world_site
                .filter_plots(move |plot| match_plot(data_ref, id, plot))
                .map(move |plot| (id, world_site, plot))
        })
        .choose(rng)
    {
        let wpos = site.tile_center_wpos(plot.root_tile());
        let wpos = wpos
            .as_()
            .with_z(world.sim().get_alt_approx(wpos).unwrap_or(0.0));
        let mut npc = Npc::new(rng.random(), wpos, body, death.role.clone())
            .with_personality(personality)
            .with_home(id);
        if let Some(faction) = data.sites[id].faction {
            npc = npc.with_faction(faction);
        }
        data.spawn_npc(npc);

        true
    } else {
        false
    }
}

fn spawn_profession(
    data: &mut Data,
    world: &World,
    index: IndexRef,
    death: &Death,
    rng: &mut impl RngExt,
    body: Body,
    personality: Personality,
    profession: Option<Profession>,
) -> bool {
    match profession {
        Some(Profession::Pirate(captain)) => {
            spawn_at_plot(
                data,
                world,
                index,
                death,
                rng,
                body,
                personality,
                |data, s, p| {
                    // Don't spawn multiple captains at the same site.
                    if captain
                        && data.sites[s].population.iter().any(|npc| {
                            data.npcs.get(*npc).is_some_and(|npc| {
                                matches!(npc.profession(), Some(Profession::Pirate(true)))
                            })
                        })
                    {
                        return false;
                    }
                    matches!(p.kind(), world::site::PlotKind::PirateHideout(_))
                },
            )
        },
        _ => spawn_at_plot(
            data,
            world,
            index,
            death,
            rng,
            body,
            personality,
            |_, _, p| {
                matches!(
                    p.kind().meta(),
                    Some(world::site::plot::PlotKindMeta::House { .. })
                )
            },
        ),
    }
}

fn spawn_npc(data: &mut Data, world: &World, index: IndexRef, death: &Death) -> bool {
    let mut rng = rng();
    let body = randomize_body(death.body, &mut rng);
    let personality = role_personality(&mut rng, &death.role);
    // First try and respawn in the same faction.
    let did_spawn = if let Some(faction_id) = death.faction
        && data.factions.get(faction_id).is_some()
    {
        if let Some((id, site)) = data
            .sites
            .iter()
            .filter(|(_, site)| site.faction == Some(faction_id) && !site.is_loaded())
            .choose(&mut rng)
        {
            let wpos = site.wpos;
            let wpos = wpos
                .as_()
                .with_z(world.sim().get_alt_approx(wpos).unwrap_or(0.0));
            data.spawn_npc(
                Npc::new(rng.random(), wpos, body, death.role.clone())
                    .with_personality(personality)
                    .with_home(id)
                    .with_faction(faction_id),
            );

            true
        } else {
            false
        }
    } else {
        match &death.role {
            Role::Civilised(profession) => spawn_profession(
                data,
                world,
                index,
                death,
                &mut rng,
                body,
                personality,
                *profession,
            ),
            Role::Wild => {
                let site_filter: fn(&SiteKind) -> bool = match body {
                    Body::BirdLarge(body) => match body.species {
                        comp::bird_large::Species::Phoenix => {
                            |site| matches!(site, SiteKind::DwarvenMine)
                        },
                        comp::bird_large::Species::Cockatrice => {
                            |site| matches!(site, SiteKind::Myrmidon)
                        },
                        comp::bird_large::Species::Roc => |site| matches!(site, SiteKind::Haniwa),
                        comp::bird_large::Species::FlameWyvern => {
                            |site| matches!(site, SiteKind::Terracotta)
                        },
                        comp::bird_large::Species::CloudWyvern => {
                            |site| matches!(site, SiteKind::Sahagin)
                        },
                        comp::bird_large::Species::FrostWyvern => {
                            |site| matches!(site, SiteKind::Adlet)
                        },
                        comp::bird_large::Species::SeaWyvern => {
                            |site| matches!(site, SiteKind::ChapelSite)
                        },
                        comp::bird_large::Species::WealdWyvern => {
                            |site| matches!(site, SiteKind::GiantTree)
                        },
                    },
                    _ => |_| true,
                };

                if let Some((id, site)) = data
                    .sites
                    .iter()
                    .filter(|(_, site)| {
                        !site.is_loaded()
                            && site
                                .world_site
                                .and_then(|s| index.sites.get(s).kind)
                                .is_some_and(|s| site_filter(&s))
                    })
                    .choose(&mut rng)
                {
                    let wpos = site.wpos;
                    let wpos = wpos
                        .as_()
                        .with_z(world.sim().get_alt_approx(wpos).unwrap_or(0.0));
                    data.spawn_npc(
                        Npc::new(rng.random(), wpos, body, death.role.clone())
                            .with_personality(personality)
                            .with_home(id),
                    );
                    true
                } else {
                    false
                }
            },
            Role::Monster => {
                let chunk_filter: fn(&SimChunk) -> bool = match body {
                    Body::BipedLarge(body) => match body.species {
                        comp::biped_large::Species::Tursus
                        | comp::biped_large::Species::Gigasfrost
                        | comp::biped_large::Species::Wendigo => {
                            |chunk| !chunk.is_underwater() && chunk.temp < CONFIG.snow_temp
                        },
                        comp::biped_large::Species::Gigasfire => |chunk| {
                            !chunk.is_underwater()
                                && chunk.temp > CONFIG.desert_temp
                                && chunk.humidity < CONFIG.desert_hum
                        },
                        comp::biped_large::Species::Mountaintroll => {
                            |chunk| !chunk.is_underwater() && chunk.alt > 500.0
                        },
                        comp::biped_large::Species::Swamptroll => {
                            |chunk| !chunk.is_underwater() && chunk.humidity > CONFIG.jungle_hum
                        },
                        _ => |chunk| !chunk.is_underwater(),
                    },
                    Body::Arthropod(_)
                    | Body::Humanoid(_)
                    | Body::QuadrupedSmall(_)
                    | Body::BipedSmall(_)
                    | Body::QuadrupedMedium(_)
                    | Body::Golem(_)
                    | Body::Theropod(_)
                    | Body::QuadrupedLow(_) => |chunk| !chunk.is_underwater(),
                    Body::Dragon(_) | Body::BirdLarge(_) | Body::BirdMedium(_) => |_| true,
                    Body::Crustacean(_) | Body::FishSmall(_) | Body::FishMedium(_) => {
                        |chunk| chunk.is_underwater()
                    },
                    Body::Object(_) | Body::Ship(_) | Body::Item(_) | Body::Plugin(_) => |_| true,
                };

                for _ in 0..RESPAWN_ATTEMPTS {
                    let cpos = world
                        .sim()
                        .map_size_lg()
                        .chunks()
                        .map(|s| rng.random_range(0..s as i32));

                    // TODO: If we had access to `ChunkStates` here we could make sure
                    // these aren't getting respawned in loaded chunks.
                    if let Some(chunk) = world.sim().get(cpos)
                        && chunk_filter(chunk)
                    {
                        let wpos = cpos.cpos_to_wpos_center();
                        let wpos = wpos.as_().with_z(world.sim().get_surface_alt_approx(wpos));

                        data.spawn_npc(
                            Npc::new(rng.random(), wpos, body, death.role.clone())
                                .with_personality(personality),
                        );
                        return true;
                    }
                }

                false
            },
            Role::Vehicle => {
                // Vehicles don't die as of now.
                unimplemented!()
            },
        }
    };

    // If enough time has passed, try spawning anyway.
    if !did_spawn && death.time.0 + MIN_SPAWN_DELAY * 5.0 < data.time_of_day.0 {
        match death.role {
            Role::Civilised(profession) => {
                if !spawn_profession(
                    data,
                    world,
                    index,
                    death,
                    &mut rng,
                    body,
                    personality,
                    profession,
                ) {
                    spawn_anywhere(data, world, death, &mut rng, body, personality)
                }
            },
            Role::Wild | Role::Monster => {
                spawn_anywhere(data, world, death, &mut rng, body, personality)
            },
            Role::Vehicle => {
                // Vehicles don't die as of now.
                unimplemented!()
            },
        }

        true
    } else {
        did_spawn
    }
}
