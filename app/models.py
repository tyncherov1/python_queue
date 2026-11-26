from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class Student:
    id: int
    full_name: str
    telegram_id: int | None
    username: str | None
    is_admin: bool = False
    last_joined_at: float | None = None
    last_served_at: float | None = None
    last_left_at: float | None = None
    visits_count: int = 0
    active_in_roster: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "full_name": self.full_name,
            "telegram_id": self.telegram_id,
            "username": self.username,
            "is_admin": self.is_admin,
            "visits_count": self.visits_count,
        }


@dataclass
class QueueEntry:
    id: int
    student_id: int | None
    display_name: str
    telegram_id: int | None
    status: Literal["waiting", "active", "done", "left", "skipped", "removed"]
    position: int | None
    joined_at: float
    started_at: float | None = None
    finished_at: float | None = None
    added_by_admin: bool = False
    visits_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "student_id": self.student_id,
            "display_name": self.display_name,
            "status": self.status,
            "position": self.position,
            "joined_at": self.joined_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "visits_count": self.visits_count,
        }


@dataclass
class ActionResult:
    ok: bool
    message: str
    entry: QueueEntry | None = None
    position: int | None = None
    after_name: str | None = None
    after_is_active: bool = False
    cooldown_remaining_sec: int = 0
    extra: dict[str, Any] = field(default_factory=dict)
