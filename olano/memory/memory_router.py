"""Context-aware memory dispatch — loads the right memory types per situation."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from olano.memory.typed_memory import TypedFact, TypedMemoryStore

logger = logging.getLogger(__name__)


class SituationType:
    MAIN_SESSION = "main_session"
    GROUP_CHAT = "group_chat"
    HEARTBEAT = "heartbeat"
    SUBAGENT = "subagent"
    SUMMARIZATION = "summarization"


# Which memory types to load per situation
_LOAD_RULES: dict[str, list[str]] = {
    SituationType.MAIN_SESSION: [
        "working",
        "episodic",
        "semantic",
        "procedural",
        "preference",
        "safety",
    ],
    SituationType.GROUP_CHAT: [
        "episodic",
        "semantic",  # public-only (private=True facts are stripped)
        "safety",  # No preference (personal/private by definition)
    ],
    SituationType.HEARTBEAT: ["procedural", "safety"],
    SituationType.SUBAGENT: ["semantic", "procedural", "safety"],
    SituationType.SUMMARIZATION: ["semantic", "episodic"],
}

# Situations where private facts are allowed
_PRIVATE_ALLOWED = {SituationType.MAIN_SESSION}

# Maximum facts to load per type
_MAX_FACTS_PER_TYPE = 20
# Minimum confidence threshold for loading
_MIN_CONFIDENCE = 0.3


class MemoryRouter:
    """Routes memory loading based on conversation situation."""

    def __init__(self, store: TypedMemoryStore) -> None:
        self.store = store

    def load_for_situation(
        self,
        situation: str,
        topic: str | None = None,
        max_per_type: int = _MAX_FACTS_PER_TYPE,
    ) -> dict[str, list[TypedFact]]:
        """
        Load relevant memory types for the given situation.
        Private facts are stripped for any situation outside MAIN_SESSION to
        prevent personal/preference data from leaking into group chats,
        subagent delegations, or heartbeat runs.
        """
        types_to_load = _LOAD_RULES.get(situation, _LOAD_RULES[SituationType.MAIN_SESSION])
        allow_private = situation in _PRIVATE_ALLOWED
        result: dict[str, list[TypedFact]] = {}

        for memory_type in types_to_load:
            if topic:
                facts = self.store.search(
                    query=topic,
                    memory_type=memory_type,
                    min_confidence=_MIN_CONFIDENCE,
                    max_results=max_per_type,
                )
            else:
                facts = self.store.get_all(memory_type, min_confidence=_MIN_CONFIDENCE)
                facts.sort(key=lambda f: f.relevance_score, reverse=True)
                facts = facts[:max_per_type]

            if not allow_private:
                facts = [f for f in facts if not f.private]

            if facts:
                result[memory_type] = facts

        return result

    # P5.10 / P5.14: query routing ────────────────────────────────────────────

    _RELATIONSHIP_PATTERNS = re.compile(
        r"\b(who|whom|which|relationship|related to|connected|linked|worked with|"
        r"knows|between .+ and|colleague|dependency|depends on)\b",
        re.IGNORECASE,
    )
    _SIMILARITY_PATTERNS = re.compile(
        r"\b(similar|like|resemble|compare|analogy|alternative|instead of|vs\.?|"
        r"versus|closest|nearest)\b",
        re.IGNORECASE,
    )

    @classmethod
    def route_query(cls, query: str) -> str:
        """Classify a query and return the recommended backend.

        Returns one of: 'graph', 'rag', 'notes'.
        - 'graph'  — relationship queries ('who works with', 'connected to', etc.)
        - 'rag'    — semantic similarity queries ('similar to', 'like', 'compare')
        - 'notes'  — concept/fact lookups (default)
        """
        if cls._RELATIONSHIP_PATTERNS.search(query):
            return "graph"
        if cls._SIMILARITY_PATTERNS.search(query):
            return "rag"
        return "notes"

    def format_for_prompt(self, situation: str, topic: str | None = None) -> str:
        """Format loaded memory as a prompt-injectable block."""
        memory = self.load_for_situation(situation, topic=topic)
        if not memory:
            return ""

        parts: list[str] = ["## Memory Context"]
        type_labels = {
            "working": "Current Session",
            "semantic": "Known Facts",
            "procedural": "How-to Knowledge",
            "preference": "User Preferences",
            "safety": "Safety Rules",
            "episodic": "Recent Events",
            "relational": "Relationships",
        }
        for mtype, facts in memory.items():
            label = type_labels.get(mtype, mtype.title())
            parts.append(f"\n### {label}")
            for fact in facts:
                parts.append(f"- {fact.content}")

        return "\n".join(parts)
