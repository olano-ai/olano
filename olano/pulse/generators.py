"""PULSE generators for curated agent insights, ops notes, and review queues."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from olano.config.home import get_olano_paths
from olano.config.workspace import WORKSPACE_STATE_DIRNAME
from olano.pulse.engine import PulseEngine
from olano.pulse.models import (
    CurationResult,
    PulseItem,
    PulseLane,
    PulsePriority,
    PulseType,
)

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig

logger = logging.getLogger(__name__)

_CURATED_PULSE_PROMPT = """\
You are the proactive editorial layer for a Olano agent.

Your job is to produce a small number of genuinely useful PULSE items for the user based on:
- the agent's identity, soul, and style
- the user's stated interests and preferences
- recent conversations and work activity
- durable memory and current workspace context

Only generate items that feel worth proactively surfacing.

Good PULSE items are:
- strategic insights
- opportunities
- high-value questions worth exploring
- concrete recommendations

Do NOT generate:
- runtime health, infra, budget, failure, anomaly, or system-noise alerts
- generic reminders
- internal implementation chatter
- repetitive restatements of the same topic

Return JSON only in this shape:
{{
  "items": [
    {{
      "type": "insight|opportunity|question|recommendation",
      "priority": "normal|low|urgent",
      "title": "short title",
      "summary": "1-2 sentence grounded summary",
      "why_now": "why it matters now",
      "suggested_next_step": "concrete next step",
      "references": ["optional reference", "optional reference"]
    }}
  ]
}}

Every item MUST include a non-empty "title" and "summary" (use those EXACT key
names). If nothing is truly meaningful, return: {{"items": []}}

Agent name: {agent_name}
Agent identity:
{identity}

Agent soul:
{soul}

User profile:
{user_profile}

User interests:
{interests}

Recent activity:
{recent_activity}

Workspace context:
{workspace_context}

