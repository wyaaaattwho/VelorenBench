//! Initial physics-time synchronization, separate from TimeOfDay/ProgramTime.
//!
//! GameSync initializes TimeOfDay but does not contain physics Time. Until the
//! first ServerGeneral::TimeOfDay packet (which DOES contain Time), the client's
//! Time starts at zero and cannot interpret server-authored buff timestamps.

use common::resources::Time;
use specs::{World, WorldExt};

#[derive(Default)]
struct ServerTimeSync {
    received: bool,
}

pub(crate) fn initialize(ecs: &mut World) { ecs.insert(ServerTimeSync::default()); }

/// Handle the physics-time part of the existing server synchronization packet.
/// Establish the epoch exactly on the first packet, even for a small or negative
/// offset. Subsequent updates retain the client's existing correction policy.
pub(crate) fn apply_server_time(ecs: &World, new_time: Time) -> f64 {
    let mut sync = ecs.write_resource::<ServerTimeSync>();
    if !new_time.0.is_finite() {
        sync.received = false;
        return 1.0;
    }
    let mut time = ecs.write_resource::<Time>();
    let first = !sync.received;
    sync.received = true;
    if first || new_time.0 > time.0 + 5.0 {
        *time = new_time;
        1.0
    } else if new_time.0 > time.0 {
        1.01
    } else {
        0.99
    }
}

/// The same evolving physics clock used by the native UI, but only after its
/// epoch has been initialized from a server packet. Unknown is not inactive.
pub(crate) fn synchronized_time(ecs: &World) -> Option<f64> {
    if !ecs.try_fetch::<ServerTimeSync>()?.received {
        return None;
    }
    let now = ecs.read_resource::<Time>().0;
    now.is_finite().then_some(now)
}

#[cfg(test)]
mod tests {
    use super::*;
    use common::resources::{ProgramTime, TimeOfDay};

    #[test]
    fn physics_time_sync_requires_packet_and_initializes_independent_epochs() {
        for (local, server) in [(0.872119747, 40.0), (1.0, 3.0), (45.0, 2.0)] {
            let mut ecs = World::new();
            ecs.insert(Time(local));
            ecs.insert(TimeOfDay(70_000.0));
            ecs.insert(ProgramTime(9_000.0));
            initialize(&mut ecs);
            assert_eq!(synchronized_time(&ecs), None);
            assert_eq!(apply_server_time(&ecs, Time(server)), 1.0);
            assert_eq!(synchronized_time(&ecs), Some(server));
            assert_eq!(ecs.read_resource::<TimeOfDay>().0, 70_000.0);
            assert_eq!(ecs.read_resource::<ProgramTime>().0, 9_000.0);
            // Elapsed client physics time, not wall/day time, drives the UI.
            ecs.write_resource::<Time>().0 += 0.25;
            ecs.write_resource::<ProgramTime>().0 += 5.0;
            assert_eq!(synchronized_time(&ecs), Some(server + 0.25));
        }
    }

    #[test]
    fn physics_time_sync_preserves_subsequent_native_correction_policy() {
        let mut ecs = World::new();
        ecs.insert(Time(0.0));
        initialize(&mut ecs);
        apply_server_time(&ecs, Time(40.0));
        assert_eq!(apply_server_time(&ecs, Time(40.1)), 1.01);
        assert_eq!(synchronized_time(&ecs), Some(40.0));
        assert_eq!(apply_server_time(&ecs, Time(39.9)), 0.99);
        assert_eq!(synchronized_time(&ecs), Some(40.0));
        assert_eq!(apply_server_time(&ecs, Time(46.0)), 1.0);
        assert_eq!(synchronized_time(&ecs), Some(46.0));
        assert_eq!(apply_server_time(&ecs, Time(f64::NAN)), 1.0);
        assert_eq!(synchronized_time(&ecs), None);
    }

    #[test]
    fn physics_time_sync_is_wired_to_real_client_initialization_and_packet_handler() {
        // Guard the real wiring in addition to the runtime ECS/packet replay.
        // This test does not construct Client::new (which would connect).
        let source = include_str!("lib.rs");
        assert!(source.contains("time_sync::initialize(state.ecs_mut());"));
        let packet_arm = source.split_once(
            "ServerGeneral::TimeOfDay(time_of_day, calendar, new_time, time_scale) => {",
        ).unwrap().1.split_once("ServerGeneral::EntitySync").unwrap().0;
        assert!(packet_arm.contains(
            "self.dt_adjustment = time_sync::apply_server_time(self.state.ecs(), new_time);",
        ));
        assert!(!packet_arm.contains("write_resource::<Time>"));
    }
}
