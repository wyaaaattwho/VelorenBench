use crate::utils;
use common::comp::{FixtureAuthoring, Pos, Vel};
use specs::WorldExt;
use std::time::Duration;
use vek::Vec3;

#[test]
fn fixture_authoring_stays_fixed_across_delays_bootstrap_and_moves_then_releases() {
    let mut state = utils::setup(veloren_common_systems::add_local_systems);
    let administrator = utils::create_player(&mut state);
    let ordinary = utils::create_player(&mut state);
    let initial = Pos(Vec3::new(16.,16.,265.));
    let old_velocity = Vel(Vec3::new(1.,-2.,-40.));
    let hold = FixtureAuthoring {
        owner:"owned-fixture".into(), position:Pos(Vec3::new(16.5,16.5,501.8)),
        restore_position:initial, restore_velocity:old_velocity,
    };
    state.ecs_mut().write_storage::<FixtureAuthoring>().insert(administrator, hold.clone()).unwrap();
    state.ecs_mut().write_storage::<Vel>().insert(administrator, old_velocity).unwrap();
    state.ecs_mut().write_storage::<Pos>().insert(ordinary, hold.position).unwrap();
    for anchor in [hold.position, Pos(Vec3::new(32.5,16.5,601.8)), Pos(Vec3::new(16.5,32.5,501.8))] {
        state.ecs_mut().write_storage::<FixtureAuthoring>().get_mut(administrator).unwrap().position = anchor;
        for delay in [10,350,2000,500] {
            utils::tick(&mut state, Duration::from_millis(delay));
            let (actual, velocity, _) = utils::get_transform(&state, administrator).unwrap();
            assert_eq!(actual, anchor);
            assert_eq!(velocity, Vel::zero());
            assert!(state.ecs().read_storage::<common::comp::PhysicsState>()
                .get(administrator).unwrap().on_ground.is_none());
        }
    }
    // The existing physics for an unheld entity is not globally frozen.
    assert!(utils::get_transform(&state, ordinary).unwrap().0.0.z < hold.position.0.z);
    let released = state.ecs_mut().write_storage::<FixtureAuthoring>().remove(administrator).unwrap();
    state.ecs_mut().write_storage::<Pos>().insert(administrator, released.restore_position).unwrap();
    state.ecs_mut().write_storage::<Vel>().insert(administrator, released.restore_velocity).unwrap();
    assert_eq!(utils::get_transform(&state, administrator).unwrap().0, initial);
    assert_eq!(utils::get_transform(&state, administrator).unwrap().1, old_velocity);
    utils::tick(&mut state, Duration::from_millis(10));
    assert_ne!(utils::get_transform(&state, administrator).unwrap().1, Vel::zero());
}
