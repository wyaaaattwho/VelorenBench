"""Deliberately inventory successor native edits; never change historical pins."""
import argparse
import json

from native_source import ROOT, EXTERNAL_RESOURCE, inventory, sha, tree_digest, write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", required=True)
    args = parser.parse_args()
    manifest = ROOT / "native/source.json"
    value = json.loads(manifest.read_text())
    baseline = value.get("successor_baseline", {
        "manifest_sha256": sha(manifest), "source_tree_sha256": value["source_tree_sha256"],
        "distribution": "v1.0.1", "commit": "2e3aa071417a31173726b785689eeb6ec0548273"})
    files = inventory(ROOT / "native/source")
    files.setdefault(EXTERNAL_RESOURCE, value["files_sha256"][EXTERNAL_RESOURCE])
    value.update(files_sha256=files, source_tree_sha256=tree_digest(files),
                 successor_baseline=baseline,
                 successor_status="unreleased modified source; no Git commit or native qualification inferred")
    write(manifest, value)
    write(ROOT / "native/selection.json", {
        "schema": "velorenbench-native-selection/1",
        "source_manifest_sha256": sha(manifest), "source_tree_sha256": tree_digest(files),
        "distribution": "1.1.0", "release_commit": None,
        "baseline": baseline})
    contract_path = ROOT / "native/frame-contract.json"
    contract = json.loads(contract_path.read_text())
    features = json.loads((ROOT / "harness/scripts/rpg_benchmark/native_features.json").read_text())
    contract["benchmark_features"] = features["renderer"]
    contract["implementation_revision"] = features["implementation_revision"]
    contract["visual_ui"].update(
        gestures=["hover", "left_click", "right_click", "middle_click", "interact", "context_trade",
                  "drag", "scroll_up", "scroll_down"],
        unsupported=["hidden target addressing", "automatic task solutions"],
        qualification="successor implementation only; real native task/profile checks pending")
    contract["ui_support"]["visible_item_slot_target_dialogue_trade_address_tokens"] = (
        "native HUD hit tests; canonical operations remain private; dispatch is not an effect")
    contract["visual_ui"]["arguments"] = ["action_id", "frame_id", "ui_state_id", "expected_session_id",
        "actuator", "gesture", "x", "y", "to_x", "to_y", "modifiers"]
    contract["fixture_authoring"] = {
        "audience": "private pre-measurement fixture administrator, not a public player action",
        "command": "fixture_authoring OWNER begin | move X Y Z | end",
        "admission": "server Admin, self target, enabled assigned CanBuild for begin/move",
        "ownership": "one owner; no replay or takeover; owned end restores captured position/velocity",
        "physics": "scoped position/zero-velocity hold; not grounded or support evidence",
        "terrain": "actual world bounds and resident client chunk required; synthetic defaults rejected",
        "confirmation": "exact client terrain readback; server_mutation_confirmed remains null",
        "python_cleanup": "release own hold, restore enabled flag, revoke/remove only own build area",
    }
    write(contract_path, contract)
    print(json.dumps({"manifest_sha256": sha(manifest), "source_tree_sha256": tree_digest(files)}))


if __name__ == "__main__":
    main()
