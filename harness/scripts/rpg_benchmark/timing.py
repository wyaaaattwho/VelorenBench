from datetime import datetime, timezone
import math
import time


def stamp():
    return {"monotonic": time.monotonic(), "wall": datetime.now(timezone.utc).isoformat()}


class AttemptTiming:
    def __init__(self):
        self.points = {"attempt_start": stamp()}

    def mark(self, name, point=None):
        if name not in self.points:
            value = stamp() if point is None else point
            if (not isinstance(value, dict) or type(value.get("monotonic")) not in {int, float}
                    or not math.isfinite(value["monotonic"]) or not isinstance(value.get("wall"), str)):
                raise ValueError("Invalid lifecycle timestamp")
            datetime.fromisoformat(value["wall"])
            self.points[name] = value

    def finish(self, reason):
        self.mark("terminal")
        self.mark("cleanup_end")
        order = (
            "attempt_start", "fixture_setup_start", "setup_qualified", "agent_launch_start",
            "agent_launched", "harness_entry", "terminal", "native_success",
            "agent_final_stop", "cleanup_end",
        )
        previous = None
        for name in order:
            if name in self.points:
                current = self.points[name]["monotonic"]
                if not math.isfinite(current) or (previous is not None and current < previous):
                    raise ValueError("Lifecycle start/launch/entry/terminal/end timestamps are out of order")
                previous = current
        start = self.points["attempt_start"]["monotonic"]
        terminal = self.points["terminal"]["monotonic"]
        end = self.points["cleanup_end"]["monotonic"]
        launch = self.points.get("agent_launch_start")
        launched = self.points.get("agent_launched")
        entry = self.points.get("harness_entry")
        evaluator = self.points.get("evaluator_start")
        if ((launch is None and any(point is not None for point in (launched, entry, evaluator)))
                or (launched is None and any(point is not None for point in (entry, evaluator)))):
            raise ValueError("Lifecycle entry/evaluator timestamp lacks a preceding launch")
        if evaluator is not None and not launched["monotonic"] <= evaluator["monotonic"] <= terminal:
            raise ValueError("Evaluator start/launch/terminal timestamps are out of order")
        setup = self.points.get("setup_qualified", self.points["terminal"])["monotonic"]
        return {
            "points": self.points,
            "setup_elapsed_seconds": setup - start,
            "task_elapsed_seconds": terminal - launch["monotonic"] if launch else None,
            "task_duration_null_reason": None if launch else reason,
            "task_clock": "agent_launch_start_to_terminal",
            "harness_entry_elapsed_seconds": entry["monotonic"] - launch["monotonic"] if entry else None,
            "evaluator_start_offset_seconds": (
                evaluator["monotonic"] - launch["monotonic"] if evaluator else None),
            "attempt_elapsed_seconds": end - start,
            "cleanup_elapsed_seconds": end - terminal,
            "terminal_reason": reason,
        }
