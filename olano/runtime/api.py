"""
Runtime API — FastAPI app exposing all runtime state to the dashboard.

Endpoints:
  GET    /api/status                        — system health
  GET    /api/agents                        — list booted agents
  GET    /api/agents/{name}                 — full agent config (agent.json5) + display-only resolved views
  GET    /api/agents/{name}/config/raw      — verbatim agent.json5 text (source of truth, no injected fields)
  PUT    /api/agents/{name}/config/raw      — overwrite agent.json5 with raw text (validated, written verbatim)
  POST   /api/agents                        — create new agent
  POST   /api/agents/generate-prompt        — draft a system prompt via LLM (name/desc/tools)
  PUT    /api/agents/{name}                 — update agent config (typed partial patch)
  DELETE /api/agents/{name}                 — delete agent
  POST   /api/agents/{name}/subagents       — add subagent
  PUT    /api/agents/{name}/subagents/{sub} — update subagent
  DELETE /api/agents/{name}/subagents/{sub} — delete subagent
  POST   /api/agents/{name}/channels        — add channel
  PUT    /api/agents/{name}/channels/{type} — update channel
  DELETE /api/agents/{name}/channels/{type} — delete channel
  GET    /api/agents/{name}/connectors/health — connector diagnostics (Discord gateway, visibility, …)
  POST   /api/agents/{name}/mcp             — add MCP server
  DELETE /api/agents/{name}/mcp/{url}       — delete MCP server
  POST   /api/agents/{name}/mcp/test        — probe a server (http/sse/stdio) with full logs + OAuth detection
  GET    /api/agents/{name}/mcp/status      — live status of all configured servers
  PATCH  /api/agents/{name}/mcp/tools?url=… — set one server's disabled_tools (per-tool switches)
  POST   /api/agents/{name}/mcp/oauth/start — begin interactive OAuth (authorization-code + PKCE)
  GET    /api/agents/{name}/mcp/oauth/poll/{id} — poll OAuth flow status / authorization URL
  DELETE /api/agents/{name}/mcp/oauth?url=… — clear stored OAuth tokens for a server
  GET    /api/mcp/oauth/callback            — OAuth redirect target (matches state → completes flow)
  GET    /api/catalog/tools                 — tools (name/description/category/bundles) + bundles
  GET    /api/shell/stream?command=…        — SSE: live stdout/stderr of an allowlisted command
  GET    /api/catalog/skills                — list available skills
  GET    /api/catalog/channels              — list channel types
  GET    /api/catalog/providers             — list LLM providers + models
                                              (?fields=basic omits model data)
  GET    /api/credits                       — cost display unit (usd|credits) + the
                                              cloud-published olano-credit rate
  GET    /api/olano/credits                 — the deployment's olano-credit balance
                                              (cloud-authoritative, cached ~60s)
  POST   /api/chat                          — send message to agent
  GET    /api/stream                        — SSE stream of agent events
  GET    /api/scheduler/tasks               — list scheduled tasks
  POST   /api/scheduler/tasks               — create task
  PUT    /api/scheduler/tasks/{id}          — update task
  DELETE /api/scheduler/tasks/{id}          — delete task
  POST   /api/scheduler/tasks/{id}/run      — run a task now (DB or cfg: job)
  GET    /api/scheduler/history/{id}        — task execution history
  GET    /api/files                         — list workspace files (?hidden= for dot-entries)
  GET    /api/files/{path}                  — read file content
  GET    /api/memory                        — read MEMORY.md
  POST   /api/memory                        — append to MEMORY.md
  PUT    /api/memory                        — overwrite MEMORY.md (inline edit)
  GET    /api/notes                         — list/search workspace wiki notes
  GET    /api/notes/entry                   — read one note (+links/backlinks)
  POST   /api/notes                         — create or update a note
  DELETE /api/notes/entry                   — delete a note
  GET    /api/pulse?agent=&lane=             — list pending pulse/approval/ops items
  POST   /api/pulse/approve                  — approve a pulse/approval item
  POST   /api/pulse/reject                   — reject an item
  POST   /api/pulse/defer                    — defer an item
  POST   /api/pulse/generate                 — run agent-curated pulse generation
  POST   /api/self-improve                   — trigger a self-improvement cycle
  GET    /api/self-learning/status           — background service status (failures = issues)
  GET    /api/logs                          — recent log entries
"""

import asyncio
import copy
import hmac
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Optional

if TYPE_CHECKING:
    from fastapi import Request

    from olano.config.internal_tools import InternalToolRow
from olano.config.loader import _load_json5
from olano.deployment import enforcement as dep_enforcement
from olano.deployment import guard as dep_guard

# The Codex device-auth session table, bound to the SAME dict object the flow
# itself writes. The flow moved to models/codex_auth.py once the handshake page
# needed to drive it too; this alias keeps the routes here (and their tests)
# reading exactly what the poller writes.

logger = logging.getLogger(__name__)

# SSE fan-out: every connected /api/stream client gets its own queue. The
# previous design used a single shared queue, which meant only ONE consumer
# could ever pull a given event — so opening the dashboard in two tabs
# (chat + playground, say) silently dropped half the agent_activity /
# agent_message / log events to whichever tab woke up faster. The
# subscribers list is the source of truth; _push_event broadcasts.
_subscribers: list[asyncio.Queue] = []
_log_buffer: list = []
_MAX_LOG_LINES = 500


class _ApprovalSlot:
    """Bridges the runner's `approval_handler` callback (which awaits user
    decisions) to an HTTP POST from the dashboard. `future` is replaced after
    each round so the same thread can request approval multiple times in a
    single turn (e.g. several tool calls)."""

    def __init__(self) -> None:
        self.future: asyncio.Future = asyncio.Future()

    def reset(self) -> None:
        self.future = asyncio.Future()


# Thread-id → ApprovalSlot. Populated only while a manual/plan stream is
# in-flight; cleared in the SSE generator's `finally` block.
_pending_approvals: dict[str, _ApprovalSlot] = {}

# Client-minted steer ids (`POST /api/chat/steer`): a UUID hex or similar. Bounded
# and charset-guarded because the id becomes a LangGraph message id and a JSONL key.
_STEER_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")

# Prepended to the user's message in chat "Plan" mode so the agent drafts a plan
# before acting. Mirrors how the scheduler prefixes autonomous prompts.
_PLAN_MODE_PREAMBLE = (
    "[Plan mode] Draft a clear, numbered plan for the request below before "
    "acting. You may use read-only tools to investigate, but do NOT perform "
    "mutating or external actions yet — present the plan and wait for the user "
    "to approve or refine it.\n\n"
)

# Hard ceiling on how much of a workspace file `GET /api/files/{path}` reads.
# The dashboard asks for less (1 MiB) for a preview; this is the bound nothing
# can ask past, so the runtime never holds an arbitrary-size file in memory
# because a browser tab pointed at it.
_FILES_READ_MAX_BYTES_DEFAULT = 8 * 1024 * 1024


def _files_read_max_bytes() -> int:
    raw = os.environ.get("LIONCLAW_FILES_READ_MAX_BYTES", "").strip()
    try:
        value = int(raw) if raw else _FILES_READ_MAX_BYTES_DEFAULT
    except ValueError:
        value = _FILES_READ_MAX_BYTES_DEFAULT
    return max(1, value)


# Workspace basenames that feed the stable prompt sections
# (PromptAssembler.stable_sections). Saving one via the Files API nudges the
# prompt refresh sweep so the edit hot-applies to running agents.
_PROMPT_BEARING_BASENAMES = frozenset(
    {
        "AGENTS.md",
        "IDENTITY.md",
        "CORE_VIBES.md",
        "SAFETY_BOUNDARIES.md",
        "EXECUTION_BIAS.md",
        "TOOLS.md",
    }
)

# Module-level imports for test patching
from olano.core.thread_events import THREAD_UPDATED
from olano.core.thread_labels import split_sender_tag
from olano.magic_links.chat_delivery import EVENT_KIND as MAGIC_LINK_EVENT
from olano.runtime.security_headers import apply_security_headers
from olano.scheduler.db import SchedulerDB
from olano.security.rate_limit import RUNTIME_RATE_LIMITER
from olano.security.request_context import push_request_context


def _a2a_activity_path() -> Path:
    from olano.analytics.a2a_log import a2a_log_path

    path = a2a_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _append_a2a_event(event: dict[str, Any]) -> None:
    try:
        path = _a2a_activity_path()
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    except Exception:
        logger.debug("Failed to persist A2A activity event", exc_info=True)


def _highlights_show_all() -> bool:
    """Whether the work-highlights digests are shown in full to everyone
    (team-transparency mode, `highlights_all_agents` in config.yaml)."""
    try:
        from olano.config.home import load_home_config

        return bool(load_home_config().highlights_all_agents)
    except Exception:
        return False


def _scope_narrative_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Scope a payload carrying ``narrative`` + the per-agent ``agent_highlights``
    map to the viewer, and strip the internal map from the response.

    The narrative is the whole fleet's for admins / single-user, and — unless
    the ``highlights_all_agents`` team-transparency flag is on — only the
    viewing member's granted agents otherwise (composed from the per-agent map;
    a legacy entry without a map yields no narrative for a member). Shared by
    the daily journal, the Overview digest, and the weekly/monthly reports.
    """
    from olano.analytics import journal
    from olano.runtime import auth as auth_layer

    allowed = auth_layer.allowed_agent_names()
    narrative_agents = None if (allowed is None or _highlights_show_all()) else allowed
    data = dict(data)
    data["narrative"] = journal.scoped_narrative(data, narrative_agents)
    if not data.get("narrative"):
        data["narrative_model"] = None
    data.pop("agent_highlights", None)
    data.pop("narrative_lead", None)
    return data


def _scope_journal_entry(entry: dict[str, Any], allowed: set[str] | None) -> dict[str, Any]:
    """Scope a work-journal entry to a member's granted agents.

    The highlights narrative is scoped per :func:`_scope_narrative_payload`
    (respecting the team-transparency flag). The per-agent usage metrics are a
    separate concern (financial data, never governed by the highlights flag):
    for a member they are always recomputed from only their agents' rows, and
    the cross-agent by-model split + default-workspace events are dropped.
    ``allowed is None`` (admin / single-user) keeps the full metrics.
    Works on a copy so the on-disk journal entry is never rewritten scoped.
    """
    entry = _scope_narrative_payload(json.loads(json.dumps(entry)))
    if allowed is None:
        return entry
    rows = [r for r in entry.get("by_agent") or [] if (r.get("agent") or "") in allowed]
    entry["by_agent"] = rows
    entry["metrics"] = {
        **(entry.get("metrics") or {}),
        "usd": round(sum(float(r.get("usd") or 0.0) for r in rows), 6),
        # Both books, or the fleet-wide credits total spread in from the
        # unscoped metrics above would survive as a member-visible number.
        "credits": round(sum(float(r.get("credits") or 0.0) for r in rows), 6),
        "input_tokens": sum(int(r.get("input_tokens") or 0) for r in rows),
        "output_tokens": sum(int(r.get("output_tokens") or 0) for r in rows),
        "total_tokens": sum(int(r.get("total_tokens") or 0) for r in rows),
        "calls": sum(int(r.get("calls") or 0) for r in rows),
        "tool_calls": sum(int(r.get("tool_calls") or 0) for r in rows),
        "a2a_messages": sum(int(r.get("a2a_sent") or 0) for r in rows),
        "active_agents": [r.get("agent") for r in rows],
    }
    entry["by_model"] = []
    entry["events"] = {"cognition_actions": [], "cortex": []}
    return entry


# How many raw events the A2A endpoints scan when filters/folding are in play.
# Filters run over this window and *then* the page limit is taken, so a narrow
# filter still fills a page instead of returning whatever survived a tail slice.
_A2A_SCAN_LIMIT = 5000
_A2A_PREVIEW_CHARS = 160
# A send with no reply this old stopped being "thinking" and became "no reply":
# a failed talk_to_agent returns an error string to the caller and publishes no
# reply frame at all, so the exchange would otherwise stay open forever.
_A2A_STALE_SECONDS = 15 * 60


def _read_a2a_activity(limit: int = 500) -> list[dict[str, Any]]:
    from olano.analytics.a2a_log import read_a2a_events

    return read_a2a_events(limit)


def _a2a_event_data(ev: Any) -> dict[str, Any] | None:
    """The ``data`` payload of an ``agent_message`` envelope, or None."""
    if not isinstance(ev, dict):
        return None
    etype = str(ev.get("type") or "agent_message")
    if etype != "agent_message":
        return None
    data = ev.get("data")
    return data if isinstance(data, dict) else None


# Conversation kinds on the Agent to Agent view. ``peer`` and ``subagent`` are
# Olano agents talking to each other; ``inbound`` is an outside caller reaching
# an agent over MCP or A2A (``from`` = ``mcp:<label>`` / ``a2a:<label>``) and
# ``remote`` is an agent calling an external A2A agent (``to`` = ``a2a:<name>``).
# The dashboard's "External" segment is the last two together.
_A2A_EXTERNAL_KINDS = frozenset({"inbound", "remote"})


def _a2a_kind(data: dict[str, Any]) -> str:
    kind = str(data.get("kind") or "")
    return kind if kind == "subagent" or kind in _A2A_EXTERNAL_KINDS else "peer"


def _a2a_protocol(data: dict[str, Any]) -> str:
    """``mcp`` / ``a2a`` for an external exchange, ``""`` otherwise. New frames
    carry it explicitly; older ones are read off the external party's prefix."""
    proto = str(data.get("protocol") or "").lower()
    if proto in ("mcp", "a2a"):
        return proto
    for side in ("from", "to"):
        value = str(data.get(side) or "")
        if value.startswith("mcp:"):
            return "mcp"
        if value.startswith("a2a:"):
            return "a2a"
    return ""


def _a2a_thread_key(data: dict[str, Any]) -> str:
    """Stable conversation key for an exchange.

    Peer exchanges use the **unordered** pair key: the runtime keeps directional
    checkpointer threads (``acp::<caller>::<callee>``) so each side has its own
    model context, but ``a → b`` and ``b → a`` are one conversation to read.
    Subagent delegations stay directional — ``to`` is a subagent *type*, not an
    agent, so the pair is not symmetric.
    """
    from olano.analytics.a2a_log import canonical_pair, norm_agent

    src = str(data.get("from") or "agent")
    dst = str(data.get("to") or "agent")
    kind = _a2a_kind(data)
    if kind == "subagent":
        return f"subagent:{norm_agent(src)}::{norm_agent(dst)}"
    if kind in _A2A_EXTERNAL_KINDS:
        # Directional too: the external party is a caller (inbound) or a callee
        # (remote), never both, so the pair is not symmetric.
        return f"{kind}:{norm_agent(src)}::{norm_agent(dst)}"
    return f"peer:{canonical_pair(src, dst)}"


