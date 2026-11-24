"""Daily work journal — a structured record of what the agents did each day.

Each day is one JSON file at ``<home>/journal/YYYY-MM-DD.json`` combining:

- **metrics**: deterministic rollup of tokens / USD / calls / tool-calls / A2A
  messages for that day (always present — no LLM, no cost).
- **by_agent / by_model**: per-agent and per-model breakdowns for the day.
- **events**: cognition audit actions + cortex items touched that day.
- **narrative**: an *optional* LLM-written highlights paragraph, filled in only
  when explicitly requested (hybrid model — stats always, prose on demand). The
  narrative summarises the actual **work** the agents did — the conversations
  they handled, the scheduled tasks and heartbeat checks they ran, the
  agent-to-agent messages they exchanged, and their cortex activity per agent —
  and deliberately carries **no** usage statistics (tokens / spend / call
  counts / tool counts). Generated on demand and automatically each morning for
  the just-completed day.

The companion ``scripts/journal_to_markdown.py`` renders these JSON files to
human-readable markdown without needing the backend running.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from olano.analytics import aggregate
from olano.analytics.a2a_log import iter_a2a_events
from olano.config.home import get_olano_paths

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_MAX_TOP_TOOLS = 12

# Bounds for the highlights *work context* (conversations / tasks / heartbeat /
# agent-to-agent messages / cortex) so a busy day can't blow up the highlights
# prompt. These shape only the LLM input, never the deterministic metrics.
_MAX_CONV_AGENTS = 24
_MAX_CONV_THREADS = 6
_MAX_USER_TURNS_SHOWN = 2
_CONV_TURN_CHARS = 200
_MAX_TASKS_SHOWN = 20
_MAX_CORTEX_PER_GROUP = 15
_MAX_HEARTBEAT_PER_AGENT = 8
_MAX_AGENT_MESSAGES_SHOWN = 24
_HEARTBEAT_PREFIX = "heartbeat_"
# JSONL session threads with these prefixes are autonomous machinery, not
# user-facing conversations: peer (agent-to-agent) exchanges, delegated
# subagents, scheduled-task runs, and heartbeat polls. Each is summarised in its
# own dedicated highlights section (or sourced from the scheduler DB / the
# a2a activity log), so they're kept out of the per-agent *conversation* digest
# to avoid double-counting the same work under two headings.
_SYSTEM_THREAD_PREFIXES = (
    "acp::",
    "delegate::",
    "scheduled_",
    "issues_",
    "agenttest::",
    _HEARTBEAT_PREFIX,
)


def _is_user_thread_stem(stem: str) -> bool:
    """True if a JSONL thread file stem is a genuine user-facing conversation
    (not peer / delegated / scheduled / issue-poll / heartbeat machinery).

    A stem may carry the retired per-human ``u:<user_id>:`` namespace (the
    dashboard used to prepend it, and it forked scheduled runs a human replied
    to as ``u:<uid>:scheduled_…``); it comes off before the prefix check so
    those rows stay out of the conversation digest."""
    from olano.core.thread_labels import strip_human_namespace

    stem = strip_human_namespace(stem)
    return not any(stem.startswith(p) for p in _SYSTEM_THREAD_PREFIXES)


def journal_dir() -> Path:
    d = get_olano_paths().journal_dir
    d.mkdir(parents=True, exist_ok=True)
    return d


def journal_path(day: str) -> Path:
    return journal_dir() / f"{day}.json"


# ─────────────────────────────── day-scoped readers ──────────────────────────


def _day_usage(day: str) -> dict[str, Any]:
    """Per-day usage sourced from the thread checkpoint store (cache-aware, real
    per-call model) so the journal matches the Insights Overview + drill-down.

    Imported lazily to avoid pulling LangGraph at journal import time. Falls back to
    an empty rollup if the checkpoint store / cache is unavailable.
    """
    try:
        from olano.analytics import threadcost

        return threadcost.day_usage(day)
    except Exception:
        logger.debug("journal: threadcost day_usage unavailable", exc_info=True)
        return {
            "by_agent": {},
            "by_model": {},
            "totals": {
                "usd": 0.0,
                "credits": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
                "calls": 0,
            },
        }


def _day_tools(day: str) -> dict[str, dict[str, int]]:
    """Map ``agent -> {tool_name: count}`` for the given day."""
    sessions_dir = get_olano_paths().sessions_dir
    out: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    if not sessions_dir.exists():
        return out
    day_start = datetime.fromisoformat(f"{day}T00:00:00+00:00").timestamp()
    for agent_dir in sessions_dir.iterdir():
        if not agent_dir.is_dir():
            continue
        agent = agent_dir.name
        for f in agent_dir.glob("*.jsonl"):
            try:
                if f.stat().st_mtime < day_start:
                    continue
                lines = f.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                line = line.strip()
                if not line or '"tool_call"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict) or entry.get("role") != "tool_call":
                    continue
                if str(entry.get("ts") or "")[:10] != day:
                    continue
                # Subagent dispatches are tracked separately, not as tools; and
                # recover real names from legacy "<name>: …" content.
                if aggregate.is_subagent_call(entry):
                    continue
                out[agent][aggregate.tool_name_of(entry)] += 1
    return out


def _day_a2a(day: str) -> dict[str, dict[str, int]]:
    """Map ``agent -> {"sent": n, "received": m}`` for the given day."""
    out: dict[str, dict[str, int]] = defaultdict(lambda: {"sent": 0, "received": 0})
    for ev in iter_a2a_events():
        if ev.get("type") != "agent_message":
            continue
        if str(ev.get("ts") or "")[:10] != day:
            continue
        raw_data = ev.get("data")
        data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
        if str(data.get("phase") or "send") not in ("send", ""):
            continue
        out[str(data.get("from") or "unknown")]["sent"] += 1
        out[str(data.get("to") or "unknown")]["received"] += 1
    return out


def _gather_workspace_events(ws_root: Path, day: str) -> dict[str, list[dict[str, Any]]]:
    """Cognition audit actions + cortex items touched on ``day`` in one workspace.

    Best-effort — a missing/locked workspace yields empty lists rather than
    raising. Shared by ``_day_events`` (the default workspace, for the journal
    entry) and ``_day_cortex_by_agent`` (every agent's workspace, for the
    highlights narrative).
    """
    actions: list[dict[str, Any]] = []
    cortex: list[dict[str, Any]] = []
    try:
        from olano.cognition import audit

        for a in audit.read_actions(ws_root, limit=500):
            if str(a.get("ts") or "")[:10] == day:
                actions.append(
                    {
                        "action": a.get("action", ""),
                        "summary": a.get("summary", ""),
                        "ts": a.get("ts", ""),
                    }
                )
    except Exception:
        logger.debug("journal: cognition audit unavailable", exc_info=True)
    try:
        from olano.cognition import cortex_items

        for it in cortex_items.list_items(ws_root):
            created = str(it.get("created_at") or "")[:10]
            updated = str(it.get("updated_at") or "")[:10]
            if day in (created, updated):
                cortex.append(
                    {
                        "kind": it.get("kind", ""),
                        "title": it.get("title", ""),
                        "status": it.get("status", ""),
                        "summary": (it.get("summary") or "").strip(),
                        "created": created == day,
                        "ts": it.get("updated_at") or it.get("created_at") or "",
                    }
                )
    except Exception:
        logger.debug("journal: cortex items unavailable", exc_info=True)
    return {"cognition_actions": actions, "cortex": cortex}


def _day_events(day: str) -> dict[str, list[dict[str, Any]]]:
    """Cognition audit actions + cortex items touched on ``day`` in the default
    workspace (best-effort) — the events stored on the journal entry."""
    try:
        from olano.config.workspace import get_workspace

        ws_root = get_workspace().root
    except Exception:
        return {"cognition_actions": [], "cortex": []}
    return _gather_workspace_events(ws_root, day)


# ──────────────────────────────────── builder ────────────────────────────────


def build_journal_day(day: str, *, save: bool = True) -> dict[str, Any]:
    """Compute the deterministic journal entry for ``day`` (``YYYY-MM-DD``).

    Preserves any previously-generated ``narrative`` so rebuilding metrics never
    silently drops the LLM highlights.
    """
    usage = _day_usage(day)
    tools = _day_tools(day)
    a2a = _day_a2a(day)
    events = _day_events(day)

    agents = sorted(
        set(usage["by_agent"]) | set(tools) | set(a2a),
        key=lambda a: (
            -float(usage["by_agent"].get(a, {}).get("usd", 0.0)),
            -float(usage["by_agent"].get(a, {}).get("credits", 0.0)),
            a,
        ),
    )
    by_agent = []
    for a in agents:
        u = usage["by_agent"].get(a, {})
        agent_tools = tools.get(a, {})
        ranked = sorted(agent_tools.items(), key=lambda kv: kv[1], reverse=True)
        comm = a2a.get(a, {"sent": 0, "received": 0})
        by_agent.append(
            {
                "agent": a,
                "usd": round(float(u.get("usd", 0.0)), 6),
                # Olano-managed spend (usd stays 0 for those calls by design).
                "credits": round(float(u.get("credits", 0.0)), 6),
                "input_tokens": int(u.get("input_tokens", 0)),
                "output_tokens": int(u.get("output_tokens", 0)),
                "total_tokens": int(u.get("input_tokens", 0) + u.get("output_tokens", 0)),
                "calls": int(u.get("calls", 0)),
                "tool_calls": sum(agent_tools.values()),
                "top_tools": [{"tool": t, "count": c} for t, c in ranked[:_MAX_TOP_TOOLS]],
                "a2a_sent": comm["sent"],
                "a2a_received": comm["received"],
            }
        )

    by_model = []
    for m, agg in sorted(usage["by_model"].items(), key=lambda kv: kv[1]["usd"], reverse=True):
        by_model.append(
            {
                "model": m,
                "usd": round(float(agg["usd"]), 6),
                "credits": round(float(agg.get("credits", 0.0)), 6),
                "total_tokens": int(agg["input_tokens"] + agg["output_tokens"]),
                "calls": int(agg["calls"]),
            }
        )

    t = usage["totals"]
    total_tool_calls = sum(sum(v.values()) for v in tools.values())
    total_a2a = sum(v["sent"] for v in a2a.values())
    entry: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "date": day,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "metrics": {
            "usd": round(float(t["usd"]), 6),
            "credits": round(float(t.get("credits", 0.0)), 6),
            "input_tokens": int(t["input_tokens"]),
            "output_tokens": int(t["output_tokens"]),
            "total_tokens": int(t["input_tokens"] + t["output_tokens"]),
            "calls": int(t["calls"]),
            "tool_calls": total_tool_calls,
            "a2a_messages": total_a2a,
            "active_agents": [r["agent"] for r in by_agent],
        },
        "by_agent": by_agent,
        "by_model": by_model,
        "events": events,
        "narrative": None,
        # Per-agent highlights map ({agent: markdown}) + its lead line — the
        # source a member's scoped narrative is composed from (see
        # scoped_narrative). Empty until highlights are generated.
        "agent_highlights": {},
        "narrative_lead": None,
        "narrative_model": None,
    }

    # Don't clobber an existing narrative on a metrics-only rebuild — unless it
    # was written by the old extraction (a raw reasoning-block dump) or is a
    # legacy usage-statistics blurb, in which case drop it so it regenerates
    # cleanly as a work summary next time. The per-agent map rides along with it.
    existing = read_journal(day)
    prior = existing.get("narrative") if existing else None
    if existing and prior and not _should_discard_narrative(prior):
        entry["narrative"] = prior
        entry["narrative_lead"] = existing.get("narrative_lead")
        entry["narrative_model"] = existing.get("narrative_model")
        ah = existing.get("agent_highlights")
        if isinstance(ah, dict):
            entry["agent_highlights"] = ah

    if save:
        try:
            journal_path(day).write_text(
                json.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            logger.warning("journal: failed to write %s", day, exc_info=True)
    return entry


def read_journal(day: str) -> dict[str, Any] | None:
    path = journal_path(day)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def list_journal_days() -> list[str]:
    """Available journal dates, newest first."""
    d = get_olano_paths().journal_dir
    if not d.exists():
        return []
    days = [p.stem for p in d.glob("*.json")]
    days.sort(reverse=True)
    return days


def latest_active_day() -> str | None:
    """Most recent UTC day that has *any* recorded LLM usage, or None.

    Drives the journal's default selection so the dashboard opens on a day with
    something to show instead of an empty "today". Sourced from the thread
    checkpoint cache (same source as the Overview/drill-down). Falls back to the
    most recent journal file if the cache is empty/unavailable.
    """
    try:
        from contextlib import closing

        from olano.analytics.threadcost import cache as tc_cache

        with closing(tc_cache.connect()) as cc:
            row = cc.execute(
                "SELECT MAX(day) FROM thread_cost WHERE call_count > 0 AND day != ''"
            ).fetchone()
        if row and row[0]:
            return str(row[0])
    except Exception:
        logger.debug("journal: latest_active_day via threadcost failed", exc_info=True)
    days = list_journal_days()
    return days[0] if days else None


def latest_highlights() -> dict[str, Any]:
    """The most recent day that has a written highlights narrative, for the
    Overview page's daily-digest panel.

    Scans journal files newest-first and returns the first with a non-empty
    ``narrative`` (plus ``today`` so the UI's "Generate" button knows which day
    to (re)generate). Legacy usage-statistics blurbs and corrupt reasoning dumps
    are skipped so the panel prompts a fresh work-focused regeneration instead of
    surfacing stale cost stats. When nothing usable has been generated yet,
    ``narrative`` is ``None`` and ``date`` is the latest active day (falling back
    to today).
    """
    today = datetime.now(timezone.utc).date().isoformat()
    for d in list_journal_days():
        data = read_journal(d)
        if not data:
            continue
        narrative = (data.get("narrative") or "").strip()
        if narrative and not _should_discard_narrative(narrative):
            return {
                "date": d,
                "narrative": data.get("narrative"),
                # The per-agent map + lead so a member's Overview digest can be
                # composed to their granted agents (see scoped_narrative).
                "agent_highlights": data.get("agent_highlights") or {},
                "narrative_lead": data.get("narrative_lead"),
                "generated_at": data.get("generated_at"),
                "narrative_model": data.get("narrative_model"),
                "today": today,
            }
    return {
        "date": latest_active_day() or today,
        "narrative": None,
        "agent_highlights": {},
        "narrative_lead": None,
        "generated_at": None,
        "narrative_model": None,
        "today": today,
    }


# ───────────────────────────── day-scoped work readers ───────────────────────
# These feed the *highlights narrative* (the human story of what the agents did)
# and deliberately carry NO usage statistics — the narrative summarises real
# work: the conversations agents handled, the scheduled tasks and heartbeat
# checks they ran, the agent-to-agent messages they exchanged, and their
# autonomous cortex activity.


def _day_conversations(day: str) -> dict[str, list[dict[str, Any]]]:
    """Per-agent conversation digests for ``day`` from the JSONL session logs.

    Returns ``agent -> [ {thread, user_turns, assistant_turns, excerpt} ]`` with
    a compact, bounded excerpt of what the human asked and how the agent replied.
    Only genuine ``user`` / ``assistant`` turns are read (tool calls, system
    injections, and reasoning blocks are ignored), so the narrative model
    summarises the substance of the exchange rather than plumbing. Autonomous
    machinery threads (heartbeat, scheduled tasks, peer/subagent exchanges) are
    skipped here — they get their own dedicated highlights sections.
    """
    sessions_dir = get_olano_paths().sessions_dir
    out: dict[str, list[dict[str, Any]]] = {}
    if not sessions_dir.exists():
        return out
    day_start = datetime.fromisoformat(f"{day}T00:00:00+00:00").timestamp()
    for agent_dir in sorted(sessions_dir.iterdir()):
        if not agent_dir.is_dir():
            continue
        if len(out) >= _MAX_CONV_AGENTS:
            break
        agent = agent_dir.name
        threads: list[dict[str, Any]] = []
        for f in sorted(agent_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
            if len(threads) >= _MAX_CONV_THREADS:
                break
            if not _is_user_thread_stem(f.stem):
                continue
            try:
                if f.stat().st_mtime < day_start:
                    continue
                lines = f.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            users: list[str] = []
            assistants: list[str] = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("ts") or "")[:10] != day:
                    continue
                role = entry.get("role")
                content = str(entry.get("content") or "").strip()
                if not content:
                    continue
                if role == "user":
                    users.append(content)
                elif role == "assistant":
                    assistants.append(content)
            if not users and not assistants:
                continue
            parts: list[str] = []
            if users:
                shown = " / ".join(u[:_CONV_TURN_CHARS] for u in users[:_MAX_USER_TURNS_SHOWN])
                parts.append(f"asked: {shown}")
            if assistants:
                parts.append(f"replied: {assistants[-1][:_CONV_TURN_CHARS]}")
            threads.append(
                {
                    "thread": f.stem,
                    "user_turns": len(users),
                    "assistant_turns": len(assistants),
                    "excerpt": " · ".join(parts),
                }
            )
        if threads:
            out[agent] = threads
    return out


def _day_scheduled_tasks(day: str) -> list[dict[str, Any]]:
    """Scheduled-task runs that completed (or failed) on ``day``.

    Sourced from the scheduler DB's ``task_history`` so the narrative can report
    the concrete work the fleet's cron/heartbeat tasks did, with the owning agent
    and a short result/error snippet.
    """
    try:
        from olano.scheduler.db import SchedulerDB

        rows = SchedulerDB().history_for_day(day)
    except Exception:
        logger.debug("journal: scheduler history unavailable", exc_info=True)
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        name = (r.get("name") or "").strip()
        agent = (r.get("agent_name") or "").strip()
        if not agent:
            # A config-schedule run (``agent.json5 → schedule``) is recorded
            # under a synthetic ``cfg:<agent>:<index>`` id with no ``tasks``
            # row, so the JOIN yields nothing and the run used to reach the
            # journal as "(unnamed task)" with an empty agent — which
            # ``filter_work`` then dropped from every per-agent view. The
            # agent is in the id itself.
            agent, index = _config_job_owner(str(r.get("task_id") or ""))
            if agent and not name:
                name = f"{agent} schedule {index + 1}"
        out.append(
            {
                "name": name or "(unnamed task)",
                "agent": agent,
                "result": (r.get("result") or "").strip(),
                "error": (r.get("error") or "").strip(),
                "ran_at": r.get("ran_at") or "",
            }
        )
    return out


def _config_job_owner(task_id: str) -> tuple[str, int]:
    """``(agent, index)`` for a ``cfg:…`` config-schedule id, ``("", -1)`` otherwise."""
    from olano.admin.scheduler import is_config_job_id, parse_config_job_id

    if not is_config_job_id(task_id):
        return "", -1
    try:
        return parse_config_job_id(task_id)
    except ValueError:
        return "", -1


def _day_heartbeat(day: str) -> dict[str, list[dict[str, Any]]]:
    """Per-agent heartbeat activity for ``day`` from the ``heartbeat_<agent>``
    JSONL threads.

    Each heartbeat poll appends a ``HEARTBEAT: <task>`` user turn followed by the
    agent's response, so we pair them into ``agent -> [ {task, finding} ]``. A
    response of ``HEARTBEAT_OK`` (the "nothing to report" sentinel) is treated as
    a quiet, uneventful check and carries no finding. This lets the narrative
    describe the fleet's autonomous background checks as real work — never as
    usage statistics.
    """
    sessions_dir = get_olano_paths().sessions_dir
    out: dict[str, list[dict[str, Any]]] = {}
    if not sessions_dir.exists():
        return out
    day_start = datetime.fromisoformat(f"{day}T00:00:00+00:00").timestamp()
    for agent_dir in sorted(sessions_dir.iterdir()):
        if not agent_dir.is_dir():
            continue
        agent = agent_dir.name
        runs: list[dict[str, Any]] = []
        for f in sorted(agent_dir.glob(f"{_HEARTBEAT_PREFIX}*.jsonl")):
            try:
                if f.stat().st_mtime < day_start:
                    continue
                lines = f.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            pending_task: str | None = None
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("ts") or "")[:10] != day:
                    continue
                role = entry.get("role")
                content = str(entry.get("content") or "").strip()
                if not content:
                    continue
                if role == "user":
                    # "HEARTBEAT: <task>\n\nContext: …" → keep just the task label.
                    task = content
                    if task.upper().startswith("HEARTBEAT:"):
                        task = task.split(":", 1)[1].strip()
                    pending_task = task.splitlines()[0][:_CONV_TURN_CHARS] if task else None
                elif role == "assistant" and pending_task is not None:
                    finding = (
                        "" if content.strip() == "HEARTBEAT_OK" else content[:_CONV_TURN_CHARS]
                    )
                    runs.append({"task": pending_task, "finding": finding})
                    pending_task = None
                    if len(runs) >= _MAX_HEARTBEAT_PER_AGENT:
                        break
            if len(runs) >= _MAX_HEARTBEAT_PER_AGENT:
                break
        if runs:
            out[agent] = runs
    return out


def _day_agent_messages(day: str) -> list[dict[str, Any]]:
    """Agent-to-agent messages exchanged on ``day``, with their actual content.

    Sourced from the ``a2a_activity.jsonl`` peer-message log so the narrative can
    describe *what* the agents said to each other — who reached out to whom and
    the substance of the exchange — not merely how many messages flew. Prefers
    the ``reply`` phase (a complete ask→answer exchange) and falls back to any
    ``send`` that never produced a reply, deduped so a single exchange is never
    listed twice. Carries no counts or usage statistics.
    """
    replies: list[dict[str, Any]] = []
    sends: list[dict[str, Any]] = []
    seen_exchange: set[tuple[str, str, str]] = set()
    for ev in iter_a2a_events():
        if ev.get("type") != "agent_message":
            continue
        if str(ev.get("ts") or "")[:10] != day:
            continue
        raw_data = ev.get("data")
        data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
        sender = str(data.get("from") or "unknown")
        receiver = str(data.get("to") or "unknown")
        message = str(data.get("message") or "").strip()
        if not message:
            continue
        phase = str(data.get("phase") or "send")
        if phase == "reply":
            replies.append(
                {
                    "from": sender,
                    "to": receiver,
                    "message": message[:_CONV_TURN_CHARS],
                    "reply": str(data.get("reply") or "").strip()[:_CONV_TURN_CHARS],
                }
            )
            seen_exchange.add((sender, receiver, message))
        elif phase in ("send", ""):
            sends.append(
                {
                    "from": sender,
                    "to": receiver,
                    "message": message[:_CONV_TURN_CHARS],
                    "reply": "",
                    "_key": (sender, receiver, message),
                }
            )

    # A `send` whose exchange also has a `reply` is the same message twice —
    # keep only sends that never completed (errored / still in flight).
    out = list(replies)
    for s in sends:
        if s.pop("_key") not in seen_exchange:
            out.append(s)
    return out[:_MAX_AGENT_MESSAGES_SHOWN]


def _day_cortex_by_agent(day: str) -> list[dict[str, Any]]:
    """Cortex items + cognition actions touched on ``day``, grouped by the
    agent(s) whose workspace they live in.

    Iterates every configured agent's workspace (deduped by resolved path so
    agents that share a workspace are gathered once and co-attributed) to give
    the narrative genuine *per-agent* cortex activity. Falls back to the default
    workspace when the agent list can't be resolved.
    """
    from olano.config.workspace import get_workspace

    groups: list[dict[str, Any]] = []
    by_path: dict[str, dict[str, Any]] = {}
    try:
        from olano.config.loader import list_agents

        agent_names = list_agents()
    except Exception:
        agent_names = []

    for name in agent_names:
        try:
            from olano.config.loader import load_agent_config

            cfg = load_agent_config(name)
            ws_root = get_workspace(cfg.workspace).root
            key = str(ws_root.resolve())
        except Exception:
            continue
        existing = by_path.get(key)
        if existing is not None:
            existing["agents"].append(name)
            continue
        events = _gather_workspace_events(ws_root, day)
        group = {"agents": [name], **events}
        by_path[key] = group
        groups.append(group)

    if not groups:
        try:
            ws_root = get_workspace().root
            events = _gather_workspace_events(ws_root, day)
            if events["cognition_actions"] or events["cortex"]:
                groups.append({"agents": [], **events})
        except Exception:
            logger.debug("journal: default-workspace cortex unavailable", exc_info=True)
    return groups


def gather_day_work(day: str, *, agents: set[str] | None = None) -> dict[str, Any]:
    """Assemble the *work* signals for ``day`` that the highlights narrative
    summarises — the conversations agents handled, the scheduled tasks and
    heartbeat checks they ran, the agent-to-agent messages they exchanged, and
    their autonomous cortex activity.

    Intentionally free of any usage statistics (tokens / spend / call counts /
    tool counts): the narrative is a human story of what the fleet did, not a
    cost report. ``agents`` restricts the work to those agents (multi-user
    member scoping); A2A exchanges are kept when at least one participant is in
    the set (mirrors the dashboard's A2A visibility rule).
    """
    work = {
        "conversations": _day_conversations(day),
        "scheduled_tasks": _day_scheduled_tasks(day),
        "heartbeat": _day_heartbeat(day),
        "agent_messages": _day_agent_messages(day),
        "cortex_groups": _day_cortex_by_agent(day),
    }
    return filter_work(work, agents) if agents is not None else work


def filter_work(work: dict[str, Any], agents: set[str]) -> dict[str, Any]:
    """Scope an already-gathered work dict to ``agents`` (member view)."""
    conversations = {a: v for a, v in (work.get("conversations") or {}).items() if a in agents}
    heartbeat = {a: v for a, v in (work.get("heartbeat") or {}).items() if a in agents}
    tasks = [t for t in (work.get("scheduled_tasks") or []) if (t.get("agent") or "") in agents]
    messages = [
        m
        for m in (work.get("agent_messages") or [])
        if str(m.get("from") or "") in agents or str(m.get("to") or "") in agents
    ]
    groups: list[dict[str, Any]] = []
    for g in work.get("cortex_groups") or []:
        kept = [a for a in (g.get("agents") or []) if a in agents]
        if kept:
            groups.append({**g, "agents": kept})
    return {
        "conversations": conversations,
        "scheduled_tasks": tasks,
        "heartbeat": heartbeat,
        "agent_messages": messages,
        "cortex_groups": groups,
    }


def work_is_empty(work: dict[str, Any]) -> bool:
    """True when a day recorded no conversations, scheduled/heartbeat runs,
    agent-to-agent messages, or cortex activity."""
    if (
        work.get("conversations")
        or work.get("scheduled_tasks")
        or work.get("heartbeat")
        or work.get("agent_messages")
    ):
        return False
    for group in work.get("cortex_groups", []):
        if group.get("cortex") or group.get("cognition_actions"):
            return False
    return True


# ─────────────────────────────── LLM highlights ──────────────────────────────


def _looks_corrupt_narrative(text: str) -> bool:
    """True if a stored narrative contains a leaked raw reasoning/content block.

    Old highlight generation stringified non-text content blocks, leaving dumps
    like ``{'type': 'reasoning', 'encrypted_content': ...}`` in the saved
    narrative; detect those so a rebuild can discard and regenerate them.
    """
    return (
        "'type': 'reasoning'" in text
        or "'encrypted_content'" in text
        or '"encrypted_content"' in text
    )


def _looks_stats_narrative(text: str) -> bool:
    """True if a stored narrative is a legacy *usage-statistics* blurb rather than
    a summary of the fleet's actual work.

    Early highlights reported cost/usage ("Cindy executed 145 model calls … total
    spend of $1.03 on 8.3M tokens"). The work-focused prompt never emits those,
    so we detect and discard the legacy ones on the next rebuild — they then
    regenerate cleanly as a real work summary. Deliberately conservative: it
    fires only when a token reference is paired with a dollar amount, a model/API
    call count, or explicit spend wording, a fingerprint a genuine work narrative
    is virtually never going to match.
    """
    low = text.lower()
    has_tokens = "token" in low
    has_money = bool(re.search(r"\$\s?\d", text))
    has_calls = "model call" in low or "api call" in low
    has_spend = "spend" in low or "spent" in low
    return has_tokens and (has_money or has_calls or has_spend)


def _should_discard_narrative(text: str) -> bool:
    """True when a stored narrative must be dropped and regenerated — either a
    corrupt reasoning-block dump or a legacy usage-statistics blurb."""
    return _looks_corrupt_narrative(text) or _looks_stats_narrative(text)


def _extract_text(result: Any) -> str:
    """Pull human-readable text out of an LLM result, ignoring non-text blocks.

    Reasoning/extended-thinking models return ``content`` as a list of typed
    blocks; reasoning blocks have no ``text`` key (only ``encrypted_content`` /
    metadata). The previous ``str(b.get("text", b))`` dumped those raw dicts into
    the narrative. This extracts only genuine text: plain-string content,
    ``type=="text"`` blocks, and LangChain's ``.text()`` accessor; everything
    else (reasoning, thinking, tool_use, redacted) is skipped.
    """
    # LangChain AIMessage exposes a .text() helper that already flattens blocks.
    text_attr = getattr(result, "text", None)
    if callable(text_attr):
        try:
            flat = str(text_attr()).strip()
            if flat:
                return flat
        except Exception:
            pass

    content = getattr(result, "content", result)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                if block.strip():
                    parts.append(block.strip())
            elif isinstance(block, dict):
                btype = block.get("type")
                if btype in ("reasoning", "thinking", "redacted_thinking", "tool_use"):
                    continue
                t = block.get("text")
                if isinstance(t, str) and t.strip():
                    parts.append(t.strip())
        return " ".join(parts).strip()
    # A bare dict (rare) — pull a "text" key if present, but never dump the raw
    # dict repr into the narrative.
    if isinstance(content, dict):
        t = content.get("text")
        return t.strip() if isinstance(t, str) else ""
    return str(content or "").strip()


def _work_highlights_prompt(day: str, work: dict[str, Any]) -> str:
    """Render the day's *work* (conversations, scheduled tasks, heartbeat checks,
    agent-to-agent messages, cortex activity) into the highlights prompt body.
    Carries NO usage statistics by design."""
    lines: list[str] = [f"Date: {day}", ""]

    conversations: dict[str, list[dict[str, Any]]] = work.get("conversations", {})
    if conversations:
        lines.append("Conversations handled (by agent):")
        for agent, threads in conversations.items():
            lines.append(f"- {agent}: {len(threads)} conversation(s)")
            for th in threads:
                excerpt = str(th.get("excerpt") or "").strip()
                if excerpt:
                    lines.append(f"    • {excerpt}")
        lines.append("")

    tasks: list[dict[str, Any]] = work.get("scheduled_tasks", [])
    if tasks:
        lines.append("Completed scheduled tasks:")
        for tk in tasks[:_MAX_TASKS_SHOWN]:
            who = f"[{tk['agent']}] " if tk.get("agent") else ""
            if tk.get("error"):
                lines.append(f"- {who}{tk['name']} → FAILED: {tk['error'][:200]}")
            else:
                lines.append(f"- {who}{tk['name']} → {(tk.get('result') or 'completed')[:200]}")
        lines.append("")

    heartbeat: dict[str, list[dict[str, Any]]] = work.get("heartbeat", {})
    if heartbeat:
        lines.append("Heartbeat checks (autonomous background polls, by agent):")
        for agent, runs in heartbeat.items():
            lines.append(f"- {agent}: {len(runs)} check(s)")
            for run in runs:
                task = str(run.get("task") or "").strip()
                finding = str(run.get("finding") or "").strip()
                if finding:
                    lines.append(f"    • {task} → {finding}")
                elif task:
                    lines.append(f"    • {task} → nothing to report")
        lines.append("")

    messages: list[dict[str, Any]] = work.get("agent_messages", [])
    if messages:
        lines.append("Agent-to-agent messages exchanged:")
        for m in messages:
            sender = str(m.get("from") or "?").strip()
            receiver = str(m.get("to") or "?").strip()
            asked = str(m.get("message") or "").strip()
            answered = str(m.get("reply") or "").strip()
            line = f"- {sender} → {receiver}: {asked}"
            if answered:
                line += f" · answered: {answered}"
            lines.append(line)
        lines.append("")

    cortex_lines: list[str] = []
    for group in work.get("cortex_groups", []):
        label = ", ".join(group.get("agents", [])) or "fleet"
        for c in group.get("cortex", [])[:_MAX_CORTEX_PER_GROUP]:
            verb = "created" if c.get("created") else "updated"
            summary = f" — {c['summary'][:160]}" if c.get("summary") else ""
            cortex_lines.append(
                f"- [{label}] {verb} {c.get('kind')} '{c.get('title')}' "
                f"[{c.get('status')}]{summary}"
            )
        for a in group.get("cognition_actions", [])[:_MAX_CORTEX_PER_GROUP]:
            cortex_lines.append(f"- [{label}] {a.get('action')}: {a.get('summary')}")
    if cortex_lines:
        lines.append("Autonomous cortex activity:")
        lines.extend(cortex_lines)
        lines.append("")

    body = "\n".join(lines).strip()
    if work_is_empty(work):
        body += (
            "\n(No conversations, scheduled or heartbeat runs, agent-to-agent "
            "messages, or cortex activity were recorded.)"
        )
    return body


# Marker the highlights model wraps each agent's section in, so a single
# generation is machine-splittable into a per-agent map (which any view — a
# member's granted subset, the whole fleet, a weekly rollup — is composed from).
_AGENT_MARK = re.compile(r"^\s*\[\[agent:\s*(.+?)\s*\]\]\s*$", re.MULTILINE)
_LEAD_MARK = re.compile(r"^\s*LEAD:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)

HIGHLIGHTS_FORMAT_INSTRUCTIONS = (
    "You are the Olano operations journalist. Summarise the ACTUAL WORK the "
    "agents did — the substance of the conversations they handled, the scheduled "
    "tasks they completed, the heartbeat checks they ran, the agent-to-agent "
    "messages they exchanged, and their autonomous cortex activity. Focus on the "
    "topics, tasks, and outcomes.\n\n"
    "Output EXACTLY this structure and nothing else:\n"
    "LEAD: <one short sentence naming the most substantive work of the period>\n"
    "[[agent:<exact agent name>]]\n"
    "<1-2 lines on what THAT agent actually did and the outcome>\n"
    "[[agent:<next agent name>]]\n"
    "<...>\n\n"
    "Rules:\n"
    "- One [[agent:NAME]] section per agent that did genuine, meaningful work. "
    "Use the agent's EXACT name from the data. Omit agents with no notable "
    "activity entirely (do NOT invent an 'Others' section).\n"
    "- Keep each agent's summary to one or two lines; **bold** only the few most "
    "important terms, tasks, or outcomes — never whole sentences, never the "
    "agent name (the UI adds that).\n"
    "- Do NOT mention tokens, spend, dollar amounts, model calls, tool counts, "
    "or any usage statistics. No Markdown headings (#) or tables.\n"
    "- If the period was genuinely quiet, emit just the LEAD line saying so, "
    "with no agent sections.\n"
)


def _parse_agent_highlights(text: str) -> tuple[str, dict[str, str]]:
    """Split a marker-structured highlights generation into (lead, {agent: md}).

    Robust to models that drop the markers: when no ``[[agent:…]]`` sections are
    found, returns ``("", {})`` so the caller keeps the raw text as a single
    fleet narrative (legacy behaviour) rather than losing it.
    """
    lead_m = _LEAD_MARK.search(text)
    lead = lead_m.group(1).strip() if lead_m else ""
    sections: dict[str, str] = {}
    marks = list(_AGENT_MARK.finditer(text))
    for i, m in enumerate(marks):
        name = m.group(1).strip()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = " ".join(text[m.end() : end].split()).strip()
        if name and body:
            sections[name] = body
    return lead, sections


def compose_narrative(
    lead: str | None, agent_highlights: dict[str, str], agents: set[str] | None = None
) -> str:
    """Render a per-agent highlights map into scannable Markdown.

    ``agents`` restricts which agents appear (``None`` = all). Returns ``""``
    when nothing is in scope so callers can treat it as "no highlights".
    """
    items = agent_highlights or {}
    if agents is not None:
        items = {k: v for k, v in items.items() if k in agents}
    if not items:
        return ""
    bullets = [f"- **{name}** {text}" for name, text in items.items()]
    body = "\n".join(bullets)
    return f"{lead.strip()}\n\n{body}" if lead else body


def scoped_narrative(entry: dict[str, Any], agents: set[str] | None) -> str | None:
    """The highlights narrative for a viewer whose visible agents are ``agents``.

    ``agents is None`` (admin / single-user / "show all" mode) → the full stored
    fleet ``narrative``. Otherwise compose from the per-agent map so a member
    only ever sees their granted agents' story. A legacy entry with no per-agent
    map cannot be safely split, so a member gets ``None`` (nothing) there.
    """
    if agents is None:
        return entry.get("narrative")
    ah = entry.get("agent_highlights")
    if not isinstance(ah, dict) or not ah:
        return None
    return compose_narrative(entry.get("narrative_lead"), ah, agents) or None


async def generate_highlights(
    day: str,
    *,
    model: str | None = None,
    save: bool = True,
    work: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the day's entry (if needed) and attach a per-agent highlights map.

    A single LLM pass emits a marker-structured summary that is parsed into
    ``agent_highlights`` (``{agent: markdown}``) plus a ``narrative_lead``; the
    composed fleet ``narrative`` (all agents) is stored for back-compat and the
    admin / show-all view, while member views compose only their granted agents'
    entries from the same map (see :func:`scoped_narrative`).

    ``work`` accepts an already-gathered :func:`gather_day_work` dict so a
    caller that gathered it (e.g. to check emptiness first) doesn't pay the
    session-JSONL re-read; ``None`` gathers here. The gathering, journal reads,
    and model construction are blocking file/CPU work, so this coroutine runs
    them in a thread — a busy day's sessions can span hundreds of MB and must
    never be parsed on the event loop.

    Best-effort: on any failure the deterministic entry is returned with
    ``narrative`` left as ``None`` and a transient ``narrative_error`` field
    (never persisted) explaining what went wrong.
    """
    entry = await asyncio.to_thread(lambda: read_journal(day) or build_journal_day(day, save=save))
    entry.pop("narrative_error", None)
    model = model or await asyncio.to_thread(_resolve_highlights_model)
    if not model:
        entry["narrative_error"] = (
            "No model configured — set preferred_model in the dashboard Config "
            "view (or the cognition fast_model)."
        )
        return entry

    # The narrative summarises WORK, not usage — never tokens / spend / calls.
    if work is None:
        work = await asyncio.to_thread(gather_day_work, day)
    prompt = f"{HIGHLIGHTS_FORMAT_INSTRUCTIONS}\n{_work_highlights_prompt(day, work)}"
    try:
        from olano.models import build_model

        llm = await asyncio.to_thread(build_model, model)
        result = await llm.ainvoke(prompt)
        # This call runs outside any AgentRunner turn, so nothing else records
        # its spend. Fleet-level work → attributed to "system" (never a member
        # agent), origin-tagged so Insights can name it.
        from olano.analytics.usage_recorder import record_result_usage

        record_result_usage("system", model, result, origin="highlights")
        text = _extract_text(result)
        if text:
            _apply_highlights(entry, text, model)
            if save:
                await asyncio.to_thread(
                    journal_path(day).write_text,
                    json.dumps(entry, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
        else:
            entry["narrative_error"] = f"Model '{model}' returned no usable text."
    except Exception as exc:
        logger.warning("journal: highlight generation failed for %s", day, exc_info=True)
        detail = " ".join(str(exc).split())[:300] or type(exc).__name__
        entry["narrative_error"] = f"{model}: {detail}"
    return entry


def _apply_highlights(entry: dict[str, Any], text: str, model: str) -> None:
    """Store a raw highlights generation onto an entry as the per-agent map +
    composed fleet narrative (falling back to raw text if markers are absent)."""
    lead, sections = _parse_agent_highlights(text)
    entry["narrative_model"] = model
    if sections:
        entry["agent_highlights"] = sections
        entry["narrative_lead"] = lead or None
        entry["narrative"] = compose_narrative(lead, sections, None) or text
    else:
        # Model ignored the markers — keep its text as one fleet narrative; it
        # can't be split per agent, so members won't see it (admins/show-all do).
        entry["agent_highlights"] = {}
        entry["narrative_lead"] = None
        entry["narrative"] = text


def _resolve_highlights_model() -> str | None:
    """Pick the model to write the highlights with.

    Order: home ``preferred_model`` → the default agent's own model → the
    cognition ``fast_model`` (always set, so a fresh install still works even
    when the default agent doesn't exist and no preferred model was chosen —
    previously that combination made Generate highlights a silent no-op).
    """
    try:
        from olano.config.home import load_home_config

        preferred = load_home_config().preferred_model
        if preferred:
            return preferred
    except Exception:
        pass
    return _default_agent_model() or _cognition_fast_model()


def _cognition_fast_model() -> str | None:
    try:
        from olano.config.system_settings import load_system_settings

        return load_system_settings().fast_model or None
    except Exception:
        return None


def _default_agent_model() -> str | None:
    try:
        from olano.config.home import load_home_config
        from olano.config.loader import load_agent_config

        cfg = load_home_config()
        agent_cfg = load_agent_config(cfg.default_agent)
        return getattr(agent_cfg, "model", None)
    except Exception:
        return None
