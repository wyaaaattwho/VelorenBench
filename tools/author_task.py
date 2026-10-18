"""Create a separate content-addressable catalog with a data-only task variant."""
import argparse
import json
from pathlib import Path


def extend(base, example):
    value = json.loads(base.read_text())
    definition = json.loads(example.read_text())
    if definition["id"] in {row["id"] for row in value["scenes"]}:
        raise ValueError("example ID already present")
    value["scenes"].append(definition)
    return value


def main():
    from scripts.rpg_benchmark import suite
    parser = argparse.ArgumentParser(description=__doc__)
    package = Path(suite.__file__).parent
    parser.add_argument("--base", type=Path, default=package / "scenes.json")
    parser.add_argument("--example", type=Path, default=package / "examples/combat-variant.json")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    value = extend(args.base, args.example)
    with args.out.open("x") as stream:
        stream.write(json.dumps(value, indent=2) + "\n")
    definitions = suite.load(args.out)
    print(json.dumps({"manifest": str(args.out), "tasks": len(definitions),
                      "example": value["scenes"][-1]["id"], "qualification": "unqualified"}))


if __name__ == "__main__":
    main()
