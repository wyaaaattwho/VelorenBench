"""No-world import/dependency check for the deprecated source GameAgent entry."""
import argparse
import importlib.metadata
from pathlib import Path
import re
import subprocess
import sys
import tomllib


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    source = args.source
    declared = tomllib.loads((source / "pyproject.toml").read_text())["project"]["dependencies"]
    missing = []
    for requirement in declared:
        name = re.split(r"[^A-Za-z0-9_.-]", requirement, maxsplit=1)[0]
        try:
            importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(requirement)
    if missing:
        raise SystemExit("Selected GameAgent interpreter lacks declared dependencies: "
                         + ", ".join(missing)
                         + ". Install the Agent's own resolved dependency manifest in this environment.")
    subprocess.run([sys.executable, "-B", str(source / "tools/run_veloren_agent.py"), "--help"],
                   check=True, stdout=subprocess.DEVNULL)
    print("Declared GameAgent dependencies present and actual entry imports; no world/model entered.")


if __name__ == "__main__":
    main()
