//! On-demand views of the connected normal client's public map, never terrain queries.
use crate::Client;
use common::{grid::Grid, terrain::TerrainChunkSize, vol::RectVolSize};
use common_net::msg::world_msg::PoiKind;
use serde::Deserialize;
use serde_json::{Value, json};
use vek::Vec2;

const MAX_SIDE: usize = 48;
const MARKER_PAGE: usize = 16;
const SYMBOLS: &[u8] = b"0123456789abcdefghijklmnopqrstuv";
type Error = (&'static str, String);

#[derive(Default, Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    // Native horizontal block coordinates, min inclusive / max exclusive.
    bounds: Option<[f64; 4]>,
    cell_stride: Option<u32>,
    #[serde(default)]
    marker_offset: usize,
}

pub(super) fn capability() -> Value {
    json!({
        "schema_version": "1.0", "read_only": true, "normal_player_map": true,
        "max_raster_side": MAX_SIDE, "marker_page_size": MARKER_PAGE,
        "land_height_bands": "lod_alt_quantized_32_min_max_v1",
    })
}

// Matches WorldData.lod_base's little-endian RGB and client/lib.rs visual water
// predicate. Alpha is deliberately ignored by the native map renderer too.
fn classify(word: u32) -> u8 {
    let [r, g, b, _a] = word.to_le_bytes();
    match [r, g, b] {
        [55, 41, 35] => 4,
        [128, 128, 128] => 8,
        _ if r == 0 && b > 102 && g < 77 => 2,
        _ => 1,
    }
}

#[cfg(test)]
fn raster(grid: &Grid<u32>, dims: Vec2<i32>, args: &Value) -> Result<Value, Error> {
    raster_with_height(grid, None, dims, args)
}

