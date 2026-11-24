"""talk_to_agent — peer-to-peer messaging between booted Olano agents.

This is the lightweight in-process equivalent of the optional ACP HTTP
bridge: instead of spinning up an HTTP server per agent, the tool reaches
into the live `RuntimeServer._runners` registry and invokes the target
agent's runner directly. The dashboard surfaces every call as an
`agent_message` SSE event so users can watch agents collaborate.

Usage from an agent:
    talk_to_agent(agent_name="emily-assistant", message="Can you draft the agenda?")

The peer's reply is returned as the tool's string result so the calling
agent can react to it in the same turn.

NOTE: this tool is for *peer* agents (other top-level agents managed by the
runtime). To delegate to your own subagents, use the built-in `task` tool.
The error path below explicitly distinguishes these two cases so the model
gets unambiguous guidance when it picks the wrong one.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, cast

from langchain_core.tools import tool

from olano.analytics.a2a_log import canonical_pair, norm_agent
from olano.security.request_context import (
    ORIGIN_KEY,
    get_request_context,
    propagated_origin,
    push_request_context,
)

logger = logging.getLogger(__name__)


# Bound to the live RuntimeServer at boot time so the tool can resolve agent
# names without import-cycling against runtime/server.py. Set by
# RuntimeServer.start() — None outside a running runtime.
_RUNTIME: Any = None
# Optional callback so the runtime can publish "agent_message" SSE events
# without this module having to import the API queue directly.
_PUBLISHER: Any = None

# Set by /api/chat/stream around an agent's turn so any tool the agent
# invokes (notably this one) can know who called it. ContextVars propagate
# across async boundaries and per-task, so concurrent chats stay isolated.
current_agent: contextvars.ContextVar[str] = contextvars.ContextVar("lc_current_agent", default="")

# ── Peer callback routing ───────────────────────────────────────────────────
#
# When agent A (running on user thread T) calls talk_to_agent(B), we want B's
# *eventual async callback* (B → A, fired later from a scheduled run or
# heartbeat) to land back on T so the user sees the full exchange. Storing
# `(A → B) ↦ T` here makes that possible: when B later opens
# talk_to_agent(A), we look up the reverse pair `(B → A)`... actually we
# store the pair indexed by *who will call next* (B), so the lookup is the
# trivial `(caller, target)` = `(B, A)` direction. See `_remember_pair` and
# `_recall_pair_thread`.
#
# Entries are TTL-bounded so we don't hold stale links forever. A single
# pair-map is keyed only on the agent pair; multi-tenant deployments where
# two different users are simultaneously talking to A about different things
# will see "last write wins" routing for B's callback. Acceptable for now;
# documented in TROUBLESHOOTING.md.
_PAIR_LINK_TTL_SECONDS = 24 * 60 * 60
_pair_links: dict[tuple[str, str], tuple[str, float]] = {}

# ── Idempotency window ──────────────────────────────────────────────────────
#
# Real-world transcripts have shown models occasionally re-issuing the same
# `talk_to_agent` call several times in one turn (often after hallucinating
# a "cancellation" from a perfectly successful prior call). At the RPC
# layer, two safeguards keep the platform deterministic:
#
#   - **In-flight dedup**: if an identical (caller, target, message) call
#     is already running, the duplicate awaits the same Future instead of
#     starting a second invocation. Concurrent duplicates collapse to one.
#   - **Cooldown cache**: after a call completes successfully, its reply
#     is cached for `_COOLDOWN_SECS`. A second identical call in that
#     window returns the cached reply wrapped with a brief note nudging
#     the model to use it instead of asking again.
#
# Errors are NOT cached — the model should be free to retry after a real
# failure. The key is the SHA-256 of the message text (truncated) so we
# don't keep raw message bytes resident in a long-lived map.
_COOLDOWN_SECS = 30.0
_inflight: dict[tuple[str, str, str], asyncio.Future] = {}
_completed: dict[tuple[str, str, str], tuple[str, float]] = {}


def _dedup_key(caller: str, target: str, message: str) -> tuple[str, str, str]:
    digest = hashlib.sha256((message or "").encode("utf-8")).hexdigest()[:16]
    return (caller, target, digest)


def _gc_completed_cache() -> None:
    if len(_completed) <= 200:
        return
    now = time.time()
    for key in [k for k, (_, exp) in _completed.items() if exp < now]:
        _completed.pop(key, None)


# The tool return value is intentionally JUST the peer's reply text — one
# source of truth, no wrapping. The "Sent / Replied" user-facing format
# lives in the prompt (see prompt_assembler._build_agent_roster_block) so
# the model gets exactly one consistent signal about how to present the
# exchange to the user. Wrapping inside the tool return causes small
# models to double-echo (once verbatim, once wrapped) — see commit log.


def _is_user_thread(thread_id: str | None) -> bool:
    """True if ``thread_id`` is a thread a user actively observes.

    Anything with an internal prefix (peer-scoped, delegated subagent,
    scheduled cron run, heartbeat) is a system thread — routing callbacks
    onto those would be invisible to the user. Everything else is treated
    as a user-facing thread (dashboard UUID, channel-derived `slack_*` /
    `wa_*` / `email:*` threads — all of which a user does watch).
    """
    if not thread_id:
        return False
    for prefix in ("acp::", "delegate::", "mcp::", "scheduled_", "heartbeat_", "agenttest::"):
        if thread_id.startswith(prefix):
            return False
    return True


def _norm_agent(name: str) -> str:
    """Case-stable normalization for agent names used in thread ids / digest keys."""
    return norm_agent(name)


def _pair_thread_id(caller: str, callee: str) -> str:
    """Isolated per-pair A2A thread id: ``acp::<caller>::<callee>``.

    Keying the callee's peer thread on BOTH parties (not just the callee) is
    what stops concurrent conversations from different callers collapsing onto
    one shared thread and contaminating each other. The persistent checkpointer
    then resumes this same thread every call, so each caller↔callee pair
    accumulates its own conversation history for free.

    The leading ``acp::`` prefix is preserved so every existing consumer
    (`_is_user_thread`, `scope_thread`, analytics prefix checks) keeps treating
    it as a system thread exactly as before. The caller is casefolded so
    ``Kevin`` and ``kevin`` never fork into two threads.
    """
    return f"acp::{_norm_agent(caller)}::{callee}"


# ── peer thread rotation ──────────────────────────────────────────────────────
#
# The per-pair thread above was resumed forever: every scheduled run of A that
# consulted B appended to the same conversation, and after a few months one
# managed box carried 20,078 checkpoints on `acp::aurelius::zoho-email`, each
# exchange replaying a history no reply needed. A peer thread now rolls over
# to a new generation (`acp::<caller>::<callee>::<stamp>`) once its transcript
# holds `peer_threads.max_turns` rows or is older than `peer_threads.max_age_days`
# (`config.yaml`, edited from Config → Agent-to-agent conversations, read at
# every call); `peer_threads.rotate: false` keeps the one thread forever. The
# retired generation keeps its transcript and is swept like any other thread.
# The `acp::` prefix and the pair in the id are unchanged, so every consumer
# that reads the shape (`_is_user_thread`, thread labels, the A2A view,
# analytics) keeps working.

#: ``path -> (size, mtime_ns, rows, first_ts)``: a transcript is re-read only
#: when it changed, so a call costs one stat; the bounds are applied at call
#: time, so a changed setting or a file that merely aged needs no re-read.
_PEER_THREAD_STATS: dict[str, tuple[int, int, int, float | None]] = {}


def _peer_thread_limits() -> tuple[int, float] | None:
    """``(max_rows, max_age_seconds)`` from ``config.yaml``, or ``None`` when
    rotation is switched off."""
    from olano.config.system_settings import load_peer_thread_settings

    settings = load_peer_thread_settings()
    if not settings.rotate:
        return None
    return settings.max_turns, float(settings.max_age_days) * 86400.0


def _latest_peer_generation(base: str, session_path: Any) -> str:
    """The newest generation of ``base`` with a transcript on disk, else ``base``."""
    directory = session_path(base).parent
    try:
        names: list[str] = [str(p.name) for p in directory.glob(f"{base}::*.jsonl")]
    except OSError:
        return base
    if not names:
        return base
    newest = sorted(names)[-1]
    return newest[: -len(".jsonl")]


def _peer_thread_exhausted(path: Any, now: float, limits: tuple[int, float]) -> bool:
    """True once the transcript at ``path`` is over the row or age bound."""
    try:
        st = path.stat()
    except OSError:
        return False
    key = str(path)
    hit = _PEER_THREAD_STATS.get(key)
    if hit is None or hit[0] != st.st_size or hit[1] != st.st_mtime_ns:
        rows = 0
        first_ts: float | None = None
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    rows += 1
                    if first_ts is None:
                        first_ts = _row_timestamp(line)
        except OSError:
            return False
        hit = (st.st_size, st.st_mtime_ns, rows, first_ts)
        _PEER_THREAD_STATS[key] = hit
    _size, _mtime, rows, first_ts = hit
    max_rows, max_age = limits
    if rows >= max_rows:
        return True
    return first_ts is not None and now - first_ts > max_age


def _row_timestamp(line: str) -> float | None:
    try:
        import json

        raw = json.loads(line).get("ts")
        if not raw:
            return None
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _peer_thread_id(caller: str, callee: str, runner: Any, *, now: float | None = None) -> str:
    """The pair thread to run ``callee`` on: the current generation, or a new one.

    ``runner`` is the callee's runner; its ``_session_path`` locates the
    transcripts the bounds are measured on. A runner without one (a test
    double) gets the base id, exactly as before rotation existed.
    """
    base = _pair_thread_id(caller, callee)
    session_path = getattr(runner, "_session_path", None)
    if not callable(session_path):
        return base
    try:
        limits = _peer_thread_limits()
        current = _latest_peer_generation(base, session_path)
        if limits is None:
            return current
        at = now if now is not None else time.time()
        if not _peer_thread_exhausted(session_path(current), at, limits):
            return current
        stamp = datetime.fromtimestamp(at, timezone.utc).strftime("%Y%m%dT%H%M%S")
        rotated = f"{base}::{stamp}"
        logger.info("talk_to_agent: peer thread %s rolled over to %s", current, rotated)
        return rotated
    except Exception:
        logger.debug("talk_to_agent: peer thread rotation skipped", exc_info=True)
        return base


def _remember_pair(caller: str, target: str, user_thread: str) -> None:
    """Record that ``caller`` reached ``target`` while running on ``user_thread``.

    Later, when ``target`` initiates a callback to ``caller``, we use this
    to route the callback onto ``user_thread`` (where the user is watching)
    instead of an isolated `acp::` thread.
    """
    if not (caller and target and user_thread and _is_user_thread(user_thread)):
        return
    _pair_links[(target, caller)] = (user_thread, time.time() + _PAIR_LINK_TTL_SECONDS)
    # Cheap lazy GC: only sweep once the table grows past a sensible cap.
    if len(_pair_links) > 200:
        now = time.time()
        for key in [k for k, (_, exp) in _pair_links.items() if exp < now]:
            _pair_links.pop(key, None)


def _recall_pair_thread(caller: str, target: str) -> str | None:
    """Return the remembered user thread for a ``caller → target`` callback, or None."""
    entry = _pair_links.get((caller, target))
    if not entry:
        return None
    thread, expires = entry
    if expires < time.time():
        _pair_links.pop((caller, target), None)
        return None
    return thread


# ── Per-pair conversation digest (opt-in long-term + short-term memory) ──────
#
# Layer 1 (per-pair threads) already gives the callee an accumulating,
# auto-summarized view of ITS OWN direction of the conversation (the caller→
# callee turns live on the `acp::<caller>::<callee>` checkpoint). This digest
# adds a *shared, bidirectional, cross-session* rollup keyed on the unordered
# agent pair, so either participant can see the whole relationship — including
# the reverse direction, which each agent's single acp thread never holds.
#
# It is OFF by default (`LIONCLAW_A2A_DIGEST=1` to enable) because it costs a
# store write per exchange plus extra input tokens on the injected preamble.
# When disabled, none of this code touches the store and `runner.ainvoke` sees
# no `a2a_preamble` metadata, so behaviour is exactly Layer-1-only.
_A2A_DIGEST_RECENT = 8  # short-term window: verbatim exchanges kept per pair
_A2A_DIGEST_SUMMARY_MAX = 2000  # long-term rolling summary hard cap (chars)
_A2A_PREAMBLE_MAX = 2000  # injected-preamble hard cap (chars)
_A2A_MSG_CLIP = 500  # per-message/-reply clip in the stored record


def _a2a_digest_enabled() -> bool:
    return os.environ.get("LIONCLAW_A2A_DIGEST", "").strip().lower() in {"1", "true", "yes", "on"}


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _clip_tail(text: str, limit: int) -> str:
    """Clip keeping the most-recent tail (used for the rolling summary blob)."""
    text = text or ""
    return text if len(text) <= limit else "…" + text[-(limit - 1) :]


def _canonical_pair(a: str, b: str) -> str:
    return canonical_pair(a, b)


def _digest_namespace(a: str, b: str) -> tuple[str, str]:
    """Store namespace for a pair — canonical (unordered) so both directions
    share one record."""
    return ("a2a_digest", _canonical_pair(a, b))


def _fold_summary(existing: str, overflow: list[dict[str, Any]]) -> str:
    """Fold overflow exchanges into the rolling long-term summary."""
    from olano.orchestration.context_summary import summarize_context

    msgs: list[dict[str, Any]] = []
    for ex in overflow:
        msgs.append({"role": "user", "content": f"{ex.get('from', '?')}: {ex.get('message', '')}"})
        msgs.append({"role": "assistant", "content": f"{ex.get('to', '?')}: {ex.get('reply', '')}"})
    folded = summarize_context(msgs, max_chars=_A2A_DIGEST_SUMMARY_MAX)
    existing = (existing or "").strip()
    combined = f"{existing}\n{folded}".strip() if existing else folded
    return _clip_tail(combined, _A2A_DIGEST_SUMMARY_MAX)


async def _load_pair_digest(a: str, b: str) -> dict[str, Any]:
    """Load the shared per-pair digest record (best-effort)."""
    try:
        from olano.core.stores import get_async_store

        store = await get_async_store()
        item = await store.aget(_digest_namespace(a, b), "conversation")
        if item is not None and isinstance(item.value, dict):
            return dict(item.value)
    except Exception as exc:
        logger.debug("talk_to_agent: pair digest load failed: %s", exc)
    return {"summary": "", "recent": []}


async def _update_pair_digest(caller: str, callee: str, message: str, reply: str, ts: str) -> None:
    """Append the just-completed exchange to the shared per-pair digest."""
    try:
        from olano.core.stores import get_async_store

        store = await get_async_store()
        ns = _digest_namespace(caller, callee)
        item = await store.aget(ns, "conversation")
        record: dict[str, Any] = (
            dict(item.value) if (item is not None and isinstance(item.value, dict)) else {}
        )
        recent = list(record.get("recent") or [])
        recent.append(
            {
                "from": _norm_agent(caller),
                "to": _norm_agent(callee),
                "message": _clip(message, _A2A_MSG_CLIP),
                "reply": _clip(reply, _A2A_MSG_CLIP),
                "ts": ts,
            }
        )
        if len(recent) > _A2A_DIGEST_RECENT:
            overflow, recent = recent[:-_A2A_DIGEST_RECENT], recent[-_A2A_DIGEST_RECENT:]
            record["summary"] = _fold_summary(record.get("summary", ""), overflow)
        record["recent"] = recent
        record["updated"] = ts
        await store.aput(ns, "conversation", record)
    except Exception as exc:
        logger.debug("talk_to_agent: pair digest update failed: %s", exc)


def _render_pair_preamble(record: dict[str, Any], caller: str, callee: str) -> str:
    """Render the digest as a system preamble injected into the callee so it
    can see the full (bidirectional) relationship with the caller."""
    summary = (record.get("summary") or "").strip()
    recent = record.get("recent") or []
    if not summary and not recent:
        return ""
    lines = [
        f"## Prior conversation context with peer agent '{caller}'",
        "(Shared history of your ongoing collaboration — use it for continuity; "
        "do not repeat it back verbatim.)",
    ]
    if summary:
        lines += ["", "Earlier (summarized):", summary]
    if recent:
        lines += ["", "Recent exchanges:"]
        for ex in recent:
            lines.append(f"- {ex.get('from', '?')} → {ex.get('to', '?')}: {ex.get('message', '')}")
            lines.append(f"  ↳ {ex.get('to', '?')}: {ex.get('reply', '')}")
    return _clip_tail("\n".join(lines), _A2A_PREAMBLE_MAX)


def _render_recall_text(record: dict[str, Any], peer: str) -> str:
    """Render the digest for the `recall_conversation` pull tool."""
    summary = (record.get("summary") or "").strip()
    recent = record.get("recent") or []
    if not summary and not recent:
        return f"[recall_conversation] No recorded conversation history with '{peer}' yet."
    lines = [f"Conversation history with peer agent '{peer}':"]
    if summary:
        lines += ["", "Summary of earlier exchanges:", summary]
    if recent:
        lines += ["", "Recent exchanges (oldest first):"]
        for ex in recent:
            lines.append(f"- {ex.get('from', '?')} → {ex.get('to', '?')}: {ex.get('message', '')}")
            lines.append(f"  ↳ reply: {ex.get('reply', '')}")
    return "\n".join(lines)


def bind_runtime(runtime: Any, publisher: Any | None = None) -> None:
    """Wire this tool to a live RuntimeServer + optional event publisher."""
    global _RUNTIME, _PUBLISHER
    _RUNTIME = runtime
    _PUBLISHER = publisher


def publish_activity_event(event_type: str, payload: dict) -> None:
    """Publish an SSE/activity frame through the runtime's bound publisher.

    The same chokepoint ``talk_to_agent`` uses for its ``agent_message`` frames
    (which ``runtime/api._push_event`` also appends to ``a2a_activity.jsonl``),
    exposed so the outbound A2A client and the inbound MCP server land in the
    same Agent to Agent history without re-implementing the wiring. Best
    effort: a publisher failure never reaches the caller.
    """
    if not _PUBLISHER:
        return
    try:
        _PUBLISHER(event_type, payload)
    except Exception:
        pass


def get_bound_runtime() -> Any:
    """The live ``RuntimeServer`` bound via :func:`bind_runtime`, or ``None``.

    Lets runtime-side helpers outside this module (e.g. the MCP OAuth completion
    watcher) reach the runtime — for hot-reloads and the like — without importing
    the runtime package at module load time (which would invert the dependency
    and risk an import cycle). ``None`` in a CLI one-shot or tests where no
    runtime was booted.
    """
    return _RUNTIME


def list_peers() -> list[str]:
    """Names of all booted agents the current runner can talk to."""
    if _RUNTIME is None:
        return []
    return list(getattr(_RUNTIME, "_runners", {}).keys())


def _resolve_peer_name(target: str) -> tuple[str | None, str | None]:
    """Resolve ``target`` to a canonical peer name.

    Returns ``(canonical_name, runner_key)`` where both are the same string —
    the second value is also the key into ``_RUNTIME._runners``. If no booted
    peer matches the name (even case-insensitively), returns ``(None, None)``.

    Lookup is three-pass:
      1. Exact match (preserves the original behaviour for clean callers).
      2. Case-insensitive fallback so the model can write "Kevin" or "KEVIN"
         and still reach `kevin`. This is the most common LLM mistake — they
         capitalize names in prose and then carry that into the tool call.
      3. Case-insensitive match on a peer's `display_name`, because the roster
         shows it and a model that reads "Merli the Analyst" will sometimes
         address that. Deliberately **last**: a handle must always win, or one
         agent's display name could shadow another agent's actual name and
         silently reroute the message.
    """
    if _RUNTIME is None or not target:
        return None, None
    runners = getattr(_RUNTIME, "_runners", {}) or {}
    if target in runners:
        return target, target
    folded = target.casefold()
    for key in runners:
        if key.casefold() == folded:
            return key, key
    for key, runner in runners.items():
        cfg = getattr(runner, "config", None)
        display = (getattr(cfg, "display_name", "") or "").strip()
        if display and display.casefold() == folded:
            return key, key
    return None, None


_SHELL_TOOL_NAMES = frozenset({"safe_shell", "safe_shell_tool", "execute", "node_run"})
_SHELL_BACKEND_TYPES = frozenset({"local_shell", "filesystem+shell", "lionclaw_docker"})


def _config_has_shell(cfg: object) -> bool:
    """True when an agent config can run shell commands (tool or backend)."""
    tools = {t for t in (getattr(cfg, "tools", []) or []) if t}
    if tools & _SHELL_TOOL_NAMES:
        return True
    backend = getattr(cfg, "backend", None)
    backend_type = getattr(backend, "type", None) or (backend if isinstance(backend, str) else "")
    return backend_type in _SHELL_BACKEND_TYPES


def _peer_title(name: str, cfg: object) -> str:
    try:
        from olano.config.org_store import resolve_title

        return resolve_title(name, getattr(cfg, "org", None))
    except Exception:
        return ""


def list_peers_info() -> list[dict]:
    """Same as list_peers() but returns a richer record per peer so the
    prompt assembler can show descriptions / tools / role hints — without
    that, agents have no way to choose the right peer for a given task."""
    if _RUNTIME is None:
        return []
    out: list[dict] = []
    for name, runner in getattr(_RUNTIME, "_runners", {}).items():
        cfg = getattr(runner, "config", None)
        if cfg is None:
            continue
        tools = list(getattr(cfg, "tools", []) or [])
        out.append(
            {
                "name": name,
                # The peer's display name when it has one, else its handle.
                # The roster shows both: `name` is what `talk_to_agent` takes.
                "label": (getattr(cfg, "label", "") or name),
                # The peer's position (`org.title`, central store first) so
                # the roster can say what each peer *is*, not only what it
                # is called.
                "title": _peer_title(name, cfg),
                "description": (getattr(cfg, "description", "") or "").strip(),
                "emoji": getattr(cfg, "emoji", "🦁"),
                "tools": tools,
                # Internal/test agents (hidden: true) are booted but must never
                # be advertised in a prompt roster or a customer-facing listing.
                # Callers filter on this so test agents stop leaking into the
                # "which agents exist?" answer.
                "hidden": getattr(cfg, "hidden", False) is True,
                # Shell can come from an explicit tool OR the backend (the default
                # `local_shell` backend exposes deepagents' `execute` tool), so
                # detect both — matching the prompt assembler and security layer.
                "has_shell": _config_has_shell(cfg),
                # Heuristic role hint helps the model pick the right peer
                # for canonical tasks. We deliberately keep this short.
                "is_admin": bool(
                    "admin" in name.lower()
                    or ("safe_shell" in tools and "admin" in (cfg.description or "").lower())
                ),
                # What a teammate could ask this peer FOR: its service toolkits
                # and connected MCP servers (cognition/fleet.capability_summary
                # renders them; the chat roster and the background team digest
                # both read these so they describe a peer in the same words).
                "toolkits": peer_toolkit_labels(tools),
                "mcp": peer_mcp_servers(name, cfg),
            }
        )
    return out


#: Toolkits every agent has, or plumbing an agent uses for itself: not a
#: capability a teammate would ask for, so the roster leaves them out.
_INFRA_TOOLKITS: frozenset[str] = frozenset(
    {
        "talk_to",
        "a2a",
        "time",
        "vision",
        "web_search",
        "web_fetch",
        "http",
        "firecrawl",
        "ddg",
        "arxiv",
        "wikipedia",
        "scheduler",
        "heartbeat",
        "pulse",
        "monitor",
        "issues",
        "groups",
        "pages",
        "ideas",
        "cortex",
        "knowledge",
        "graph",
        "notes",
        "secrets",
        "magic_link",
        "notification",
        "chat_directory",
        "image",
        "render",
        "media",
        "fileops",
        "workspace_files",
        "self_config",
        "olano_admin",
    }
)

#: Toolkits that bundle several services; label them by the tool's own prefix
#: (``gmail_send`` → gmail, ``calendar_create_event`` → calendar) so "google"
#: does not hide that this peer can send mail.
_PREFIX_LABELLED_TOOLKITS: frozenset[str] = frozenset({"google", "microsoft365"})

_MODULE_TO_TOOLKIT: dict[str, str] | None = None


def _module_to_toolkit() -> dict[str, str]:
    global _MODULE_TO_TOOLKIT
    if _MODULE_TO_TOOLKIT is None:
        from olano.tools.registry import _TOOLKITS

        _MODULE_TO_TOOLKIT = {module: key for key, module in _TOOLKITS.items()}
    return _MODULE_TO_TOOLKIT


def peer_toolkit_labels(tools: list[str]) -> list[str]:
    """Capability labels for a tool list: service toolkits by name, the Google
    and Microsoft bundles by tool prefix, infrastructure left out. Stable order
    (first occurrence) so a roster does not reshuffle between prompts."""
    try:
        from olano.tools._name_index import NAME_INDEX
    except Exception:  # pragma: no cover - generated module always ships
        return []
    rev = _module_to_toolkit()
    labels: list[str] = []
    for tool_name in tools:
        module = NAME_INDEX.get(tool_name)
        toolkit = rev.get(module or "")
        if not toolkit or toolkit in _INFRA_TOOLKITS:
            continue
        label = tool_name.split("_", 1)[0] if toolkit in _PREFIX_LABELLED_TOOLKITS else toolkit
        if label and label not in labels:
            labels.append(label)
    return labels


def peer_mcp_servers(name: str, cfg: object) -> list[dict[str, Any]]:
    """The peer's MCP servers as ``{name, status, ok, tools}`` rows, from the
    builder's last connect summary (so a server that is configured but down
    reads as down, not as a capability)."""
    try:
        from olano.core.builder import get_mcp_summary

        rows = get_mcp_summary(name)
    except Exception:
        return []
    configured = {}
    for srv in getattr(cfg, "mcp_servers", None) or []:
        url = str(getattr(srv, "url", "") or "")
        if url:
            configured[url] = str(getattr(srv, "name", "") or "")
    out: list[dict[str, Any]] = []
    for row in rows or []:
        url = str(row.get("url") or "")
        label = configured.get(url) or _mcp_label(url)
        if not label:
            continue
        tools = row.get("tools")
        out.append(
            {
                "name": label,
                "status": str(row.get("status") or ""),
                "ok": bool(row.get("ok")),
                "tools": len(tools) if isinstance(tools, list) else int(tools or 0),
            }
        )
    return out


def _mcp_label(url: str) -> str:
    """A short name for a server with none configured: its host, or the
    command for a stdio server."""
    from urllib.parse import urlsplit

    if not url:
        return ""
    if "://" in url:
        host = urlsplit(url).netloc
        return host.split("@")[-1].split(":")[0] if host else url[:40]
    return url.split()[0].rsplit("/", 1)[-1][:40]


def peer_roster(config: object) -> dict[str, Any]:
    """The peers ``config`` may know about, split by whether it may message them.

    One definition for the chat prompt's roster and the background team
    digest. ``talk``: visible AND messageable (the agent holds ``talk_to_agent``
    and the org chart permits it); ``aware``: visible only. ``org`` is the
    governing org block (central organization.yaml first), for callers that
    tag manager / reports. Hidden peers and the agent itself never appear.
    """
    own_name = str(getattr(config, "name", "") or "")
    all_peers = [
        p for p in (list_peers_info() or []) if p.get("name") != own_name and not p.get("hidden")
    ]
    has_talk_tool = "talk_to_agent" in set(getattr(config, "tools", []) or [])

    org: Any = None
    try:
        from olano.config.org_store import get_org_if_present

        org = get_org_if_present(own_name)
    except Exception:
        org = None
    if org is None:
        org = getattr(config, "org", None)
    try:
        reachable = org.reachable_peer_names() if org is not None else set()
    except Exception:
        reachable = set()

    vis = getattr(config, "agent_visibility", None)

    def _is_visible(peer_name: str) -> bool:
        if vis is not None and hasattr(vis, "is_visible"):
            return bool(vis.is_visible(peer_name, reachable))
        return peer_name.strip().casefold() in reachable

    talk: list[dict[str, Any]] = []
    aware: list[dict[str, Any]] = []
    for p in all_peers:
        name = str(p.get("name", ""))
        if not _is_visible(name):
            continue
        if has_talk_tool and can_message(own_name, name):
            talk.append(p)
        else:
            aware.append(p)
    return {"talk": talk, "aware": aware, "org": org, "has_talk_tool": has_talk_tool}


def list_configured_agents() -> list[str]:
    """All agents on disk (booted or not) — discovered via the loader."""
    try:
        from olano.config.loader import list_agents

        return list_agents()
    except Exception:
        return []


# ── Org-chart peer-messaging policy ──────────────────────────────────────────
#
# Each agent's `org` config block (input_agents / output_agents / manager /
# reports) governs who may talk_to_agent whom. The *rule* lives on
# `OrgConfig.allows_output` / `allows_input` so the dashboard org-chart endpoint
# and this RPC layer agree on a single source of truth. A call is allowed iff
# BOTH ends agree: the caller's output policy permits the target AND the
# target's input policy permits the caller. Empty allow-lists mean "no
# restriction" (backward compatible), and hierarchy neighbours (manager /
# reports) always pass.


def _runner_org(name: str) -> Any | None:
    """Return the governing OrgConfig for ``name``, or None if unknown/legacy.

    The central ``organization.yaml`` store is authoritative when it has an
    entry (so live org edits take effect without a restart). Otherwise fall back
    to the booted runner's loaded ``config.org`` (legacy / not-yet-migrated).
    """
    try:
        from olano.config.org_store import get_org_if_present

        org = get_org_if_present(name)
        if org is not None:
            return org
    except Exception:
        pass
    if _RUNTIME is None:
        return None
    runners = getattr(_RUNTIME, "_runners", {}) or {}
    cfg = getattr(runners.get(name), "config", None)
    return getattr(cfg, "org", None)


def _target_is_human_only(target: str) -> bool:
    """Whether ``target`` is configured ``human_only`` (no agent peers allowed)."""
    if _RUNTIME is None:
        return False
    runners = getattr(_RUNTIME, "_runners", {}) or {}
    cfg = getattr(runners.get(target), "config", None)
    return bool(getattr(cfg, "human_only", False))


def _peer_policy_error(sender: str, target: str) -> str | None:
    """Return a structured error if org policy forbids ``sender`` → ``target``.

    Returns None when the exchange is permitted (or when policy can't be
    evaluated — e.g. no runtime, or legacy configs without an ``org`` block —
    so the historical "anyone may talk to anyone" default is preserved).
    """
    # Human-only agents (e.g. AgentFather) are never reachable by peers,
    # regardless of org-chart allow-lists. This is a hard gate.
    if _target_is_human_only(target):
        return (
            f"[talk_to_agent error] '{target}' is a human-only agent and does not accept "
            "messages from other agents. A human operator must interact with it directly "
            "(CLI, dashboard, or a locked-down channel). This is a deliberate restriction — "
            "do NOT retry."
        )

    sender_org = _runner_org(sender)
    target_org = _runner_org(target)

    # Sender-side (output) gate: am I allowed to message the target?
    if sender_org is not None and not sender_org.allows_output(target):
        return _format_policy_error(sender, target, side="output", org=sender_org)
    # Receiver-side (input) gate: does the target accept messages from me?
    if target_org is not None and not target_org.allows_input(sender):
        return _format_policy_error(sender, target, side="input", org=target_org)
    return None


def _format_policy_error(sender: str, target: str, *, side: str, org: Any) -> str:
    """Render an actionable error when org policy blocks a peer message."""
    lines = [f"[talk_to_agent error] Org policy does not permit messaging '{target}'."]
    if side == "output":
        lines.append(
            f"Your agent ('{sender}') has an `output_agents` allow-list that does not "
            f"include '{target}'."
        )
        allowed = sorted(
            {
                *(org.output_agents or []),
                *([org.manager] if org.manager else []),
                *(org.reports or []),
            }
        )
        if allowed:
            lines.append(
                "Peers you ARE allowed to message: " + ", ".join(f"`{a}`" for a in allowed) + "."
            )
    else:
        lines.append(
            f"The target agent ('{target}') only accepts messages from a fixed "
            f"`input_agents` allow-list, and '{sender}' is not on it."
        )
        if org.manager:
            lines.append(
                f"That agent reports to `{org.manager}` — route the request up the chain instead."
            )
    lines.append(
        "This is an org-chart restriction, not a transient failure — do NOT retry the same "
        "call. If the user explicitly asked for this, tell them the org chart doesn't permit "
        "it and suggest who can relay the message."
    )
    return "\n".join(lines)


def can_message(caller: str, target: str) -> bool:
    """Whether org policy permits ``caller`` to ``talk_to_agent`` ``target``.

    Public helper used by the prompt assembler to avoid advertising peers the
    agent would only be blocked from reaching, and usable by any caller that
    wants to pre-flight a peer message.
    """
    return _peer_policy_error(caller, target) is None


def _suggest_peer(target: str, candidates: list[str]) -> str | None:
    """Best-guess closest peer name for ``target``.

    Uses difflib so the model gets a concrete retry instead of having to
    guess. Returns None when nothing is even loosely similar — better to say
    nothing than to misdirect.
    """
    if not target or not candidates:
        return None
    import difflib

    matches = difflib.get_close_matches(
        target.casefold(), [c.casefold() for c in candidates], n=1, cutoff=0.6
    )
    if not matches:
        return None
    folded = matches[0]
    for c in candidates:
        if c.casefold() == folded:
            return c
    return None


def _format_unknown_peer_error(target: str) -> str:
    """Render a helpful error string when ``target`` isn't a booted peer.

    The model sees a structured response covering the three possible
    intents — talk_to_agent for a peer, task for a subagent, or asking
    the user to start a stopped agent — so it can recover instead of
    repeating the same mistake.
    """
    # Exclude hidden/internal test agents from the recovery listing — they are
    # booted but must never be advertised as reachable peers.
    hidden = {p["name"] for p in list_peers_info() if p.get("hidden")}
    running = sorted(n for n in list_peers() if n not in hidden)
    on_disk = sorted(set(list_configured_agents()) - set(running) - hidden)

    lines: list[str] = [f"[talk_to_agent error] No booted Olano agent named '{target}'."]

    # Surface the closest match prominently so the model retries with it
    # instead of giving up. Case is already handled by the resolver, so a
    # near-miss usually means hyphen/underscore or word-order differences.
    suggestion = _suggest_peer(target, running) or _suggest_peer(target, on_disk)
    if suggestion:
        verb = "Retry with" if suggestion in running else "Did you mean"
        lines.append("")
        lines.append(f"{verb} `{suggestion}`.")

    if running:
        lines.append("")
        lines.append("Running agents you CAN talk_to_agent right now:")
        for name in running:
            lines.append(f"  • {name}")
    else:
        lines.append("No other agents are running right now.")

    if on_disk:
        lines.append("")
        lines.append("Configured but NOT running (ask the user to start them first):")
        for name in on_disk:
            lines.append(f"  • {name}")

    lines.append("")
    lines.append(
        f"If '{target}' is one of YOUR OWN subagents (declared in your agent.json5), "
        "DO NOT use talk_to_agent for it — use the built-in `task` tool instead "
        '(`task(subagent_type="...", description="...")`). The `task` tool '
        "lists your available subagents in its description."
    )
    return "\n".join(lines)


def _sender_name() -> str:
    """The agent a peer message is sent AS: the credential scope (a worker's
    parent) when one is bound, else the bound agent, else ``"agent"``."""
    try:
        from olano.tools._credentials import credential_agent_name

        scope = credential_agent_name()
    except Exception:
        scope = None
    return scope or current_agent.get() or "agent"


@tool
async def talk_to_agent(agent_name: str, message: str) -> str:
    """Send a message to another booted Olano PEER agent and return their reply.

    Use this to collaborate with a separately-booted Olano agent — for
    example asking emily-assistant to draft a calendar invite while you
    keep working on something else.

    IMPORTANT: This is for *peer* agents only. To delegate to your own
    subagents (the ones declared in your agent.json5), use the built-in
    `task` tool instead. If you call this with a subagent name, the tool
    will tell you exactly that and list your peer options.

    ── Conversation memory ─────────────────────────────────────────────
    Peers remember you. Each caller↔peer pair has its own private,
    persistent conversation thread, so the peer resumes your prior
    exchanges every time you call it — you can reference earlier messages
    ("as I mentioned", "the file you sent") without repeating them.
    Different callers talking to the same peer never see each other's
    conversations. You still need to give enough context for the *current*
    request to stand on its own, but you do NOT need to re-paste the whole
    history. (If you're unsure what was already said, `recall_conversation`
    returns a digest of your shared history with a peer.)

    ── How to present the result to the user ───────────────────────────
    The dashboard renders this tool call as a native two-bubble exchange
    (your message on top, the peer's reply below) — the user will see
    BOTH the message you sent and the peer's full reply rendered as a
    structured conversation card, automatically. You do NOT need to
    repeat any of it in your text response.

    Your text response after this call should be brief and additive:
      • a one-line acknowledgement ("Done — kevin agreed to handle it."),
      • OR the next concrete step you're taking based on the reply,
      • OR a clarifying question for the user if the reply changes things.

    Strict rules:
      • Do NOT paste the peer's reply text in your response. The user
        already sees it in the exchange card above your text.
      • Do NOT write "Sent to <agent>: ..." or "<agent> replied: ..." —
        the card already shows that.
      • Do NOT lead your response with the peer's reply verbatim.
      • If the reply is `[talk_to_agent error]` or `[talk_to_agent note]`,
        explain what happened and propose a recovery — don't re-paste
        the bracketed message.

    Args:
        agent_name: Name of the booted peer agent. Matching is
            case-insensitive ("Kevin", "kevin", and "KEVIN" all reach the
            agent registered as `kevin`), but prefer the exact name from
            the roster. The tool lists running peers if you provide an
            unknown name.
        message: The natural-language request to send to the peer. The peer
            retains your prior conversation, so you may refer back to earlier
            turns; still include enough new context that this specific request
            is actionable on its own.

    Returns:
        The peer's reply as plain text. If the peer isn't booted or the
        call fails, a structured error string starting with
        "[talk_to_agent error]" that explains exactly how to recover.
    """
    if _RUNTIME is None:
        return (
            "[talk_to_agent error] No runtime registered. This tool only works "
            "inside a booted Olano runtime."
        )
    canonical, _ = _resolve_peer_name(agent_name)
    if canonical is None:
        return _format_unknown_peer_error(agent_name)
    # Use the canonical (case-correct) name from here on so the SSE feed,
    # thread id, and any downstream lookups all agree.
    agent_name = canonical
    runner = _RUNTIME._runners[agent_name]

    # Who is asking: the credential-scope owner, so a Cortex worker or a
    # delegated sub-agent (`<parent>__cog__…`, `<parent>__sub__…`) speaks as
    # its parent. The org chart, the pair thread and the Agent to Agent view
    # all key on that name; the throwaway child name would pass no allow-list
    # and would let a worker "message" its own parent.
    sender = _sender_name()
    if sender.casefold() == agent_name.casefold():
        return (
            f"[talk_to_agent error] '{agent_name}' is you. Do the work yourself, or ask a "
            "different teammate; the roster in your prompt lists who can do what."
        )

    # ── Org-chart policy gate ───────────────────────────────────────────
    # Enforce the agent.json5 `org` allow-lists before doing any work. A
    # blocked call returns immediately with a structured, non-retryable error
    # (no SSE activity is emitted for a message that never leaves the gate).
    policy_err = _peer_policy_error(sender, agent_name)
    if policy_err is not None:
        logger.info(
            "talk_to_agent: org policy blocked %s→%s",
            sender,
            agent_name,
        )
        return policy_err

    # ── Idempotency: dedup in-flight + cooldown ─────────────────────────
    sender_for_dedup = sender
    dedup_key = _dedup_key(sender_for_dedup, agent_name, message)

    inflight_fut = _inflight.get(dedup_key)
    if inflight_fut is not None and not inflight_fut.done():
        logger.info(
            "talk_to_agent: deduping in-flight %s→%s call (awaiting shared result)",
            sender_for_dedup,
            agent_name,
        )
        try:
            return cast(str, await inflight_fut)
        except Exception as exc:
            return f"[talk_to_agent error] Concurrent call to '{agent_name}' failed: {exc}"

    cached = _completed.get(dedup_key)
    if cached:
        cached_raw_reply, expires = cached
        if expires > time.time():
            logger.info(
                "talk_to_agent: returning cached %s→%s reply (asked %.1fs ago)",
                sender_for_dedup,
                agent_name,
                _COOLDOWN_SECS - max(0.0, expires - time.time()),
            )
            return (
                f"[talk_to_agent note] You asked '{agent_name}' the exact same "
                f"thing within the last {int(_COOLDOWN_SECS)}s. Their previous "
                f"reply is reproduced below — use it directly and DO NOT call "
                f"this tool again with the same message.\n\n" + cached_raw_reply
            )
        _completed.pop(dedup_key, None)

    started = datetime.now(timezone.utc).isoformat()
    sender = sender

    def _publish(event_type: str, payload: dict) -> None:
        """Best-effort publish; swallowing errors so SSE problems never break
        the agent's actual conversation."""
        if not _PUBLISHER:
            return
        try:
            _PUBLISHER(event_type, payload)
        except Exception:
            pass

    _publish(
        "agent_message",
        {
            "kind": "peer",
            "phase": "send",
            "from": sender,
            "to": agent_name,
            "message": message,
            "ts": started,
        },
    )
    # Mark the receiver as "talking" and tag the caller as `peer` so the
    # playground can walk the receiver over to stand beside the caller (rather
    # than off to its own desk) and work side-by-side while it replies.
    _publish("agent_activity", {"agent": agent_name, "status": "talking", "peer": sender})

    # ── Resolve the routing thread ───────────────────────────────────────
    # Two distinct modes, each with a clear contract:
    #
    #   SYNC: caller is mid-turn on a user-facing thread (or already inside
    #     an a2a chain rooted on one). The user is actively streaming the
    #     caller's response; they will see the tool_call + tool_result
    #     frames as part of that stream. Run the callee on an isolated
    #     `acp::<callee>` thread so the callee's own checkpointer doesn't
    #     get polluted with peer messages from whichever user thread
    #     happened to be active. Remember the pair so a future async
    #     callback in the reverse direction can find its way home.
    #
    #   ASYNC CALLBACK: caller is running from a system thread (scheduler,
    #     heartbeat, channel cron, etc.) and the pair-map has a remembered
    #     route to a user thread. Route onto that user thread so the
    #     human sees the follow-up land in their chat. Continue
    #     propagating the user thread via `originator_user_thread` so any
    #     nested calls the callee makes during this turn also surface.
    #
    #   COLD: no user-thread context anywhere. Use `acp::` — these
    #     exchanges only show up in the A2A Activity feed.
    ctx = get_request_context()
    inherited_thread = (ctx.metadata or {}).get("originator_user_thread")
    caller_thread = ctx.session_id or ""

    routing_mode: str  # "sync" | "async_callback" | "cold"
    user_thread: str | None = None  # set only in async_callback mode

    if inherited_thread and _is_user_thread(inherited_thread):
        # We're a nested hop inside an in-flight async chain. Stay on the
        # user thread the chain is rooted on.
        routing_mode = "async_callback"
        user_thread = inherited_thread
    elif _is_user_thread(caller_thread):
        # Caller is mid-turn on a user thread. Sync mode: isolate the
        # callee's checkpoint state, but remember the pair so a later
        # callback from `agent_name` back to `sender` lands on the
        # caller's thread.
        routing_mode = "sync"
        _remember_pair(sender, agent_name, caller_thread)
    else:
        recalled = _recall_pair_thread(sender, agent_name)
        if recalled and _is_user_thread(recalled):
            routing_mode = "async_callback"
            user_thread = recalled
            logger.info(
                "talk_to_agent: routing %s→%s callback onto user thread %s "
                "(recalled from pair-map)",
                sender,
                agent_name,
                recalled,
            )
        else:
            routing_mode = "cold"

    thread_id = (
        user_thread
        if routing_mode == "async_callback"
        else _peer_thread_id(sender, agent_name, runner)
    )

    # Sync and async-callback calls get a `thread_message` mirror so the
    # target agent's chat thread shows the peer-inbound turn live. Cold calls
    # have no user-facing thread to surface to.
    if routing_mode in {"sync", "async_callback"}:
        _publish(
            "thread_message",
            {
                "agent": agent_name,
                "thread_id": thread_id,
                "role": "peer_inbound",
                "from": sender,
                "content": message,
                "ts": started,
            },
        )

    # Register the in-flight Future BEFORE awaiting the peer so any
    # concurrent duplicate that arrives during the call collapses onto
    # the same result instead of starting a second invocation.
    inflight_fut = asyncio.get_running_loop().create_future()
    _inflight[dedup_key] = inflight_fut

    # Opt-in: load the shared per-pair digest and render it as a preamble the
    # callee sees for this turn (bidirectional continuity). Never persisted to
    # the callee's JSONL — see runner.ainvoke. Off by default (no store touch).
    a2a_preamble = ""
    if _a2a_digest_enabled():
        a2a_preamble = _render_pair_preamble(
            await _load_pair_digest(sender, agent_name), sender, agent_name
        )

    try:
        try:
            callee_token = current_agent.set(agent_name)
            try:
                with push_request_context(
                    source="peer",
                    channel="talk_to_agent",
                    user_id=sender,
                    session_id=f"{sender}->{thread_id}",
                    authenticated=True,
                    is_owner=False,
                    is_group=False,
                    # Only async callbacks propagate the user-thread anchor.
                    # Sync calls intentionally drop it so a nested talk_to
                    # from the callee doesn't accidentally re-route onto the
                    # caller's user thread (which would defeat the isolation
                    # we just chose).
                    metadata={
                        # Carry the chain's ROOT origin forward so the callee can
                        # resolve HITL correctly (human chains forward to the
                        # human; scheduled/cognition use their policy). Without
                        # this the origin would be lost — every hop is "peer".
                        ORIGIN_KEY: propagated_origin(sender),
                        **(
                            {"originator_user_thread": user_thread}
                            if routing_mode == "async_callback"
                            else {}
                        ),
                        **({"a2a_preamble": a2a_preamble} if a2a_preamble else {}),
                    },
                ):
                    reply = await runner.ainvoke(message, thread_id=thread_id)
            finally:
                current_agent.reset(callee_token)
        except Exception as exc:
            logger.warning("talk_to_agent('%s'): %s", agent_name, exc)
            _publish("agent_activity", {"agent": agent_name, "status": "idle"})
            err_str = f"[talk_to_agent error] Calling '{agent_name}' failed: {exc}"
            # Surface the same error to any waiter via the Future, but
            # don't cache errors — the model should be free to retry a
            # real failure.
            if not inflight_fut.done():
                inflight_fut.set_result(err_str)
            return err_str

        finished = datetime.now(timezone.utc).isoformat()
        reply_str = reply if isinstance(reply, str) else str(reply)
        _publish(
            "agent_message",
            {
                "kind": "peer",
                "phase": "reply",
                "from": sender,
                "to": agent_name,
                "message": message,
                "reply": reply_str,
                "ts_sent": started,
                "ts_reply": finished,
            },
        )
        if routing_mode in {"sync", "async_callback"}:
            _publish(
                "thread_message",
                {
                    "agent": agent_name,
                    "thread_id": thread_id,
                    "role": "assistant",
                    "content": reply_str,
                    "via_peer": sender,
                    "ts": finished,
                },
            )
        _publish("agent_activity", {"agent": agent_name, "status": "idle"})

        # Publish the result to any concurrent waiters AND cache it for the
        # cooldown window. Format/presentation is the prompt's job, not
        # the tool's — keep this contract simple.
        if not inflight_fut.done():
            inflight_fut.set_result(reply_str)
        _completed[dedup_key] = (reply_str, time.time() + _COOLDOWN_SECS)
        _gc_completed_cache()
        # Opt-in: record this exchange in the shared per-pair digest (long-term
        # summary + short-term window). Best-effort; never breaks the exchange.
        if _a2a_digest_enabled():
            await _update_pair_digest(sender, agent_name, message, reply_str, finished)
        return reply_str
    finally:
        _inflight.pop(dedup_key, None)


@tool
async def recall_conversation(agent_name: str) -> str:
    """Recall your shared conversation history with a peer agent.

    Returns a compact digest — a rolling summary of earlier exchanges plus the
    most recent messages — of your ongoing collaboration with the named peer,
    spanning both directions and prior sessions. Use it to reload context
    before a follow-up `talk_to_agent` call when you're unsure what was already
    discussed. This is a read-only lookup; it does not message the peer.

    Args:
        agent_name: Name of the peer agent (case-insensitive).

    Returns:
        A plain-text digest of your conversation, or a note that no shared
        history has been recorded (peer-pair digests are enabled by the
        operator; each peer still remembers your prior exchanges on its own
        thread regardless).
    """
    me = current_agent.get() or "agent"
    peer = agent_name
    if _RUNTIME is not None:
        canonical, _ = _resolve_peer_name(agent_name)
        if canonical is not None:
            peer = canonical
    if not _a2a_digest_enabled():
        return (
            f"[recall_conversation] Shared per-pair history recording is disabled, so "
            f"there is no stored digest for '{peer}'. The peer still remembers your prior "
            f"exchanges on its own thread — just include the context you need in your "
            f"next `talk_to_agent` message."
        )
    record = await _load_pair_digest(me, peer)
    return _render_recall_text(record, peer)


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
