//! Bounded loaded-scene door discovery for the ordinary observation.
use crate::Client;
use common::{terrain::SpriteKind, vol::ReadVol};
use serde_json::{Value, json};
use vek::{Aabb, Vec3};

const RADIUS_XY: i32 = 24;
const BELOW: i32 = 4;
const ABOVE: i32 = 5;
const MAX_DOORS: usize = 32;

fn summarize(center: Vec3<i32>, candidates: impl IntoIterator<Item = (Vec3<i32>, SpriteKind)>) -> Value {
    let mut doors: Vec<_> = candidates.into_iter()
        .filter(|(voxel, sprite)| {
            sprite.is_door()
                && (voxel.x - center.x).abs() <= RADIUS_XY
                && (voxel.y - center.y).abs() <= RADIUS_XY
                && voxel.z >= center.z - BELOW && voxel.z <= center.z + ABOVE
        })
        .collect();
    doors.sort_by_key(|(voxel, _)| {
        let delta = *voxel - center;
        (i64::from(delta.x) * i64::from(delta.x)
            + i64::from(delta.y) * i64::from(delta.y)
            + i64::from(delta.z) * i64::from(delta.z),
         voxel.x, voxel.y, voxel.z)
    });
    let total = doors.len();
    json!({
        "doors": doors.into_iter().take(MAX_DOORS).map(|(voxel, sprite)| json!({
            "position": [voxel.x, voxel.y, voxel.z], "sprite": format!("{sprite:?}"),
        })).collect::<Vec<_>>(),
        "total": total, "omitted": total.saturating_sub(MAX_DOORS),
        "radius_xy": RADIUS_XY, "vertical_range_relative": [-BELOW, ABOVE],
        "coordinate_frame": "native_xyz_voxels",
        "scope": "only loaded scene voxels inside bounded scan; unloaded, other heights and outside radius unknown; door sprites are not collision or walkability certification",
    })
}

pub(super) fn observe(client: &Client, position: Vec3<f32>) -> Value {
    let center = position.map(|value| value.floor() as i32);
    let mut candidates = Vec::new();
    client.state().terrain().for_each_in(
        Aabb {
            min: center - Vec3::new(RADIUS_XY, RADIUS_XY, BELOW),
            max: center + Vec3::new(RADIUS_XY, RADIUS_XY, ABOVE + 1),
        },
        |voxel, block| {
            if let Some(sprite) = block.get_sprite()
                && sprite.is_door()
            {
                candidates.push((voxel, sprite));
            }
        },
    );
    summarize(center, candidates)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn actual_native_door_predicate_discovers_unsampled_door_and_reports_bounds() {
        let candidates = [
            (Vec3::new(18, 0, 2), SpriteKind::DoorDark),
            (Vec3::new(1, 0, 2), SpriteKind::KeyDoor),
            (Vec3::new(30, 0, 2), SpriteKind::Door),
        ];
        let result = summarize(Vec3::new(0, 0, 2), candidates);
        assert_eq!(result["total"], 1);
        assert_eq!(result["doors"][0]["position"], json!([18, 0, 2]));
        assert_eq!(result["doors"][0]["sprite"], "DoorDark");
        assert_eq!(result["omitted"], 0);
        assert_eq!(result["radius_xy"], RADIUS_XY);
    }
}
