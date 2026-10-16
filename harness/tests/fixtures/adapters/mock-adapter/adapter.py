import json
from pathlib import Path
from scripts.rpg_benchmark.external import Launch, EVIDENCE

ROOT = Path(__file__).resolve().parent


def build_launch(context):
    if not context["engineering"]:
        raise ValueError("Lightweight mock is engineering-only, not a research baseline")
    config = context["config"]
    if set(config) - {"failure"}:
        raise ValueError("Unknown mock option")
    if config.get("failure") == "prelaunch":
        raise RuntimeError("Explicit synthetic prelaunch failure")
    command = (context["python"], str(ROOT / "entry.py"), context["episode_socket"],
               context["output"], str(Path(context["common_interface"]).parents[1]))
    return Launch(command, ROOT, {"PYTHONDONTWRITEBYTECODE": "1"})


def collect_evidence(context):
    data = json.loads((Path(context["output"]) / "receipt.json").read_text())
    return {"schema": EVIDENCE, "adapter_id": context["adapter_id"],
            "player_interface": context["player_interface"], "entry": data["entry"],
            "entry_receipt_present": True,
            "model_requests": [{"model": context["model"], "effort": context["reasoning_effort"],
                                "synthetic": True}],
            "tool_calls": [], "submissions": [], "provider_errors": [],
            "owned_processes": data["owned_processes"], "cleanup_complete": data["cleanup_complete"]}