fn raster_with_height(
    grid: &Grid<u32>, height: Option<(&Grid<u32>, f32, f32)>,
    dims: Vec2<i32>, args: &Value,
) -> Result<Value, Error> {
    let request: Request = serde_json::from_value(args.clone())
        .map_err(|e| ("invalid_request", e.to_string()))?;
    if dims.x <= 0 || dims.y <= 0 {
        return Err(("map_unavailable", "Normal world map has no extent".into()));
    }
    let scale = TerrainChunkSize::RECT_SIZE.map(f64::from);
    let extent = [0.0, 0.0, f64::from(dims.x) * scale.x, f64::from(dims.y) * scale.y];
    let bounds = request.bounds.unwrap_or(extent);
    if !bounds.iter().all(|n| n.is_finite()) || bounds[0] >= bounds[2] || bounds[1] >= bounds[3] {
        return Err(("invalid_request", "bounds must be finite [min_x,min_y,max_x,max_y] with positive area".into()));
    }
    let clipped = [bounds[0].max(0.0), bounds[1].max(0.0),
                   bounds[2].min(extent[2]), bounds[3].min(extent[3])];
    if clipped[0] >= clipped[2] || clipped[1] >= clipped[3] {
        return Err(("map_out_of_bounds", "Requested view does not intersect the normal map".into()));
    }
    let start = Vec2::new((clipped[0] / scale.x).floor() as i32, (clipped[1] / scale.y).floor() as i32);
    let end = Vec2::new((clipped[2] / scale.x).ceil() as i32, (clipped[3] / scale.y).ceil() as i32);
    let span = end - start;
    let automatic = (span.x.max(span.y) as usize).div_ceil(MAX_SIDE) as u32;
    let stride = request.cell_stride.unwrap_or(automatic);
    if stride == 0 || stride > 65535 {
        return Err(("invalid_request", "cell_stride must be an integer in 1..65535".into()));
    }
    let size = span.map(|n| (n as usize).div_ceil(stride as usize));
    if size.x > MAX_SIDE || size.y > MAX_SIDE {
        return Err(("map_view_too_large", format!(
            "View exceeds {MAX_SIDE}x{MAX_SIDE} symbols; crop bounds or use cell_stride >= {automatic}. No silent loss of requested resolution."
        )));
    }
    let mut rows = Vec::new();
    let mut height_rows = Vec::new();
    let mut used = [false; 32];
    let mut unknown = 0usize;
    for row in 0..size.y {
        let mut text = String::new();
        let mut heights = String::new();
        for col in 0..size.x {
            let lo = start + Vec2::new(col as i32, row as i32) * stride as i32;
            let hi = (lo + stride as i32).map2(end, i32::min);
            let mut mask = 0u8;
            let mut low = 31usize;
            let mut high = 0usize;
            let mut height_unknown = false;
            for y in lo.y..hi.y {
                for x in lo.x..hi.x {
                    let cell = Vec2::new(x, y);
                    mask |= grid.get(cell).map_or_else(|| {
                        unknown += 1;
                        16
                    }, |word| classify(*word));
                    if let Some((layer, _, _)) = height {
                        match (grid.get(cell), layer.get(cell)) {
                            (Some(color), Some(value)) if classify(*color) == 1 => {
                                let band = ((*value >> 3).min(8191) / 256) as usize;
                                low = low.min(band);
                                high = high.max(band);
                            },
                            _ => height_unknown = true,
                        }
                    }
                }
            }
            used[mask as usize] = true;
            text.push(SYMBOLS[mask as usize] as char);
            if height.is_some() {
                if height_unknown {
                    heights.push_str("??");
                } else {
                    heights.push(SYMBOLS[low] as char);
                    heights.push(SYMBOLS[high] as char);
                }
            }
        }
        rows.push(text);
        if height.is_some() {
            height_rows.push(heights);
        }
    }
    let mut legend = serde_json::Map::new();
    for mask in 1..32 {
        if used[mask] {
            let classes: Vec<_> = [(1, "land"), (2, "water"), (4, "road"), (8, "bridge"), (16, "unknown")]
                .into_iter().filter_map(|(bit, name)| (mask & bit != 0).then_some(name)).collect();
            legend.insert((SYMBOLS[mask] as char).to_string(), json!(classes.join("+")));
        }
    }
    let mut result = json!({
        "schema_version": "1.0",
        "source": "connected_client_world_data",
        "coordinate_frame": "native_xy",
        "world_bounds": extent,
        "requested_bounds": bounds,
        "coverage_bounds": [f64::from(start.x) * scale.x, f64::from(start.y) * scale.y,
                            f64::from(end.x) * scale.x, f64::from(end.y) * scale.y],
        "clipped_to_world": bounds != clipped,
        "bounds_semantics": "min inclusive, max exclusive; coverage snapped outward to native cells; last row/column may be narrower",
        "native_cell_blocks": [scale.x, scale.y],
        "cell_stride": stride,
        "symbol_blocks": [scale.x * f64::from(stride), scale.y * f64::from(stride)],
        "raster_size": [size.x, size.y],
        "rows": rows,
        "legend": legend,
        "orientation": "rows increase native +y, columns increase +x; unflipped lod_base[y*width+x]",
        "aggregation": "presence union of EVERY native cell, not majority/sample/connectivity; mixed symbols do not locate features within a tile. Crop and cell_stride=1 resolves to native map cells.",
        "map_complete": grid.size() == dims,
        "unknown_cells_in_view": unknown,
        "limits": "Visual map only, not collision, depth, slope or body walkability. Roads/bridges overlay terrain color: underlying water there is unknown. Land means other non-water RGB, not safe passage. Alpha ignored. No route computed.",
        "marker_offset": request.marker_offset,
    });
    if let Some((_, sea_level, height_span)) = height {
        result["height_rows"] = json!(height_rows);
        result["height_encoding"] = json!({
            "source": "connected_client_world_data.lod_alt",
            "scale": "each pair of 0-9a-v characters is the minimum/maximum 32-band land altitude in that raster cell; ?? means at least one unknown, water, road or bridge cell",
            "band": "floor((lod_alt >> 3) / 256), clamped to 0..31; land surface only; quantized, not cave height or walkability",
            "sea_level_z_m": sea_level, "height_span_m": height_span,
            "approximate_z_m": "sea_level_z_m + band * height_span_m / 32; band bounds are coarse, not a precise elevation",
        });
    }
    Ok(result)
}

