from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


ActionStatus = Literal["queued", "running", "completed", "failed", "cancelled"]


@dataclass(frozen=True)
class ActionResult:
    action_id: str
    action_type: str
    status: ActionStatus
    started_tick: int
    finished_tick: int | None = None
    duplicate: bool = False
    detail: dict[str, Any] = field(default_factory=dict)
    observation: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ActionResult:
        return cls(
            action_id=str(value["action_id"]),
            action_type=str(value["action_type"]),
            status=value["status"],
            started_tick=int(value["started_tick"]),
            finished_tick=(
                int(value["finished_tick"])
                if value.get("finished_tick") is not None
                else None
            ),
            duplicate=bool(value.get("duplicate", False)),
            detail=dict(value.get("detail") or {}),
            observation=value.get("observation"),
        )


@dataclass(frozen=True)
class EventBatch:
    events: list[dict[str, Any]]
    oldest_seq: int
    next_seq: int
    truncated: bool = False
    has_more: bool = False

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> EventBatch:
        return cls(
            events=[dict(event) for event in value.get("events", [])],
            oldest_seq=int(value["oldest_seq"]),
            next_seq=int(value["next_seq"]),
            truncated=bool(value.get("truncated", False)),
            has_more=bool(value.get("has_more", False)),
        )
