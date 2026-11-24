"""Cross-agent context summarization — structured handoff contracts.

P6.5: Every delegation produces a structured HandoffContract instead of a raw
transcript snippet. The contract includes a task summary, key findings, artifacts
produced, confidence level, and risks/blockers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class HandoffContract:
    """Structured handoff produced when one agent delegates to another.

    Never share raw transcripts — always use this structured summary.
    """

    from_agent: str
    to_agent: str
    task: str
    summary: str = ""
    key_findings: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    confidence: float = 0.8  # 0.0–1.0
    risks: list[str] = field(default_factory=list)
    context_chars: int = 0  # how many chars of source context were used

    def to_prompt(self) -> str:
        """Render the contract as a system/context message for the receiving agent."""
        lines = [
            f"## Handoff from {self.from_agent} → {self.to_agent}",
            f"**Task:** {self.task}",
        ]
        if self.summary:
            lines += ["", f"**Summary:** {self.summary}"]
        if self.key_findings:
            lines += ["", "**Key Findings:**"]
            lines += [f"- {f}" for f in self.key_findings]
        if self.artifacts:
            lines += ["", "**Artifacts:**"]
            lines += [f"- {a}" for a in self.artifacts]
        if self.risks:
            lines += ["", "**Risks / Blockers:**"]
            lines += [f"- {r}" for r in self.risks]
        lines += ["", f"**Confidence:** {self.confidence:.0%}"]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "task": self.task,
            "summary": self.summary,
            "key_findings": self.key_findings,
            "artifacts": self.artifacts,
            "confidence": self.confidence,
            "risks": self.risks,
        }


def build_handoff_contract(
    from_agent: str,
    to_agent: str,
    task: str,
    messages: list[dict[str, Any]],
    max_context_chars: int = 3000,
) -> HandoffContract:
    """Build a structured HandoffContract from a conversation history.

    Extracts key findings from assistant turns, artifact references from tool
    results, and produces a short summary — without dumping the raw transcript.
    """
    key_findings: list[str] = []
    artifacts: list[str] = []
    risks: list[str] = []
    total_chars = 0

    for msg in reversed(messages[-20:]):
        role = msg.get("role", "")
        content = str(msg.get("content", ""))
        if total_chars >= max_context_chars:
            break
        total_chars += len(content)

        if role == "assistant":
            # Treat each sentence-like fragment as a potential finding
            for sentence in _split_sentences(content):
                if len(sentence) < 20:
                    continue
                lower = sentence.lower()
                if any(
                    w in lower
                    for w in (
                        "found",
                        "discovered",
                        "identified",
                        "confirmed",
                        "result",
                        "output",
                        "error",
                        "failed",
                        "blocked",
                        "cannot",
                    )
                ):
                    trimmed = sentence.strip()[:200]
                    if trimmed not in key_findings:
                        key_findings.append(trimmed)
                    if any(w in lower for w in ("error", "failed", "blocked", "cannot", "unable")):
                        risks.append(trimmed)

        elif role == "tool":
            # Tool results often reference files/URLs as artifacts
            for line in content.splitlines():
                line = line.strip()
                if (
                    any(line.startswith(prefix) for prefix in ("/", "~/", "./", "http"))
                    and len(line) < 300
                    and line not in artifacts
                ):
                    artifacts.append(line)

    # Limit list sizes
    key_findings = key_findings[:8]
    artifacts = artifacts[:6]
    risks = list(dict.fromkeys(risks))[:4]

    # Derive a one-line summary from the last assistant message
    summary = ""
    for msg in reversed(messages[-5:]):
        if msg.get("role") == "assistant":
            content = str(msg.get("content", ""))
            summary = content[:300].replace("\n", " ").strip()
            break

    confidence = max(0.1, min(1.0, 0.9 - 0.1 * len(risks)))

    return HandoffContract(
        from_agent=from_agent,
        to_agent=to_agent,
        task=task,
        summary=summary,
        key_findings=key_findings,
        artifacts=artifacts,
        confidence=confidence,
        risks=risks,
        context_chars=total_chars,
    )


def build_handoff_prompt(
    from_agent: str,
    to_agent: str,
    task: str,
    context_summary: str | HandoffContract,
) -> str:
    """Build a system message for an agent receiving a delegated task.

    Accepts either a pre-rendered summary string or a HandoffContract.
    """
    if isinstance(context_summary, HandoffContract):
        rendered = context_summary.to_prompt()
    else:
        rendered = f"## Handoff Context\n{context_summary}"

    return (
        f"You are {to_agent}, receiving a delegated task from {from_agent}.\n\n"
        f"Task: {task}\n\n"
        f"{rendered}\n\n"
        "Complete the task based on the context above, then return your result."
    )


def summarize_context(
    messages: list[dict[str, Any]],
    max_chars: int = 2000,
    focus: str | None = None,
) -> str:
    """Produce a compact summary of recent messages suitable for handoff.

    Kept for backwards-compatibility; prefer build_handoff_contract() for new code.
    """
    if not messages:
        return ""

    recent = messages[-10:]
    parts: list[str] = []

    if focus:
        parts.append(f"Topic: {focus}\n")

    for msg in recent:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if not content:
            continue
        if len(content) > 500:
            content = content[:500] + "..."
        if role == "user":
            parts.append(f"User: {content}")
        elif role == "assistant":
            parts.append(f"Agent: {content}")

    summary = "\n".join(parts)
    if len(summary) > max_chars:
        summary = summary[:max_chars] + "\n[...context truncated for handoff]"

    return f"## Handoff Context\n{summary}"


# ── helpers ───────────────────────────────────────────────────────────────────


def _split_sentences(text: str) -> list[str]:
    import re

    return re.split(r"(?<=[.!?])\s+", text)
