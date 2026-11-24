"""Typed memory taxonomy for Olano agents.

Memory types:
    working     — Current session state (volatile, in-context)
    episodic    — Chronological event logs (today + yesterday at L1)
    semantic    — Curated facts (on-demand retrieval, MEMORY.md)
    procedural  — How-to skills and workflows
    preference  — User style and behavior preferences
    safety      — Hard limits and safety boundaries (always L0)
    relational  — Entity relationships (memory graph)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MEMORY_TYPES = (
    "working",
    "episodic",
    "semantic",
    "procedural",
    "preference",
    "safety",
    "relational",
)


@dataclass
class TypedFact:
    """A single typed memory fact."""

    id: str
    type: str  # One of MEMORY_TYPES
    content: str
    confidence: float = 1.0  # 0.0–1.0
    source: str = ""  # Where this fact came from
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_accessed: datetime = field(default_factory=datetime.utcnow)
    access_count: int = 0
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    # Private facts are never exposed to shared contexts (group chat, subagent, heartbeat).
    # Only visible in main (direct) sessions.
    private: bool = False

    # Decay score — reduced over time if not accessed
    @property
    def relevance_score(self) -> float:
        age_days = (datetime.utcnow() - self.last_accessed).days
        decay = max(0.1, 1.0 - age_days * 0.05)  # 5% decay per day, floor 10%
        return self.confidence * decay * min(1.0, 1 + self.access_count * 0.1)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "content": self.content,
            "confidence": self.confidence,
            "source": self.source,
            "created_at": self.created_at.isoformat(),
            "last_accessed": self.last_accessed.isoformat(),
            "access_count": self.access_count,
            "tags": self.tags,
            "metadata": self.metadata,
            "private": self.private,
        }

    @classmethod
    def from_dict(cls, d: dict) -> TypedFact:
        return cls(
            id=d["id"],
            type=d["type"],
            content=d["content"],
            confidence=d.get("confidence", 1.0),
            source=d.get("source", ""),
            created_at=datetime.fromisoformat(d["created_at"]),
            last_accessed=datetime.fromisoformat(d.get("last_accessed", d["created_at"])),
            access_count=d.get("access_count", 0),
            tags=d.get("tags", []),
            metadata=d.get("metadata", {}),
            private=d.get("private", False),
        )


class ContradictionEvent:
    """Record of a detected memory contradiction for PULSE notification."""

    def __init__(self, memory_type: str, existing: TypedFact, incoming: TypedFact) -> None:
        self.memory_type = memory_type
        self.existing = existing
        self.incoming = incoming

    def as_pulse_content(self) -> str:
        return (
            f"Memory contradiction detected in '{self.memory_type}':\n"
            f'- Existing: "{self.existing.content}" (confidence {self.existing.confidence:.2f})\n'
            f'- Incoming: "{self.incoming.content}"\n'
            f"Please review and resolve."
        )

    def as_pulse_content_short(self) -> str:
        old = self.existing.content[:80] + ("…" if len(self.existing.content) > 80 else "")
        new = self.incoming.content[:80] + ("…" if len(self.incoming.content) > 80 else "")
        return f'[{self.memory_type}] "{old}" → "{new}"'


class TypedMemoryStore:
    """
    Persistent typed memory store.
    Each type is stored in its own JSONL file under .olano/state/memory/<type>/facts.jsonl
    """

    def __init__(self, state_memory_dir: Path) -> None:
        self.base_dir = state_memory_dir
        self._caches: dict[str, list[TypedFact]] = {}
        self._pending_contradictions: list[ContradictionEvent] = []

    def _type_path(self, memory_type: str) -> Path:
        d = self.base_dir / memory_type
        d.mkdir(parents=True, exist_ok=True)
        return d / "facts.jsonl"

    def _load_type(self, memory_type: str) -> list[TypedFact]:
        if memory_type in self._caches:
            return self._caches[memory_type]
        path = self._type_path(memory_type)
        facts: list[TypedFact] = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        facts.append(TypedFact.from_dict(json.loads(line)))
                    except Exception:
                        pass
        self._caches[memory_type] = facts
        return facts

    def _save_type(self, memory_type: str) -> None:
        facts = self._caches.get(memory_type, [])
        path = self._type_path(memory_type)
        lines = [json.dumps(f.to_dict(), ensure_ascii=False) for f in facts]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def store(self, fact: TypedFact) -> None:
        """Store a fact. Detects contradictions and updates confidence if duplicate."""
        facts = self._load_type(fact.type)

        # Check for contradictions / duplicates by content similarity
        for existing in facts:
            if existing.content.strip().lower() == fact.content.strip().lower():
                # Exact duplicate — just boost confidence
                existing.confidence = min(1.0, existing.confidence + 0.1)
                existing.access_count += 1
                self._save_type(fact.type)
                return

        # Check for potential contradiction (same subject, different claim).
        # Use first-word prefix (up to 6 words) to find same-subject facts, then
        # only flag as contradiction when the tails actually differ meaningfully.
        words = fact.content.split()
        subject_key = " ".join(w.strip(".,;:!?") for w in words[:6]).lower()
        for existing in facts:
            ex_words = existing.content.split()
            ex_key = " ".join(w.strip(".,;:!?") for w in ex_words[:6]).lower()
            if ex_key == subject_key and existing.content != fact.content:
                # Only flag when the strings diverge past the shared subject words
                tail_new = " ".join(words[6:]).strip().lower()
                tail_old = " ".join(ex_words[6:]).strip().lower()
                if tail_new == tail_old:
                    continue  # subject prefix matched but tails are same — cosmetic duplicate
                existing.confidence = max(0.1, existing.confidence - 0.3)
                existing.metadata["contradicted_by"] = fact.id
                logger.debug("Typed memory: contradiction detected for '%s...'", subject_key)
                self._pending_contradictions.append(ContradictionEvent(fact.type, existing, fact))

        facts.append(fact)
        self._save_type(fact.type)

    def flush_contradictions(self) -> list[ContradictionEvent]:
        """Return and clear all pending contradiction events."""
        events, self._pending_contradictions = self._pending_contradictions, []
        return events

    def search(
        self,
        query: str,
        memory_type: str | None = None,
        min_confidence: float = 0.3,
        max_results: int = 10,
    ) -> list[TypedFact]:
        """Simple keyword search across typed facts."""
        types = [memory_type] if memory_type else list(MEMORY_TYPES)
        query_lower = query.lower()
        results: list[TypedFact] = []

        for mtype in types:
            for fact in self._load_type(mtype):
                if fact.confidence < min_confidence:
                    continue
                if query_lower in fact.content.lower() or any(
                    query_lower in tag.lower() for tag in fact.tags
                ):
                    fact.access_count += 1
                    fact.last_accessed = datetime.utcnow()
                    results.append(fact)

        results.sort(key=lambda f: f.relevance_score, reverse=True)
        return results[:max_results]

    def get_all(self, memory_type: str, min_confidence: float = 0.0) -> list[TypedFact]:
        return [f for f in self._load_type(memory_type) if f.confidence >= min_confidence]

    def store_fact(
        self,
        content: str,
        memory_type: str,
        confidence: float = 1.0,
        source: str = "",
        tags: list[str] | None = None,
        private: bool = False,
    ) -> TypedFact:
        """Convenience method to create and store a fact."""
        import uuid

        fact = TypedFact(
            id=str(uuid.uuid4())[:8],
            type=memory_type,
            content=content,
            confidence=confidence,
            source=source,
            tags=tags or [],
            private=private,
        )
        self.store(fact)
        return fact

    def invalidate_cache(self, memory_type: str | None = None) -> None:
        if memory_type:
            self._caches.pop(memory_type, None)
        else:
            self._caches.clear()