fn add_markers(result: &mut Value, mut markers: Vec<Value>) {
    let b = result["coverage_bounds"].as_array().expect("raster bounds");
    let bounds: Vec<_> = b.iter().map(|v| v.as_f64().unwrap()).collect();
    markers.retain(|m| {
        let x = m["position"][0].as_f64().unwrap();
        let y = m["position"][1].as_f64().unwrap();
        x >= bounds[0] && y >= bounds[1] && x < bounds[2] && y < bounds[3]
    });
    // Towns first is a generic public-map directory, not a destination/corridor choice.
    markers.sort_by_key(|m| (m["kind"] != "Town", m.to_string()));
    let offset = result["marker_offset"].as_u64().unwrap() as usize;
    let total = markers.len();
    let page: Vec<_> = markers.into_iter().skip(offset).take(MARKER_PAGE).collect();
    let next = offset.saturating_add(page.len());
    result["markers"] = json!(page);
    result["markers_in_view"] = json!(total);
    result["next_marker_offset"] = if next < total { json!(next) } else { Value::Null };
    result["marker_scope"] = json!("normal map markers and peak/lake POIs in coverage_bounds; towns first, deterministic pages; positions horizontal only, altitude unknown; labels bounded to 256 UTF-8 bytes");
}

pub(super) fn inspect(client: &Client, state: &super::BridgeState, args: &Value) -> Result<Value, Error> {
    let world = client.world_data();
    let mut result = raster_with_height(
        &world.lod_base, Some((&world.lod_alt, world.min_chunk_alt(), world.max_chunk_alt())),
        world.chunk_size().map(i32::from), args)?;
    let mut markers: Vec<_> = client.markers().map(|m| {
        let p = super::marker_wpos_to_world_xy(m.wpos);
        let plain = m.label.as_ref().and_then(|label| label.as_plain());
        json!({
            "kind": format!("{:?}", m.kind),
            "name": plain.map(super::bounded_site_name),
            "label_truncated": plain.is_some_and(|s| s.len() > super::MAX_SITE_NAME_BYTES),
            "label_localization_unavailable": m.label.is_some() && plain.is_none(),
            "position": [p.x, p.y],
        })
    }).collect();
    markers.extend(state.interactions.markers.iter().filter_map(|receipt| {
        let marker: common::map::Marker = serde_json::from_value(receipt.get("marker")?.clone())
            .expect("dialogue ledger contains only validated native Marker values");
        Some(json!({
            "kind": marker.kind, "name": null, "label": marker.label,
            "label_text": marker.label.as_ref().map(super::native_interactions::text),
            "position": [marker.wpos.x, marker.wpos.y], "source": "received_dialogue_marker",
            "sender": receipt["sender"], "dialogue_id": receipt["dialogue_id"],
            "received_client_tick": receipt["received_client_tick"],
        }))
    }));
    markers.extend(client.pois().iter().map(|p| json!({
        "kind": match p.kind { PoiKind::Peak(_) => "Peak", PoiKind::Lake(_) => "Lake" },
        "name": super::bounded_site_name(&p.name),
        "label_truncated": p.name.len() > super::MAX_SITE_NAME_BYTES,
        "position": [p.wpos.x, p.wpos.y],
    })));
    add_markers(&mut result, markers);
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> Grid<u32> {
        Grid::from_raw(Vec2::new(3, 2), vec![
            u32::from_le_bytes([55, 41, 35, 0]), u32::from_le_bytes([0, 20, 150, 0]),
            u32::from_le_bytes([128, 128, 128, 0]), u32::from_le_bytes([90, 80, 70, 0]),
            u32::from_le_bytes([0, 76, 103, 255]), u32::from_le_bytes([0, 77, 103, 0]),
        ])
    }

    #[test]
    fn player_map_real_grid_orientation_alpha_palette_and_fine_crop() {
        let result = raster(&fixture(), Vec2::new(3, 2), &json!({"cell_stride": 1})).unwrap();
        assert_eq!(result["rows"], json!(["428", "121"]));
        assert_eq!(result["world_bounds"], json!([0.0, 0.0, 96.0, 64.0]));
        assert_eq!(result["map_complete"], true);
        assert_eq!(classify(u32::from_le_bytes([0, 0, 102, 0])), 1);
        let crop = raster(&fixture(), Vec2::new(3, 2),
            &json!({"bounds": [32, 0, 64, 64], "cell_stride": 1})).unwrap();
        assert_eq!(crop["rows"], json!(["2", "2"]));
    }

    #[test]
    fn actual_lod_altitude_is_aggregated_and_unknown_over_water_or_overlay() {
        let heights = Grid::from_raw(Vec2::new(3, 2), vec![
            0, 0, 0, 4096 << 3, 0, 8191 << 3,
        ]);
        let result = raster_with_height(
            &fixture(), Some((&heights, 100.0, 800.0)), Vec2::new(3, 2),
            &json!({"cell_stride": 1})).unwrap();
        assert_eq!(result["height_rows"], json!(["??????", "gg??vv"]));
        assert_eq!(result["height_encoding"]["sea_level_z_m"], 100.0);
        assert_eq!(result["height_encoding"]["height_span_m"], 800.0);
        let coarse = raster_with_height(
            &fixture(), Some((&heights, 100.0, 800.0)), Vec2::new(3, 2),
            &json!({"cell_stride": 2})).unwrap();
        assert_eq!(coarse["height_rows"], json!(["????"]));
    }

    #[test]
    fn player_map_union_keeps_single_water_and_roads_without_claiming_connections() {
        let result = raster(&fixture(), Vec2::new(3, 2), &json!({"cell_stride": 2})).unwrap();
        assert_eq!(result["rows"], json!(["79"]));
        assert_eq!(result["legend"]["7"], "land+water+road");
        assert_eq!(result["legend"]["9"], "land+bridge");
        let missing = raster(&fixture(), Vec2::new(4, 2), &json!({"cell_stride": 1})).unwrap();
        assert_eq!(missing["rows"], json!(["428g", "121g"]));
        assert_eq!(missing["unknown_cells_in_view"], 2);
        assert_eq!(missing["map_complete"], false);
    }

    #[test]
    fn player_map_invalid_outside_clipped_and_bounded_views() {
        for args in [json!({"bounds": [0, 0, 0, 32]}), json!({"bounds": [false, 0, 1, 1]}),
                     json!({"cell_stride": 0}), json!({"cell_stride": 1.5}),
                     json!({"marker_offset": -1}), json!({"route": true})] {
            assert_eq!(raster(&fixture(), Vec2::new(3, 2), &args).unwrap_err().0, "invalid_request");
        }
        assert_eq!(raster(&fixture(), Vec2::new(3, 2),
            &json!({"bounds": [-64, 0, -32, 32]})).unwrap_err().0, "map_out_of_bounds");
        let clipped = raster(&fixture(), Vec2::new(3, 2), &json!({"bounds": [-1, 1, 33, 35]})).unwrap();
        assert_eq!(clipped["clipped_to_world"], true);
        assert_eq!(clipped["coverage_bounds"], json!([0.0, 0.0, 64.0, 64.0]));
        let big = Grid::new(Vec2::new(1024, 1024), u32::from_le_bytes([55, 41, 35, 0]));
        let overview = raster(&big, big.size(), &json!({})).unwrap();
        assert_eq!(overview["cell_stride"], 22);
        assert_eq!(overview["raster_size"], json!([47, 47]));
        assert_eq!(raster(&big, big.size(), &json!({"cell_stride": 1})).unwrap_err().0, "map_view_too_large");
    }

    #[test]
    fn player_map_marker_scope_paging_and_no_fabricated_height() {
        let mut result = raster(&fixture(), Vec2::new(3, 2), &json!({})).unwrap();
        let markers = (0..35).map(|i| json!({"kind": "Town", "name": format!("Town{i}"),
                                           "position": [i, 12]})).chain([
            json!({"kind": "Lake", "name": "outside", "position": [300, 12]})
        ]).collect();
        add_markers(&mut result, markers);
        assert_eq!(result["markers_in_view"], 35);
        assert_eq!(result["markers"].as_array().unwrap().len(), 16);
        assert_eq!(result["next_marker_offset"], 16);
        assert_eq!(result["markers"][0]["position"].as_array().unwrap().len(), 2);
        println!("PLAYER_MAP_FIXTURE={result}");
    }
}
