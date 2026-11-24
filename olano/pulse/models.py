"""PULSE data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any


class PulseType(str, Enum):
    INSIGHT = "insight"
    OPPORTUNITY = "opportunity"
    QUESTION = "question"
    RECOMMENDATION = "recommendation"
    SUGGESTION = "suggestion"
    FINDING = "finding"
    PROPOSAL = "proposal"
    ALERT = "alert"
    REMINDER = "reminder"
    COST_ALERT = "cost_alert"


class PulseLane(str, Enum):
    PULSE = "pulse"
    OPS = "ops"
    APPROVAL = "approval"

    @classmethod
    def from_value(cls, value: PulseLane | str | None) -> PulseLane:
        if isinstance(value, cls):
            return value
        text = str(value or "").strip().lower()
        if text in {"ops", "operation", "operations"}:
            return cls.OPS
        if text in {"approval", "approvals", "review"}:
            return cls.APPROVAL
        return cls.PULSE


class PulsePriority(str, Enum):
    URGENT = "urgent"
    NORMAL = "normal"
    LOW = "low"

    @property
    def score(self) -> int:
        return {
            PulsePriority.URGENT: 90,
            PulsePriority.NORMAL: 60,
            PulsePriority.LOW: 30,
        }[self]

    @classmethod
    def from_value(cls, value: PulsePriority | str | int | None) -> PulsePriority:
        if isinstance(value, cls):
            return value
        if isinstance(value, int):
            if value >= 80:
                return cls.URGENT
            if value >= 40:
                return cls.NORMAL
            return cls.LOW
        text = str(value or "").strip().lower()
        if text in {"urgent", "high", "critical"}:
            return cls.URGENT
        if text in {"low", "later"}:
            return cls.LOW
        return cls.NORMAL


class PulseStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    DELIVERED = "delivered"


@dataclass
class PulsePreferences:
    """Parsed user preferences from PULSE.md."""

    delivery: str = "batch"
    cadence: str = ""
    max_items: int | None = None
    quiet_hours_start: str = ""
    quiet_hours_end: str = ""
    timezone: str = "UTC"
    interests: list[str] = field(default_factory=list)
    check_routines: list[str] = field(default_factory=list)
    delivery_preferences: list[str] = field(default_factory=list)
    excluded_topics: list[str] = field(default_factory=list)


@dataclass
class PulseItem:
    id: str
    source: str
    type: PulseType
    priority: PulsePriority | str | int
    content: str
    lane: PulseLane | str = PulseLane.PULSE
    title: str = ""
    why_now: str = ""
    suggested_next_step: str = ""
    references: list[str] = field(default_factory=list)
    status: PulseStatus = PulseStatus.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    action: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.metadata = dict(self.metadata or {})
        if self.action is not None:
            self.action = dict(self.action)
        self.references = [str(ref).strip() for ref in (self.references or []) if str(ref).strip()]

    @property
    def lane_type(self) -> PulseLane:
        return PulseLane.from_value(self.lane)

    @property
    def priority_level(self) -> PulsePriority:
        return PulsePriority.from_value(self.priority)

    @property
    def priority_score(self) -> int:
        if isinstance(self.priority, int):
            return self.priority
        return self.priority_level.score

    @property
    def is_pending(self) -> bool:
        return self.status in (PulseStatus.PENDING, PulseStatus.DELIVERED)

    @property
    def is_active(self) -> bool:
        if not self.is_pending:
            return False
        return not (self.expires_at and datetime.now(timezone.utc) > self.expires_at)

    def matches_duplicate(
        self,
        other_content: str,
        *,
        window: timedelta,
        lane: PulseLane | str = PulseLane.PULSE,
        title: str = "",
    ) -> bool:
        other_key = title.strip() or other_content.strip()
        self_key = self.title.strip() or self.content.strip()
        if (
            self.lane_type != PulseLane.from_value(lane)
            or self_key != other_key
            or not self.is_active
        ):
            return False
        return datetime.now(timezone.utc) - self.created_at <= window

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "lane": self.lane_type.value,
            "type": self.type.value,
            "priority": self.priority_level.value,
            "priority_score": self.priority_score,
            "content": self.content,
            "title": self.title,
            "why_now": self.why_now,
            "suggested_next_step": self.suggested_next_step,
            "references": self.references,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "action": self.action,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PulseItem:
        raw_lane = data.get("lane") or _infer_lane_from_dict(data)
        return cls(
            id=data["id"],
            source=data["source"],
            lane=PulseLane.from_value(raw_lane),
            type=PulseType(data["type"]),
            priority=data.get("priority") or data.get("priority_score") or PulsePriority.NORMAL,
            content=data["content"],
            title=str(data.get("title") or ""),
            why_now=str(data.get("why_now") or ""),
            suggested_next_step=str(data.get("suggested_next_step") or ""),
            references=[str(ref) for ref in data.get("references") or []],
            status=PulseStatus(data.get("status", PulseStatus.PENDING.value)),
            created_at=datetime.fromisoformat(data["created_at"]),
            expires_at=datetime.fromisoformat(data["expires_at"])
            if data.get("expires_at")
            else None,
            action=data.get("action"),
            metadata=data.get("metadata") or {},
        )


def _infer_lane_from_dict(data: dict[str, Any]) -> PulseLane:
    raw_type = str(data.get("type") or "").strip().lower()
    source = str(data.get("source") or "").strip().lower()
    if raw_type == PulseType.PROPOSAL.value or data.get("action"):
        return PulseLane.APPROVAL
    if source in {"runtime", "budget", "security", "heartbeat"}:
        return PulseLane.OPS
    if raw_type in {PulseType.ALERT.value, PulseType.COST_ALERT.value}:
        return PulseLane.OPS
    return PulseLane.PULSE


@dataclass
class CurationResult:
    """Outcome of a pulse-curation run — items PLUS why there were none.

    Curation can legitimately produce nothing (``empty``), be intentionally
    skipped by a gate (``skipped``), be blocked by budget (``budget_blocked``),
    or genuinely fail (``error``). Carrying the status + a human ``reason`` (and,
    for ``error``, the raw ``error`` for classification) lets every surface tell
    the user what happened and how to fix it instead of a bare "0 items".
    """

    items: list[PulseItem] = field(default_factory=list)
    status: str = "completed"  # completed | empty | skipped | budget_blocked | error
    reason: str = ""  # user-facing explanation when status != completed
    error: str = ""  # raw error string (status == "error") for classification

    def __len__(self) -> int:  # len(result) == number of items (back-compat ergonomics)
        return len(self.items)

    def __iter__(self):  # allow `for item in result` / `list(result)`
        return iter(self.items)

    def __bool__(self) -> bool:  # truthy when it carried items
        return bool(self.items)