def _a2a_scan_ts(ev: dict[str, Any]) -> str:
    """Envelope timestamp — stamped by ``_push_event`` at append time, so it is
    monotonic with file order. This is the only field ``since``/``before`` may
    compare against: the payload's own ``ts_sent``/``ts_reply`` are stamped
    inside ``talk_to`` and differ from it by the call duration, which is exactly
    how a "load older" cursor ends up looping."""
    return str(ev.get("ts") or "")


def _a2a_event_ts(ev: dict[str, Any], data: dict[str, Any]) -> str:
    """Display timestamp — when the exchange actually happened."""
    for key in ("ts_reply", "ts", "ts_sent"):
        value = data.get(key)
        if value:
            return str(value)
    return _a2a_scan_ts(ev)


def _a2a_join_key(data: dict[str, Any]) -> str:
    """Key pairing a ``reply`` frame back to its ``send``.

    Subagent frames carry a ``tool_call_id``; peer frames do not — but the peer
    reply re-sends ``message`` verbatim, so ``(from, to, message)`` joins the
    two halves. The dashboard's long-standing fallback ("most recent open send
    to the same recipient") cross-wires concurrent exchanges: with marcus→ana
    and kevin→ana both open, ana's first reply closes whichever send was later
    and gets attributed to the wrong caller.
    """
    from olano.analytics.a2a_log import norm_agent

    call_id = data.get("tool_call_id")
    if call_id:
        return f"id:{call_id}"
    src = norm_agent(str(data.get("from") or ""))
    dst = norm_agent(str(data.get("to") or ""))
    return f"msg:{src}|{dst}|{data.get('message') or ''}"


def _a2a_matches(
    data: dict[str, Any],
    *,
    kind: str | None,
    participants: set[str],
    query: str,
) -> bool:
    """Filter predicate shared by the activity and threads endpoints."""
    from olano.analytics.a2a_log import norm_agent

    if kind:
        actual = _a2a_kind(data)
        if kind == "external":
            if actual not in _A2A_EXTERNAL_KINDS:
                return False
        elif actual != kind:
            return False
    if participants:
        pair = {norm_agent(str(data.get("from") or "")), norm_agent(str(data.get("to") or ""))}
        if not participants.issubset(pair):
            return False
    if query:
        haystack = " ".join(
            str(data.get(k) or "") for k in ("from", "to", "message", "reply")
        ).casefold()
        if query not in haystack:
            return False
    return True


def _a2a_stale_before() -> str:
    """ISO cutoff before which an unanswered send counts as failed, not pending."""
    return (datetime.now(timezone.utc) - timedelta(seconds=_A2A_STALE_SECONDS)).isoformat()


