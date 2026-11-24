"""
Self-learning service — periodically processes session history and updates memory.

Runs as a background asyncio task. On each cycle it:
1. Reads recent JSONL session transcripts for each agent
2. Passes them to a summarizer LLM
3. Appends distilled facts/insights to today's daily log AND MEMORY.md
4. Optionally indexes them into the RAG store
5. Weekly: distills all daily logs into curated MEMORY.md entries
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig
    from olano.memory.markdown_memory import MarkdownMemory
    from olano.memory.memory_graph import MemoryGraph
    from olano.memory.rag_memory import RAGMemory

from olano.config.home import get_olano_paths
from olano.config.workspace import WORKSPACE_STATE_DIRNAME
from olano.core.thread_labels import AGENT_TEST_THREAD_PREFIX

logger = logging.getLogger(__name__)

# NOTE: This service is fully AGENTIC — every learning path runs a headless
# deepagent (run_worker) gated by the shared budget + daily deep-run cap. There
# are no single-shot ``init_chat_model().ainvoke`` prompts anymore. When the cap
# or budget is exhausted, a cycle is *skipped* (deep-only, never cheapened).


def _read_new_lines(path: Path, offset: int) -> tuple[list[str], int]:
    """Read the lines of ``path`` after byte ``offset``. Returns
    ``(new_lines, new_byte_offset)``.

    ``offset`` is a byte position, so ``seek()`` jumps straight to the
    unprocessed tail — a line-count offset forced iterating every transcript
    from byte 0 each cycle, so I/O scaled with total corpus size rather than
    new turns (observed: a 10,495-turn session re-read every cycle).
    """
    new_lines: list[str] = []
    consumed = 0
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            for raw in fh:
                consumed += len(raw)
                new_lines.append(raw.decode("utf-8").rstrip("\r\n"))
    except OSError:
        return [], offset
    return new_lines, offset + consumed


def _byte_offset_after_line(path: Path, line_count: int) -> int | None:
    """Byte position just after the first ``line_count`` lines of ``path``.

    One-time migration for legacy offset state that recorded lines processed
    rather than bytes consumed. Returns ``None`` when the file can't be read —
    the caller must keep the legacy entry and retry, because assuming offset 0
    would re-distill every already-processed turn.
    """
    pos = 0
    try:
        with path.open("rb") as fh:
            for i, raw in enumerate(fh):
                if i >= line_count:
                    break
                pos += len(raw)
    except OSError:
        return None
    return pos


class SelfLearningService:
    """
    Background service that reads agent session transcripts and distills
    learned facts into daily memory logs, MEMORY.md, and optionally RAG.
    """

    def __init__(
        self,
        agent_config: AgentConfig,
        workspace_root: Path,
        markdown_memory: MarkdownMemory,
        rag_memory: RAGMemory | None = None,
        interval_seconds: int = 3600,
        state_dir: Path | None = None,
    ) -> None:
        self.config = agent_config
        self.workspace_root = workspace_root
        self.markdown_memory = markdown_memory
        self.rag_memory = rag_memory
        self.interval = interval_seconds
        # State dir for typed memory and PULSE; defaults to the workspace's state folder
        self._state_dir: Path = state_dir or (workspace_root / WORKSPACE_STATE_DIRNAME)
        self._task: asyncio.Task | None = None
        # session_path → lines_processed. Legacy offset shape — kept in step with
        # the byte map below: a path present here but absent there is the signal
        # for the one-time line→byte conversion in _scan_session_turns.
        self._processed_offsets: dict[str, int] = {}
        self._processed_byte_offsets: dict[str, int] = {}  # session_path → bytes consumed
        self._last_weekly_distill: datetime | None = None
        self._distillation_count: int = 0
        self._activity_lines: int = 0  # daily-log activity lines written this process
        self._last_run: datetime | None = None
        # Per-stage failure counters surfaced via ``status()`` so users see when
        # self-learning is silently degrading rather than only seeing log lines.
        self._failures: dict[str, int] = {}
        self._last_failure: tuple[str, str] | None = None  # (stage, message)
        self._memory_graph: MemoryGraph | None = None
        # Typed memory store — used to detect/flag contradictions
        self._typed_store: object | None = None  # TypedMemoryStore, lazy-loaded
        self._workspace_obj: Any = None  # memoized Workspace for skill enumeration

    @property
    def _workspace(self) -> Any:
        """Memoized ``Workspace`` for the skill helpers — built once instead of
        re-running ``Workspace.__init__`` (which mkdirs) on every draft cycle."""
        if self._workspace_obj is None:
            from olano.config.workspace import Workspace

            self._workspace_obj = Workspace(root=self.workspace_root)
        return self._workspace_obj

    def reset_failures(self, stage: str | None = None) -> int:
        """Clear recorded failure counters. With ``stage``, clears only that one
        stage (so dismissing one Self-Checks row doesn't wipe sibling rows);
        without it, clears all. Returns how many stages were cleared. Used by the
        dashboard's Self-Checks "Dismiss" action to acknowledge a stale/transient
        failure so it leaves the issue list."""
        if stage is not None:
            if stage not in self._failures:
                return 0
            del self._failures[stage]
            if self._last_failure and self._last_failure[0] == stage:
                self._last_failure = None
            return 1
        cleared = len(self._failures)
        self._failures.clear()
        self._last_failure = None
        return cleared

    def _record_failure(self, stage: str, exc: BaseException) -> None:
        """Tally a typed failure and emit a structured log line.

        ``stage`` is a short identifier — ``distill_llm``, ``rag_save``,
        ``skill_parse``, ``weekly``, ``self_improve`` — so the ``status()``
        snapshot tells you *where* things are going wrong.  The first
        recurrence is escalated to ``logger.error`` so log scrapers can pick
        it up; the counter is exposed via ``status()`` so users can spot
        sustained degradation.
        """
        previous = self._failures.get(stage, 0)
        self._failures[stage] = previous + 1
        msg = f"{type(exc).__name__}: {exc}"
        self._last_failure = (stage, msg)
        if previous >= 1:
            logger.error(
                "self_learning[%s] repeat failure (%d): %s",
                stage,
                self._failures[stage],
                msg,
            )
        else:
            logger.warning("self_learning[%s] failure: %s", stage, msg)

    # ----------------------------------------------------------------- helpers

    def _get_typed_store(self):
        """Lazy-load TypedMemoryStore (avoids import at module level)."""
        if self._typed_store is None:
            from olano.memory.typed_memory import TypedMemoryStore

            mem_dir = self._state_dir / "state" / "memory"
            self._typed_store = TypedMemoryStore(state_memory_dir=mem_dir)
        return self._typed_store

    def _flush_contradictions_to_pulse(self, contradictions: list) -> None:
        """No-op: contradiction resolution is handled by the agent autonomously."""
        # Contradictions are logged at DEBUG level so the agent can learn from them
        # without surfacing approval requests the user never asked for.
        for event in contradictions:
            logger.debug("self_learning: contradiction detected: %s", event)

    # ----------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Start the background self-learning loop."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name=f"self_learn_{self.config.name}")
            logger.info("Self-learning service started for agent '%s'", self.config.name)

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("Self-learning service stopped for agent '%s'", self.config.name)

    # ----------------------------------------------------------------- loop

    async def _loop(self) -> None:
        while True:
            try:
                await self._run_cycle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                # Top-level safety net so the background task never dies.
                self._record_failure("cycle", e)
            await asyncio.sleep(self.interval)

    async def _run_cycle(self) -> None:
        # Activity first, and unconditionally: it is deterministic file I/O
        # with no model call, so it is not subject to the deep gate below —
        # which is the point. The distiller is skipped for the rest of the day
        # once Cortex has spent the shared deep-run cap, and a busy day is
        # exactly when "what did you do yesterday?" needs an answer.
        try:
            self._activity_lines += await asyncio.to_thread(self._log_activity)
        except Exception as e:
            self._record_failure("activity_log", e)

        # Regular cycle: distill from session transcripts
        await self._distill_from_sessions()
        self._last_run = datetime.now(timezone.utc)

        # Embed MEMORY.md into RAG after each distillation cycle
        if self.rag_memory:
            await self._embed_memory_into_rag()

        # Weekly distillation: summarize daily logs into MEMORY.md
        now = datetime.now(timezone.utc)
        if self._should_run_weekly(now):
            await self._weekly_distillation_routed()
            self._last_weekly_distill = now

        # Sync graph entities back into MEMORY.md (P5.4). SQLite graph read +
        # whole-file MEMORY.md rewrite — keep it off the runtime event loop.
        try:
            await asyncio.to_thread(self.sync_graph_to_memory_md)
        except Exception as e:
            self._record_failure("graph_sync_cycle", e)

    def _log_activity(self) -> int:
        """Append the agent's new scheduled / peer / heartbeat / Cortex activity
        to the daily logs (``memory/activity_log.py``). Sync, corpus-scale I/O —
        always called via ``asyncio.to_thread``. Returns lines written."""
        from olano.memory.activity_log import ActivityLogger

        return ActivityLogger(
            self.config.name,
            self.workspace_root,
            self.markdown_memory,
            state_dir=self._state_dir,
        ).run()

    def _should_run_weekly(self, now: datetime) -> bool:
        if self._last_weekly_distill is None:
            return now.weekday() == 6  # Sunday
        return (now - self._last_weekly_distill).days >= 7

    # ----------------------------------------------------------------- session distillation

    async def _distill_from_sessions(self) -> None:
        # Back off while the memory gardener is rewriting MEMORY.md to avoid
        # clobbering its whole-file rewrite with a concurrent append.
        try:
            from olano.cognition.memory_gardener import is_garden_locked

            if is_garden_locked(self.workspace_root):
                logger.debug("self_learning: memory garden in progress, skipping distill cycle")
                return
        except Exception:
            pass

        # Corpus-scale file I/O — never on the runtime event loop that also
        # drives the API and channel pollers.
        new_turns, pending = await asyncio.to_thread(self._scan_session_turns)

        if len(new_turns) < 4:
            # Nothing worth a deepagent run; commit so we don't reprocess these.
            self._commit_offsets(pending)
            return

        # Deep-only gate: when the budget OR the daily deep-run cap is exhausted,
        # SKIP this cycle (no cheap fallback) and retry the same turns next time.
        from olano.cognition.orchestrator import deep_gate
        from olano.cognition.router import record_deep_run

        ok, reason = deep_gate(self.config, self.workspace_root, force=False)
        if not ok:
            logger.debug(
                "self_learning: skipping distill cycle for '%s' — %s", self.config.name, reason
            )
            return  # offsets NOT committed → retried next cycle

        logger.info(
            "Self-learning: distilling %d new turns for agent '%s'",
            len(new_turns),
            self.config.name,
        )
        record_deep_run(self.workspace_root)
        facts = await self._distill_facts(new_turns)
        # The agentic pass ran — commit offsets even if it found nothing new.
        self._commit_offsets(pending)
        if facts:
            await self._save_facts(facts)
            self._distillation_count += 1
            # Extract reusable skills from turns (gated by the skills feature).
            from olano.config.system_settings import feature_enabled

            if feature_enabled("skills_optimization", self.config):
                await self._extract_skills_polished(new_turns)

    def _scan_session_turns(self) -> tuple[list[dict], dict[str, tuple[int, int]]]:
        """Collect unprocessed session turns. Returns ``(new_turns, pending)``
        where ``pending`` maps session_path → (lines processed, bytes consumed).

        Sync corpus-scale file I/O (glob, stat sort, tail reads, JSON parse) —
        must always be called via ``asyncio.to_thread``.

        ``pending`` is NOT committed here: a capped/skipped cycle must retry
        these turns next time rather than silently drop them. The one exception
        is the legacy line→byte offset conversion, committed immediately — it
        covers only already-distilled lines (so it can never skip an unprocessed
        turn) and deferring it would repeat the full-file scan every cycle.
        """
        sessions_dir = get_olano_paths().sessions_dir / self.config.name
        if not sessions_dir.exists():
            return [], {}

        # `olano_test_agent` threads are AgentFather driving this agent with
        # made-up inputs; distilling them would file test data as facts.
        session_files = [
            f
            for f in sessions_dir.glob("*.jsonl")
            if not f.stem.startswith(AGENT_TEST_THREAD_PREFIX)
        ]
        if not session_files:
            return [], {}

        # Prune offsets for session files that no longer exist (rotated/deleted)
        # so the maps can't grow without bound over a long-lived process.
        current_paths = {str(f) for f in session_files}
        for tracked in (self._processed_offsets, self._processed_byte_offsets):
            for stale in [k for k in tracked if k not in current_paths]:
                del tracked[stale]

        pending: dict[str, tuple[int, int]] = {}
        new_turns: list[dict] = []
        for session_file in sorted(session_files, key=lambda f: f.stat().st_mtime):
            key = str(session_file)
            lines_done = self._processed_offsets.get(key, 0)
            offset = self._processed_byte_offsets.get(key)
            if offset is None:
                offset = _byte_offset_after_line(session_file, lines_done) if lines_done else 0
                if offset is None:
                    continue  # unreadable right now — retry the conversion next cycle
                self._processed_byte_offsets[key] = offset
            new_lines, new_offset = _read_new_lines(session_file, offset)
            if new_lines:
                for line in new_lines:
                    try:
                        new_turns.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
                pending[key] = (lines_done + len(new_lines), new_offset)
        return new_turns, pending

    def _commit_offsets(self, pending: dict[str, tuple[int, int]]) -> None:
        """Mark ``pending`` turns as distilled — both maps advance together so a
        line-count entry never goes stale against its byte twin."""
        for key, (lines, byte_offset) in pending.items():
            self._processed_offsets[key] = lines
            self._processed_byte_offsets[key] = byte_offset

    async def _run_deep_worker(
        self,
        *,
        goal: str,
        playbook: str,
        tools: list[str],
        label: str,
        context: str | None = None,
    ):
        """Run a headless deepagent and return its :class:`WorkerOutcome`.

        Records a typed failure (surfaced via ``status()`` → the Self-Checks tab)
        when the worker errors, but always returns an outcome so callers can decide
        whether to skip silently (background distill) or surface the error (the
        engine's improve phase). Budget/cap are gated by the caller, so the worker
        runs with ``budget_check=False``.
        """
        from olano.cognition.worker import WorkerOutcome

        try:
            from olano.cognition.router import effective_deep_model
            from olano.cognition.worker import run_worker

            outcome = await run_worker(
                parent_config=self.config,
                workspace_root=self.workspace_root,
                playbook=playbook,
                goal=goal,
                context=context,
                tools_allow=tools,
                model=effective_deep_model(self.config.cognition, self.config.model),
                budget_check=False,
                worker_label=label,
            )
        except Exception as e:
            self._record_failure(f"{label}_worker", e)
            return WorkerOutcome(status="error", error=str(e))
        if outcome.status not in ("completed", "empty"):
            self._record_failure(f"{label}_worker", RuntimeError(outcome.error or outcome.status))
        return outcome

    def _parse_json_list(self, text: str, *, stage: str) -> list:
        """Extract a JSON array from a worker's summary text.

        Tolerates a ```json code fence and the common ``{"skills": [...]}`` /
        ``{"items": [...]}`` wrapper models emit despite being asked for a bare
        array — otherwise a renamed wrapper would silently yield zero items."""
        import re

        def _unwrap(data: object) -> list | None:
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                for key in ("skills", "items", "ideas", "facts", "results", "data", "list"):
                    val = data.get(key)
                    if isinstance(val, list):
                        return val
            return None

        fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
        for cand in ([fenced.group(1)] if fenced else []) + [text]:
            try:
                out = _unwrap(json.loads(cand))
                if out is not None:
                    return out
            except json.JSONDecodeError:
                pass
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group())
            return data if isinstance(data, list) else []
        except json.JSONDecodeError as parse_exc:
            self._record_failure(stage, parse_exc)
            return []

    async def _distill_facts(self, turns: list[dict]) -> list[str]:
        """Agentic fact distillation — a headless deepagent reasons over the turns,
        dedupes against memory, optionally verifies, and returns durable facts.

        No single-shot ``model.ainvoke``: this is a real reasoning run.
        """
        conversation = "\n".join(
            f"[{t.get('role', '?').upper()}]: {t.get('content', '')[:500]}" for t in turns[-40:]
        )
        from olano.cognition.playbooks import FACT_DISTILL_PLAYBOOK

        outcome = await self._run_deep_worker(
            goal=(
                "From the conversation turns in your CONTEXT, extract the NEW, durable "
                "facts worth remembering. Return ONLY a JSON array of strings."
            ),
            playbook=FACT_DISTILL_PLAYBOOK,
            tools=["read_file", "ls", "memory_read", "memory_search", "web_search", "web_fetch"],
            label="fact_distill",
            context=f"Recent conversation turns:\n{conversation}",
        )
        if outcome.status not in ("completed", "empty") or not outcome.summary:
            return []
        facts = self._parse_json_list(outcome.summary, stage="distill_parse")
        return [str(f) for f in facts if isinstance(f, str) and f.strip()]

    async def _save_facts(self, facts: list[str]) -> None:
        """Persist distilled facts to daily log, MEMORY.md, RAG store, and typed memory."""
        # Append to today's daily log
        daily_entry = "Self-learned facts:\n" + "\n".join(f"- {f}" for f in facts)
        self.markdown_memory.append_daily(daily_entry)

        # Also curate into MEMORY.md under agent-specific section
        section = f"Self-Learned ({self.config.name})"
        for fact in facts:
            self.markdown_memory.append_fact(fact, section=section)
            if self.rag_memory:
                try:
                    # Embeds — keep it off the event loop (see _embed_memory_into_rag).
                    await asyncio.to_thread(
                        self.rag_memory.add,
                        fact,
                        metadata={"source": "self_learning", "agent": self.config.name},
                    )
                except Exception as e:
                    # RAG failure shouldn't block markdown memory updates.
                    self._record_failure("rag_save", e)

        # Store as typed semantic facts — contradiction detection happens here.
        try:
            typed_store = self._get_typed_store()
            for fact in facts:
                typed_store.store_fact(
                    fact,
                    memory_type="semantic",
                    confidence=0.8,
                    source=f"self_learning:{self.config.name}",
                )
            contradictions = typed_store.flush_contradictions()
            self._flush_contradictions_to_pulse(contradictions)
        except Exception as e:
            self._record_failure("typed_save", e)

        # Extract entities from facts into the memory graph (P5.4).
        try:
            self._extract_entities_from_facts(facts)
        except Exception as e:
            self._record_failure("graph_extract", e)

        logger.info(
            "Self-learning: saved %d facts for agent '%s'",
            len(facts),
            self.config.name,
        )
        # Keep the Self-Learned section bounded so it never grows into bloat.
        # dedupe_facts handles exact + subject-prefix duplicates; trim_section
        # caps the total bullet count after dedup.
        section = f"Self-Learned ({self.config.name})"
        try:
            self.markdown_memory.trim_section(section, max_bullets=50)
        except Exception as e:
            self._record_failure("trim_section", e)
        self.markdown_memory.summarize_and_trim()

    # ----------------------------------------------------------------- skill extraction

    async def _draft_skill_candidates(self, turns: list[dict]) -> list[dict]:
        """Agentic skill scouting — a headless deepagent reads the agent's existing
        skills and the conversation, then returns candidate reusable skills.

        No single-shot ``model.ainvoke``: the worker reasons over the live skill
        library (via ls/read_file) to avoid near-duplicates.
        """
        conversation = "\n".join(
            f"[{t.get('role', '?').upper()}]: {t.get('content', '')[:400]}" for t in turns[-30:]
        )
        from olano.cognition.playbooks import SKILL_EXTRACT_PLAYBOOK

        # Enumerate EVERY skill the agent already has (incl. managed/third-party
        # ones its workspace-scoped file tools can't see) so the scout dedupes
        # against the full library, not just what it can ls under skills/.
        existing = self._existing_skills_digest()
        outcome = await self._run_deep_worker(
            goal=(
                "Identify reusable skills from the conversation turns in your CONTEXT, "
                "checking the existing skill library first. Return ONLY a JSON array of "
                "skill objects."
            ),
            playbook=SKILL_EXTRACT_PLAYBOOK,
            tools=["read_file", "ls", "memory_search"],
            label="skill_extract",
            context=(
                "Skills already available to this agent (do NOT recreate these — use "
                'action "skip" when a capability is already covered, or "update" with '
                'the existing skill\'s name as "target" to improve one):\n'
                f"{existing}\n\n"
                f"Recent conversation turns:\n{conversation}"
            ),
        )
        if outcome.status not in ("completed", "empty") or not outcome.summary:
            return []
        skills = self._parse_json_list(outcome.summary, stage="skill_parse")
        # Normalize renamed schema keys, then drop anything marked "skip" (already
        # covered) or with no usable name — so a paraphrased schema still lands.
        normalized = [self._normalize_skill_candidate(s) for s in skills]
        return [s for s in normalized if s is not None and str(s.get("action", "create")) != "skip"]

    @staticmethod
    def _normalize_skill_candidate(s: object) -> dict | None:
        """Map a model's skill object to canonical keys, tolerating schema drift.

        Models routinely rename ``name``→``skill_name``/``title`` or
        ``steps``→``instructions``; without this every such skill would collapse
        to a single ``unnamed-skill`` slug and be skipped as a duplicate."""
        if not isinstance(s, dict):
            return None

        def first(*keys: str) -> str:
            for k in keys:
                v = s.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            return ""

        def first_list(*keys: str) -> list:
            for k in keys:
                v = s.get(k)
                if isinstance(v, list) and v:
                    return v
            return []

        name = first("name", "skill_name", "title", "skill", "id")
        description = first("description", "summary", "desc", "what", "purpose")
        if not name and not description:
            return None
        if not name:
            name = description[:60]
        return {
            "name": name,
            "description": description or name,
            "trigger": first("trigger", "when", "use_when", "when_to_use", "applies_when"),
            "steps": first_list("steps", "instructions", "procedure", "how", "actions"),
            "tags": first_list("tags", "labels", "keywords", "categories"),
            "action": str(s.get("action") or "create").lower(),
            "target": first("target", "target_skill", "existing", "improves"),
            "update_reason": first("update_reason", "reason", "why", "justification"),
        }

    def _existing_skills_digest(self, max_skills: int = 200) -> str:
        """Return a compact 'name: description' list of EVERY skill the agent can
        currently use — across ALL source dirs (config skills, managed
        ``~/.olano/skills``, ``<ws>/skills``, and ``<ws>/skills/learned``).

        Fed into the skill-extraction prompt so the scout skips/updates instead of
        drafting near-duplicates. Crucially this includes managed/third-party
        skills the worker cannot see through its workspace-scoped file tools, so
        the agent stops re-learning capabilities it already has.
        """
        from olano.skills.loader import iter_agent_skills

        try:
            skills = iter_agent_skills(self.config, self._workspace)
        except Exception:
            return "(none yet)"
        if not skills:
            return "(none yet)"
        entries = sorted((s["name"], s.get("description", "")) for s in skills)
        return "\n".join(f"- {n}: {d}" for n, d in entries[:max_skills])

    @staticmethod
    def _skill_slug(name: str) -> str:
        """Convert a skill name to a kebab-case folder slug.

        ``summarize_emails`` → ``summarize-emails``
        ``GetCryptoPrice`` → ``get-crypto-price``

        The result is also the skill's ``name``, so it must be spec-legal:
        ``naming.skill_slug`` collapses runs (``foo - bar`` used to become
        ``foo---bar``, illegal as both name and folder) and caps the length.
        """
        import re

        from olano.skills.naming import skill_slug

        # Insert hyphen before uppercase runs following lowercase (camelCase).
        return skill_slug(re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name))

    def _skill_exists(self, slug: str) -> bool:
        """Return True if this slug or a near-duplicate already exists in ANY of the
        agent's skill source dirs (managed ``~/.olano/skills``, ``<ws>/skills``,
        or ``<ws>/skills/learned``) — so we never create a learned duplicate of a
        skill the agent already has, including managed/third-party ones."""
        import re

        from olano.skills.loader import iter_agent_skills

        slug_clean = re.sub(r"[^a-z0-9]", "", slug.lower())
        try:
            existing_slugs = [
                self._skill_slug(s["name"]) for s in iter_agent_skills(self.config, self._workspace)
            ]
        except Exception:
            existing_slugs = []
        for existing_slug in existing_slugs:
            existing_clean = re.sub(r"[^a-z0-9]", "", existing_slug.lower())
            if existing_clean == slug_clean:
                return True
            # Allow ONLY trivial variants (plural/typo): one slug is a prefix of the
            # other AND they differ by ≤2 chars. A blanket 6-char-prefix match would
            # wrongly fuse verb-prefixed siblings like deploy-frontend/deploy-backend.
            if (
                len(slug_clean) > 5
                and abs(len(existing_clean) - len(slug_clean)) <= 2
                and (existing_clean.startswith(slug_clean) or slug_clean.startswith(existing_clean))
            ):
                return True
        return False

    def _find_source_skill(self, slug: str) -> dict | None:
        """Locate a NON-learned source skill (managed ``~/.olano/skills`` or
        ``<ws>/skills``) whose slug matches ``slug``.

        Returns ``{"name", "location", "dir", "sha256"}`` for the original
        SKILL.md, or ``None``. Used when self-learning improves an existing
        skill: the improvement lives in a full-folder learned fork that records
        this provenance, so the original (possibly third-party) file is never
        overwritten.

        Scans the non-learned source dirs **directly** (highest priority first), not
        the name-deduped ``iter_agent_skills`` — otherwise an existing learned shadow
        of the same name would hide the original and break provenance on the second
        improvement. Hashes raw bytes so the recorded hash matches ``_stale_shadows``'
        byte hash regardless of line endings / encoding.
        """
        import hashlib
        import re

        from olano.skills.loader import _skill_source_dirs, discover_skills

        target_clean = re.sub(r"[^a-z0-9]", "", slug.lower())
        learned_root = (self.workspace_root / "skills" / "learned").resolve()
        dirs = [Path(d) for d in _skill_source_dirs(self.config, self._workspace)]
        for d in reversed(dirs):  # highest priority first; record the effective source
            try:
                if d.resolve() == learned_root:
                    continue  # never treat a learned shadow as the original
            except OSError:
                continue
            for s in discover_skills(d):
                if re.sub(r"[^a-z0-9]", "", self._skill_slug(s["name"]).lower()) != target_clean:
                    continue
                location = Path(str(s.get("location", "")))
                try:
                    location.resolve().relative_to(learned_root)
                    continue  # this skill lives under learned/ — not an original
                except (ValueError, OSError):
                    pass
                try:
                    content = location.read_bytes()
                except OSError:
                    continue  # try other candidates rather than aborting the search
                return {
                    "name": s["name"],
                    "location": str(location),
                    "dir": str(location.parent),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
        return None

    def _ensure_fork(self, slug: str, source: dict) -> Path | None:
        """Ensure ``skills/learned/<slug>/`` is a full copy-on-write fork of the
        original skill folder; return its path.

        First improvement forks the ENTIRE original folder (SKILL.md plus
        scripts/, references/, forms…) so the learned copy is self-contained and
        starts byte-identical — a summary can never replace a runbook again. A
        legacy machine-written SKILL.md-only stub in the way is snapshotted (for
        Undo) and replaced by the fork; a hand-written learned skill of the same
        name is respected and returned as-is (improvements append to it).
        """
        import shutil as _shutil

        from olano.skills.fork import fork_skill, is_fork, is_machine_written

        fork_dir = self.workspace_root / "skills" / "learned" / slug
        if fork_dir.exists():
            if is_fork(fork_dir):
                return fork_dir
            skill_md = fork_dir / "SKILL.md"
            if skill_md.is_file() and not is_machine_written(skill_md):
                return fork_dir  # user-authored learned override — never replace
            rels = [
                str(p.relative_to(self.workspace_root))
                for p in sorted(fork_dir.rglob("*"))
                if p.is_file()
            ]
            try:
                from olano.cognition.backups import snapshot_backup

                snapshot_backup(self.workspace_root, rels, "skill_fork_replace")
            except Exception as e:  # pragma: no cover - defensive
                logger.debug("skill fork: stub snapshot failed: %s", e)
            _shutil.rmtree(fork_dir)
        return fork_skill(
            Path(str(source["location"])),
            self.workspace_root,
            slug,
            source_name=str(source["name"]),
        )

    def _append_learned_notes(self, skill_md: Path, skill: dict) -> None:
        """Deterministic, additive fallback improvement: append the distilled
        delta as a clearly-labelled section at the END of the fork's SKILL.md.
        Never rewrites existing content; skips when the same delta is already
        present (the scout often re-proposes an update it has made before)."""
        desc = str(skill.get("description") or "").strip()
        reason = str(skill.get("update_reason") or "").strip()
        steps = [str(s).strip() for s in (skill.get("steps") or []) if str(s).strip()]
        if not desc and not steps:
            return
        existing = skill_md.read_text(encoding="utf-8") if skill_md.is_file() else ""
        already = (not desc or desc in existing) and all(s in existing for s in steps)
        if already and (desc or steps):
            return
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        lines = ["", f"## Learned update ({date})", ""]
        if reason:
            lines += [f"_Why: {reason}_", ""]
        if desc:
            lines += [desc, ""]
        if steps:
            lines += [f"{i + 1}. {s}" for i, s in enumerate(steps)]
        skill_md.write_text(
            existing.rstrip("\n") + "\n" + "\n".join(lines) + "\n", encoding="utf-8"
        )

    def _write_skill_file(self, skill: dict) -> str:
        """Write a candidate skill to skills/learned/<slug>/; return the slug.

        Creates follow the standard skill structure: one folder per skill
        (kebab-case), ``SKILL.md`` inside with YAML frontmatter + markdown body.
        Skips writing if a near-duplicate already exists.

        Improvements (``action == "update"``) of an existing managed/workspace
        skill are applied to a full-folder **fork** (see ``_ensure_fork``): the
        fork reuses the original's exact ``name`` (so it shadows it under
        deepagents' last-source-wins), records provenance (``lc_improves`` /
        ``lc_source_path`` / ``lc_source_sha256`` + a ``.lc-provenance.json``
        per-file manifest), and the delta is appended additively. The original
        file is never touched, so a future third-party refresh can be detected
        (source manifest drift) and re-merged.
        """
        action = str(skill.get("action", "create")).lower()
        display_name = str(skill.get("name") or "unnamed_skill")
        # "update" improves an existing skill; resolve the slug from the target the
        # model named and improve a learned fork rather than overwriting the source.
        if action == "update" and skill.get("target"):
            slug = self._skill_slug(str(skill.get("target")))
            source = self._find_source_skill(slug)
            if source:
                fork_dir = self._ensure_fork(slug, source)
                if fork_dir is not None:
                    self._append_learned_notes(fork_dir / "SKILL.md", skill)
                    logger.info("Self-learning: improved fork skills/learned/%s (additive)", slug)
                return slug
            # The target didn't resolve to a real non-learned original (typo,
            # hallucination, or already a learned-only skill). Degrade to a normal
            # new learned skill named by slug — NOT a mis-named, provenance-less
            # duplicate that fails to shadow anything.
            fm_name = slug
        else:
            slug = self._skill_slug(display_name)
            # Learned skills have historically used the kebab slug as their name.
            fm_name = slug
        if not slug:
            slug = "unnamed-skill"
            fm_name = fm_name or slug

        if self._skill_exists(slug):
            logger.info("Self-learning: skill '%s' already exists, skipping", slug)
            return slug

        skill_dir = self.workspace_root / "skills" / "learned" / slug
        skill_dir.mkdir(parents=True, exist_ok=True)

        desc = skill.get("description", "")
        trigger = skill.get("trigger", "")
        steps = skill.get("steps", [])
        tags = skill.get("tags", [])
        steps_md = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))

        fm_lines = [
            f"name: {fm_name}",
            f"description: {desc}",
            f'trigger: "{trigger}"',
            "tier: light",
            "auto_learned: true",
            "category: learned",
        ]
        if tags:
            fm_lines.append("tags:")
            fm_lines += [f"  - {t}" for t in tags]

        content_md = (
            "---\n"
            + "\n".join(fm_lines)
            + "\n---\n\n"
            + f"## {display_name}\n\n{desc}\n\n**Trigger:** {trigger}\n\n**Steps:**\n{steps_md}\n"
        )
        (skill_dir / "SKILL.md").write_text(content_md, encoding="utf-8")
        logger.info("Self-learning: wrote skill '%s' at skills/learned/%s/SKILL.md", fm_name, slug)
        return slug

    def _capture_learned_skills_pulse(self, names: list[str]) -> None:
        if not names:
            return
        try:
            from olano.pulse.generators import PulseGenerator

            PulseGenerator(self.config, self.workspace_root).capture_learned_skills(names)
        except Exception as e:
            self._record_failure("skill_pulse", e)
            return
        # "Fully Autonomous": keep these skills without a human click. Background
        # distillation posts skill pulses outside the cognition phase runner, so
        # approve them here too. No-op when the auto-approve switch is off.
        try:
            from olano.cognition.orchestrator import auto_approve_cognition_items

            auto_approve_cognition_items(self.config, self.workspace_root)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("self_learning: auto-approve after skills failed: %s", e)

    async def _extract_skills(self, turns: list[dict]) -> None:
        """Extract reusable skill patterns from conversation turns (FAST direct write)."""
        try:
            skills = await self._draft_skill_candidates(turns)
            if not skills:
                return
            learned = [self._write_skill_file(skill) for skill in skills]
            self._capture_learned_skills_pulse(learned)
        except json.JSONDecodeError as e:
            self._record_failure("skill_parse", e)
        except OSError as e:
            self._record_failure("skill_io", e)
        except Exception as e:
            self._record_failure("skill_extract", e)

    async def _improve_existing_skill(self, candidate: dict) -> str | None:
        """Improve an existing skill through its copy-on-write fork (DEEP).

        Deterministic fork first (never lossy — see ``_ensure_fork``), then a
        deep worker applies the candidate as a CHANGE REQUEST across the whole
        fork (SKILL.md, scripts/, references/…). The preservation gate
        (``fork_violations``) then checks the fork against its recorded base: a
        violating or failed run is rolled back from the pre-run snapshot and the
        delta is preserved via the additive fallback instead. The old behaviour
        of rendering the tiny candidate JSON over the skill is gone — an
        improvement can no longer replace a runbook with a summary.
        """
        target = str(candidate.get("target") or candidate.get("name") or "")
        slug = self._skill_slug(target)
        source = self._find_source_skill(slug) if slug else None
        if not slug or source is None:
            # No real original to fork — same degrade path as the deterministic
            # writer (a clean learned skill named by slug, no fake provenance).
            try:
                return self._write_skill_file(candidate)
            except OSError as e:
                self._record_failure("skill_io", e)
                return None
        try:
            fork_dir = self._ensure_fork(slug, source)
        except OSError as e:
            self._record_failure("skill_io", e)
            return None
        if fork_dir is None:
            return None

        from olano.cognition.backups import restore, snapshot_backup
        from olano.cognition.playbooks import SKILL_IMPROVER_PLAYBOOK
        from olano.skills.fork import fork_violations, is_fork

        if not is_fork(fork_dir):
            # Hand-written learned override: the preservation gate has no base to
            # check against, so never let a worker rewrite user-authored content —
            # additive notes only.
            try:
                self._append_learned_notes(fork_dir / "SKILL.md", candidate)
            except OSError as e:
                self._record_failure("skill_io", e)
            return slug

        rel_files = [
            str(p.relative_to(self.workspace_root))
            for p in sorted(fork_dir.rglob("*"))
            if p.is_file()
        ]
        job_id = snapshot_backup(self.workspace_root, rel_files, "skill_improve")
        fork_rel = fork_dir.relative_to(self.workspace_root).as_posix()
        outcome = await self._run_deep_worker(
            goal=(
                f"Apply the CHANGE REQUEST in your CONTEXT to the skill fork at "
                f"{fork_rel}/ — targeted, additive edits that preserve all existing "
                f"content. End with an ARTIFACTS: line."
            ),
            playbook=SKILL_IMPROVER_PLAYBOOK,
            tools=["read_file", "write_file", "ls", "memory_read"],
            label="skill_improve",
            context=(
                f"Fork folder (edit ONLY files under this): {fork_rel}/\n"
                f"Files in the fork:\n" + "\n".join(f"- {f}" for f in rel_files) + "\n\n"
                f"CHANGE REQUEST (JSON):\n{json.dumps(candidate, indent=2, ensure_ascii=False)}"
            ),
        )
        # A worker that deleted the sidecar would otherwise skate past the gate
        # (no baseline to check) — treat that as a violation in its own right.
        violations = (
            fork_violations(fork_dir)
            if is_fork(fork_dir)
            else ["provenance sidecar removed by worker"]
        )
        if outcome.status not in ("completed", "empty") or violations:
            restore(self.workspace_root, job_id)
            if violations:
                self._record_failure("skill_improve_gate", RuntimeError("; ".join(violations[:3])))
                logger.warning(
                    "Self-learning: rolled back improvement of skills/learned/%s — %s",
                    slug,
                    "; ".join(violations[:3]),
                )
            # Deterministic fallback so the learning isn't lost entirely.
            try:
                self._append_learned_notes(fork_dir / "SKILL.md", candidate)
            except OSError as e:
                self._record_failure("skill_io", e)
        return slug

    async def _extract_skills_polished(self, turns: list[dict]) -> None:
        """Draft candidates (FAST), then validate/dedup/polish each via SkillSmith (DEEP).

        Falls back to a plain direct write when SkillSmith is disabled, over
        budget, or fails — so a skill is never silently lost.
        """
        if not getattr(self.config.cognition, "enable_skillsmith", True):
            await self._extract_skills(turns)
            return
        try:
            candidates = await self._draft_skill_candidates(turns)
        except Exception as e:
            self._record_failure("skill_parse", e)
            return
        if not candidates:
            return

        from olano.cognition.skillsmith import run_skillsmith

        learned: list[str] = []
        for candidate in candidates[:2]:  # cap learned skills per cycle
            # Improvements of an existing skill go through the fork-based improver:
            # deterministic full-folder fork (provenance always recorded, original
            # never overwritten) + deep worker + preservation gate.
            if str(candidate.get("action", "create")).lower() == "update":
                improved = await self._improve_existing_skill(candidate)
                if improved:
                    learned.append(improved)
                continue
            # CREATE: full-library duplicate guard. SkillSmith's own dedup only
            # scans skills/learned/ folder names, so without this a misclassified
            # "create" of an existing managed/workspace skill would land as a
            # provenance-less shadow of the original.
            create_slug = self._skill_slug(str(candidate.get("name") or ""))
            if create_slug and self._skill_exists(create_slug):
                logger.info(
                    "Self-learning: skill '%s' already exists, skipping create", create_slug
                )
                continue
            try:
                outcome = await run_skillsmith(self.config, self.workspace_root, candidate)
            except Exception as e:
                self._record_failure("skillsmith", e)
                outcome = None
            if outcome is not None and outcome.status == "completed":
                name = candidate.get("name")
                if name:
                    learned.append(name)
            else:
                # Fallback: direct write so the skill isn't lost.
                try:
                    learned.append(self._write_skill_file(candidate))
                except OSError as e:
                    self._record_failure("skill_io", e)
        self._capture_learned_skills_pulse(learned)

    # ----------------------------------------------------------------- RAG embedding

    async def _embed_memory_into_rag(self) -> None:
        """Embed MEMORY.md sections into the RAG 'memory' collection."""
        try:
            memory_path = self.workspace_root / "MEMORY.md"
            rag = self.rag_memory
            if not memory_path.exists() or rag is None:
                return

            # ingest_text chunks + embeds (and may cold-load the embedding
            # model), and MEMORY.md can grow whole-corpus large — run both the
            # read and the ingest off-loop so neither can stall the runtime
            # event loop that also drives the API and channel pollers.
            def _ingest() -> None:
                rag.ingest_text(
                    memory_path.read_text(encoding="utf-8"),
                    source_id="MEMORY.md",
                    collection="memory",
                )

            await asyncio.to_thread(_ingest)
            logger.debug("Self-learning: embedded MEMORY.md into RAG")
        except OSError as e:
            self._record_failure("rag_embed_io", e)
        except Exception as e:
            self._record_failure("rag_embed", e)

    # ----------------------------------------------------------------- self-improvement

    async def self_improve(self) -> str:
        """Agentic self-improvement.

        Delegates to the CortexEngine's ``improve`` phase: a headless deepagent
        reviews recent memory + sessions and writes grounded proposals to
        ``memories/improvement-proposals.md`` (SOUL.md/AGENTS.md are NEVER edited
        directly — proposals land in the APPROVAL lane). This is the single,
        fully-agentic improvement path (no ``model.ainvoke``).
        """
        try:
            from olano.cognition.orchestrator import run_phase

            result = await run_phase(
                "improve", self.config, self.workspace_root, source="manual", force=True
            )
        except Exception as e:
            self._record_failure("self_improve", e)
            return f"Self-improvement failed: {e}"

        status = result.get("status")
        if status == "disabled":
            return "Self-improvement is disabled for this agent."
        if status in ("skipped", "budget_blocked"):
            return f"Self-improvement skipped: {result.get('summary', '')}".strip()
        if status == "error":
            self._record_failure("self_improve", RuntimeError(result.get("summary", "error")))
            return f"Self-improvement failed: {result.get('summary', '')}".strip()
        return result.get("summary") or "Self-improvement proposals ready for review."

    def _parse_json_object(self, text: str, *, stage: str) -> dict:
        """Extract a JSON object from a worker's summary text (tolerates a fence)."""
        import re

        fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
        for cand in ([fenced.group(1)] if fenced else []) + [text]:
            try:
                data = json.loads(cand)
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                pass
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return {}
        try:
            data = json.loads(match.group())
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError as parse_exc:
            self._record_failure(stage, parse_exc)
            return {}

    @staticmethod
    def _normalize_proposals(proposals: dict) -> dict:
        """Map renamed self-improve keys to canonical ones, coercing each addition
        bucket to a list of strings — so a paraphrased schema still yields
        approve-to-apply proposals instead of silently empty buckets."""
        if not isinstance(proposals, dict) or not proposals:
            return {}

        def as_str_list(*keys: str) -> list[str]:
            for k in keys:
                v = proposals.get(k)
                if isinstance(v, str) and v.strip():
                    return [v.strip()]
                if isinstance(v, list):
                    out = [str(x).strip() for x in v if str(x).strip()]
                    if out:
                        return out
            return []

        def first_str(*keys: str) -> str:
            for k in keys:
                v = proposals.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            return ""

        return {
            "soul_additions": as_str_list(
                "soul_additions", "soul", "soul_md", "soul_updates", "soul_changes"
            ),
            "agents_additions": as_str_list(
                "agents_additions", "agents", "agents_md", "agents_updates", "agent_additions"
            ),
            "memory_additions": as_str_list(
                "memory_additions", "memory", "memory_md", "memory_updates"
            ),
            "memory_reorganization": first_str(
                "memory_reorganization", "memory_reorg", "reorganization", "memory_organization"
            ),
            "summary": first_str("summary", "overview", "rationale", "notes"),
        }

    async def _run_self_improve_worker(self) -> str:
        """IMPLEMENTATION of the improve phase (called by the CortexEngine, which
        has already gated budget + the daily cap).

        A headless deepagent grounds proposals in memory and returns them as JSON;
        we then write ``memories/improvement-proposals.md`` and route each addition
        through the APPROVAL lane as an approve-to-apply PROPOSAL item (SOUL.md /
        AGENTS.md are NEVER edited directly). No single-shot ``model.ainvoke``.
        """
        memory_content = self.markdown_memory.read()
        if not memory_content or len(memory_content) < 50:
            return "Not enough memory content to generate improvement proposals."

        from olano.cognition.playbooks import SELF_IMPROVE_AGENT_PLAYBOOK

        outcome = await self._run_deep_worker(
            goal=(
                "Review the agent's recent memory and sessions and propose CONCRETE, "
                "grounded improvements. Return ONLY a JSON object with keys "
                '"soul_additions" (list of strings), "agents_additions" (list), '
                '"memory_additions" (list), and "summary" (2-3 sentences). Do NOT edit '
                "any files — proposals require approval."
            ),
            playbook=SELF_IMPROVE_AGENT_PLAYBOOK,
            tools=["read_file", "ls", "memory_read", "memory_search", "web_search"],
            label="self_improve",
        )
        # Surface a real error (the engine classifies it: locked vault, bad key, …).
        if outcome.status not in ("completed", "empty"):
            raise RuntimeError(outcome.error or "self-improvement worker failed")
        if not outcome.summary:
            return "Self-improvement produced no proposals."
        proposals = self._normalize_proposals(
            self._parse_json_object(outcome.summary, stage="self_improve_parse")
        )
        if not proposals:
            return "Could not parse improvement proposals."

        # Write a dated proposals file (human-readable record).
        proposals_path = self.workspace_root / "memories" / "improvement-proposals.md"
        proposals_path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        soul_bullets = "\n".join(f"- {b}" for b in proposals.get("soul_additions", []))
        agents_bullets = "\n".join(f"- {b}" for b in proposals.get("agents_additions", []))
        summary = proposals.get("summary", "")
        md = (
            f"## Self-Improvement Proposals ({ts})\n\n"
            f"### Summary\n{summary}\n\n"
            f"### Suggested additions to SOUL.md\n{soul_bullets or '(none)'}\n\n"
            f"### Suggested additions to AGENTS.md\n{agents_bullets or '(none)'}\n\n---\n"
        )
        existing = proposals_path.read_text(encoding="utf-8") if proposals_path.exists() else ""
        proposals_path.write_text(md + existing, encoding="utf-8")

        # Route each addition through the APPROVAL lane as an approve-to-apply item.
        item_ids: list[str] = []
        try:
            from olano.pulse.generators import PulseGenerator

            pulse_items = PulseGenerator(
                self.config, self.workspace_root
            ).capture_self_improvement_proposals(proposals)
            item_ids = [item.id for item in pulse_items]
        except Exception as e:
            self._record_failure("self_improve_pulse", e)

        logger.info("Self-improvement proposals saved to %s", proposals_path)

        display = [summary, ""]
        if proposals.get("soul_additions"):
            display.append("**Suggested SOUL.md additions:**")
            display += [f"  • {b}" for b in proposals["soul_additions"]]
            display.append("")
        if proposals.get("agents_additions"):
            display.append("**Suggested AGENTS.md additions:**")
            display += [f"  • {b}" for b in proposals["agents_additions"]]
            display.append("")
        if item_ids:
            display.append(f"Queued PULSE proposal items: {', '.join(item_ids)}")
        display.append(f"Full proposals saved to: {proposals_path}")
        return "\n".join(display)

    # ----------------------------------------------------------------- P5.4: graph sync

    def _get_memory_graph(self):
        """Lazy-load MemoryGraph (SQLite-backed entity graph)."""
        if not hasattr(self, "_memory_graph") or self._memory_graph is None:
            from olano.memory.memory_graph import MemoryGraph

            db_path = self._state_dir / "state" / "memory" / "graph.db"
            self._memory_graph = MemoryGraph(db_path=db_path)
        return self._memory_graph

    def _extract_entities_from_facts(self, facts: list[str]) -> None:
        """Heuristically extract entities from distilled fact strings and upsert into the graph.

        Simple patterns: quoted terms, "X is a Y" phrasing, and Title-Case proper
        nouns. A large stop-word set prevents common English words, greetings, and
        verbs from polluting the Knowledge Graph with noise like "hi", "yoo",
        "Building", "Want", "Considering".
        """
        import re

        graph = self._get_memory_graph()

        # Words that should NEVER become graph entities.
        _STOP: frozenset[str] = frozenset(
            {
                # Articles / determiners
                "The",
                "This",
                "That",
                "These",
                "Those",
                "A",
                "An",
                # Question words
                "When",
                "Where",
                "Which",
                "Who",
                "What",
                "How",
                "Why",
                # Conjunctions / prepositions
                "And",
                "But",
                "For",
                "With",
                "From",
                "Into",
                "Over",
                "Then",
                "Also",
                "About",
                "After",
                "Before",
                "During",
                "Between",
                "Among",
                # Generic nouns / pronouns that aren't entities
                "User",
                "Agent",
                "People",
                "Person",
                "Time",
                "Day",
                "Year",
                "Version",
                # Common verbs / gerunds frequently Title-Cased in sentences
                "Uses",
                "Use",
                "Has",
                "Have",
                "Is",
                "Are",
                "Was",
                "Were",
                "Works",
                "Working",
                "Work",
                "Builds",
                "Building",
                "Build",
                "Gets",
                "Getting",
                "Get",
                "Wants",
                "Want",
                "Runs",
                "Run",
                "Running",
                "Does",
                "Doing",
                "Developing",
                "Develop",
                "Considers",
                "Considering",
                "Interested",
                "Interests",
                "Prefers",
                "Prefer",
                "Likes",
                "Like",
                "Needs",
                "Need",
                "Tracks",
                "Track",
                "Tracking",
                "Expects",
                "Expect",
                "Saves",
                "Save",
                "Saving",
                # Common adjectives / adverbs
                "Always",
                "Never",
                "Often",
                "Usually",
                "Mostly",
                "Currently",
                "Technical",
                "Casual",
                "Direct",
                "Brief",
                "Short",
                "Long",
                # Generic IT/software words too broad to be useful entities
                "Admin",
                "Tech",
                "Stack",
                "System",
                "Data",
                "File",
                "Assistant",
                "Platform",
                "Service",
                "Setup",
                "Config",
                # Informal greetings / words that appear in chat logs
                "Hi",
                "Hey",
                "Yoo",
                "Yooo",
                "Meow",
                "Hello",
                "Thanks",
                "Yes",
                "No",
                "Ok",
                "Okay",
                # Other frequent false-positives from graph dump
                "Asian",
                "Asia",
            }
        )

        # Only extract entities from facts that look like structured knowledge
        # (not conversational scraps). Require ≥30 chars to skip one-liners.
        for fact in facts:
            if len(fact) < 30:
                continue

            # "X is a <type>" — richer: store with entity type
            is_a = re.compile(
                r"\b([A-Z][A-Za-z0-9_\-]{2,30})\s+is\s+(?:a|an|the)\s+([a-z][a-z ]{2,30})\b",
                re.IGNORECASE,
            )
            for m in is_a.finditer(fact):
                name, etype = m.group(1), m.group(2).strip()
                if name in _STOP or name.lower() in {s.lower() for s in _STOP}:
                    continue
                type_map = {
                    "tool": "tool",
                    "language": "concept",
                    "framework": "tool",
                    "library": "tool",
                    "project": "project",
                    "company": "org",
                    "person": "person",
                    "concept": "concept",
                    "service": "tool",
                }
                canonical = next((v for k, v in type_map.items() if k in etype.lower()), "concept")
                try:
                    graph.upsert_entity(name, canonical, description=fact[:120])
                except Exception:
                    pass

            # Quoted terms → concept (e.g. "Python", 'Django')
            quoted = re.compile(r'["\']([A-Za-z][A-Za-z0-9_\- ]{2,40})["\']')
            for m in quoted.finditer(fact):
                name = m.group(1).strip()
                if name in _STOP or len(name) < 3:
                    continue
                try:
                    graph.upsert_entity(name, "concept", description=fact[:120])
                except Exception:
                    pass

            # Title-Case proper nouns ≥ 4 chars (harder threshold to reduce noise)
            proper = re.compile(r"\b([A-Z][a-z]{3,30}(?:\s[A-Z][a-z]{3,30}){0,2})\b")
            for m in proper.finditer(fact):
                name = m.group(1).strip()
                if name in _STOP or len(name) < 4:
                    continue
                # Skip words that end in common verb/adjective suffixes.
                if re.search(r"(ing|fers|acks|ects|ates|aves|ests|ikes|eeds)\s*$", name, re.I):
                    continue
                try:
                    graph.upsert_entity(name, "concept", description=fact[:120])
                except Exception:
                    pass

    def sync_graph_to_memory_md(self) -> bool:
        """Regenerate the auto-generated 'Knowledge Graph' section of MEMORY.md from the graph.

        Preserves all other sections the user may have written.  Returns True if MEMORY.md
        was updated, False if the graph was empty or MEMORY.md could not be read.
        """
        try:
            graph = self._get_memory_graph()
            new_section = graph.to_memory_md_section()
            if not new_section:
                return False

            content = self.markdown_memory.read()
            import re

            # Replace existing auto-generated section if present, otherwise append.
            pattern = re.compile(
                r"## Knowledge Graph\n<!-- auto-generated.*?(?=\n## |\Z)",
                re.DOTALL,
            )
            if pattern.search(content):
                updated = pattern.sub(new_section.rstrip() + "\n", content)
            else:
                updated = content.rstrip() + "\n\n" + new_section

            self.markdown_memory.write(updated)
            logger.info(
                "Graph sync: updated MEMORY.md Knowledge Graph section for agent '%s'",
                self.config.name,
            )
            return True
        except Exception as e:
            self._record_failure("graph_sync", e)
            return False

    def status(self) -> dict:
        """Return current service status."""
        return {
            "agent": self.config.name,
            "distillation_count": self._distillation_count,
            "activity_lines": self._activity_lines,
            "last_run": self._last_run.isoformat() if self._last_run else None,
            "last_weekly": self._last_weekly_distill.isoformat()
            if self._last_weekly_distill
            else None,
            "interval_seconds": self.interval,
            "running": self._task is not None and not self._task.done(),
            "failures": dict(self._failures),
            "last_failure": list(self._last_failure) if self._last_failure else None,
        }

    # ----------------------------------------------------------------- weekly distillation

    async def _weekly_distillation_routed(self) -> None:
        """Weekly consolidation — fully agentic, gated by feature + deep_self_learning.

        Deep-only: if deep self-learning is disabled or gated, the cycle is simply
        skipped (there is no cheap single-prompt fallback anymore).
        """
        from olano.config.system_settings import feature_enabled

        if not feature_enabled("self_learning", self.config):
            return
        if not getattr(self.config.cognition, "deep_self_learning", True):
            return
        await self._weekly_distillation()

    async def self_improve_deep(self) -> str:
        """Deep self-improvement — alias for the agentic :meth:`self_improve` path."""
        return await self.self_improve()

    async def _weekly_distillation(self) -> None:
        """Agentic weekly consolidation — a headless deepagent dedupes the week into
        durable learnings (no single-shot ``model.ainvoke``)."""
        daily_files = self.markdown_memory.list_daily_files(limit=7)
        if not daily_files:
            return

        from olano.cognition.orchestrator import deep_gate
        from olano.cognition.playbooks import SELF_LEARNING_AGENT_PLAYBOOK
        from olano.cognition.router import record_deep_run

        ok, reason = deep_gate(self.config, self.workspace_root, force=False)
        if not ok:
            logger.debug("self_learning: skipping weekly distillation — %s", reason)
            return
        record_deep_run(self.workspace_root)
        # The worker writes memory itself; failures are recorded inside _run_deep_worker
        # (surfaced via status() → the Self-Checks tab).
        await self._run_deep_worker(
            goal=(
                "Consolidate the past week of daily logs and recent sessions into clean, "
                "deduplicated, durable learnings. Read MEMORY.md and recent memories/*.md, "
                "dedupe against what is already saved, optionally verify a claim with web "
                "search, then write consolidated learnings to LEARNINGS.md and durable facts "
                "to MEMORY.md. End with an ARTIFACTS: line."
            ),
            playbook=SELF_LEARNING_AGENT_PLAYBOOK,
            tools=[
                "read_file",
                "write_file",
                "ls",
                "web_search",
                "web_fetch",
                "memory_search",
                "memory_read",
                "memory_compact",
                "cortex_guidance_save",
            ],
            label="weekly_distillation",
        )

    # ----------------------------------------------------------------- on-demand skill learning

    def _recent_turns(self, limit: int = 60) -> list[dict]:
        """Read the most recent session turns for this agent (newest sessions first)."""
        sessions_dir = get_olano_paths().sessions_dir / self.config.name
        if not sessions_dir.exists():
            return []
        turns: list[dict] = []
        files = sorted(sessions_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime)
        for sf in files[-5:]:
            try:
                for line in sf.read_text(encoding="utf-8").splitlines():
                    try:
                        turns.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            except OSError:
                continue
        return turns[-limit:]

    def _count_skill_files(self) -> int:
        skills_root = self.workspace_root / "skills"
        if not skills_root.exists():
            return 0
        # Folder form only — ``rglob`` already covers learned/<slug>/SKILL.md.
        return len([p for p in skills_root.rglob("SKILL.md") if p.is_file()])

    async def learn_skills_now(self) -> int:
        """Run the agentic skill-extraction pass over recent sessions on demand.

        Used by the CortexEngine ``skills`` phase (autonomous + manual "Think").
        Returns the number of new skill files drafted. Budget/cap are gated by the
        engine before this is called.
        """
        from olano.config.system_settings import feature_enabled

        if not feature_enabled("skills_optimization", self.config):
            return 0
        turns = self._recent_turns()
        if len(turns) < 2:
            return 0
        before = self._count_skill_files()
        await self._extract_skills_polished(turns)
        return max(0, self._count_skill_files() - before)
