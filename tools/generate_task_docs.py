"""Generate current catalog/support sections using the authoritative resolver."""
import argparse
from pathlib import Path

from scripts.rpg_benchmark import qualification, support

ROOT = Path(__file__).resolve().parents[1]
BEGIN = "<!-- BEGIN GENERATED SUCCESSOR -->"
END = "<!-- END GENERATED SUCCESSOR -->"


def sections():
    catalog = support.catalog()
    records = qualification.load()["records"]
    qualified = sum(row["status"] == "engineering-qualified" for row in records)
    tasks = [
        "## Task catalog (generated)",
        "",
        "The packaged scored catalog remains 29 definitions / 37 authored terrain cells.",
        "The separate arbitrary-ID example is not silently included.",
        "",
        "| Task | Family | Semantic template | Terrain | Requirements |",
        "| --- | --- | --- | --- | --- |",
    ]
    for d in catalog.values():
        tasks.append(f"| {d['id']} | {d['kind']} | {d['template']} | "
                     f"{', '.join(d['terrains'])} | {', '.join(d['requires'])} |")
    rows = [
        "## Support declarations and evidence (generated)",
        "",
        "Implementation declarations below are not real-native qualification.",
        f"The validated production registry contains {len(records)} records, {qualified} engineering-qualified.",
        "Those exact selected-native/code keys support ordinary bound admission; qualification",
        "does not assert model success or final release acceptance. T05/image remains task-engineering only.",
        "This source-only table has no selected native identity and cannot certify a binary.",
        "Use installed `support --config PATH --config-sha256 SHA` for exact bound readiness.",
        "Historical scored admission is audit-only, never inherited by a new selection.",
        "Unqualified cells require `--unqualified-engineering` and",
        "a selected native contract declaring the required features; capability mode cannot",
        "use this opt-in. Public build/assembly/binding is renderer-only, never a headless fallback.",
        "",
        "The successor intentionally defaults renderer/image to `visual-ui-v1`, including",
        "omitted CLI flags, ordinary Responses declarations and this matrix (one resolver).",
        "External adapters keep their declared defaults; effective negotiation precedes admission.",
        "Structured/hybrid default to `ordinary-renderer-native-v1`; legacy headless uses `raw-native-v2`.",
        "Image uses actual PNGs and `visual-ui-v1` for semantic HUD operations. Older",
        "native binaries without an explicit `middle_click` gesture capability cannot expose",
        "the middle-button map waypoint input (the ordinary HUD's M3 binding).",
        "Legacy `visual-motor-v1` retains its restricted controls. Map cursor labels are cartographic",
        "pixels, not structured coordinates or private goal overlays. Dispatch-only receipts",
        "never assert a craft/trade/skill/inventory effect.",
        "",
        "| Task/terrain | Structured implementation | Hybrid implementation | Image implementation (visual-ui-v1) | Legacy image implementation (visual-motor-v1) | Historical scored modes | Successor native qualification |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for d in catalog.values():
        for terrain in d["terrains"]:
            cells = [support.resolve(d, terrain, "renderer", mode)
                for mode in support.MODES]
            motor = support.resolve(d, terrain, "renderer", "image", "visual-motor-v1")
            legacy = [mode for mode in support.MODES
                      if support.historical_cell(d, terrain, mode).get("scored_evaluation")]
            rows.append(f"| {d['id']}/{terrain} | "
                        + " | ".join("declared" if c["implemented"] else c["reason"] for c in cells)
                        + " | " + ("declared" if motor["implemented"] else motor["reason"])
                        + f" | {', '.join(legacy) or 'none'} | "
                        + ", ".join(c["integration_qualification"] for c in cells) + " (source-only) |")
    return {"TASKS.md": "\n".join(tasks), "SUPPORT.md": "\n".join(rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true")
    group.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for name, section in sections().items():
        path = ROOT / name
        text = path.read_text()
        prefix = text.split(BEGIN)[0].rstrip()
        expected = prefix + "\n\n" + BEGIN + "\n" + section + "\n" + END + "\n"
        if args.write:
            path.write_text(expected)
        elif text != expected:
            raise SystemExit(name + " differs; regenerate deliberately")
    print("catalog/support documentation " + ("written" if args.write else "verified"))


if __name__ == "__main__":
    main()