def _fold_a2a_threads(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold a chronological event list into one entry per conversation.

    ``messages`` counts exchanges (``phase == "send"``), matching how the
    analytics edge rollup counts and how the dashboard has always labelled the
    number: a ``reply`` echoes the same pair and would double every total.
    """
    from olano.analytics.a2a_log import norm_agent

    threads: dict[str, dict[str, Any]] = {}
    # Sends still awaiting a reply, per thread: (join key, display ts).
    open_sends: dict[str, list[tuple[str, str]]] = {}
    durations: dict[str, list[float]] = {}
    stale_before = _a2a_stale_before()

    for ev in events:
        data = _a2a_event_data(ev)
        if data is None:
            continue
        key = _a2a_thread_key(data)
        src = str(data.get("from") or "agent")
        dst = str(data.get("to") or "agent")
        ts = _a2a_event_ts(ev, data)
        entry = threads.get(key)
        if entry is None:
            # `a`/`b` keep display casing but are ordered to match the key, so
            # the rail label is stable no matter who spoke first.
            kind = _a2a_kind(data)
            a, b = (src, dst)
            if kind == "peer" and norm_agent(b) < norm_agent(a):
                a, b = b, a
            entry = threads[key] = {
                "key": key,
                "kind": kind,
                "protocol": _a2a_protocol(data),
                "thread_id": "",
                "a": a,
                "b": b,
                "messages": 0,
                "first_ts": ts,
                "last_ts": ts,
                "last_preview": "",
                "in_flight": 0,
                "no_reply": 0,
                "avg_ms": None,
            }
            open_sends[key] = []
            durations[key] = []
        if ts:
            if not entry["first_ts"] or ts < str(entry["first_ts"]):
                entry["first_ts"] = ts
            if ts > str(entry["last_ts"]):
                entry["last_ts"] = ts
        # The agent-side transcript thread an external exchange ran on, so the
        # dashboard can link a call straight to its conversation.
        if data.get("thread_id"):
            entry["thread_id"] = str(data["thread_id"])

        phase = str(data.get("phase") or "send")
        if phase == "reply":
            pending = open_sends[key]
            join = _a2a_join_key(data)
            # Newest matching send first: repeated identical messages between
            # the same pair each get their own open entry, and each reply
            # closes exactly one. An unmatched reply (its send scrolled out of
            # the scan window) closes nothing rather than closing the wrong one.
            idx = next((i for i in range(len(pending) - 1, -1, -1) if pending[i][0] == join), -1)
            if idx >= 0:
                pending.pop(idx)
            sent, replied = data.get("ts_sent"), data.get("ts_reply")
            if sent and replied:
                try:
                    delta = (
                        datetime.fromisoformat(str(replied)) - datetime.fromisoformat(str(sent))
                    ).total_seconds() * 1000
                except ValueError:
                    delta = None
                if delta is not None and delta >= 0:
                    durations[key].append(delta)
            preview = str(data.get("reply") or data.get("message") or "")
        else:
            entry["messages"] = int(entry["messages"]) + 1
            open_sends[key].append((_a2a_join_key(data), ts))
            preview = str(data.get("message") or "")
        if preview:
            entry["last_preview"] = preview.strip()[:_A2A_PREVIEW_CHARS]

    for key, entry in threads.items():
        pending = open_sends[key]
        entry["in_flight"] = sum(1 for _cid, ts in pending if ts >= stale_before)
        entry["no_reply"] = len(pending) - int(entry["in_flight"])
        samples = durations[key]
        if samples:
            entry["avg_ms"] = round(sum(samples) / len(samples))

    return sorted(threads.values(), key=lambda e: str(e["last_ts"]), reverse=True)


def _window_days(days: Any) -> int | None:
    """Normalize a ``?days=`` query param into a trailing-window size.

    ``0``/missing/garbage → ``None`` ("all history", the pre-window behaviour);
    anything else is clamped to the 1..366 range the analytics windows accept.
    """
    try:
        n = int(days or 0)
    except (TypeError, ValueError):
        return None
    return max(1, min(n, 366)) if n > 0 else None


def _window_hours(hours: Any) -> int | None:
    """Normalize a ``?hours=`` query param into a rolling-window size.

    ``0``/missing/garbage → ``None`` (fall through to ``days``); anything else
    is clamped to the same 1..168 range ``/api/analytics/overview`` accepts, so
    the two tabs cannot disagree about what a given window means.
    """
    try:
        n = int(hours or 0)
    except (TypeError, ValueError):
        return None
    return max(1, min(n, 168)) if n > 0 else None


def _append_thread_tool_event(data: dict[str, Any]) -> None:
    role = str(data.get("role") or "")
    if role not in {"tool_call", "tool_result"}:
        return
    agent = str(data.get("agent") or "").strip()
    thread_id = str(data.get("thread_id") or "").strip()
    if not agent or not thread_id:
        return
    try:
        from olano.config.home import get_olano_paths

        sessions = get_olano_paths().sessions_dir / agent
        sessions.mkdir(parents=True, exist_ok=True)
        path = sessions / f"{thread_id}.jsonl"
        entry = {
            "ts": data.get("ts") or datetime.now(timezone.utc).isoformat(),
            "role": role,
            "message_id": data.get("message_id") or "",
            "tool_call_id": data.get("tool_call_id") or "",
            "name": data.get("name") or "tool",
            "args": data.get("args") or "",
            "content": data.get("content") or "",
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception:
        logger.debug("Failed to persist thread tool event", exc_info=True)
        return
    # This row bypassed the runner's append (it comes from the delegation
    # middleware), so announce it the way the runner would have — an open chat
    # must draw every row a reload will.
    _push_event(THREAD_UPDATED, {"agent": agent, "thread_id": thread_id, "entry": entry})


# Transcript rows the chat renders. `system` rows stay hidden as a class —
# prompt-update notices and context notes are written for the model's
# cold-start rehydration, not for a human reading the thread (see
# runner._notify_prompt_update). The one exception is a magic-link outcome,
# which is written FOR the chat: it is how a connection completed out-of-band
# ("✅ curtis is connected") is still there after a reload, and in a second tab.
_CHAT_VISIBLE_ROLES = frozenset({"user", "peer_inbound", "assistant", "tool_call", "tool_result"})

# Content types a browser would execute if a workspace file were opened as a
# top-level document on the dashboard origin. ``read_file_raw`` serves these
# under a CSP ``sandbox`` so they render as a unique, script-less origin.
_SCRIPT_CAPABLE_MIME_TYPES = frozenset(
    {"text/html", "application/xhtml+xml", "image/svg+xml", "application/xml", "text/xml"}
)


def _safe_media_refs(text: str, root: Path, agent: str) -> list[dict[str, Any]]:
    """``detect_media_refs`` with a hard floor: never raise, never stop a turn.

    Media detection is presentation — it decides whether a path in the text is
    drawn as a card. It runs on tool results and on the agent's finished reply,
    which means a failure here lands AFTER the answer is written and persisted,
    where the only thing left to break is the report of success. That is how a
    reply containing "13 open at ~$2.115M" ended a turn with ``Error: Could not
    determine home directory.``: the scan tried to expand ``~$2.115M`` as a home
    reference, and the raised message named an env var, so a presentation bug
    read as a broken box. ``media_refs`` no longer raises on that token; this is
    the floor under the next one.
    """
    from olano.runtime.media_refs import detect_media_refs

    try:
        return detect_media_refs(text, root, agent)
    except Exception:
        logger.warning("media detection failed for %s — no cards this turn", agent, exc_info=True)
        return []


def _chat_visible(entry: dict[str, Any]) -> bool:
    """Whether :func:`_transcript_message` projects this row (rather than ``None``).

    Decided by the row's own fields alone, so a paged read can pick its page
    before paying for the projection (media detection) of each row.
    """
    role = entry.get("role")
    is_magic_link = role == "system" and entry.get("event") == MAGIC_LINK_EVENT
    if role not in _CHAT_VISIBLE_ROLES and not is_magic_link:
        return False
    # A prompt the platform wrote for the model (the onboarding greeting's
    # "a new owner just signed up, say hi"): kept for the checkpoint and the
    # cold-start rehydration, never drawn (request_context.HIDDEN_PROMPT_KEY).
    return not entry.get("hidden")


def _transcript_message(
    agent: str, entry: dict[str, Any], *, media_root: Path | None = None
) -> dict[str, Any] | None:
    """Project one persisted JSONL row onto the message shape the chat draws.

    ``None`` for rows the chat never shows. This is THE projection — both
    ``GET /api/agents/{name}/threads/{id}`` (a reload) and the live
    ``thread_updated`` frame (``_push_event``) go through it, which is what
    guarantees a chat watching a thread from another device renders exactly
    what a reload would. ``media_root`` lets a whole-thread read resolve the
    workspace once instead of per row; the live frame (one row) resolves it.
    """
    if not _chat_visible(entry):
        return None
    role = entry.get("role")
    is_magic_link = role == "system" and entry.get("event") == MAGIC_LINK_EVENT
    msg: dict[str, Any] = {
        "role": role,
        "content": entry.get("content", ""),
        "channel": entry.get("channel", ""),
        "sender": entry.get("sender", ""),
        "from": entry.get("from", ""),
        "via_peer": entry.get("via_peer", ""),
        "message_id": entry.get("message_id", ""),
        "tool_call_id": entry.get("tool_call_id", ""),
        "name": entry.get("name", ""),
        "args": entry.get("args", ""),
        "ts": entry.get("ts"),
        # A user row the runner wrote mid-turn (core/steering.py): the chat
        # labels it and matches the id against its own pending chips.
        "steer": bool(entry.get("steer")),
        "steer_id": str(entry.get("steer_id") or ""),
    }
    if is_magic_link:
        # The marker key rather than a wording match, with its extra fields
        # riding along so the chat can rebuild the same card the live
        # `magic_link_updated` frame draws.
        msg.update(
            {
                "event": MAGIC_LINK_EVENT,
                "link_id": entry.get("link_id", ""),
                "status": entry.get("status", ""),
                "flow": entry.get("flow", ""),
                "label": entry.get("label", ""),
                "target_agent": entry.get("target_agent", ""),
                "apply": entry.get("apply", ""),
            }
        )
    # Peel the runtime-authored identity tags off inbound turns. The chat
    # window rendered them verbatim, so every platform message opened with
    # "[from @jrivera (Jamie | Acme, id=…)]: " before its actual words.
    # `split_sender_tag`, NOT `strip_directives`: the latter is a title
    # builder that replaces an email body with its subject line and a
    # document body with its filename — correct for a one-line rail row, and
    # a deletion of the message here.
    if role in ("user", "peer_inbound"):
        body, tag_sender = split_sender_tag(str(msg.get("content") or ""))
        msg["content"] = body
        msg["sender"] = msg.get("sender") or tag_sender
    # A peer agent's message (talk_to / runner write `from`, never `sender`).
    # Without this the chat had no author for the row and labelled the other
    # agent's question "You" in the callee's thread.
    if role == "peer_inbound" and not msg["sender"]:
        msg["sender"] = str(entry.get("from") or "")
    # The assistant turn's ordered text segments (runner.astream_events), so
    # the chat can put tool cards between the paragraphs they belong to.
    # Optional: a turn that never interleaved text with tools has none.
    if role == "assistant" and isinstance(entry.get("segments"), list):
        msg["segments"] = entry["segments"]
    # The Langfuse trace this reply belongs to, when the turn was traced
    # (olano/observability): the admin "Open in Langfuse" link and feedback
    # scores key on it. Optional; untraced turns have none.
    if role == "assistant" and isinstance(entry.get("trace_id"), str) and entry["trace_id"]:
        msg["trace_id"] = entry["trace_id"]
    # The turn this reply closed (runner._TURN_ID): what a feedback vote names
    # (core/feedback.py). Rows older than the stamp have none and get no vote.
    if role == "assistant" and isinstance(entry.get("turn_id"), str) and entry["turn_id"]:
        msg["turn_id"] = entry["turn_id"]
    # Which model answered (runner._model_stamp). Older rows have none; the
    # chat uses it to badge a reply that ran on a different model than the
    # conversation's current one.
    if role == "assistant" and entry.get("model"):
        msg["model"] = str(entry["model"])
        if entry.get("reasoning_effort"):
            msg["reasoning_effort"] = str(entry["reasoning_effort"])
    # Re-derive any inline media so the row renders it the same way the live
    # dashboard stream did instead of dropping to a bare path: files the agent
    # itself named in its reply ("Saved: reports/x.png") on assistant rows, and
    # chat-uploaded files (saved under uploads/ with a path note — see
    # runtime/chat_attachments.py) on user rows.
    #
    # `tool_result` rows are deliberately NOT in this list — see
    # `media_refs.SCAN_TOOL_RESULTS`. Scanning them attached every path an
    # intermediate tool happened to print, and the reload has to agree with the
    # live stream or a refresh would grow cards the turn never showed.
    if role in ("assistant", "user", "peer_inbound", "tool_result"):
        try:
            from olano.runtime.media_refs import SCAN_TOOL_RESULTS, detect_media_refs

            if role != "tool_result" or SCAN_TOOL_RESULTS:
                root = media_root if media_root is not None else _workspace_root(agent)
                refs = detect_media_refs(str(msg.get("content") or ""), root, agent)
                if refs:
                    msg["media"] = refs
        except Exception:
            logger.debug("thread media detection skipped", exc_info=True)
    return msg


def _enrich_thread_updated(data: Any) -> dict[str, Any] | None:
    """Turn a raw ``thread_updated`` payload into what the chat consumes.

    The runner publishes the row as written (``entry``); the wire frame carries
    the projected ``message`` instead, or is dropped (``None``) when the row is
    one the chat hides — the rail still needs no poke for those, since a
    hidden row never changes what a reload shows.
    """
    if not isinstance(data, dict):
        return None
    agent = str(data.get("agent") or "").strip()
    thread_id = str(data.get("thread_id") or "").strip()
    entry = data.get("entry")
    if not agent or not thread_id or not isinstance(entry, dict):
        return None
    try:
        message = _transcript_message(agent, entry)
    except Exception:
        logger.debug("thread_updated projection failed", exc_info=True)
        return None
    if message is None:
        return None
    return {
        "agent": agent,
        "thread_id": thread_id,
        "role": message.get("role") or "",
        "ts": message.get("ts") or datetime.now(timezone.utc).isoformat(),
        "message": message,
    }


# config.yaml parse cache for _default_agent_name — the name is resolved per
# unattributed SSE frame, so an uncached read re-parses YAML on the event loop
# at frame rate. Keyed by (path, mtime_ns): edits apply on the next call.
_DEFAULT_AGENT_CACHE: tuple[tuple[str, int | None], str] | None = None


def _default_agent_name() -> str:
    global _DEFAULT_AGENT_CACHE
    from olano.config.home import get_olano_paths, load_home_config

    path = get_olano_paths().config_path
    try:
        key: tuple[str, int | None] = (str(path), path.stat().st_mtime_ns)
    except OSError:
        key = (str(path), None)
    if _DEFAULT_AGENT_CACHE is not None and _DEFAULT_AGENT_CACHE[0] == key:
        return _DEFAULT_AGENT_CACHE[1]
    name = load_home_config().default_agent
    _DEFAULT_AGENT_CACHE = (key, name)
    return name


def _sandbox_config_block(settings: Any) -> dict[str, Any]:
    """The Config section's sandbox payload: stored values + what they resolve to.

    ``memory``/``cpus`` are the writable fields (``None`` = auto). Everything
    else is read-only context the card needs to explain itself: how big the box
    is, what each limit actually comes out as and which layer decided it, and
    whether an environment variable is overriding the field the operator is
    looking at. Runs in a worker thread — ``docker info`` is a socket
    round-trip.
    """
    from olano.core.host_resources import (
        ENV_CPUS,
        ENV_MEMORY,
        detect_host_resources,
        resolve_sandbox_limits,
    )

    host = detect_host_resources()
    limits = resolve_sandbox_limits(settings=settings, host=host)
    return {
        "memory": settings.memory,
        "cpus": settings.cpus,
        "host": {"ncpu": host.ncpu, "mem_bytes": host.mem_bytes, "source": host.source},
        "effective": {
            "memory": limits.memory,
            "cpus": limits.cpus,
            "memory_source": limits.memory_source,
            "cpus_source": limits.cpus_source,
        },
        "env_overrides": {
            "memory": bool((os.environ.get(ENV_MEMORY) or "").strip()),
            "cpus": bool((os.environ.get(ENV_CPUS) or "").strip()),
        },
    }


def _usage_budget_config_block() -> dict[str, Any]:
    """The Config section's usage-budget payload: what is set, and what it means.

    Each of the six budgets ships with the ceiling actually being enforced, the
    deployment's own number, the live usage against it, and whether this box
    may change it — so the form can show an override next to what it replaced
    and refuse to offer an edit that would be ignored. Runs in a worker thread:
    it reads the deployment artifact and the counters DB.

    ``posture`` matters for the copy: on an unmanaged install nothing is
    counted or enforced at all, so the card says that rather than rendering six
    limits that do nothing.
    """
    from olano.config.system_settings import (
        DEFAULT_BUDGET_ALERT_PERCENTS,
        OVERRIDABLE_USAGE_LIMITS,
        load_usage_budget_settings,
    )
    from olano.deployment import resolver as dep_resolver
    from olano.deployment.usage_budgets import effective_usage_limits

    settings = load_usage_budget_settings()
    status = dep_resolver.deployment_status()
    enforced = status.posture != "open"

    def _current(key: str) -> int | None:
        if not enforced:
            return None
        try:
            from olano.deployment.usage_counters import current_usage_for_limit

            return current_usage_for_limit(key)
        except Exception:
            return None

    limits = []
    for eff in effective_usage_limits(status.settings.usage):
        local = getattr(settings, eff.key, None) if eff.key in OVERRIDABLE_USAGE_LIMITS else None
        limits.append(
            {
                "key": eff.key,
                "label": eff.label,
                "unit": eff.unit,
                "window": eff.limit.window,
                "limit": eff.limit.value,
                "deployment_limit": eff.deployment_limit.value,
                "source": eff.source,
                "overridable": eff.key in OVERRIDABLE_USAGE_LIMITS,
                # What is stored in config.yaml for this key: absent → null,
                # an explicit "no ceiling" → the string, otherwise the number.
                # The three are different states and the form must keep them
                # apart (see UsageBudgetSettings).
                "override": (
                    None if local is None else ("unlimited" if local.value is None else local.value)
                ),
                "current": _current(eff.key),
            }
        )
    return {
        "enforced": enforced,
        "posture": status.posture,
        "alerts_enabled": settings.alerts_enabled,
        "alert_percents": list(settings.alert_percents),
        "default_alert_percents": list(DEFAULT_BUDGET_ALERT_PERCENTS),
        "limits": limits,
    }


async def _start_codex_device_flow() -> dict[str, Any]:
    """Start an OpenAI Codex device-auth session (delegates to models/codex_auth).

    Kept as a thin module-level indirection because the routes below and the
    handshake adapter both start the same flow, and tests patch this name.
    """
    from olano.models.codex_auth import start_codex_device_flow

    return await start_codex_device_flow()


# ── Boot-failure diagnostics ───────────────────────────────────────────────
# The most common (and user-fixable) reason an agent fails to boot is a
# missing credential: a model provider or tool needs an API key that lives in
# the vault, but the vault is locked so the key never reached os.environ.
# These helpers turn the raw provider error (e.g. a pydantic
# "DEEPSEEK_API_KEY must be set" validation dump) into a credential name + a
# yes/no "is this a secret problem" signal so the dashboard can offer a
# one-click "unlock vault & retry" instead of a cryptic toast.

_SECRET_NAME_PATTERNS = [
    re.compile(r"\b([A-Z][A-Z0-9_]{2,})\s+must be set"),
    re.compile(r"environment variable[\s`'\"]+([A-Z][A-Z0-9_]{2,})"),
    re.compile(r"\b([A-Z][A-Z0-9_]*(?:API_KEY|API_TOKEN|_TOKEN|_KEY|_SECRET))\b"),
]

_SECRET_ERROR_NEEDLES = (
    "api_key",
    "api key",
    "must be set",
    "api base",
    "credential",
    "environment variable",
    "did not find",
    "not found in vault",
    "unauthorized",
    "401",
)


def _extract_secret_name(msg: str) -> str | None:
    """Best-effort extraction of the missing env-var / credential name."""
    for pat in _SECRET_NAME_PATTERNS:
        match = pat.search(msg or "")
        if match:
            return match.group(1)
    return None


def _looks_like_secret_error(msg: str) -> bool:
    """True when a boot error reads like a missing/locked credential."""
    low = (msg or "").lower()
    return any(needle in low for needle in _SECRET_ERROR_NEEDLES)


# The runtime bound by the most recent create_api() call. Module-level helpers
# like _workspace_root can't take the runtime as a parameter (their callers
# predate it), but a booted agent's config is already in memory on its runner —
# reaching it here avoids a per-request agent.json5 parse on the event loop.
_ACTIVE_RUNTIME: Any | None = None


def _agent_config_for(name: str) -> Any:
    """The parsed AgentConfig for `name` — from the booted runner when possible.

    A booted runner already holds its parsed config — but only trust a genuine
    AgentConfig (a test MagicMock's `.workspace` is itself a mock, and would
    scaffold a bogus workspace tree next to the cwd), and only while the
    on-disk config it mirrors still exists, so a runner-only agent keeps the
    historical FileNotFoundError fallback in the callers.
    """
    from olano.config.agent_config import AgentConfig
    from olano.config.loader import find_agent_config_path

    runners = getattr(_ACTIVE_RUNTIME, "_runners", None) or {}
    cfg = getattr(runners.get(name), "config", None)
    if isinstance(cfg, AgentConfig) and find_agent_config_path(name) is not None:
        return cfg
    return _load_agent_config_cached(name)


def _workspace_root(agent: str | None = None) -> Path:
    """Resolve the workspace root for `agent`, falling back to the default agent.

    Used by the dashboard's Files / Memory endpoints so each view is scoped to
    the agent currently selected in the topbar (state.contextAgent), instead
    of always pointing at the default agent's workspace.
    """
    from olano.config.workspace import get_workspace

    name = agent or _default_agent_name()
    try:
        return get_workspace(_agent_config_for(name).workspace).root
    except FileNotFoundError:
        # Unknown agent — try the default before giving up
        if agent:
            try:
                return get_workspace(_agent_config_for(_default_agent_name()).workspace).root
            except FileNotFoundError:
                pass
        return get_workspace().root


def _shared_projection_roots(agent: str | None, *, write: bool = False) -> list[Path]:
    """Resolved sources behind this agent's ``workspace/shared/<name>`` symlinks.

    ``sync_workspace_share_projections`` builds those entries as symlinks into
    ``<home>/shared`` (and any operator-declared share source), so they resolve
    *outside* the workspace root — which is why the Files endpoints' containment
    check refused every one of them with a 403. The tree still listed
    ``shared/default`` (entry paths there are built lexically, without
    resolving), so the folder was visible but could not be opened.

    These are exactly the roots the agent itself sees (`sandbox_visible_roots`
    is built from the same mounts), so honouring them here only lets the
    dashboard show what the agent already reads. ``write=True`` drops ``ro``
    grants, keeping a read-only share read-only.

    Best-effort: any failure resolving the mounts returns no extra roots, i.e.
    the historical workspace-only containment.
    """
    try:
        from olano.security.sharing import resolve_shared_mounts

        config = _agent_config_for(agent or _default_agent_name())
        mounts = resolve_shared_mounts(config)
    except Exception:
        return []

    roots: list[Path] = []
    for mount in mounts:
        if write and mount.mode != "rw":
            continue
        try:
            roots.append(mount.source.resolve())
        except OSError:
            continue
    return roots


def _resolve_in_workspace(
    root: Path,
    rel: str,
    *,
    agent: str | None = None,
    write: bool = False,
) -> Path:
    """Resolve `rel` under `root` and require it to stay somewhere this agent may
    reach; raise HTTP 403 otherwise. Returns the resolved path.

    ``resolve()`` collapses ``..`` and follows symlinks, so a crafted
    ``../../vault`` lands outside the workspace and is refused — that is the
    check that keeps ``GET /api/files`` from enumerating ``/app``,
    ``/opt/olano`` or the home tree. A ``shared/*`` projection also lands
    outside, but legitimately, so it is admitted via
    `_shared_projection_roots` (which is only consulted on the miss — the
    common in-workspace case stays a pure path comparison).
    """
    target = (root / rel) if rel else root
    resolved = target.resolve()
    root_resolved = root.resolve()
    if resolved == root_resolved or root_resolved in resolved.parents:
        return resolved
    for shared in _shared_projection_roots(agent, write=write):
        if resolved == shared or shared in resolved.parents:
            return resolved
    raise HTTPException(status_code=403, detail="Access denied")


def _auth_token() -> str:
    return os.environ.get("LIONCLAW_DASHBOARD_TOKEN", "")


# task id → owning agent, for the scheduler DB lookup below. A task's agent is
# fixed for its lifetime, so one sqlite open+query per task per process is
# enough; only hits are stored, so the map is bounded by the task table.
_SCHEDULER_TASK_AGENTS: dict[str, str] = {}


def _scheduler_frame_agent(task_id: str) -> str | None:
    """Resolve the owning agent of a scheduler_update frame's task id.

    Config jobs encode it in the id (``cfg:<agent>:<idx>`` / ``cfg:<agent>``);
    DB tasks carry an opaque uuid, so fall back to the scheduler DB. Best-effort
    — a miss returns ``None`` (the frame stays unattributed, which only triggers
    a refetch of the already-filtered ``GET /api/scheduler/tasks``).
    """
    if not task_id:
        return None
    if task_id.startswith(_CONFIG_JOB_PREFIX):
        body = task_id[len(_CONFIG_JOB_PREFIX) :]
        head, sep, tail = body.rpartition(":")
        return head if (sep and tail.isdigit()) else body or None
    cached = _SCHEDULER_TASK_AGENTS.get(task_id)
    if cached is not None:
        return cached
    try:
        task = SchedulerDB().get_task(task_id)
    except Exception:
        return None
    agent = (task or {}).get("agent_name") or None
    if agent:
        _SCHEDULER_TASK_AGENTS[task_id] = agent
    return agent


def _push_event(event_type: str, data: Any) -> None:
    """Broadcast an event to every connected /api/stream subscriber.
    Non-blocking — drops the event for any subscriber whose queue is full
    (back-pressure protection against a misbehaving client)."""
    # Attribute workspace-mutation frames to their concrete agent: the handlers
    # carry the nullable ``?agent=`` param (``None`` → the default agent's
    # workspace). A null agent would otherwise pass the per-subscriber SSE
    # filter as "unattributed", leaking default-agent file paths/sizes to
    # members with no grant on it — so resolve it here, at the single chokepoint.
    if (
        event_type in ("file_updated", "memory_updated")
        and isinstance(data, dict)
        and not data.get("agent")
    ):
        data = {**data, "agent": _default_agent_name()}
    # scheduler_update frames carry only {action, id}; a config-job id is
    # ``cfg:<agent>:<idx>`` (or ``cfg:<agent>``), which embeds a — possibly
    # non-granted — agent name. Stamp the owning agent so the SSE filter scopes
    # the frame (otherwise a member would learn a non-granted agent exists and
    # has scheduler activity, defeating the by-id endpoints' 404 existence-hiding).
    if event_type == "scheduler_update" and isinstance(data, dict) and not data.get("agent"):
        task_id = str(data.get("id") or "")
        # Evict before resolving, not after: the DB row is already deleted by
        # the time this frame is emitted, so a cached owner would attribute a
        # frame that has always gone out unattributed.
        if str(data.get("action") or "") == "deleted":
            _SCHEDULER_TASK_AGENTS.pop(task_id, None)
        owner = _scheduler_frame_agent(task_id)
        if owner:
            data = {**data, "agent": owner}
    # A transcript row leaves the runner raw; the wire carries the same
    # projection GET /threads/{id} serves, or nothing for a row the chat hides.
    if event_type == THREAD_UPDATED:
        data = _enrich_thread_updated(data)
        if data is None:
            return
    event = {"type": event_type, "data": data, "ts": datetime.now(timezone.utc).isoformat()}
    if event_type == "agent_message":
        _append_a2a_event(event)
    elif event_type == "thread_message" and isinstance(data, dict):
        _append_thread_tool_event(data)
    for q in list(_subscribers):  # copy: subscribers may change mid-iteration
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


def _assert_message_rate_limit(agent: str, runner: Any) -> None:
    """Enforce the per-agent / per-platform inbound message cap for dashboard &
    REST chat (platform label ``"dashboard"``).

    Raises HTTP 429 with a ``Retry-After`` header when the agent is over its
    configured limit; a no-op when nothing is configured (the common case). This
    is the API-side twin of the platform/channel ingress gate in
    :mod:`olano.runtime.server`.
    """
    from fastapi import HTTPException

    from olano.security.message_rate_limit import (
        check_message_rate_limit,
        rate_limit_notice,
    )

    decision = check_message_rate_limit(agent, "dashboard", getattr(runner, "config", None))
    if not decision.allowed:
        _push_event("rate_limited", {"agent": agent, "retry_after": decision.retry_after})
        raise HTTPException(
            status_code=429,
            detail=rate_limit_notice(decision),
            headers={"Retry-After": str(decision.retry_after)},
        )


async def _screen_message_policy(agent: str, runner: Any, text: str) -> str:
    """Apply ``message_policy`` to a dashboard / REST message and return the
    text to run — the API-side twin of the channel gate in
    :mod:`olano.runtime.server`.

    Returns the (possibly truncated) text when the turn may proceed, and raises
    HTTP 422 when it may not. 422 rather than 400: the request is well-formed,
    it is the *content* the agent's own policy refuses, and the dashboard shows
    ``detail`` verbatim — so an operator who capped their agent at 500
    characters and then pastes 3,000 gets the notice their customers would get,
    not a silent nothing.

    The policy applies here as well as on messengers on purpose. The dashboard
    is where an operator tests the agent they are shipping, and a cap that is
    invisible until a stranger hits it is a cap nobody can tune.
    """
    from fastapi import HTTPException

    from olano.security import reply_policy

    verdict = await reply_policy.ascreen_inbound(
        text, reply_policy.resolve_policy(getattr(runner, "config", None))
    )
    if verdict.action == "truncate":
        _push_event(
            "message_policy",
            {"agent": agent, "action": verdict.action, "reason": verdict.reason},
        )
        return verdict.text
    if verdict.blocked:
        _push_event(
            "message_policy",
            {"agent": agent, "action": verdict.action, "reason": verdict.reason},
        )
        raise HTTPException(
            status_code=422,
            detail=verdict.notice or _POLICY_IGNORED_DETAIL,
        )
    return text


#: Shown when a dashboard message is dropped by a rule whose `ignore_notice` is
#: empty. On a messenger that case is true silence — the point is not to confirm
#: a live bot to a spammer. On the operator's own console silence would just
#: read as a broken send, so the rule is named instead.
_POLICY_IGNORED_DETAIL = (
    "No reply — this message matched this agent's reply policy "
    "(Agent → Multi-user → Message policy)."
)


def _broadcast_roster_changed(name: str | None = None) -> None:
    """Notify dashboards that the agent roster changed (create / update / delete /
    install / lifecycle) so the sidebar agent picker and the Agents view refresh
    live. The frontend (currentAgent.tsx, AgentsView.tsx) reloads on this frame.
    Emitted alongside the more specific events (agent_started, agent_deleted, …),
    which other consumers may still depend on."""
    _push_event("agents_update", {"name": name})


def _append_log(line: str) -> None:
    _log_buffer.append(line)
    if len(_log_buffer) > _MAX_LOG_LINES:
        _log_buffer.pop(0)
    _push_event("log", line)


# ── Pydantic models (module-level to avoid annotation resolution issues) ───────

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel, ConfigDict, Field
    from starlette.responses import JSONResponse

    class ChatRequest(BaseModel):
        message: str
        agent: str = _default_agent_name()
        thread_id: Optional[str] = None
        # Optional per-turn client overrides, both consumed by /api/chat/stream:
        #   model — run this turn against a different model than the agent's
        #     configured one (dashboard chat model picker). Empty/omitted → the
        #     agent's configured model. An unbuildable value yields an error
        #     frame, not a 422.
        #   mode  — ephemeral approval profile: "auto" | "plan" | "manual".
        model: Optional[str] = None
        mode: Optional[str] = None  # "auto" | "plan" | "manual" | "act"
        # reasoning_effort — per-turn override of the agent's reasoning effort
        # (dashboard chat effort picker). Only meaningful for models whose
        # profile advertises effort levels; silently ignored otherwise.
        reasoning_effort: Optional[str] = None
        # Attachments. Each item has the shape {"kind": "image"|"audio"|
        # "video"|"file", "name": str, "mime": str, "data": str}. Media kinds
        # carry base64 of the raw bytes and are persisted to the agent
        # workspace's uploads/ dir, with the saved path noted in the message
        # (runtime/chat_attachments.py; admins only — read-only members fall
        # back to inline vision blocks). "file" carries the file's UTF-8 text
        # (binary files are rejected upstream) and is inlined into the prompt.
        attachments: Optional[list[dict]] = None
        # The app that sent the turn: "mobile" for the Olano mobile app, which
        # labels the conversation MOBILE (core/thread_labels.CHANNEL_MOBILE).
        # Anything else, or nothing, is the dashboard. It changes the label
        # only, never what the turn may do.
        client: Optional[str] = None

    class SteerRequest(BaseModel):
        """`POST /api/chat/steer` — a message for the turn running on a thread."""

        agent: str = _default_agent_name()
        thread_id: str
        message: str
        # The `turn_id` the `start` frame carried. A steer typed against a turn
        # that has since ended is refused (409 turn_ended) so the client can
        # queue it as the next turn instead of steering a later one.
        turn_id: Optional[str] = None
        # Client-minted id ([A-Za-z0-9_-]{8,64}) so the chip can match the later
        # `steer` frame — and cancel — without waiting for this response.
        steer_id: Optional[str] = None

    class SpeakRequest(BaseModel):
        """`POST /api/speech/speak`: words to read aloud in the box's voice."""

        # Whose reply it is. The caller must be allowed to chat with this
        # agent, and the agent's own speech credentials apply.
        agent: str = _default_agent_name()
        text: str
        # Optional container (mp3, opus, wav, ...). Empty asks for mp3, which
        # every phone and browser plays.
        audio_format: str = ""

    class SteerCancelRequest(BaseModel):
        """`POST /api/chat/steer/cancel` — withdraw a steer the model has not read."""

        agent: str = _default_agent_name()
        thread_id: str
        steer_id: str

    class MemoryAppendRequest(BaseModel):
        content: str
        section: str = ""

    class MemoryWriteRequest(BaseModel):
        content: str

    class NoteUpsertRequest(BaseModel):
        title: str
        content: str = ""
        tags: list[str] = []
        aliases: list[str] = []
        sources: list[str] = []
        auto_inject: bool = False

    class FileWriteRequest(BaseModel):
        content: str

    class FileMoveRequest(BaseModel):
        source: str
        dest: str

    class FileDuplicateRequest(BaseModel):
        path: str

    class FileCreateRequest(BaseModel):
        path: str
        type: str = "file"  # "file" | "directory"

    class TaskCreateRequest(BaseModel):
        name: str
        prompt: str
        schedule: str = ""
        run_at: str = ""
        timezone: str = "UTC"
        agent_name: str = ""
        visibility: str = "private"
        # `provider:model` the task runs on; "" = the agent's configured model.
        model: str = ""
        # Effort level for the task's runs; "" = the agent's own setting.
        # Validated against the ladder of the model the task runs on.
        reasoning_effort: str = ""

    class TaskUpdateRequest(BaseModel):
        name: Optional[str] = None
        prompt: Optional[str] = None
        schedule: Optional[str] = None
        run_at: Optional[str] = None
        enabled: Optional[bool] = None
        timezone: Optional[str] = None
        visibility: Optional[str] = None
        # None = keep; "" = back to the agent's configured model.
        model: Optional[str] = None
        # None = keep; "" = back to the agent's own effort setting.
        reasoning_effort: Optional[str] = None

    class AgentCreateRequest(BaseModel):
        # The permanent handle. Optional: left empty it is MINTED from
        # `display_name` (`config/agent_names.mint_agent_name` — slug, then
        # `-2`, `-3` on a collision), which is what both dashboards do now;
        # a person names an agent, nobody chooses its key. Given explicitly
        # (API clients, the AgentFather tool, tests) it is validated and used
        # verbatim, as it always was.
        name: str = ""
        # Editable display name. Empty ⇒ the agent goes by `name`. Required
        # when `name` is empty — there is nothing else to mint a handle from.
        display_name: str = ""
        # The agent's position ("Recruitment Specialist"), stored as
        # `org.title`. Empty on an archetype create ⇒ the archetype's own
        # `title`/`label`; empty on a from-scratch create ⇒ none.
        title: str = ""
        emoji: str = "🦁"
        description: str = ""
        # "" ⇒ resolve the deployment-aware default at create time
        # (_default_agent_model): the managed fast tier on a managed box, the
        # BYOK literal everywhere else.
        model: str = ""
        system_prompt: str = ""
        tools: list[str] = Field(default_factory=list)
        backend: str = "filesystem+shell"
        color: Optional[str] = None
        # Optional archetype slug. Empty (the default, and what every existing
        # caller sends) creates the blank agent this endpoint has always
        # created. Set, it installs that archetype under `name` through the
        # same installer `POST /api/archetypes/install` uses — and it is the
        # value `self_serve_agents.allowed_archetypes` is checked against, so a
        # deployment can hand members a fixed menu of bots.
        archetype: str = ""

    class AgentUpdateRequest(BaseModel):
        # Accept ANY AgentConfig field, not just the ones enumerated below. The
        # named fields stay declared for OpenAPI docs and the live-apply logic
        # (heartbeat / schedule / cognition); every other field arrives as an
        # "extra" and is validated when the merged config is re-parsed as
        # AgentConfig in the handler. This is what lets the dashboard's
        # schema-driven editor persist EVERY agent.json5 field without adding a
        # new typed attribute here for each one.
        model_config = ConfigDict(extra="allow")
        emoji: Optional[str] = None
        description: Optional[str] = None
        model: Optional[str] = None
        system_prompt: Optional[str] = None
        tools: Optional[list[str]] = None
        # Explicit opt-outs from the auto-injected universal/ops defaults (e.g.
        # ["talk_to_agent"] to revoke peer messaging). Persisted so removing a
        # default tool actually sticks — a plain `tools` edit can't drop a
        # universal default because AgentConfig re-injects it.
        disabled_tools: Optional[list[str]] = None
        # Which OTHER agents this agent is told exist (prompt awareness). Shape:
        # {"mode": "none"|"all"|"list", "agents": [names]}. Validated against
        # AgentConfig.agent_visibility on save.
        agent_visibility: Optional[dict[str, Any]] = None
        skills: Optional[list[str]] = None
        # Skill NAMES this agent must not load, from the Skills tab's per-skill
        # switches. Skills arrive by directory (the shared managed pack), so
        # this denylist is the only way to turn one off for one agent — see
        # AgentConfig.disabled_skills.
        disabled_skills: Optional[list[str]] = None
        # Backend accepts either the legacy shorthand string ("filesystem",
        # "filesystem+shell", "read") or the modern block form
        # ({"type": ..., "root_dir": ..., "virtual_mode": ...}). Normalization
        # to the block form happens in the handler.
        backend: Optional[Any] = None
        color: Optional[str] = None
        memory: Optional[dict[str, Any]] = None
        security: Optional[dict[str, Any]] = None
        # Heartbeat poll settings ({enabled, interval_minutes}; null fields
        # inherit the system-wide defaults). Applied live to the scheduler.
        heartbeat: Optional[dict[str, Any]] = None
        # Agent-level cron schedule (agent.json5 `schedule`). The Config view's
        # Schedule editor PUTs the whole array; applied live via the scheduler.
        schedule: Optional[list[dict[str, Any]]] = None
        # Chat slash-command exposure + custom commands (the Commands tab).
        commands: Optional[dict[str, Any]] = None
        # End-user subscription pricing (the Monetization tab). Non-secret
        # pricing/copy only; STRIPE_API_KEY / STRIPE_WEBHOOK_SECRET stay in the
        # vault. Validated against AgentConfig.monetization on save.
        monetization: Optional[dict[str, Any]] = None
        # Per-agent Cortex engine overrides (agent.json5 `cognition`): master
        # enable + per-feature/autonomy/auto-approve/intensity three-state
        # overrides. Applied live to the scheduler (cognition cron jobs).
        cognition: Optional[dict[str, Any]] = None
        # Gmail watch (agent.json5 `gmail_watch`): the Overview card's
        # settings. Applied live to the scheduler (the watch job).
        gmail_watch: Optional[dict[str, Any]] = None

    class PromptGenerateRequest(BaseModel):
        # Draft a system prompt from the in-progress create/edit form. The agent
        # need not exist on disk yet, so everything comes from the body.
        name: str = ""
        description: str = ""
        tools: list[str] = Field(default_factory=list)
        # Model that writes the prompt. Empty → inherit the parent agent's model
        # (subagent "inherit"), else the platform default.
        model: Optional[str] = None
        agent: Optional[str] = None  # parent agent — for subagent model inheritance
        kind: Literal["agent", "subagent"] = "agent"

    class OrgDirectionBody(BaseModel):
        # "everyone" | "none" | "list" — pydantic 422s on anything else.
        mode: Literal["everyone", "none", "list"] = "everyone"
        agents: list[str] = Field(default_factory=list)

    class OrgUpdateBody(BaseModel):
        title: Optional[str] = None
        manager: Optional[str] = None
        reports: list[str] = Field(default_factory=list)
        coworkers: list[str] = Field(default_factory=list)
        input: OrgDirectionBody = Field(default_factory=OrgDirectionBody)
        output: OrgDirectionBody = Field(default_factory=OrgDirectionBody)
        autogenerate: bool = False
        mutating_from_manager_only: bool = False

    class SubagentCreateRequest(BaseModel):
        name: str
        description: str
        system_prompt: str
        model: Optional[str] = None
        tools: list[str] = Field(default_factory=list)
        skills: list[str] = Field(default_factory=list)

    class SubagentFromArchetypeRequest(BaseModel):
        archetype: str
        # Optional overrides written NEXT TO the ref (`{"from_archetype": …,
        # "model": …}`) — AgentConfig._resolve_subagent_references merges them
        # over the specialist's defaults, so the ref stays live.
        model: Optional[str] = None
        system_prompt_extension: Optional[str] = None
        tools: Optional[list[str]] = None

    class SubagentFromIntegrationRequest(BaseModel):
        toolkit_agent: str
        model: Optional[str] = None
        system_prompt_extension: Optional[str] = None
        extra_tools: list[str] = Field(default_factory=list)
        disabled_tools: list[str] = Field(default_factory=list)

    class SubagentOverridesRequest(BaseModel):
        """PUT semantics over a catalog-sourced subagent's override set.

        Every field is the complete new value for that override; an omitted or
        empty field CLEARS the override (back to the archetype/toolkit
        default). The ref itself is never dropped and resolved defaults are
        never written back into agent.json5.
        """

        model: Optional[str] = None
        system_prompt: Optional[str] = None
        system_prompt_extension: Optional[str] = None
        tools: Optional[list[str]] = None  # archetype refs only
        extra_tools: Optional[list[str]] = None  # builtin refs only
        disabled_tools: Optional[list[str]] = None  # builtin refs only

    class ChannelCreateRequest(BaseModel):
        type: str
        config: dict = Field(default_factory=dict)

    class SignalRegisterRequest(BaseModel):
        account: str  # E.164 phone number, e.g. +15551234567
        captcha: Optional[str] = None  # signalcaptcha://… token — sensitive, never logged

    class SignalVerifyRequest(BaseModel):
        account: str
        code: str  # SMS/voice verification code

    class SignalProfileRequest(BaseModel):
        account: str
        given_name: str
        agent: Optional[str] = None  # used to prefer the live daemon's JSON-RPC

    class SignalDetectRequest(BaseModel):
        # No fields — present for symmetry / future options.
        pass

    class MCPCreateRequest(BaseModel):
        url: str
        transport: str = "sse"
        enabled: bool = True
        command: Optional[str] = None
        args: list[str] = Field(default_factory=list)
        env: dict[str, str] = Field(default_factory=dict)
        disabled_tools: list[str] = Field(default_factory=list)
        headers: dict[str, str] = Field(default_factory=dict)
        auth: str = "none"  # "none" | "oauth"
        oauth_scope: Optional[str] = None
        # Pre-registered OAuth client credentials for servers without Dynamic
        # Client Registration (e.g. HubSpot's MCP auth apps).
        oauth_client_id: Optional[str] = None
        oauth_client_secret: Optional[str] = None

    class MCPToolsPatchRequest(BaseModel):
        """Body of ``PATCH /api/agents/{name}/mcp/tools`` — the deny-list only.

        Deliberately not `MCPCreateRequest`: routing a per-tool switch through
        the full-entry PUT rebuilds the stored server from this model alone, so
        any field the model does not name is dropped, and it round-trips
        `oauth_client_secret` through the browser on every click.
        """

        disabled_tools: list[str] = Field(default_factory=list)

    class MCPTestRequest(MCPCreateRequest):
        pass

    class WebhookCreateRequest(BaseModel):
        name: str
        direction: str = "outbound"
        url: str = ""
        events: list[str] = Field(default_factory=lambda: ["*"])
        enabled: bool = True
        # ``None`` leaves the stored secret untouched on update; "" clears it.
        secret: Optional[str] = None
        headers: dict[str, str] = Field(default_factory=dict)
        timeout_seconds: float = 10.0
        max_retries: int = 2
        rate_limit_per_minute: int = 60
        verify_tls: bool = True
        block_private_addresses: bool = True
        # Inbound-only.
        prompt: str = ""
        respond: bool = False
        accumulate_thread: bool = False
        max_body_bytes: int = 256_000

    class WebhookTestRequest(WebhookCreateRequest):
        pass

    class MCPOAuthStartRequest(MCPCreateRequest):
        # Origin the dashboard is served from (e.g. http://localhost:3200), used
        # to build the OAuth redirect URI so the browser callback lands back on
        # the same origin (proxied to /api/mcp/oauth/callback). Falls back to the
        # request base URL when omitted.
        redirect_base: Optional[str] = None

    class ArchetypeInstallRequest(BaseModel):
        archetype: str
        agent_name: Optional[str] = None  # defaults to archetype name
        # Editable display name for the installed agent. Empty falls back to
        # the archetype's own `display_name`/`label`, then the handle. With no
        # `agent_name`, the handle is minted from this (slug + `-2`… on a
        # collision) rather than defaulting to the archetype's slug.
        display_name: Optional[str] = None
        # The installed agent's position (`org.title`). Empty ⇒ the
        # archetype's own `title`, else its `label`.
        title: Optional[str] = None
        model: Optional[str] = None
        # Per-member model overrides for a team install: {ref: "provider:model"}
        # where ref is a member's `from_archetype` slug, inline name, or
        # `builtin_subagents` entry name (the namespaces are disjoint). Written
        # next to the member's ref in the installed agent.json5.
        subagent_models: dict[str, str] = Field(default_factory=dict)
        overwrite: bool = False
        command: Optional[str] = None
        args: list[str] = Field(default_factory=list)
        env: dict = Field(default_factory=dict)

    class IntegrationInstallRequest(BaseModel):
        toolkit_agent: str  # ToolkitAgent name or toolkit slug
        agent_name: Optional[str] = None  # empty ⇒ minted from display_name
        display_name: Optional[str] = None  # empty ⇒ the catalog label / handle
        title: Optional[str] = None  # the position (`org.title`); empty ⇒ none
        extra_tools: list[str] = Field(default_factory=list)
        disabled_tools: list[str] = Field(default_factory=list)
        model: Optional[str] = None
        overwrite: bool = False

    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False


# ── Agent YAML helpers ─────────────────────────────────────────────────────────


def _agent_config_path(name: str, create_dir: bool = False) -> Path:
    """Resolve ``agents/<name>/agent.json5`` — local ``./agents/`` first, then the
    home agents dir. Uses ``get_olano_paths()`` so a custom ``LIONCLAW_HOME``
    is honored (matches loader.list_agents / find_agent_config_path); hardcoding
    ``~/.olano/agents`` desynced reads from enumeration under a custom home."""
    from olano.config.home import get_olano_paths

    local_root = Path("agents").resolve()
    global_root = get_olano_paths().agents_dir
    fname = "agent.json5"

    for root in (local_root, global_root):
        candidate = root / name / fname
        if candidate.exists():
            return candidate

    if create_dir:
        target_root = local_root if local_root.exists() else global_root
        target = target_root / name
        target.mkdir(parents=True, exist_ok=True)
        return target / fname

    raise FileNotFoundError(f"agent.json5 not found for '{name}'")


# Keys that AgentConfig used to accept but now rejects (REARCHITECTURE_BACKENDS.md §5).
# We strip them on read/write so existing YAMLs created by older dashboard
# revisions keep booting after the validator tightened.
#
# ONLY put a key here that AgentConfig genuinely no longer has. `acp_enabled` /
# `acp_port` were listed by mistake: they are live fields (agent_config.py) that
# `RuntimeServer._boot_agent_locked` reads to start the ACP server, so stripping
# them made the Advanced tab's "Acp Enabled" toggle a no-op — the PUT returned
# 200, the write dropped the key, and the reload showed the switch back off. The
# read path was worse still: `_read_agent_config` rewrites the file when it
# strips, so a hand-edited `acp_enabled: true` was deleted from disk by the next
# dashboard read.
#
# `sandbox` is NOT in this list any more: it was removed as an on/off scalar and
# came back as a live block of resource ceilings (`SandboxResourceOverride`).
# Listing it stripped a real `sandbox: {memory: "4g"}` on every dashboard read
# and write, and it made every `save_agent_config` (which writes `sandbox: null`
# for an unset block) followed by a page load rewrite the file, so Revisions
# recorded a change to agents nobody touched. Only the old scalar spelling is
# legacy now; `_strip_legacy_sandbox` drops that and leaves a block or a null.
_LEGACY_AGENT_KEYS: tuple[str, ...] = ("sandbox_backend",)


def _strip_legacy_sandbox(data: dict) -> bool:
    """Drop the old on/off ``sandbox:`` scalar. True if it was removed.

    A dict (resource ceilings) or an explicit null is a valid value and stays,
    so reading an unchanged file never rewrites it.
    """
    if "sandbox" not in data:
        return False
    value = data["sandbox"]
    if value is None or isinstance(value, dict):
        return False
    data.pop("sandbox", None)
    return True


# Map dashboard's friendly backend shorthands → modern BackendConfig blocks.
# (String backends were valid in older releases; the validator now requires a
# block — see REARCHITECTURE_BACKENDS.md §5.)
_BACKEND_STRING_MAP: dict[str, dict] = {
    "filesystem+shell": {"type": "local_shell", "root_dir": "workspace"},
    "local_shell": {"type": "local_shell", "root_dir": "workspace"},
    "filesystem": {"type": "filesystem", "root_dir": "workspace", "access_mode": "read_write"},
    "read": {"type": "filesystem", "root_dir": "workspace", "access_mode": "read"},
    # Virtual (no real host filesystem) backends. These give the agent a working
    # virtual filesystem but no shell execution and never touch the host disk.
    #   "state" → "Virtual Files (Temporary)"  — in-memory, thread-local; cleared
    #             when the conversation thread ends.
    #   "store" → "Virtual Files (Persistent)" — backed by the shared on-disk KV
    #             store; survives across threads and process restarts. The builder
    #             namespaces it to the agent name so agents stay isolated.
    "state": {"type": "state"},
    "store": {"type": "store"},
}


def _normalize_backend(data: dict) -> bool:
    """Convert a string-form `backend:` field into the block form. Returns True if the
    field was rewritten."""
    backend = data.get("backend")
    if isinstance(backend, str):
        block = _BACKEND_STRING_MAP.get(backend)
        if block is None:
            # Unknown shorthand — fall back to local_shell so we don't lose the
            # agent. The user can edit the YAML directly if they want something
            # different.
            block = {"type": "local_shell", "root_dir": "workspace"}
        data["backend"] = block
        return True
    return False


def _normalize_default_verbosity(data: dict) -> bool:
    """Canonicalize a `default_verbosity:` alias in place. True if rewritten.

    AgentConfig accepts the same spellings ``/verbosity`` does ("full", "v",
    "silent", …), but the handler persists the raw merged dict rather than the
    validated model — so without this an alias survives into agent.json5 and
    the dashboard's select, which matches on the canonical value, renders with
    nothing selected. Normalizing on the write path keeps the file and the UI
    agreeing on one spelling.
    """
    raw = data.get("default_verbosity")
    if raw is None:
        return False
    from olano.platforms.verbosity import DEFAULT_VERBOSITY, normalize_verbosity

    canonical = normalize_verbosity(raw) or DEFAULT_VERBOSITY
    if canonical == raw:
        return False
    data["default_verbosity"] = canonical
    return True


def _normalize_channel_lists(data: dict) -> bool:
    """Channel adapters expect list-shaped fields (allowed_users, allowed_channels)
    even when there's only one entry. The dashboard's old form let users save a
    bare scalar (`1234` instead of `[1234]`), which then crashed the Telegram /
    Discord adapter with "'int' object is not iterable". Coerce here so the file
    self-heals on read."""
    changed = False
    for ch in data.get("channels") or []:
        if not isinstance(ch, dict):
            continue
        for key in (
            "allowed_users",
            "allowed_channels",
            "allowed_chats",
            "allowed_threads",
            "guild_ids",
        ):
            v = ch.get(key)
            if v is None:
                continue
            if isinstance(v, int | str):
                ch[key] = [v]
                changed = True
            elif not isinstance(v, list):
                # Pydantic configs may store these as tuples or sets after a
                # round trip — make them plain lists.
                ch[key] = list(v) if hasattr(v, "__iter__") else [v]
                changed = True
    return changed


def _flatten_nested_channel_config(data: dict) -> bool:
    """Hoist ``channels[].config`` into the parent dict so the dashboard and
    adapters see one flat map per connector (``type``, ``token``, ``guild_ids``, …).

    Some templates store ``type`` + ``config: {{...}}``; the Mission Control form
    only reads top-level keys, so ``guild_ids`` appeared to vanish after save."""
    changed = False
    channels = data.get("channels")
    if not isinstance(channels, list):
        return False
    for ch in channels:
        if not isinstance(ch, dict):
            continue
        nested = ch.pop("config", None)
        if not isinstance(nested, dict) or not nested:
            continue
        merged = {**nested, **ch}
        ch.clear()
        ch.update(merged)
        changed = True
    return changed


def _strip_legacy_keys(data: dict) -> tuple[dict, list[str]]:
    removed: list[str] = []
    for k in _LEGACY_AGENT_KEYS:
        if k in data:
            data.pop(k, None)
            removed.append(k)
    if _strip_legacy_sandbox(data):
        removed.append("sandbox(scalar)")
    if _normalize_backend(data):
        removed.append("backend(string→block)")
    if _flatten_nested_channel_config(data):
        removed.append("channels(config→flat)")
    if _normalize_channel_lists(data):
        removed.append("channels(scalar→list)")
    return data, removed


# Server-side palette mirrors the dashboard's COLOR_PRESETS so randomly-
# assigned colors look varied and on-brand whether the agent was created via
# the dashboard form or the API directly.
_AGENT_COLOR_PALETTE: tuple[str, ...] = (
    "#c96442",
    "#e07a4b",
    "#d9a04a",
    "#3aa07a",
    "#1f88a4",
    "#2f6dbe",
    "#5b8def",
    "#7a6cf3",
    "#a55cb8",
    "#d8508a",
    "#c43c5e",
    "#c0392b",
    "#5a8d59",
    "#8a9a5b",
    "#9a7b4f",
    "#3d4f7c",
    "#2b6c5b",
    "#b6635a",
    "#6f7d8c",
    "#a6786b",
)


def _pick_unused_agent_color(runtime: Any) -> str:
    """Pick a palette color that no booted agent currently uses, falling
    back to a random palette pick when every preset is taken."""
    import random

    used: set[str] = set()
    for runner in getattr(runtime, "_runners", {}).values():
        c = getattr(runner.config, "color", None)
        if isinstance(c, str):
            used.add(c.lower())
    fresh = [c for c in _AGENT_COLOR_PALETTE if c.lower() not in used]
    pool = fresh or list(_AGENT_COLOR_PALETTE)
    return random.choice(pool)


def _connector_type(adapter: Any) -> str:
    """The connector type a live channel/platform adapter was booted from.

    Mirrors the two identities ``connectors_health`` resolves against, so both
    endpoints name a connector the same way the agent config does:
      • platform adapters → ``platform:<Platform.value>`` (``platform:telegram``)
      • classic channels  → the ``BaseChannel.name`` class attr (``telegram``)
    Falls back to the class name for anything that is neither.
    """
    value = getattr(getattr(adapter, "platform", None), "value", None)
    if isinstance(value, str) and value:
        return f"platform:{value}"
    name = getattr(adapter, "name", None)
    if isinstance(name, str) and name:
        return name
    return type(adapter).__name__


def _live_connectors(runtime: Any, allowed: set[str] | frozenset[str] | None) -> list[str]:
    """Every connector currently attached to a booted agent, as type strings.

    Connectors live in TWO runtime registries — ``_channels`` (classic
    ``BaseChannel``) and ``_platforms`` (``platform:<x>`` adapters) — and the
    dashboard writes the platform form for every connector it creates. Counting
    only ``_channels`` is why the Overview "Channels" stat read 0 on a box with
    live Telegram/WhatsApp/Slack connectors.

    ``allowed`` scopes the result to a member's granted agents (None = every
    agent, i.e. admin / single-user). Scoping goes through
    ``_agent_resources``, which records exactly which adapters each agent owns.
    """
    if allowed is None:
        adapters = [
            *(getattr(runtime, "_channels", None) or []),
            *(getattr(runtime, "_platforms", None) or []),
        ]
    else:
        resources = getattr(runtime, "_agent_resources", None) or {}
        adapters = [
            adapter
            for name in allowed
            for key in ("channels", "platforms")
            for adapter in (resources.get(name, {}).get(key) or [])
        ]
    return [_connector_type(a) for a in adapters]


def _configured_connectors(runtime: Any, allowed: set[str] | frozenset[str] | None) -> int:
    """How many connectors the booted agents *declare*, hidden agents aside.

    ``_live_connectors`` only sees adapters that actually attached, so a
    connector with a stale token boots to nothing and disappears from the
    count. Reporting the declared total alongside it lets the Overview say
    "2 · 1 active" instead of hiding a broken connector behind a 0.

    Hidden/internal agents are excluded because the boot path never binds their
    connectors (see ``_boot_agent_locked``) — counting them would make the
    declared total permanently unreachable.
    """
    total = 0
    for name, runner in (getattr(runtime, "_runners", None) or {}).items():
        if allowed is not None and name not in allowed:
            continue
        config = getattr(runner, "config", None)
        if config is None or _agent_is_hidden(config):
            continue
        total += len(getattr(config, "channels", None) or [])
    return total


def _build_multimodal_content(message: str, attachments: list[dict] | None) -> Any:
    """Combine plain text + attachments into a LangChain-compatible payload.

    Returns either:
      • a plain string (no attachments, or only text-file attachments which
        get inlined into the prompt), so the existing string path stays cheap
      • a list of content blocks: [{type:text,text:...}, {type:image,source:...}]
        — required when at least one image is present.

    In the normal admin flow, media attachments were already persisted and
    replaced by path notes (``runtime/chat_attachments.py``), so this sees only
    text files. Image items still reach here — and are inlined as vision
    blocks — for read-only members (whose attachments are never written to
    disk) and as the fallback when persisting failed.

    Block shapes follow Anthropic's multimodal contract; LangChain's chat
    models translate these into the right per-provider format (OpenAI's
    image_url style, Gemini's inline_data, etc.) at invoke time.
    """
    if not attachments:
        return message

    text_inlines: list[str] = []
    image_blocks: list[dict] = []
    skipped: list[str] = []

    for att in attachments:
        if not isinstance(att, dict):
            continue
        kind = att.get("kind")
        name = str(att.get("name", "")).strip() or "attachment"
        mime = str(att.get("mime", "")).strip().lower()
        data = att.get("data", "")
        if kind == "image":
            if not data or not mime.startswith("image/"):
                skipped.append(f"{name} (bad image)")
                continue
            image_blocks.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime, "data": data},
                }
            )
        elif kind == "file":
            content = str(data) if data is not None else ""
            # Skip empty files, but keep silently — empty files happen.
            if not content.strip():
                continue
            # Truncate very large files so we don't blow the context window
            # before the agent even starts. The user can split / paste
            # selectively if they need more.
            limit = 60_000
            truncated = content[:limit]
            note = (
                ""
                if len(content) <= limit
                else f"\n[... truncated, {len(content) - limit} chars omitted]"
            )
            text_inlines.append(
                f"\n--- attached: {name} ({mime or 'text'}) ---\n{truncated}{note}\n--- end {name} ---"
            )
        elif kind == "document":
            # Persisted or inlined upstream (runtime/chat_attachments.py); one
            # reaching here means both failed, and an attachment is never silent.
            text_inlines.append(f"\n[Document attachment '{name}' could not be read.]")
        else:
            skipped.append(f"{name} (unsupported kind={kind!r})")

    composed_text = message
    if text_inlines:
        composed_text = (composed_text + "\n\n" if composed_text else "") + "\n\n".join(
            text_inlines
        )
    if skipped:
        logger.info("Dropped attachments: %s", ", ".join(skipped))

    if not image_blocks:
        return composed_text  # plain string path

    # Image attachments: emit the multimodal content list. Text always goes
    # first so the model sees the question before the imagery.
    blocks: list[dict] = []
    if composed_text:
        blocks.append({"type": "text", "text": composed_text})
    blocks.extend(image_blocks)
    return blocks


def _ai_message_text(content: Any) -> str:
    """Flatten a LangChain message ``content`` (str or list of blocks) to text.

    Providers return either a plain string or a list of content blocks
    (``{"type": "text", "text": ...}`` for Anthropic, ``{"text": ...}`` etc.);
    collapse both to a single string so callers get plain text back.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text") or block.get("content")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    return str(content) if content is not None else ""


def _serialize_agent_config(data: dict) -> str:
    """Serialize agent config as JSON (strict subset accepted by JSON5 parsers)."""
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _agent_is_hidden(source: Any) -> bool:
    """True only when an agent config explicitly opts into hidden/internal mode.

    Accepts either a parsed ``agent.json5`` dict (disk-only agents) or an
    ``AgentConfig`` object (booted runners). The check is an identity test
    against ``True`` so a test ``MagicMock`` config — whose arbitrary
    attributes are themselves truthy mocks — is never mistaken for a real
    ``hidden: true`` flag; only a genuine boolean ``True`` hides the agent.
    """
    value = source.get("hidden") if isinstance(source, dict) else getattr(source, "hidden", False)
    return value is True


def _agent_title(name: str, org: Any = None) -> str:
    """An agent's position for a roster row or detail payload.

    One resolver (`org_store.resolve_title`): the central organization.yaml
    entry when it has one, else the ``org`` block the caller already holds —
    an OrgConfig off a booted runner, or the raw dict of an on-disk read.
    Never raises: a title is decoration, and the roster must not go dark over
    a malformed organization.yaml. A MagicMock config (tests) yields "".
    """
    try:
        from olano.config.org_store import resolve_title

        title = resolve_title(name, org)
    except Exception:
        return ""
    return title if isinstance(title, str) else ""


def _read_agent_config(name: str) -> dict:
    path = _agent_config_path(name)
    text = path.read_text(encoding="utf-8")
    try:
        data = _load_json5(text)
    except Exception as exc:
        raise RuntimeError(f"Could not parse JSON5 in {path}: {exc}") from exc

    data = data or {}
    data, removed = _strip_legacy_keys(data)
    if removed:
        from olano.config.loader import write_agent_config_text

        write_agent_config_text(path, _serialize_agent_config(data))
        logger.info("Migrated legacy fields %s in %s", removed, path)
    return data


# Mtime-keyed parse caches for read-only paths. A single large agent.json5
# costs ~0.6s of pure-Python json5, and roster polls re-read every unchanged
# file. Keyed by (resolved path, st_mtime_ns) so any write — ours or an
# external editor's — invalidates on the next read. The dict variant hands out
# deepcopies (~1ms) because callers mutate their copy; the AgentConfig variant
# shares one instance, which callers must treat as immutable. Writers keep
# using _read_agent_config directly (its legacy-key migration must run).
_AGENT_CONFIG_DICT_CACHE: dict[str, tuple[tuple[str, int], dict]] = {}
_AGENT_CONFIG_OBJ_CACHE: dict[str, tuple[tuple[str, int], Any]] = {}


def _read_agent_config_cached(name: str) -> dict:
    try:
        path = _agent_config_path(name)
        key = (str(path), path.stat().st_mtime_ns)
    except (OSError, AttributeError):
        # No stat-able file (or tests stubbed the path/reader) — plain read decides.
        return _read_agent_config(name)
    hit = _AGENT_CONFIG_DICT_CACHE.get(name)
    if hit is not None and hit[0] == key:
        return copy.deepcopy(hit[1])
    data = _read_agent_config(name)
    try:
        # Legacy-key migration may have rewritten the file; re-key on its mtime.
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        pass
    _AGENT_CONFIG_DICT_CACHE[name] = (key, data)
    return copy.deepcopy(data)


def _load_agent_config_cached(name: str) -> Any:
    from olano.config.loader import find_agent_config_path, load_agent_config

    path = find_agent_config_path(name)
    if path is None:
        raise FileNotFoundError(f"No agent config found for '{name}'")
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return load_agent_config(name)
    hit = _AGENT_CONFIG_OBJ_CACHE.get(name)
    if hit is not None and hit[0] == key:
        return hit[1]
    cfg = load_agent_config(name)
    _AGENT_CONFIG_OBJ_CACHE[name] = (key, cfg)
    return cfg


def _write_agent_config(name: str, data: dict, create: bool = False) -> Path:
    """Persist an agent config dict — the API-side central write path.

    Every mutation endpoint (structured PUT, channel/MCP/webhook add/update)
    funnels through here, so the deployment-limit guard runs count → decide →
    write atomically under the interprocess lock: growing a counted resource
    beyond its cap raises the structured 403; shrinking or re-saving an
    over-limit config always succeeds. Unmanaged installs short-circuit.

    It is also where channel credentials stop being plain text: any literal bot
    token / password in ``channels`` is moved into the vault as a per-agent
    override and replaced with ``${vault:KEY}`` before the file is written (see
    olano/config/channel_secrets.py). Being here rather than in one endpoint
    means the connector form, the onboarding wizard, agentfather's config tools,
    and a raw JSON5 PUT all get it. Idempotent, and a locked vault leaves the
    value untouched rather than dropping it.
    """
    from olano.config.channel_secrets import absorb_channel_secrets
    from olano.config.loader import write_agent_config_text
    from olano.config.mcp_secrets import absorb_mcp_client_secrets_quietly
    from olano.deployment.locks import deployment_write_lock

    path = _agent_config_path(name, create_dir=create)
    data.setdefault("name", name)
    data, _ = _strip_legacy_keys(data)
    absorb_channel_secrets(name, data)
    # Same guarantee for an MCP server's OAuth client secret (the dashboard's
    # server editor and Authorize dialog both land here) — see
    # olano/config/mcp_secrets.py.
    absorb_mcp_client_secrets_quietly(name, data)
    with deployment_write_lock():
        old_raw: dict | None = None
        if path.exists():
            try:
                old_raw = _load_json5(path.read_text(encoding="utf-8"))
            except Exception:
                old_raw = {}  # unreadable ⇒ agent exists but counts as empty
        dep_guard.reject(dep_enforcement.validate_config_write(name, old_raw, data))
        write_agent_config_text(path, _serialize_agent_config(data))
        dep_enforcement.invalidate_counts()
    return path


# API-side agent.json5 writers queue here, not on the deployment lock's thread
# lock. `deployment_write_lock` is process-wide and is also taken *on the loop*
# by the handshake routes (owner login, link mint), where every extra writer
# already blocked in `_THREAD_LOCK.acquire(timeout=10)` is time the whole
# runtime is frozen — and a lost race there raises DeploymentLockTimeout.
# Waiting on the loop instead means one worker thread at a time. Every async
# API path that takes the deployment lock in a thread goes through
# `_config_write_lock()` — the structured write, the channel absorb-write, the
# raw editor and agent create.
_CONFIG_WRITE_GATE: tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None = None

# Per-agent gate for read-modify-write of agent.json5 — see
# `_read_agent_config_for_update`. The wait bound covers the slowest write
# (deployment lock spin + a managed-posture recount of every config).
_CONFIG_RMW_GATES: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}
_CONFIG_RMW_OWNERS: dict[str, "asyncio.Task[Any]"] = {}
_CONFIG_RMW_WAIT_SECONDS = 15.0


def _loop_bound_lock(
    entry: tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None,
) -> tuple[asyncio.AbstractEventLoop, asyncio.Lock]:
    """`entry` if it belongs to the running loop, else a fresh lock for it.

    An asyncio.Lock binds to the loop that first awaits it and raises "bound to
    a different event loop" anywhere else; tests (and any second in-process
    runtime) run on a fresh loop, so a module-level lock has to be rebuilt.
    """
    loop = asyncio.get_running_loop()
    if entry is not None and entry[0] is loop:
        return entry
    return (loop, asyncio.Lock())


def _config_write_lock() -> asyncio.Lock:
    """The gate every async API-side agent.json5 write queues on."""
    global _CONFIG_WRITE_GATE

    _CONFIG_WRITE_GATE = _loop_bound_lock(_CONFIG_WRITE_GATE)
    return _CONFIG_WRITE_GATE[1]


async def _write_agent_config_async(name: str, data: dict, create: bool = False) -> Path:
    """Event-loop-safe `_write_agent_config`: the sync body can block for up to
    10s on the interprocess deployment lock (thread lock + flock spin), parses
    the old config, and under a managed posture re-counts every agent.json5 —
    async handlers must call this wrapper. The interprocess lock already
    serializes concurrent writers, so running the body in a worker thread is
    safe."""
    async with _config_write_lock():
        return await asyncio.to_thread(_write_agent_config, name, data, create)


async def _read_agent_config_for_update(name: str) -> dict:
    """`_read_agent_config` off the loop, holding the agent's mutation gate.

    Every writer rewrites the whole file, so a read-modify-write is only correct
    while nothing else writes in between — which the single-threaded loop used
    to guarantee for free, back when both halves were inline. Now that the read
    and the write each hop to a worker thread, two mutating handlers interleave
    and the later write silently reverts the earlier edit (the sprite-sheet
    background task vs. an Identity save is the reachable case).

    The gate is released when the calling task ends, not at the write: a handler
    may write in more than one branch, and a read that raises (404) must not
    leak it. Re-entrant for the holding task, so a handler can read again.
    Every caller is therefore a request (or one background task) that reads,
    writes and finishes; callers that only read keep using `_read_agent_config`.
    Waiting for the gate is bounded — a stuck holder must degrade to the
    unguarded read it replaced, never hang the endpoint.
    """
    task = asyncio.current_task()
    if task is None:  # nothing to hang the release on — read unguarded
        return await asyncio.to_thread(_read_agent_config, name)
    if _CONFIG_RMW_OWNERS.get(name) is not task:
        entry = _loop_bound_lock(_CONFIG_RMW_GATES.get(name))
        _CONFIG_RMW_GATES[name] = entry
        try:
            await asyncio.wait_for(entry[1].acquire(), timeout=_CONFIG_RMW_WAIT_SECONDS)
        except TimeoutError:
            logger.warning("agent config mutation gate for '%s' timed out; reading unguarded", name)
        else:
            _CONFIG_RMW_OWNERS[name] = task
            task.add_done_callback(lambda _t: _release_config_rmw_gate(name, _t, entry[1]))
    return await asyncio.to_thread(_read_agent_config, name)


def _release_config_rmw_gate(name: str, task: "asyncio.Task[Any]", gate: asyncio.Lock) -> None:
    if _CONFIG_RMW_OWNERS.get(name) is task:
        del _CONFIG_RMW_OWNERS[name]
        gate.release()


def _provider_env_vars() -> set[str]:
    """Env-var names that hold LLM provider API keys (the BYOK surface)."""
    try:
        from olano.models import PROVIDERS

        return {info.env_var for info in PROVIDERS.values() if info.env_var}
    except Exception:
        return set()


def _byok_allowed() -> bool:
    """Whether this deployment may connect its own provider credentials.

    Two signed feature flags gate the BYOK surface: `byok` (plan entitlement —
    off below Ultra) and `model_selection` (this box may pick models at all).
    Either off blocks key entry. Missing keys default open (unmanaged boxes and
    pre-epoch-5 artifacts are unaffected).
    """
    return dep_enforcement.feature_enabled("byok") and dep_enforcement.feature_enabled(
        "model_selection"
    )


def _reject_unless_byok_allowed() -> None:
    """Raise the actionable BYOK denial for key-entry endpoints.

    Names the flag that actually caused the denial: a plan without BYOK says
    "upgrade", a box without model selection says model selection is off.
    """
    if _byok_allowed():
        return
    if not dep_enforcement.feature_enabled("byok"):
        dep_guard.reject(
            dep_enforcement.Decision(
                allowed=False,
                code="deployment_feature_byok_disabled",
                message=(
                    "Bring-your-own-keys (BYOK) is not included in this deployment's plan. "
                    "The Olano models keep working on credits."
                ),
                remediation=(
                    "Upgrade to the Ultra plan or above at cloud.olano.ai to connect "
                    "your own provider API keys."
                ),
            )
        )
    dep_guard.reject(
        dep_enforcement.feature_decision("model_selection", label="provider API keys (BYOK)")
    )


def _reject_utility_brain(*models: str | None) -> None:
    """422 if any value is an Olano utility group used as a selectable model.

    The endpoints that write a config through AgentConfig validation (PUT agent,
    raw editor) are covered by the AgentConfig validator; this guards the ones
    that assemble a template dict and write it directly (agent create, subagent
    create/update, cognition fast/deep model), so a utility-as-brain is refused
    at the API boundary instead of failing later at build.
    """
    from olano.models.olano_gateway import is_olano_utility_model

    for m in models:
        if is_olano_utility_model(m):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"'{m}' is an Olano utility service (image understanding / "
                    "generation) the product calls on the agent's behalf, not a "
                    "model you can assign as a brain. Use olano:turbo, olano:pro "
                    "or olano:max."
                ),
            )


def _validate_brain(*models: str | None) -> None:
    """422 for a model value no agent could run with: an Olano utility group,
    or a `default:*` alias naming an unknown tier. Concrete provider:model
    strings pass through unjudged — provider availability is a runtime concern,
    not a config-write one."""
    _reject_utility_brain(*models)
    from olano.models.aliases import resolve_model_alias

    for m in models:
        if not m:
            continue
        try:
            resolve_model_alias(m)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))


# The brain a new agent gets when the caller didn't pick one: the fast-tier
# alias, unconditionally. `default:turbo` resolves at build time to whatever
# this box's `default_models:` block (or built-in target) points at — the
# managed/unmanaged fork lives inside the alias layer now
# (`system_settings.builtin_default_model_targets`), not here, so "what does a
# new agent run on" has exactly one answer for custom agents and installed
# archetypes alike (`installer.MANAGED_ARCHETYPE_MODEL` is the same alias).
def _default_agent_model() -> str:
    from olano.archetypes.installer import MANAGED_ARCHETYPE_MODEL

    return MANAGED_ARCHETYPE_MODEL


# ── config-schedule jobs (agent.json5 `schedule`) ──────────────────────────────
# These live only in each agent's file (never the scheduler DB). The mapping and
# edit logic lives in ``olano.admin.scheduler`` so this HTTP surface and
# AgentFather's fleet ``olano_scheduler_*`` tools cannot disagree about what a
# task is; the wrappers below only translate its ValueErrors into HTTP codes.

from olano.admin.scheduler import CONFIG_JOB_PREFIX as _CONFIG_JOB_PREFIX

# Re-exported for callers that still reach for it at this module path (the
# scheduler suite pins it here); the implementation lives in admin.scheduler.
from olano.admin.scheduler import entry_cron_string as _entry_cron_string  # noqa: F401


def _parse_config_job_id(task_id: str) -> tuple[str, int]:
    """Split ``cfg:{agent}:{index}`` → (agent, index). 404 on a malformed id."""
    from olano.admin import scheduler as _sched

    try:
        return _sched.parse_config_job_id(task_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Invalid config job id '{task_id}'")


def _write_and_resync_schedule(agent: str, data: dict) -> None:
    """Validate the mutated agent config, write agent.json5, and live-reregister
    just that agent's config-schedule jobs (heartbeat/cognition untouched)."""
    from olano.admin import scheduler as _sched

    try:
        _sched.write_and_resync_schedule(agent, data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


def _resolve_subagents_for_display(data: dict) -> dict:
    """Attach a display-only resolved view of every subagent to ``data`` under
    ``subagents_resolved`` **without mutating the source** ``subagents`` /
    ``builtin_subagents`` lists.

    ``agent.json5`` is the source of truth: the dashboard's Config tab and any
    round-tripped save must mirror it exactly. ``from_archetype`` stubs and
    ``builtin_subagents`` refs are therefore only ever expanded to full
    name/description/tool metadata for *rendering* — the expansion that actually
    feeds ``create_deep_agent`` lives in ``AgentConfig._resolve_subagent_references``
    and is independent of this function.

    Each resolved entry carries two routing fields the dashboard uses to map a
    card back to its source so delete/update calls hit the right list:

      * ``ref``    — the stable key (inline ``name``, ``from_archetype`` value,
                     or the raw ``builtin_subagents`` entry name)
      * ``source`` — ``"inline" | "archetype" | "builtin"``
    """
    from olano.archetypes.installer import load_archetype_template

    resolved: list[dict] = []
    for sub in data.get("subagents") or []:
        if not isinstance(sub, dict):
            continue
        ref = sub.get("from_archetype")
        if not ref:
            # Hand-written inline subagent — already display-ready.
            resolved.append({**sub, "ref": sub.get("name", ""), "source": "inline"})
            continue
        try:
            specialist = load_archetype_template(ref)
        except Exception as exc:
            logger.debug("Subagent archetype '%s' unresolved: %s", ref, exc)
            resolved.append(
                {
                    "name": ref,
                    "description": f"(unresolved archetype `{ref}`)",
                    "from_archetype": ref,
                    "ref": ref,
                    "source": "archetype",
                }
            )
            continue
        defaults = {
            "name": specialist.get("label") or specialist.get("name") or ref,
            "description": specialist.get("description", ""),
            "system_prompt": specialist.get("system_prompt", ""),
            "tools": list(specialist.get("tools", []) or []),
            "model": specialist.get("model"),
            "emoji": specialist.get("emoji"),
        }
        overrides = {k: v for k, v in sub.items() if k != "from_archetype" and v is not None}
        merged = {
            **defaults,
            **overrides,
            "from_archetype": ref,
            "ref": ref,
            "source": "archetype",
            # The editor needs the pristine base prompt (to render read-only
            # next to the "append" box) and the raw override set (to prefill),
            # separate from the composed effective prompt below.
            "system_prompt_base": defaults["system_prompt"],
            "overrides": overrides,
        }
        extension = str(merged.get("system_prompt_extension") or "").strip()
        base_prompt = str(merged.get("system_prompt") or "")
        if extension and not base_prompt.rstrip().endswith(extension):
            # Same composition as AgentConfig._resolve_subagent_references, so
            # the card shows exactly the prompt the runtime builds with.
            merged["system_prompt"] = (
                f"{base_prompt.rstrip()}\n\n## Additional owner instructions\n{extension}"
            ).strip()
        resolved.append(merged)

    # Resolve builtin_subagents (OpsManager + ToolkitAgents) so the dashboard
    # shows everything the runtime will actually inject. Entries are tagged
    # `from_builtin` so the UI can render an "installed via catalog" badge and
    # route delete/update calls through the builtin_subagents list.
    builtin_entries = data.get("builtin_subagents") or []
    if isinstance(builtin_entries, list):
        from olano.subagents.registry import (
            _expand_tools,
            load_toolkit_agent,
        )

        seen = {(s.get("name") or "").lower() for s in resolved}
        for entry in builtin_entries:
            if isinstance(entry, dict):
                ref_name = str(entry.get("name", "")).strip()
                override_extra = list(entry.get("extra_tools") or [])
                override_disabled = list(entry.get("disabled_tools") or [])
                override_model = entry.get("model")
                override_prompt = entry.get("system_prompt")
                override_extension = entry.get("system_prompt_extension")
            else:
                ref_name = str(entry).strip()
                override_extra = []
                override_disabled = []
                override_model = None
                override_prompt = None
                override_extension = None
            if not ref_name:
                continue

            normalized = ref_name.lower().replace("_", "").replace("-", "")
            if normalized in {"ops", "opsmanager", "automationmanager"}:
                if "opsmanager" in seen:
                    continue
                seen.add("opsmanager")
                from olano.config.agent_config import _ops_manager_subagent

                # _resolve_builtin_subagents() (agent_config.py) ignores any
                # extra_tools/disabled_tools/model override for the ops alias —
                # mirror that here rather than applying overrides the runtime
                # would never actually honor.
                base = _ops_manager_subagent()
                resolved.append(
                    {
                        "name": base.name,
                        "description": base.description,
                        "system_prompt": base.system_prompt,
                        "tools": list(base.tools),
                        "model": base.model,
                        "from_builtin": ref_name,
                        "ref": ref_name,
                        "source": "builtin",
                        "emoji": "🛠️",
                    }
                )
                continue

            try:
                defn = load_toolkit_agent(ref_name)
            except KeyError:
                resolved.append(
                    {
                        "name": ref_name,
                        "description": f"(unresolved built-in `{ref_name}`)",
                        "from_builtin": ref_name,
                        "ref": ref_name,
                        "source": "builtin",
                    }
                )
                continue
            if defn.name.lower() in seen:
                continue
            seen.add(defn.name.lower())

            from olano.subagents._schema import ToolkitAgentOverride

            override = (
                ToolkitAgentOverride(
                    name=defn.name,
                    extra_tools=override_extra,
                    disabled_tools=override_disabled,
                    model=override_model,
                )
                if (override_extra or override_disabled or override_model)
                else None
            )
            tools = _expand_tools(defn, override)
            # Same composition as registry.resolve_as_subagent: an explicit
            # prompt override replaces the def's, the extension appends to
            # whichever base applies — the card shows the effective prompt.
            effective_prompt = str(override_prompt or defn.system_prompt or "")
            extension = str(override_extension or "").strip()
            if extension and not effective_prompt.rstrip().endswith(extension):
                effective_prompt = (
                    f"{effective_prompt.rstrip()}\n\n## Additional owner instructions\n{extension}"
                ).strip()
            builtin_overrides = {
                k: v
                for k, v in {
                    "model": override_model,
                    "system_prompt": override_prompt,
                    "system_prompt_extension": override_extension,
                    "extra_tools": override_extra or None,
                    "disabled_tools": override_disabled or None,
                }.items()
                if v is not None
            }
            resolved.append(
                {
                    "name": defn.name,
                    "description": defn.description,
                    "system_prompt": effective_prompt,
                    "system_prompt_base": defn.system_prompt,
                    "overrides": builtin_overrides,
                    "system_prompt_extension": override_extension,
                    "tools": tools,
                    "model": override_model,
                    "emoji": defn.emoji,
                    "from_builtin": ref_name,
                    "ref": ref_name,
                    "source": "builtin",
                }
            )

    data["subagents_resolved"] = resolved
    return data


def _resolve_effective_tool_names(name: str, raw: dict, cfg: Any | None = None) -> list[str]:
    """Display-only list of the tools an agent actually runs with.

    ``agent.json5 → tools`` only carries *extra* tools, so it is empty for most
    agents. Loading the config through ``AgentConfig`` runs
    ``_inject_builtin_ops``, which merges in the universal defaults (respecting
    ``disabled_tools``); we then append the deepagents built-ins. The result
    mirrors what the agent's ``whoami`` reports — never the raw ``tools`` field,
    which stays untouched for round-tripped saves.

    Falls back to the raw ``tools`` list if the config can't be loaded so the
    endpoint never fails just to enrich a listing.

    ``cfg`` lets a caller that already parsed the config pass it through —
    ``get_agent`` used to parse the same agent.json5 three times per request.
    """
    from olano.config.ops_defaults import resolve_effective_tool_names

    try:
        if cfg is None:
            from olano.config.loader import load_agent_config

            cfg = load_agent_config(name)
        return resolve_effective_tool_names(list(cfg.tools), cfg.disabled_tools, cfg)
    except Exception:
        return [t for t in (raw.get("tools") or []) if t]


def _resolve_internal_tools(name: str, cfg: Any | None = None) -> list["InternalToolRow"]:
    """Every builder/deepagents-provided tool this agent can get without a
    ``tools:`` entry, categorized, with its live on/off state.

    Unlike ``_resolve_effective_tool_names`` (a flat name list mirroring
    ``whoami``), this powers the dashboard's "Olano Internal Tools" section —
    grouped by category, including tools whose *condition* currently makes
    them inapplicable (so toggling ``memory.per_customer`` later is visibly
    documented, not just silently different). See
    ``olano.config.internal_tools.INTERNAL_TOOL_CATALOG``.
    """
    from olano.config.internal_tools import effective_internal_tools

    try:
        if cfg is None:
            from olano.config.loader import load_agent_config

            cfg = load_agent_config(name)
        return effective_internal_tools(cfg)
    except Exception:
        return []


def _collapse_subagent_refs(data: dict) -> dict:
    """Strip the display-only fields ``_resolve_subagents_for_display`` adds on
    GET before a config is validated/persisted.

    The resolved view lives in ``subagents_resolved`` and must never be written
    back to ``agent.json5``. As a defensive measure we also drop any legacy
    ``from_builtin`` mirror that an older dashboard build may have inlined into
    ``subagents`` (their real source is ``builtin_subagents``), and strip
    transient ``_``-prefixed keys (e.g. ``_running``).
    """
    if not isinstance(data, dict):
        return data
    data.pop("subagents_resolved", None)
    subs = data.get("subagents")
    if isinstance(subs, list):
        data["subagents"] = [s for s in subs if not (isinstance(s, dict) and s.get("from_builtin"))]
    # Strip transient display keys injected on GET (e.g. ``_running``).
    for key in [k for k in data if isinstance(k, str) and k.startswith("_")]:
        data.pop(key, None)
    return data


def create_api(runtime: Any) -> Any:
    """
    Create and return a FastAPI app bound to a RuntimeServer instance.

    Args:
        runtime: olano.runtime.server.RuntimeServer instance
    """
    if not _FASTAPI_AVAILABLE:
        raise ImportError("FastAPI not installed. Run: uv add fastapi uvicorn sse-starlette")

    global _ACTIVE_RUNTIME
    _ACTIVE_RUNTIME = runtime

    app = FastAPI(title="Olano Runtime API", version="1.0.0")

    # Wire the HITL approvals inbox to the dashboard: when an autonomous /
    # delegated turn defers a gated action (security/hitl.request_approval), this
    # broadcasts an event so the Approvals badge + toast update live. Resolved
    # via POST /api/hitl/resolve.
    from olano.security import hitl as _hitl_mod

    async def _hitl_delivery(req: Any) -> None:
        _push_event(
            "approvals_changed",
            {
                "id": req.id,
                "agent": req.agent_name,
                "action": req.action,
                "risk": req.risk,
                "description": req.description,
                "pending": len(_hitl_mod.list_pending()),
            },
        )
        # Nobody may be looking at the dashboard when a background turn pauses,
        # and the request times out on its own: tell the phones of everyone who
        # may resolve it. A no-op unless the server started the push dispatcher.
        try:
            from olano.push import events as _push_events

            _push_events.inbox_approval_needed(
                request_id=str(req.id),
                agent=str(req.agent_name or ""),
                action=str(req.action or ""),
                risk=str(req.risk or ""),
            )
        except Exception:
            logger.debug("inbox approval push failed", exc_info=True)

    _hitl_mod.set_delivery(_hitl_delivery)

    from olano.runtime import auth as auth_layer

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(dep_enforcement.DeploymentLimitExceeded)
    async def _deployment_limit_handler(
        request: Request, exc: dep_enforcement.DeploymentLimitExceeded
    ) -> Any:
        """A plan denial is an answer, not a fault.

        Endpoints that guard up front call ``dep_guard.reject``; the write
        guards deeper in the stack (``config.loader.save_agent_config``,
        ``archetypes.installer._guarded_agent_config_write``) raise instead, and
        without this handler those surfaced as HTTP 500 carrying only
        ``str(exc)`` — the code, the counts and the remediation all dropped, and
        a stack trace logged per click. Same shape as ``dep_guard.reject`` so
        the dashboard renders one message either way.
        """
        from starlette.responses import JSONResponse

        decision = exc.decision
        logger.warning(
            "deployment limit denied %s %s: %s",
            request.method,
            request.url.path,
            decision.code,
        )
        status = 503 if decision.code == "policy_unavailable" else 403
        return JSONResponse(status_code=status, content={"detail": decision.to_detail()})

    @app.middleware("http")
    async def security_middleware(request: Request, call_next: Any):
        token = _auth_token()
        session_id = request.headers.get("X-Olano-Session", "").strip()
        # Inbound-webhook ingress is called by external senders that have no
        # dashboard session or bearer token — it authenticates itself with the
        # unguessable token in its own path. Skip the session/bearer gates for
        # it, but keep rate limiting below (abuse protection).
        is_ingress = request.url.path.startswith("/api/webhooks/ingress/")
        # Payment-provider webhooks (Stripe) are called by the provider with no
        # dashboard session/bearer — they self-authenticate via the provider's
        # signed-payload HMAC inside the route handler. Skip the session/bearer
        # gates for them (rate limiting below still applies).
        is_payment_webhook = request.url.path.startswith("/api/monetization/stripe/")
        # OAuth provider redirects (/api/auth/<service>/callback) land straight
        # from the user's browser with no bearer token or dashboard session —
        # they self-authenticate via the unguessable ?state= of a pending flow.
        is_oauth_callback = (
            request.method == "GET"
            and request.url.path.startswith("/api/auth/")
            and request.url.path.endswith("/callback")
        )
        # Agent-published pages (/pages/<slug>/) are for external human viewers
        # who have no dashboard session or bearer token — each request
        # self-authenticates with HTTP Basic Auth against the owner-set
        # credential inside the route handler (runtime/pages_routes.py). Skip
        # the session/bearer gates for them; rate limiting below still applies.
        # Every method a page route accepts, not just GET/HEAD: a deployed
        # page's server takes form posts and API calls, and the route checks
        # the Basic credential (and refuses cross-site unsafe requests) for
        # each one before anything is forwarded.
        is_public_page = request.method in (
            "GET",
            "HEAD",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
            "OPTIONS",
        ) and request.url.path.startswith("/pages/")
        # Handshake links (/api/connect/<token>) are opened by a human on their
        # own phone, from a chat — no dashboard session, no bearer token, by
        # design. Each request self-authenticates on the unguessable token in
        # its path plus the PIN (olano/magic_links/routes.py). The trailing
        # slash matters: it must not swallow /api/connectors/… (the Weixin /
        # WhatsApp pairing streams), which are session-gated dashboard surfaces.
        is_handshake = request.url.path.startswith("/api/connect/")
        # The OpenAI-compatible agent API (/api/openai/<agent>/v1/…) is called
        # by OpenAI SDKs and chat frontends that have no dashboard session or
        # bearer token — by design, since it IS the programmatic surface. Each
        # request self-authenticates with the connector's own bearer key,
        # checked by the agent's adapter, and the proxy refuses to expose an
        # agent whose connector has no usable key at all (see
        # runtime/openai_proxy.py). Rate limiting below still applies.
        is_openai_api = request.url.path.startswith("/api/openai/")
        # The inbound MCP endpoint (/api/mcp, plus its RFC 9728 metadata) is
        # called by external MCP clients that hold an MCP bearer token, not the
        # dashboard's. Authentication is the mcp SDK's own bearer middleware in
        # olano/mcp_server (401 + WWW-Authenticate when missing). Only the exact
        # endpoint and the .well-known paths are exempt — /api/mcp/directory and
        # /api/mcp/oauth/* stay dashboard surfaces. Rate limiting still applies.
        is_mcp_server = auth_layer.is_external_access_path(request.url.path)
        # Twilio's inbound webhooks (/api/twilio/whatsapp/<agent>) come from
        # Twilio's servers with no dashboard session or bearer token. Each
        # request self-authenticates with X-Twilio-Signature, an HMAC keyed by
        # the connector's own auth token, checked by the agent's adapter before
        # anything runs (runtime/twilio_webhook_routes.py). POST only: nothing
        # else under the prefix is public. Rate limiting below still applies.
        is_twilio_webhook = request.method == "POST" and request.url.path.startswith("/api/twilio/")
        # Meta's WhatsApp Cloud API webhooks (/api/meta/whatsapp/<agent>) come
        # from Meta's servers the same way: a GET that checks the address with
        # the agent's verify token, and POSTs signed with X-Hub-Signature-256
        # (HMAC keyed by the app secret), checked by the agent's adapter before
        # anything is parsed (runtime/meta_webhook_routes.py). Rate limiting
        # below still applies.
        is_meta_webhook = request.method in ("GET", "POST") and request.url.path.startswith(
            "/api/meta/whatsapp/"
        )
        is_ingress = (
            is_ingress
            or is_payment_webhook
            or is_oauth_callback
            or is_public_page
            or is_handshake
            or is_openai_api
            or is_mcp_server
            or is_twilio_webhook
            or is_meta_webhook
        )
        if token and not is_ingress:
            auth_header = request.headers.get("Authorization", "")
            expected = f"Bearer {token}"
            # Constant-time: a plain ``!=`` stops at the first differing byte,
            # which lets a remote caller measure how much of the token matched.
            if not hmac.compare_digest(auth_header.encode("utf-8"), expected.encode("utf-8")):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid or missing auth token"},
                )
            if not session_id:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Missing X-Olano-Session header"},
                )

        client_host = request.client.host if request.client else "anon"
        # Localhost is always the dashboard proxy — exempt from rate limiting.
        is_localhost = client_host in ("127.0.0.1", "::1", "localhost")
        # The public readiness probe (/api/status) is polled every ~2.5s by every
        # dashboard tab, and a 429 on it is exactly what surfaces the
        # "Connection lost" overlay — so it must never be rate-limited.
        rate_exempt = request.url.path == "/api/status"
        if not is_localhost and not rate_exempt:
            # Behind a reverse proxy (Caddy/Vercel) the peer IP is the proxy and
            # is shared by every user, which would otherwise collapse all traffic
            # into one 60/min bucket and trip the limit for the whole dashboard.
            # Key per-user instead: session header → auth cookie → forwarded
            # client IP → peer IP. (The runtime is only reachable via the proxy in
            # deployed setups — :8765 isn't published — so X-Forwarded-For is
            # trustworthy here.)
            xff = request.headers.get("X-Forwarded-For", "")
            fwd_ip = xff.split(",")[0].strip() if xff else ""
            rl_key = (
                session_id
                or auth_layer.session_token_from_cookies(request.cookies)
                or fwd_ip
                or client_host
            )
            limit_decision = RUNTIME_RATE_LIMITER.check(rl_key, max_calls=240, window_seconds=60)
            if not limit_decision.allowed:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Runtime rate limit exceeded"},
                    headers={"Retry-After": str(limit_decision.retry_after)},
                )

        # Multi-user gate (no-op in single-user mode; ingress self-authenticates)
        # plus the active human, resolved together in a worker thread — both are
        # sqlite/config I/O and this runs on every request. The contextvar
        # binding stays here on the loop, where downstream handlers read it.
        gate, ctx_kwargs = await auth_layer.resolve_request_auth(
            request, session_id, gate=not is_ingress
        )
        if gate is not None:
            return gate
        with push_request_context(**ctx_kwargs):
            return await call_next(request)

    auth_layer.register_auth_routes(app, runtime)

    from olano.runtime.demo_access import register_demo_routes

    register_demo_routes(app)

    from olano.runtime.pages_routes import register_pages_routes

    register_pages_routes(app)

    # Public per-agent OpenAI-compatible API (proxied to each agent's own
    # api_server adapter). Registered unconditionally: an agent without the
    # connector answers 404 from the route itself.
    from olano.runtime.openai_proxy import register_openai_proxy_routes

    register_openai_proxy_routes(app)

    # Public inbound webhook for the WhatsApp over Twilio connector. Registered
    # unconditionally, like the proxy above: an agent without the connector
    # answers 404 from the route itself.
    from olano.runtime.twilio_webhook_routes import register_twilio_webhook_routes

    register_twilio_webhook_routes(app)

    # And for the WhatsApp (Meta Cloud API) connector: Meta's address check
    # (GET) and its deliveries (POST), per agent.
    from olano.runtime.meta_webhook_routes import register_meta_webhook_routes

    register_meta_webhook_routes(app)

    from olano.magic_links.routes import register_magic_link_routes

    register_magic_link_routes(app)

    from olano.runtime.issues_routes import register_issues_routes

    register_issues_routes(app)

    # Inbound MCP + A2A servers and Olano's own OAuth authorization server:
    # agents with `external_access.mcp` / `.a2a` become reachable from outside.
    # Registered unconditionally — every endpoint answers 404 while the
    # home-wide `external_access` switches are off — so the route table never
    # depends on config. The admin token/consent API rides alongside.
    try:
        from olano.a2a.server import OlanoA2AServer, register_a2a_server_routes
        from olano.mcp_server.routes import register_mcp_server_admin_routes
        from olano.mcp_server.server import OlanoMcpServer, register_mcp_server_routes

        # A2A first: the MCP server's authorization server grants access to
        # agents exposed over EITHER surface, so it asks runtime.a2a_server.
        _a2a_server = OlanoA2AServer(runtime)
        runtime.a2a_server = _a2a_server
        app.state.olano_a2a_server = _a2a_server
        _mcp_server = OlanoMcpServer(runtime)
        runtime.mcp_server = _mcp_server
        app.state.olano_mcp_server = _mcp_server
        register_mcp_server_routes(app, _mcp_server)
        register_a2a_server_routes(app, _a2a_server)
        register_mcp_server_admin_routes(app, _mcp_server, _a2a_server)
    except Exception as exc:  # pragma: no cover - mcp/a2a SDK missing/broken
        logger.warning("Inbound MCP/A2A servers unavailable: %s", exc)

    # Multi-bot group threads. Registered unconditionally so the app's route
    # table never depends on config; every one of these paths answers 404
    # while `groups.enabled` is off (the default).
    from olano.runtime.groups_routes import register_groups_routes

    register_groups_routes(app, runtime)

    # ── Interactive terminal (PTY ↔ WebSocket) ──────────────────────────────────
    #
    # Gives the dashboard's left-nav Terminal a shell: a real host shell on an
    # open/unmanaged install, or (managed posture) a shell INSIDE the agent's
    # isolated sandbox via `docker exec` — the runtime container's host shell is
    # never exposed there. The HTTP security middleware above does NOT run for
    # WebSocket scopes, so this handler re-implements every gate itself: opt-in
    # flag, bearer token (the prod proxy injects it on the upgrade), same-origin
    # check (CORS doesn't apply to WS — guards against cross-site WebSocket
    # hijacking), and an admin requirement in multi-user mode. cwd is the selected
    # agent's workspace. See olano/runtime/terminal.py for the PTY bridge.
    # Registered after security_middleware on purpose: Starlette runs the
    # later-added middleware outermost, so the headers also land on the 401
    # and 429 responses that middleware returns without calling call_next.
    @app.middleware("http")
    async def security_headers_middleware(request: Request, call_next: Any):
        response = await call_next(request)
        apply_security_headers(response.headers, request.url.path)
        return response

    # Every dashboard route lives in olano.runtime.routes (one module per topic);
    # the import is deferred so the route modules can import this module.
    from olano.runtime.routes import register_all_routes

    register_all_routes(app, runtime)

    # ── Creator monetization: Stripe webhook (POST /api/monetization/stripe/{agent})
    try:
        from olano.monetization.webhook_stripe import register_stripe_webhook

        register_stripe_webhook(app, runtime)
    except Exception as exc:  # pragma: no cover - defensive, never block API boot
        logger.warning("Could not register Stripe monetization webhook: %s", exc)

    return app


# ── Log capture ───────────────────────────────────────────────────────────────


class _ApiLogHandler(logging.Handler):
    """Capture log records into the SSE buffer."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            _append_log(line)
        except Exception:
            pass


def install_log_capture(level: int = logging.INFO) -> None:
    """Install a log handler that feeds records to the SSE event stream."""
    handler = _ApiLogHandler()
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
