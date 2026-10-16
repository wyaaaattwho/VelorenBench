//! Read-only, goal-selected recipe graph from the connected client's manifest and book.
use super::*;
use std::collections::{BTreeSet, HashSet};
use std::hash::{DefaultHasher, Hash, Hasher};

const PAGE_MAX: usize = 32;
type Error = (&'static str, String);

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    output_item_id: String,
    #[serde(default)]
    offset: usize,
    #[serde(default = "default_page_size")]
    page_size: usize,
}

fn default_page_size() -> usize { PAGE_MAX }

pub(super) fn capability() -> Value {
    json!({"schema_version": "1.0", "read_only": true, "page_max": PAGE_MAX,
           "snapshot_token": "observed_recipe_book_manifest_inventory_hash_v1",
           "source": "connected_inventory_recipe_book_and_native_recipe_manifest"})
}

fn snapshot_token(rows: &[Value], inventory: &Value, known: &HashSet<&str>) -> String {
    let mut keys = known.iter().copied().collect::<Vec<_>>();
    keys.sort_unstable();
    let mut hasher = DefaultHasher::new();
    keys.hash(&mut hasher);
    serde_json::to_string(rows).unwrap().hash(&mut hasher);
    serde_json::to_string(inventory).unwrap().hash(&mut hasher);
    format!("{:016x}", hasher.finish())
}

fn selected_rows(goal: &str, all: Vec<Value>) -> Vec<Value> {
    let mut needed = vec![goal.to_owned()];
    let mut visited = BTreeSet::new();
    let mut selected = Vec::new();
    while let Some(output) = needed.pop() {
        if !visited.insert(output.clone()) { continue; }
        for row in all.iter().filter(|row| row["output"]["item"].as_str() == Some(output.as_str())) {
            for input in row["inputs"].as_array().into_iter().flatten() {
                if input["kind"] == "item" {
                    if let Some(item) = input["item"].as_str() {
                        needed.push(item.to_owned());
                    }
                }
            }
            selected.push(row.clone());
        }
    }
    selected.sort_by(|left, right| left["recipe"].as_str().cmp(&right["recipe"].as_str()));
    selected
}

fn page_rows(rows: &[Value], offset: usize, page_size: usize) -> Result<(&[Value], Option<usize>), Error> {
    if offset > rows.len() {
        return Err(("invalid_request", "Recipe page offset exceeds relevant records".into()));
    }
    let end = offset.saturating_add(page_size).min(rows.len());
    Ok((&rows[offset..end], (end < rows.len()).then_some(end)))
}

pub(super) fn query(client: &Client, args: &Value) -> Result<Value, Error> {
    let request: Request = serde_json::from_value(args.clone())
        .map_err(|error| ("invalid_request", error.to_string()))?;
    if request.output_item_id.is_empty() || request.output_item_id.len() > 512
        || request.page_size == 0 || request.page_size > PAGE_MAX
    {
        return Err(("invalid_request", "Invalid goal recipe ID or page size".into()));
    }
    let inventory = client.current::<comp::Inventory>()
        .ok_or(("inventory_unavailable", "Character inventory is not available".into()))?;
    let known: HashSet<&str> = inventory.recipes_iter().map(String::as_str).collect();
    let manifest = client.state().ecs().read_resource::<RecipeBookManifest>();
    let missing_manifest_keys = known.iter().filter(|key| manifest.get(key).is_none()).count();
    let all = manifest.iter().map(|(key, recipe)| {
        let character_knows = known.contains(key.as_str());
        let mut row = known_recipe_record(
            key, recipe.craft_sprite, recipe,
            character_knows.then_some(&inventory),
        ).value;
        row["character_known"] = json!(character_knows);
        row["knowledge_source"] = json!(if character_knows {
            "current_character_recipe_book"
        } else {
            "native_manifest_future_option_not_character_known"
        });
        for (input, (_, amount, modular_component)) in row["inputs"].as_array_mut().unwrap()
            .iter_mut().zip(recipe.inputs())
        {
            input["reusable_zero_amount_requires_presence"] = json!(amount == 0);
            input["modular_component"] = json!(modular_component);
        }
        row
    }).collect();
    let selected = selected_rows(&request.output_item_id, all);
    let (page, next_offset) = page_rows(&selected, request.offset, request.page_size)?;
    let inventory_counts = json!(inventory_snapshot(&inventory).definition_counts);
    let token = snapshot_token(&selected, &inventory_counts, &known);
    Ok(json!({
        "schema_version": "1.0",
        "output_item_id": request.output_item_id,
        "offset": request.offset,
        "page_size": request.page_size,
        "total_relevant_recipes": selected.len(),
        "next_offset": next_offset,
        "items": page,
        "inventory_counts": inventory_counts,
        "snapshot_token": token,
        "missing_known_manifest_keys": missing_manifest_keys,
        "complete_page": next_offset.is_none() && missing_manifest_keys == 0,
        "scope": "native_manifest_future_options_separate_from_character_known_and_inventory_satisfaction",
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(key: &str, output: &str, input: &str) -> Value {
        json!({"recipe": key, "output": {"item": output},
               "inputs": [{"kind": "item", "item": input}]})
    }

    #[test]
    fn goal_graph_keeps_alternatives_and_terminates_cycles() {
        let result = selected_rows("sword", vec![
            row("sword_from_plate", "sword", "plate"),
            row("plate_from_ore", "plate", "ore"),
            row("ore_from_sword", "ore", "sword"),
            row("sword_from_gem", "sword", "gem"),
            row("unrelated", "lantern", "wood"),
        ]);
        assert_eq!(result.iter().map(|r| r["recipe"].as_str().unwrap()).collect::<Vec<_>>(),
                   vec!["ore_from_sword", "plate_from_ore", "sword_from_gem", "sword_from_plate"]);
    }

    #[test]
    fn native_graph_pages_do_not_drop_or_repeat_alternatives() {
        let rows = selected_rows("sword", vec![
            row("sword_b", "sword", "ore"), row("sword_c", "sword", "gem"),
            row("sword_a", "sword", "plate"), row("unrelated", "torch", "wood"),
        ]);
        let (first, next) = page_rows(&rows, 0, 2).unwrap();
        assert_eq!(first.iter().map(|r| r["recipe"].as_str().unwrap()).collect::<Vec<_>>(),
                   vec!["sword_a", "sword_b"]);
        assert_eq!(next, Some(2));
        let (second, end) = page_rows(&rows, next.unwrap(), 2).unwrap();
        assert_eq!(second[0]["recipe"], "sword_c");
        assert_eq!(end, None);
        assert_eq!(page_rows(&rows, 4, 2).unwrap_err().0, "invalid_request");
    }

    #[test]
    fn recipe_snapshot_token_changes_with_inventory_knowledge_and_graph() {
        let rows = vec![row("sword", "sword", "ore")];
        let known = HashSet::from(["sword"]);
        let first = snapshot_token(&rows, &json!({"ore": 1}), &known);
        assert_eq!(first, snapshot_token(&rows, &json!({"ore": 1}), &known));
        assert_ne!(first, snapshot_token(&rows, &json!({"ore": 2}), &known));
        assert_ne!(first, snapshot_token(&rows, &json!({"ore": 1}), &HashSet::new()));
        assert_ne!(first, snapshot_token(&[row("sword", "sword", "wood")],
                                         &json!({"ore": 1}), &known));
    }
}
