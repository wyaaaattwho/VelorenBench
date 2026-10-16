import tempfile
import unittest
from pathlib import Path

from veloren_controller import (
    EventBatch,
    TelemetryCollector,
    read_telemetry_jsonl,
)


class FakeController:
    def __init__(self, events: list[dict] | None = None) -> None:
        self.session_id = "telemetry-test"
        self.is_running = True
        self.event_queue_drops = 0
        self.event_sequence_gaps = 0
        self._tick = 0
        self._events = events or []
        self.drained = 0

    def observe(self) -> dict:
        self._tick += 1
        return {
            "observation_schema_version": "1.1",
            "session_id": self.session_id,
            "tick": self._tick,
            "position": [float(self._tick), 2.0, 3.0],
        }

    def events_since(self, after_seq: int, *, limit: int = 256) -> EventBatch:
        oldest_seq = (
            int(self._events[0]["seq"]) if self._events else after_seq + 1
        )
        available = [
            dict(event) for event in self._events if int(event["seq"]) > after_seq
        ]
        return EventBatch(
            events=available[:limit],
            oldest_seq=oldest_seq,
            next_seq=(
                int(self._events[-1]["seq"]) + 1
                if self._events
                else after_seq + 1
            ),
            truncated=after_seq + 1 < oldest_seq,
            has_more=len(available) > limit,
        )

    def drain_event_queue(self, limit: int = 1_024) -> list[dict]:
        self.drained += 1
        return []


class TelemetryCollectorTest(unittest.TestCase):
    def test_sample_builds_bounded_snapshot(self) -> None:
        controller = FakeController(
            [
                {"type": "event", "event": "action_started", "seq": 1},
                {"type": "event", "event": "action_completed", "seq": 2},
            ]
        )
        collector = TelemetryCollector(controller, trajectory_limit=2)

        first = collector.sample_once()
        collector.sample_once()
        third = collector.sample_once()
        snapshot = collector.snapshot()

        self.assertEqual(first.sequence, 1)
        self.assertEqual(len(first.events), 2)
        self.assertEqual(third.sequence, 3)
        self.assertEqual(len(snapshot["trajectory"]), 2)
        self.assertEqual(snapshot["trajectory"][-1]["tick"], 3)
        self.assertEqual(controller.drained, 3)

    def test_event_gap_and_pagination_are_reported(self) -> None:
        controller = FakeController(
            [
                {"type": "event", "event": "first-retained", "seq": 4},
                {"type": "event", "event": "second-retained", "seq": 5},
            ]
        )
        collector = TelemetryCollector(controller, event_page_size=1)

        frame = collector.sample_once()

        self.assertEqual([event["seq"] for event in frame.events], [4, 5])
        self.assertEqual(
            frame.event_gap,
            {"requested_after": 0, "oldest_available": 4},
        )

    def test_message_queue_drops_oldest_frame(self) -> None:
        collector = TelemetryCollector(FakeController(), message_queue_limit=1)

        collector.sample_once()
        collector.sample_once()

        self.assertEqual(collector.dropped_messages, 1)
        self.assertEqual(collector.get_message(timeout=0)["sequence"], 2)

    def test_recording_and_replay(self) -> None:
        collector = TelemetryCollector(FakeController())
        with tempfile.TemporaryDirectory() as directory:
            recording = Path(directory) / "telemetry.jsonl"
            collector.start_recording(recording)
            collector.sample_once()
            collector.stop_recording()

            messages = list(read_telemetry_jsonl(recording))

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["type"], "telemetry_frame")
        self.assertEqual(messages[0]["observation"]["tick"], 1)

    def test_background_sampler_starts_and_stops(self) -> None:
        collector = TelemetryCollector(FakeController(), sample_hz=20)
        collector.start()
        message = collector.get_message(timeout=1)
        collector.stop()

        self.assertEqual(message["type"], "telemetry_frame")
        self.assertFalse(collector.is_running)

    def test_replay_reports_invalid_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recording = Path(directory) / "invalid.jsonl"
            recording.write_text("{}\nnot-json\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "line 2"):
                list(read_telemetry_jsonl(recording))


if __name__ == "__main__":
    unittest.main()