Durable memory:
{memory}
"""


def _first_str(data: dict[str, Any], *keys: str) -> str:
    """First non-empty string value among ``keys`` in ``data`` (else "").

    Curation models frequently paraphrase the requested JSON schema (e.g. return
    ``why``/``grounding`` instead of ``why_now``/``references``), so item parsing
    must accept the common synonyms rather than silently drop a usable item.
    """
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


@dataclass
class PulseSignal:
    kind: str
    source: str
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class PulseGenerator:
    """Translate signals and contextual curation into queued items."""

    def __init__(self, config: AgentConfig, workspace_root: Path) -> None:
        self.config = (
            config
            if config.workspace
            else config.model_copy(update={"workspace": str(workspace_root)})
        )
        self.workspace_root = workspace_root
        self.engine = PulseEngine(self.config, workspace_root / WORKSPACE_STATE_DIRNAME)
        self._state_path = (
            self.workspace_root
            / WORKSPACE_STATE_DIRNAME
            / "state"
            / "pulse"
            / "curation_state.json"
        )

    def _source_enabled(self, primary: str, *aliases: str) -> bool:
        enabled = {str(source).strip().lower() for source in self.config.pulse.sources}
        candidates = {primary.strip().lower(), *(alias.strip().lower() for alias in aliases)}
        return bool(enabled & candidates)

    def generate_from_signals(self, signals: list[PulseSignal]) -> list[PulseItem]:
        items: list[PulseItem] = []
        for signal in signals:
            item = self._from_signal(signal)
            if item is not None:
                items.append(item)
        return items

    def _from_signal(self, signal: PulseSignal) -> PulseItem | None:
        if not self._source_enabled("ops", "runtime", "olano", "heartbeat", "budget"):
            return None

        title = str(signal.metadata.get("title") or "").strip()
        if signal.kind == "tool_failure":
            return self.engine.add(
                content=signal.content,
                source=signal.source,
                type=PulseType.ALERT,
                priority=PulsePriority.NORMAL,
                lane=PulseLane.OPS,
                title=title or "Tool failure",
                metadata={"kind": signal.kind, **signal.metadata},
                dedup_window_hours=6,
            )
        if signal.kind == "large_context":
            return self.engine.add(
                content=signal.content,
                source=signal.source,
                type=PulseType.FINDING,
                priority=PulsePriority.NORMAL,
                lane=PulseLane.OPS,
                title=title or "Large context — start fresh?",
                metadata={"kind": signal.kind, **signal.metadata},
                expires_in_hours=4.0,
                dedup_window_hours=12,
            )
        if signal.kind == "heartbeat_finding":
            return self.engine.add(
                content=signal.content,
                source=signal.source,
                type=PulseType.REMINDER,
                priority=signal.metadata.get("priority", PulsePriority.NORMAL),
                lane=PulseLane.OPS,
                title=title or str(signal.metadata.get("task") or "Heartbeat note"),
                metadata={"kind": signal.kind, **signal.metadata},
                dedup_window_hours=12,
            )
        if signal.kind == "workspace_anomaly":
            return self.engine.add(
                content=signal.content,
                source=signal.source,
                type=PulseType.FINDING,
                priority=PulsePriority.NORMAL,
                lane=PulseLane.OPS,
                title=title or "Workspace health note",
                metadata={"kind": signal.kind, **signal.metadata},
                dedup_window_hours=24,
            )
        if signal.kind == "memory_pressure":
            return self.engine.add(
                content=signal.content,
                source=signal.source,
                type=PulseType.FINDING,
                priority=PulsePriority.NORMAL,
                lane=PulseLane.OPS,
                title=title or "Memory pressure note",
                metadata={"kind": signal.kind, **signal.metadata},
                dedup_window_hours=24,
            )
        return None

    def capture_workspace_anomalies(self) -> list[PulseItem]:
        from olano.cartography.health_score import compute_health

        health = compute_health(self.workspace_root)
        issues = health.get("issues") or []
        if not issues:
            return []
        content = f"Workspace health issues detected: {', '.join(issues[:3])}."
        if len(issues) > 3:
            content += f" (+{len(issues) - 3} more)"
        return self.generate_from_signals(
            [
                PulseSignal(
                    kind="workspace_anomaly",
                    source="olano",
                    content=content,
                    metadata={
                        "title": "Workspace health note",
                        "score": health.get("score"),
                        "issues": issues,
                    },
                )
            ]
        )

    def capture_memory_pressure(self, max_chars: int = 20_000) -> list[PulseItem]:
        memory_path = self.workspace_root / "MEMORY.md"
        if not memory_path.exists():
            return []
        size = len(memory_path.read_text(encoding="utf-8"))
        if size <= max_chars:
            return []
        return self.generate_from_signals(
            [
                PulseSignal(
                    kind="memory_pressure",
                    source="olano",
                    content=(
                        f"MEMORY.md is large ({size:,} chars). Review whether it should be "
                        "distilled or reorganized."
                    ),
                    metadata={
                        "title": "Memory pressure note",
                        "size": size,
                        "threshold": max_chars,
                    },
                )
            ]
        )

    def capture_heartbeat_findings(self, findings: list[str | dict[str, Any]]) -> list[PulseItem]:
        signals: list[PulseSignal] = []
        for finding in findings:
            if isinstance(finding, str):
                signals.append(
                    PulseSignal(
                        kind="heartbeat_finding",
                        source="heartbeat",
                        content=finding,
                    )
                )
                continue
            priority = PulsePriority.from_value(finding.get("priority"))
            content = str(finding.get("content") or finding.get("message") or "").strip()
            if not content:
                continue
            signals.append(
                PulseSignal(
                    kind="heartbeat_finding",
                    source=str(finding.get("source") or "heartbeat"),
                    content=content,
                    metadata={
                        "priority": priority,
                        "task": finding.get("task"),
                        "title": finding.get("task") or "Heartbeat note",
                    },
                )
            )
        return self.generate_from_signals(signals)

    def capture_runtime_signals(
        self,
        *,
        usage_meta: dict[str, int] | None = None,
        tool_failures: list[str] | None = None,
    ) -> list[PulseItem]:
        signals: list[PulseSignal] = []
        total_tokens = int((usage_meta or {}).get("total_tokens", 0) or 0)
        if total_tokens >= 80_000:
            # deepagents' SummarizationMiddleware compacts automatically near the
            # model's real context limit; still nudge the user that this is a long
            # conversation they can compact now or start fresh.
            content = (
                f"This conversation is using a large context ({total_tokens:,} tokens). "
                "It is compacted automatically as it approaches the model's context "
                "limit — you can also run /compact now, or /new to start fresh when "
                "switching topics."
            )
            signals.append(
                PulseSignal(
                    kind="large_context",
                    source="runtime",
                    content=content,
                    metadata={
                        "title": "Large context — compact or start fresh?",
                        "total_tokens": total_tokens,
                    },
                )
            )
        for failure in tool_failures or []:
            signals.append(
                PulseSignal(
                    kind="tool_failure",
                    source="runtime",
                    content=f"Recent tool failure: {failure}",
                    metadata={"title": "Tool failure"},
                )
            )
        items = self.generate_from_signals(signals)
        items.extend(self.capture_memory_pressure())
        items.extend(self.capture_workspace_anomalies())
        return items

    def capture_learned_skills(self, skill_names: list[str]) -> list[PulseItem]:
        if not self._source_enabled("approvals", "self_learning", "olano"):
            return []
        items: list[PulseItem] = []
        for name in skill_names:
            # Skill is now stored as skills/learned/<slug>/SKILL.md
            from olano.memory.self_learning import SelfLearningService

            slug = SelfLearningService._skill_slug(name)
            skill_path = self.workspace_root / "skills" / "learned" / slug / "SKILL.md"
            ref_path = f"skills/learned/{slug}/SKILL.md"
            # Embed a preview of the skill so the user can read it right in the pulse.
            preview = ""
            try:
                raw = skill_path.read_text(encoding="utf-8")
                # Strip frontmatter and take up to 800 chars of the body.
                lines = raw.splitlines()
                if lines and lines[0].strip() == "---":
                    end = next(
                        (i for i in range(1, len(lines)) if lines[i].strip() == "---"),
                        -1,
                    )
                    body = "\n".join(lines[end + 1 :]).strip() if end > 0 else raw.strip()
                else:
                    body = raw.strip()
                preview = body[:800] + (" …" if len(body) > 800 else "")
            except OSError:
                preview = f"(skill file not found at `{ref_path}`)"

            content = f"**Skill draft:** `{name}`\n\n{preview}"
            items.append(
                self.engine.add(
                    content=content,
                    source="self_learning",
                    lane=PulseLane.APPROVAL,
                    type=PulseType.PROPOSAL,
                    priority=PulsePriority.LOW,
                    title=f"Review learned skill: {slug}",
                    suggested_next_step=(
                        "Approve to keep this skill, or reject to discard it. "
                        f"Edit it directly in Files → {ref_path}."
                    ),
                    references=[ref_path],
                    metadata={"kind": "learned_skill", "skill_name": name},
                    dedup_window_hours=24,
                )
            )
        return items

    def capture_self_improvement_proposals(self, proposals: dict[str, Any]) -> list[PulseItem]:
        summary = str(proposals.get("summary") or "").strip()
        items: list[PulseItem] = []

        mapping = [
            ("soul_additions", "SOUL.md", "Self-Improvement", "SOUL.md"),
            ("agents_additions", "AGENTS.md", "Self-Improvement", "AGENTS.md"),
            ("memory_additions", "MEMORY.md", "Self-Improvement", "MEMORY.md"),
        ]
        for key, rel_path, heading, label in mapping:
            lines = [str(line).strip() for line in proposals.get(key, []) if str(line).strip()]
            if not lines:
                continue
            preview = "; ".join(lines[:2])
            if len(lines) > 2:
                preview += f" (+{len(lines) - 2} more)"
            items.append(
                self.engine.add(
                    content=f"Review proposed {label} updates: {preview}",
                    source="self_learning",
                    lane=PulseLane.APPROVAL,
                    type=PulseType.PROPOSAL,
                    priority=PulsePriority.NORMAL,
                    title=f"Review {label} updates",
                    why_now=summary,
                    suggested_next_step=f"Inspect and approve the proposed additions to {label}.",
                    references=[rel_path],
                    action={
                        "type": "append_lines",
                        "path": rel_path,
                        "heading": heading,
                        "lines": lines,
                    },
                    metadata={
                        "kind": "self_improvement",
                        "target_file": rel_path,
                        "summary": summary,
                    },
                    expires_in_hours=72,
                    dedup_window_hours=24,
                )
            )

        reorg = str(proposals.get("memory_reorganization") or "").strip()
        if reorg:
            items.append(
                self.engine.add(
                    content=f"Review memory reorganization proposal: {reorg}",
                    source="self_learning",
                    lane=PulseLane.APPROVAL,
                    type=PulseType.PROPOSAL,
                    priority=PulsePriority.NORMAL,
                    title="Review memory organization proposal",
                    why_now=summary,
                    suggested_next_step="Inspect the proposal and decide whether to reorganize memory files.",
                    references=["MEMORY.md"],
                    metadata={
                        "kind": "memory_reorganization",
                        "summary": summary,
                        "proposal": reorg,
                    },
                    expires_in_hours=72,
                    dedup_window_hours=24,
                )
            )

        return items

    async def capture_agent_curated_pulse(
        self,
        *,
        thread_id: str | None = None,
        force: bool = False,
    ) -> list[PulseItem]:
        if not self._source_enabled("agent", "olano"):
            return []

        activity = self._load_recent_activity(thread_id=thread_id)
        if activity["turn_count"] < max(
            1, int(getattr(self.config.pulse, "min_turns_for_generation", 4))
        ):
            return []

        if not force and not self._should_curate(thread_id=thread_id):
            return []

        prompt = _CURATED_PULSE_PROMPT.format(
            agent_name=self.config.name,
            identity=self._read_workspace_file("IDENTITY.md", max_chars=1000),
            soul=self._read_workspace_file("SOUL.md", max_chars=1800),
            user_profile=self._read_workspace_file("USER.md", max_chars=1200),
            interests=self._interests_text(),
            recent_activity=activity["text"] or "(no recent activity)",
            workspace_context=self._workspace_context_text(),
            memory=self._read_workspace_file("MEMORY.md", max_chars=2200),
        )

        # Skip the (doomed) model build on a fresh deployment with no provider
        # key / no model configured, and log a clear operator-facing line instead
        # of swallowing a raw SDK traceback into a debug message.
        from olano.core.model_readiness import check_model_ready

        readiness = check_model_ready(self.config)
        if not readiness.ok:
            logger.info(
                "Curated PULSE skipped for %s: %s",
                self.config.name,
                readiness.error.message if readiness.error else "model not configured",
            )
            return []

        try:
            # Use olano's builder (not raw init_chat_model) so custom providers
            # like ``openai-codex:`` / ``deepseek:`` resolve — otherwise this FAST
            # path raises for every such agent and silently returns no items.
            from olano.models import build_model

            model = build_model(self.config.model)
            response = await model.ainvoke([{"role": "user", "content": prompt}])
            # Fires after every turn (and from the cognition FAST fallback) on
            # a bare model — outside any AgentRunner turn, so ledger it here.
            from olano.analytics.usage_recorder import record_result_usage

            record_result_usage(self.config.name, self.config.model, response, origin="pulse")
            content = response.content if hasattr(response, "content") else str(response)
            parsed = self._parse_curated_response(
                content if isinstance(content, str) else str(content)
            )
        except Exception as exc:
            logger.debug("Curated PULSE generation failed: %s", exc)
            return []

        items = self.add_curated_items(parsed, source="agent", thread_id=thread_id or "")

        self._save_curation_state(
            {
                "last_generated_at": datetime.now(timezone.utc).isoformat(),
                "last_thread_id": thread_id or "",
                "last_turn_count": activity["turn_count"],
                "last_item_ids": [item.id for item in items],
            }
        )
        return items

    def add_curated_items(
        self,
        raw_items: list[dict[str, Any]],
        *,
        source: str = "agent",
        thread_id: str = "",
    ) -> list[PulseItem]:
        """Add parsed curated items to the PULSE lane.

        Shared by the FAST single-prompt curation and the DEEP PulseAgent. Honors
        an optional ``action`` dict per item — that is what makes approving a
        pulse produce a real artifact (e.g. a ``run_worker`` action)."""
        max_items = max(1, int(getattr(self.config.pulse, "generated_items_per_cycle", 3) or 3))
        type_map = {
            "insight": PulseType.INSIGHT,
            "opportunity": PulseType.OPPORTUNITY,
            "question": PulseType.QUESTION,
            "recommendation": PulseType.RECOMMENDATION,
        }
        items: list[PulseItem] = []
        for raw in raw_items[:max_items]:
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("type") or raw.get("kind") or "insight").strip().lower()
            item_type = type_map.get(kind, PulseType.INSIGHT)
            # ``action`` may be a structured dict (executed on approval) OR a
            # free-text next-step string — only a dict is a real action; a string
            # is reused as the summary/next-step below.
            raw_action = raw.get("action")
            action = raw_action if isinstance(raw_action, dict) else None
            action_text = raw_action.strip() if isinstance(raw_action, str) else ""
            # Be tolerant of schema drift: models routinely rename these fields.
            title = _first_str(raw, "title", "headline", "name", "topic")
            summary = (
                _first_str(raw, "summary", "content", "detail", "description", "insight", "body")
                or action_text
            )
            why_now = _first_str(raw, "why_now", "why", "rationale", "reason")
            next_step = (
                _first_str(raw, "suggested_next_step", "next_step", "recommendation", "todo")
                or action_text
            )
            refs_raw = (
                raw.get("references")
                or raw.get("grounding")
                or raw.get("sources")
                or raw.get("links")
                or []
            )
            if isinstance(refs_raw, list):
                references = [str(ref).strip() for ref in refs_raw if str(ref).strip()]
            else:
                references = [str(refs_raw).strip()] if str(refs_raw).strip() else []
            # Backfill so a usable item is never dropped for lacking one exact key:
            # derive the missing title/summary from the other (or from why_now).
            if not summary:
                summary = why_now or title
            if not title:
                title = summary[:80]
            if not title or not summary:
                continue
            items.append(
                self.engine.add(
                    content=summary,
                    source=source,
                    lane=PulseLane.PULSE,
                    type=item_type,
                    priority=PulsePriority.from_value(raw.get("priority")),
                    title=title,
                    why_now=why_now,
                    suggested_next_step=next_step,
                    references=references,
                    expires_in_hours=float(
                        getattr(self.config.pulse, "pulse_item_ttl_hours", 168.0) or 168.0
                    ),
                    action=action,
                    metadata={"kind": "curated_pulse", "thread_id": thread_id},
                    dedup_window_hours=max(
                        24.0,
                        float(getattr(self.config.pulse, "curation_interval_hours", 24.0) or 24.0),
                    ),
                )
            )
        return items

    async def capture_pulse_agent(
        self,
        *,
        thread_id: str | None = None,
        force: bool = False,
        bypass_gates: bool = False,
    ) -> CurationResult:
        """DEEP, grounded curation that produces ACTIONABLE pulse items.

        Gated like :meth:`capture_agent_curated_pulse`; delegates the heavy
        lifting to :func:`olano.cognition.pulse_agent.curate_pulse`, which
        falls back to the FAST path on budget/failure.

        ``bypass_gates`` (set by the manual "Think" run) skips the
        min-recent-turns and rate-limit gates so pulse always curates from
        identity/memory/interests + web research, even with no recent chat.
        Scheduled background runs leave it False so the gates still apply.

        Returns a :class:`~olano.pulse.models.CurationResult` so a gated or
        failed run reports *why* it produced nothing, not just an empty list."""
        if not self._source_enabled("agent", "olano"):
            return CurationResult(
                [],
                status="skipped",
                reason=(
                    "Curated pulse is off for this agent — its pulse.sources does not include "
                    "'agent'. Add 'agent' to the agent's pulse sources to enable it."
                ),
            )
        activity = self._load_recent_activity(thread_id=thread_id)
        if not bypass_gates:
            min_turns = max(1, int(getattr(self.config.pulse, "min_turns_for_generation", 4)))
            if activity["turn_count"] < min_turns:
                return CurationResult(
                    [],
                    status="skipped",
                    reason=(
                        f"Not enough recent conversation to curate from "
                        f"(have {activity['turn_count']}, need ≥{min_turns} turns). "
                        "Chat with the agent, or run it from Cortex to curate from "
                        "identity/memory regardless."
                    ),
                )
            if not force and not self._should_curate(thread_id=thread_id):
                return CurationResult(
                    [],
                    status="skipped",
                    reason=(
                        "Already curated within the configured interval "
                        f"(~{self._curation_interval_hours():.0f}h). It will refresh on the "
                        "next cycle, or run it now from Cortex."
                    ),
                )

        from olano.cognition.pulse_agent import curate_pulse

        result = await curate_pulse(self, force=force)
        self._save_curation_state(
            {
                "last_generated_at": datetime.now(timezone.utc).isoformat(),
                "last_thread_id": thread_id or "",
                "last_turn_count": activity["turn_count"],
                "last_item_ids": [item.id for item in result.items],
            }
        )
        return result

    def _interests_text(self) -> str:
        configured = [
            str(topic).strip()
            for topic in getattr(self.config.pulse, "focus_topics", [])
            if str(topic).strip()
        ]
        explicit = [topic for topic in self.engine.preferences.interests if topic]
        all_topics = configured + [topic for topic in explicit if topic not in configured]
        if not all_topics:
            return "(none explicitly declared)"
        return "\n".join(f"- {topic}" for topic in all_topics)

    def _workspace_context_text(self) -> str:
        chunks: list[str] = []
        bridge_path = self.workspace_root / WORKSPACE_STATE_DIRNAME / "workspace_bridge.md"
        if bridge_path.exists():
            bridge = bridge_path.read_text(encoding="utf-8").strip()
            if bridge:
                chunks.append(bridge[:1600])
        agents_md = self.workspace_root / "AGENTS.md"
        if agents_md.exists():
            text = agents_md.read_text(encoding="utf-8").strip()
            if text:
                chunks.append(text[:1200])
        return "\n\n".join(chunks) or "(no additional workspace context)"

    def _read_workspace_file(self, name: str, *, max_chars: int) -> str:
        path = self.workspace_root / name
        if not path.exists():
            return "(missing)"
        return path.read_text(encoding="utf-8").strip()[:max_chars] or "(empty)"

    def _load_recent_activity(self, *, thread_id: str | None = None) -> dict[str, Any]:
        sessions_dir = get_olano_paths().sessions_dir / self.config.name
        if not sessions_dir.exists():
            return {"text": "", "turn_count": 0}

        candidate_paths: list[Path] = []
        if thread_id:
            direct = sessions_dir / f"{thread_id}.jsonl"
            if direct.exists():
                candidate_paths.append(direct)
        if not candidate_paths:
            candidate_paths = sorted(
                sessions_dir.glob("*.jsonl"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )[:3]

        entries: list[dict[str, Any]] = []
        for path in candidate_paths:
            try:
                for line in path.read_text(encoding="utf-8").splitlines()[-20:]:
                    line = line.strip()
                    if line:
                        entries.append(json.loads(line))
            except Exception:
                continue
        entries = entries[-24:]

        text_lines: list[str] = []
        turn_count = 0
        for entry in entries:
            role = str(entry.get("role") or "?").lower()
            content = str(entry.get("content") or "").strip()
            if not content:
                continue
            if role in {"user", "assistant"}:
                turn_count += 1
            text_lines.append(f"[{role.upper()}] {content[:500]}")
        return {"text": "\n".join(text_lines), "turn_count": turn_count}

    def _load_curation_state(self) -> dict[str, Any]:
        if not self._state_path.exists():
            return {}
        try:
            return cast("dict[str, Any]", json.loads(self._state_path.read_text(encoding="utf-8")))
        except Exception:
            return {}

    def _save_curation_state(self, state: dict[str, Any]) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.debug("Could not save curation state: %s", exc)

    def _curation_interval_hours(self) -> float:
        cadence = (
            str(getattr(self.config.pulse, "curation_cadence", "daily") or "daily").strip().lower()
        )
        mapped = {
            "session": 1.0,
            "daily": 24.0,
            "weekly": 24.0 * 7.0,
        }
        interval = float(
            getattr(self.config.pulse, "curation_interval_hours", mapped.get(cadence, 24.0)) or 24.0
        )
        return max(1.0, interval)

    def _should_curate(self, *, thread_id: str | None = None) -> bool:
        state = self._load_curation_state()
        last_generated_at = str(state.get("last_generated_at") or "").strip()
        if not last_generated_at:
            return True
        try:
            last = datetime.fromisoformat(last_generated_at)
        except Exception:
            return True
        return datetime.now(timezone.utc) - last >= timedelta(hours=self._curation_interval_hours())

    def _parse_curated_response(self, content: str) -> list[dict[str, Any]]:
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if not match:
                return []
            try:
                parsed = json.loads(match.group())
            except json.JSONDecodeError:
                return []
        items = parsed.get("items") if isinstance(parsed, dict) else None
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]
