"""Derive a human-readable label for a conversation thread.

The Chat rail used to title each row with the raw first user message clipped at
120 chars. That reads badly, because several producers concatenate a
machine-oriented directive onto the message *before* the runner persists it —
so the JSONL ``content`` (the rail's only title source) is prefix-first::

    [from @jrivera (Jamie | Acme, id=5550001234)]: can you check …
    [Autonomous scheduled task — execute directly and completely. Do NOT …]

A 120-char cut of a long ``[from …]`` tag yields a title that is *entirely*
prefix, so a rail of 100+ threads is unscannable.

This module is the single place that turns ``(thread_id, first entries)`` into a
:class:`ThreadLabel`. It is pure — no I/O, no model calls — so both API
consumers (``runtime/api.py`` for the Chat rail, ``analytics/threadcost`` for
the Insights drill-down, which previously had its own divergent copy) share one
answer, and the whole thing is table-testable.

Two derivation paths, because both must work:

* **New transcripts** carry a structured ``channel`` / ``sender`` on the turn
  entries, stamped by ``runner._append_session`` from the active
  :class:`~olano.security.request_context.RequestContext` via
  :func:`channel_for_context`. Those are authoritative. ``origin`` is
  deliberately *never* stamped — ``RequestContext.source`` cannot express the
  origin taxonomy (one ``"scheduled"`` covers both a heartbeat poll and a
  scheduled task, and ``"cognition"`` has no source at all), so stamping it
  would collapse distinctions the id-shape rules below still get right.
* **Existing transcripts** — every thread written before that landed — have the
  directives baked into the text, so :func:`strip_directives` peels the known
  shapes back off at read time. This is not defensive coding for a hypothetical:
  it is the only thing that makes already-recorded history readable.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# ── origins ──────────────────────────────────────────────────────────────────
# A superset of analytics.threadcost.resolver's set: ``heartbeat`` is split out
# of ``scheduled`` here because the two read very differently in a rail (a
# heartbeat poll is a recurring health check; a scheduled task is real work).
# ``resolver.classify_origin`` folds it back so its own contract is unchanged.
ORIGIN_INTERACTIVE = "interactive"
ORIGIN_SCHEDULED = "scheduled"
ORIGIN_HEARTBEAT = "heartbeat"
ORIGIN_PEER = "peer"
ORIGIN_AUTONOMOUS = "autonomous"
# A conversation a human opened from Cortex ("Chat about this" on a Pulse card,
# a Cortex item, a self-check finding). It IS an interactive dashboard chat —
# the person typed it, on the dashboard — so it stays a normal, visible thread;
# the origin only says which surface it started from, which is what lets the
# rail badge it and a reader tell it apart from a chat they opened themselves.
ORIGIN_CORTEX = "cortex"
# A turn an OUTSIDE system sent over the inbound MCP endpoint or an A2A Agent
# Card (``mcp_server/server.py``, ``a2a/server.py``). Thread ids are minted
# ``mcp::<client>::<thread>`` / ``a2a::<client>::<context>`` and the turn runs
# non-owner, so the rail badges it EXTERNAL and the channel pill names the
# protocol — the reader must be able to tell a stranger's call from a chat.
ORIGIN_EXTERNAL = "external"
EXTERNAL_THREAD_PREFIXES = ("mcp::", "a2a::")

# Thread-id prefixes minted by the Cortex surfaces. ``pulse:<item id>`` comes
# from a Pulse card, ``cortex:<item id>`` from an idea / brainstorm / prototype
# / project item. The dashboard's chat hand-off reuses these exact ids, so the
# conversation continues in the thread the old inline chat wrote to.
CORTEX_THREAD_PREFIXES = ("cortex:", "pulse:")

# Channels that aren't a messaging platform.
CHANNEL_DASHBOARD = "dashboard"
CHANNEL_SCHEDULER = "scheduler"
CHANNEL_CLI = "cli"
CHANNEL_WEBHOOK = "webhook"
# The two inbound protocols. Not messaging platforms, but they earn a pill
# (unlike ``dashboard``): which protocol a stranger used is the information.
CHANNEL_MCP = "mcp"
CHANNEL_A2A = "a2a"
# The mobile app. It talks to the same API as the dashboard and is the same
# trusted human surface, so the turn's RequestContext keeps channel "api" (the
# human gate, magic links and browser auto-approval all key on it); only the
# thread's label says MOBILE. The route marks the turn with
# ``metadata[CLIENT_METADATA_KEY] = "mobile"`` and ``channel_for_request``
# turns that into this channel when the turn is stamped.
CHANNEL_MOBILE = "mobile"
CLIENT_METADATA_KEY = "client"

# ``olano_test_agent`` threads (``tools/_agent_test.py``): AgentFather driving
# an agent with test messages. Not a conversation with a person, and kept out of
# fact distillation (``memory/self_learning``) so a test input never becomes a
# "fact" about the user.
AGENT_TEST_THREAD_PREFIX = "agenttest::"

# Thread-id prefixes that mark autonomous machinery rather than a conversation.
# Mirrors ``analytics.journal._SYSTEM_THREAD_PREFIXES`` / ``tools.talk_to``.
# ``issues_`` is the per-agent issue poll (``scheduler/cron.py:_add_issues_job``)
# — scheduler-driven like the other two ``_``-prefixed families.
SYSTEM_THREAD_PREFIXES = (
    "acp::",
    "delegate::",
    "scheduled_",
    "heartbeat_",
    "issues_",
    "cog::",
    "sub::",
    AGENT_TEST_THREAD_PREFIX,
)

# ``u:<user_id>:`` — the per-human namespace the dashboard used to prepend to
# every thread id it ran a turn on. It is no longer minted, but threads written
# under it exist on every multi-user box, including the forks it produced when a
# human replied inside a scheduled run's thread (``u:<uid>:scheduled_<agent>_…``).
# Every id-shape rule below is anchored at the start of the id, so the prefix
# has to come off first or those rows lose their SCHEDULED pill and task name.
_RE_HUMAN_NAMESPACE = re.compile(r"^u:[^:]+:")


def strip_human_namespace(thread_id: str) -> str:
    """Drop one leading ``u:<user_id>:`` namespace from a thread id, if present."""
    return _RE_HUMAN_NAMESPACE.sub("", thread_id or "", count=1)


@dataclass(frozen=True)
class ThreadLabel:
    """What a conversation row should show.

    ``title`` is line 1. ``origin`` drives the badge. ``channel`` and ``sender``
    make up the provenance segment on line 2 — ``sender`` is a *display name*
    only, never an ``@handle`` or a numeric id, because those are what made the
    old titles unreadable in the first place.
    """

    title: str
    origin: str = ORIGIN_INTERACTIVE
    channel: str = ""
    sender: str = ""


# ── directive patterns ───────────────────────────────────────────────────────
# Every entry here corresponds to a real producer in the tree; the comment names
# it so this table can be re-verified when a producer changes.

# ``runtime/server.py`` — applied OUTSIDE the sender tag, so prefixes stack and
# a title can begin with two brackets (``[think:high] [from @x (…)]: hi``).
_RE_THINK = re.compile(r"^\[think:[^\]]*\]\s*")

# Redaction placeholders the security layer can splice into ANY persisted text:
# ``[REDACTED]`` (pii_middleware), ``[PHONE_US_REDACTED]`` (pii_middleware.redact),
# ``[REDACTED:api_key]`` (output_scrub). They carry a ``]``, so any tag body that
# can hold user data has to admit them explicitly — a plain ``[^\]]*`` body stops
# at the placeholder's own bracket and leaks the tag's tail into the title
# (``[from @x (Jamie, id=[PHONE_US_REDACTED])]: hi`` → a title of ``)]: hi``).
_REDACTION = r"\[(?:[A-Z0-9_]+_)?REDACTED(?::[a-z_]+)?\]"
# A tag body: anything but a bracket, OR a whole redaction placeholder. The two
# alternatives are disjoint on their first character, so the outer ``*`` has no
# ambiguous decomposition — this stays linear, and a ``[`` that does not open a
# complete placeholder still terminates the body exactly where it used to.
_TAG_BODY = rf"(?:[^\[\]]|{_REDACTION})*"

# ``platforms/session.py:format_sender_prefix`` via ``runtime/server.py``.
# Covers ``[from @h (Name, id=1)]``, ``[from Name]``, ``[from bot @h (…)]``.
_RE_FROM = re.compile(rf"^\[from\s+(?P<body>{_TAG_BODY})\]\s*:?\s*")

# ``platforms/session.py:format_chat_prefix`` — stacked AFTER the sender tag in
# groups/channels (``[from …] [in Team, chat id=99]: hi``). Dropped, not
# captured: the room name is provenance, not a title.
_RE_IN = re.compile(rf"^\[in\s+{_TAG_BODY}\]\s*:?\s*")

# ``scheduler/cron.py:_execute_db_task``.
_RE_AUTONOMOUS = re.compile(r"^\[Autonomous scheduled task[^\]]*\]\s*", re.IGNORECASE)

# ``scheduler/cron.py`` heartbeat poll. The ``Context:`` / ``Skill instructions:``
# blocks are payload, not label — same cut ``analytics/journal.py`` makes.
_RE_HEARTBEAT = re.compile(r"^HEARTBEAT:\s*", re.IGNORECASE)
_RE_HEARTBEAT_TAIL = re.compile(r"\n\n(?:Context|Skill instructions):[\s\S]*$")

# ``platforms/email.py`` — the subject IS the best title, so it's captured.
_RE_SUBJECT = re.compile(rf"^\[Subject:\s*(?P<subject>{_TAG_BODY})\]\s*")

# ``platforms/slack.py`` — prepended ahead of the sender tag.
_RE_SLACK_CONTEXT = re.compile(r"^\[Thread context —[\s\S]*?\[End of thread context\]\s*")

# ``webhooks/dispatcher.py:build_inbound_message``.
_RE_WEBHOOK = re.compile(r"^---\s*Incoming webhook payload:\s*(?P<name>.+?)\s*---")

# ``runtime/api.py`` Plan mode (already excluded from new transcripts via
# turn_directive, but older rows may still carry it).
_RE_PLAN_MODE = re.compile(r"^\[Plan mode\][^\n]*(?:\n\n)?")

# ``platforms/inbound.py`` /start greeting + the name / deep-link notes it
# appends. This is a very common first-message-of-thread, so without this every
# freshly-opened chat would be titled with the greeting instructions.
_RE_START_GREETING = re.compile(r"^\[The user just opened this chat[\s\S]*?\]\s*")
_RE_START_NAME = re.compile(r'^\[The user\'s name is "[^"]*"\.\]\s*')
_RE_START_DEEPLINK = re.compile(r"^\[The chat was opened via a deep link[^\]]*\]\s*")

# ``platforms/inbound.py`` + ``runtime/chat_attachments.py`` — media notes that
# carry usable text. Prefer the transcript over a generic "Voice message".
_RE_VOICE_TRANSCRIPT = re.compile(r"^\[(?:Voice message|Video audio) transcript\]\s*")
_RE_DOC_CONTENT = re.compile(rf"^\[Content of (?P<name>{_TAG_BODY})\]:\s*")

# Media notes with no usable text — mapped to a friendly kind label. Ordered:
# first match wins, so keep the more specific patterns above the generic ones.
_MEDIA_KINDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^\[Image attached:", re.IGNORECASE), "Image"),
    (re.compile(r"^\[Photo received", re.IGNORECASE), "Photo"),
    (re.compile(r"^\[Video attachment saved", re.IGNORECASE), "Video"),
    (re.compile(r"^\[Document ", re.IGNORECASE), "Document"),
    (re.compile(r"^\[Voice message", re.IGNORECASE), "Voice message"),
    (re.compile(r"^\[A voice message was received", re.IGNORECASE), "Voice message"),
    (re.compile(r"^\[Attachment", re.IGNORECASE), "Attachment"),
    (re.compile(r"^\[Note: the attached file", re.IGNORECASE), "Attachment"),
)

# Directives that are simply dropped, tried in a loop so stacked ones all peel.
_DROP_PATTERNS: tuple[re.Pattern[str], ...] = (
    _RE_THINK,
    _RE_SLACK_CONTEXT,
    _RE_AUTONOMOUS,
    _RE_PLAN_MODE,
    _RE_START_GREETING,
    _RE_START_NAME,
    _RE_START_DEEPLINK,
    _RE_VOICE_TRANSCRIPT,
)


def _sender_from_tag(body: str) -> str:
    """Pull a display name out of a ``[from …]`` tag body.

    ``format_sender_prefix`` emits ``@handle (Display Name, id=123)`` — or drops
    fields that are missing/duplicated. We want the most human token available
    and *never* the numeric id, so: prefer the parenthesised display name, fall
    back to the head when it isn't an ``@handle``, else the bare handle.
    """
    body = body.strip()
    if body.lower().startswith("bot "):
        body = body[4:].strip()

    head, _, paren = body.partition("(")
    head = head.strip()
    parts = [p.strip() for p in paren.rstrip(")").split(",") if p.strip()]
    named = [p for p in parts if not p.lower().startswith("id=")]
    if named:
        return named[0]
    # No head at all means the tag carried only an id (``[from id=123]``) —
    # a raw platform id is exactly the noise this whole module removes.
    if head.lower().startswith("id="):
        return ""
    if head and not head.startswith("@"):
        return head
    return head.lstrip("@")


def split_sender_tag(text: str) -> tuple[str, str]:
    """Peel ONLY the runtime-authored identity tags off the head of a message.

    Returns ``(remaining_text, sender_display_name)`` — the same ordering as
    :func:`strip_directives`, which calls this for its own first step.

    Deliberately narrower than ``strip_directives``: the dashboard's transcript
    view needs the ``[from …]`` tag gone but must keep the body **intact**, and
    ``strip_directives`` replaces an email body with its subject line and a
    document body with its filename — correct for a one-line rail title, and a
    deletion of the message anywhere else.
    """
    out = (text or "").strip()
    sender = ""
    for _ in range(6):  # think / from / in, stacked by their producers in any order
        before = out
        m = _RE_THINK.match(out)
        if m:
            out = out[m.end() :].lstrip()
        m = _RE_FROM.match(out)
        if m:
            sender = sender or _sender_from_tag(m.group("body"))
            out = out[m.end() :].lstrip()
        m = _RE_IN.match(out)
        if m:
            out = out[m.end() :].lstrip()
        if out == before:
            break
    return out, sender


def strip_directives(text: str) -> tuple[str, str]:
    """Peel known machine directives off a persisted message.

    Returns ``(cleaned_text, sender_display_name)``. ``sender`` is empty unless
    a ``[from …]`` tag was found. Loops until nothing more matches, because
    producers stack independently and in no fixed order.
    """
    out = (text or "").strip()
    sender = ""

    # A webhook envelope has no "user words" to recover — the whole body is a
    # JSON dump, so name it by the webhook and stop.
    m = _RE_WEBHOOK.match(out)
    if m:
        return f"Webhook · {m.group('name')}", ""

    for _ in range(12):  # bounded: ~8 distinct patterns, generous headroom
        before = out

        out, tag_sender = split_sender_tag(out)
        sender = sender or tag_sender

        m = _RE_SUBJECT.match(out)
        if m:
            # The subject is a better title than the body's first line.
            subject = m.group("subject").strip()
            rest = out[m.end() :].lstrip()
            out = subject or rest

        m = _RE_HEARTBEAT.match(out)
        if m:
            out = _RE_HEARTBEAT_TAIL.sub("", out[m.end() :]).lstrip()

        m = _RE_DOC_CONTENT.match(out)
        if m:
            out = m.group("name").strip() or out[m.end() :].lstrip()

        for pattern in _DROP_PATTERNS:
            m = pattern.match(out)
            if m:
                out = out[m.end() :].lstrip()

        if out == before:
            break

    if not out.strip():
        return "", sender

    # Media-only turns: no words survived the peel, just a bracketed note.
    for pattern, kind in _MEDIA_KINDS:
        if pattern.match(out):
            return kind, sender

    return out, sender


def condense(text: str, max_len: int = 120) -> str:
    """Collapse to a single line and truncate on a word boundary.

    The old rail cut mid-token at exactly 120 chars; this keeps whole words so a
    clipped title still reads. Mirrors the dashboard's ``peek()`` helper.
    """
    clean = re.sub(r"\s+", " ", (text or "")).strip()
    if len(clean) <= max_len:
        return clean
    cut = clean[:max_len]
    space = cut.rfind(" ")
    return (cut[:space] if space > max_len * 0.6 else cut).rstrip() + "…"


def is_system_thread(thread_id: str) -> bool:
    """True for autonomous machinery (peer / delegated / scheduled / heartbeat)."""
    stem = strip_human_namespace(thread_id)
    return any(stem.startswith(p) for p in SYSTEM_THREAD_PREFIXES)


def classify_thread(
    thread_id: str,
    first_entry: Mapping | None,
    *,
    channel_hint: str = "",
) -> tuple[str, str]:
    """Return ``(origin, channel)`` for a thread.

    Resolution order: structured fields on the entry (authoritative for
    transcripts written after this landed) → thread-id shape → entry role →
    ``channel_hint`` → interactive/dashboard. The id schemes are enumerated in
    ``core/checkpoints.py``.

    ``channel_hint`` is a *recovered* answer for transcripts written before the
    stamp existed (see ``runtime/api.py:_platform_by_thread``). It ranks below
    everything derived from the transcript itself, because it is a heuristic
    join on a chat id and the transcript is evidence.
    """
    tid = strip_human_namespace(thread_id)
    entry = first_entry or {}

    origin = str(entry.get("origin") or "")
    channel = str(entry.get("channel") or "")
    # An inbound MCP / A2A turn: the id prefix is the protocol, and it fills a
    # channel an older stamp left empty so the pill never goes missing.
    external_channel = ""
    if tid.startswith("mcp::"):
        external_channel = CHANNEL_MCP
    elif tid.startswith("a2a::"):
        external_channel = CHANNEL_A2A
    if origin:
        return origin, channel or external_channel
    if external_channel:
        return ORIGIN_EXTERNAL, channel or external_channel

    # A scheduled run can genuinely arrive on a platform — ``scheduler/multi_user``
    # fans out as ``source="scheduled", channel="telegram"`` — so a stamped
    # channel refines the origin rather than being overwritten by it.
    if tid.startswith("heartbeat_"):
        return ORIGIN_HEARTBEAT, channel or CHANNEL_SCHEDULER
    # The issue poll is a scheduler firing too (``cron.py:_add_issues_job``); it
    # reads as a scheduled run so the rail badges it and the "scheduled" filter
    # bucket holds it, instead of it passing for a human conversation.
    if tid.startswith("scheduled_") or tid.startswith("issues_"):
        return ORIGIN_SCHEDULED, channel or CHANNEL_SCHEDULER
    if "cog::" in tid or tid.startswith(("sub::", AGENT_TEST_THREAD_PREFIX)):
        return ORIGIN_AUTONOMOUS, ""
    if tid.startswith("acp::") or tid.startswith("delegate::"):
        return ORIGIN_PEER, ""
    if entry.get("role") == "peer_inbound":
        return ORIGIN_PEER, channel
    # Opened from Cortex. The channel stays ``dashboard`` — that is where the
    # turn genuinely arrived, and it is what keeps the row out of the platform
    # pills — so only the origin distinguishes it from any other browser chat.
    if tid.startswith(CORTEX_THREAD_PREFIXES):
        return ORIGIN_CORTEX, channel or CHANNEL_DASHBOARD

    # ``platforms/session.py:build_session_key`` → agent:main:<platform>:<type>:…
    if tid.startswith("agent:main:"):
        parts = tid.split(":")
        return ORIGIN_INTERACTIVE, (parts[2] if len(parts) > 2 else "")
    if tid.startswith("tg_"):
        return ORIGIN_INTERACTIVE, "telegram"

    return ORIGIN_INTERACTIVE, channel or channel_hint or CHANNEL_DASHBOARD


# ── stamping: what ``runner._append_session`` records ────────────────────────

_PLATFORM_CHANNELS: frozenset[str] | None = None


def _platform_channels() -> frozenset[str]:
    """The canonical messaging-platform channel values, resolved lazily.

    ``platforms.config`` pulls the platform package in with it, and this module
    is imported on ``analytics/threadcost``'s read path — so the import stays
    inside the call and the answer is memoized. ``local`` is excluded: it is the
    ``RequestContext`` default, not a platform anyone messages from.
    """
    global _PLATFORM_CHANNELS
    if _PLATFORM_CHANNELS is None:
        try:
            from olano.platforms.config import Platform

            _PLATFORM_CHANNELS = frozenset(p.value for p in Platform if p is not Platform.LOCAL)
        except Exception:  # pragma: no cover - defensive; degrades to source-only
            _PLATFORM_CHANNELS = frozenset()
    return _PLATFORM_CHANNELS


def channel_for_context(source: str, channel: str) -> str:
    """Map a live ``RequestContext`` onto the channel to persist on a turn.

    Returns ``""`` when the context carries nothing :func:`classify_thread`
    can't already recover on its own — the caller then writes no channel and
    the read-time fallbacks decide, exactly as before.

    A recognised platform value **wins over** ``source``, because the two are
    not parallel: ``scheduler/multi_user.py`` runs a scheduled fan-out as
    ``source="scheduled", channel="telegram"``, and that turn really did arrive
    on Telegram.

    ``local`` maps to ``""`` on purpose. It is the *default* value of an unset
    ``RequestContext``, which makes it indistinguishable from a context that was
    never bound — and one is routinely lost: an SSE body generator runs after
    its Starlette middleware has already exited (``runtime/api.py`` says so at
    both of its stream handlers), so a dashboard turn can legitimately observe
    the default. Persisting ``"cli"`` there would mislabel every dashboard
    thread — a worse regression than the "Dashboard" this whole path fixes.
    """
    source = (source or "").strip()
    channel = (channel or "").strip()
    if channel and channel in _platform_channels():
        return channel
    if source == "api" or channel == "api":
        return CHANNEL_DASHBOARD
    if source == "webhook":
        return CHANNEL_WEBHOOK
    # Inbound MCP / A2A: the protocol is the channel (``channel`` is set to the
    # same value by both servers; ``source`` alone is enough).
    if channel in (CHANNEL_MCP, CHANNEL_A2A):
        return channel
    if source in (CHANNEL_MCP, CHANNEL_A2A):
        return source
    if source == "scheduled":
        return CHANNEL_SCHEDULER
    if source == "cli":
        return CHANNEL_CLI
    # peer / cognition: the origin pill already reads AGENT / AUTONOMOUS, so a
    # "talk_to_agent" channel beside it would be pure duplication. local: above.
    return ""


def channel_for_request(ctx: Any) -> str:
    """:func:`channel_for_context` for a whole ``RequestContext``.

    The one refinement over the bare pair: a dashboard turn the mobile app sent
    (``metadata["client"] == "mobile"``) is stamped ``mobile``, so the app's
    conversations carry their own label. Only the stamp changes; the context's
    channel stays ``api`` for every trust decision that reads it.
    """
    channel = channel_for_context(getattr(ctx, "source", ""), getattr(ctx, "channel", ""))
    client = str((getattr(ctx, "metadata", None) or {}).get(CLIENT_METADATA_KEY) or "")
    if channel == CHANNEL_DASHBOARD and client == CHANNEL_MOBILE:
        return CHANNEL_MOBILE
    return channel


# ── titling: what the rail renders ───────────────────────────────────────────

# Fallback titles per origin, used when nothing readable survives the peel.
_ORIGIN_FALLBACK = {
    ORIGIN_SCHEDULED: "Scheduled run",
    ORIGIN_HEARTBEAT: "Heartbeat",
    ORIGIN_PEER: "Agent message",
    ORIGIN_AUTONOMOUS: "Autonomous run",
    ORIGIN_CORTEX: "Cortex discussion",
    ORIGIN_EXTERNAL: "External call",
}


def derive_label(
    thread_id: str,
    entries: Sequence[Mapping],
    *,
    task_names: Mapping[str, str] | None = None,
    channel_hint: str = "",
    max_len: int = 120,
) -> ThreadLabel:
    """Build the row label for a thread from the head of its transcript.

    ``entries`` need only contain the first few JSONL rows. ``channel_hint`` is
    a recovered channel for transcripts written before the stamp existed; it
    ranks below anything the transcript itself says. ``task_names`` maps
    a scheduler task's short id (the ``<task8>`` in
    ``scheduled_<agent>_<task8>@<stamp>``) to its human name, so pre-existing
    scheduled threads recover the name the scheduler already stores — that is
    what turns a rail of identical ``[Autonomous scheduled task …]`` rows into
    distinct, meaningful ones.
    """
    entries = list(entries or [])
    origin, channel = classify_thread(
        thread_id, _provenance_entry(entries), channel_hint=channel_hint
    )

    # The first human turn is the title source; fall back to the last entry so a
    # thread with no user turn still shows something (matches prior behaviour).
    first_user = next(
        (e for e in entries if e.get("role") in {"user", "peer_inbound"}),
        None,
    )
    source = first_user or (entries[-1] if entries else None) or {}

    sender = str(source.get("sender") or source.get("from") or "")
    title, tag_sender = strip_directives(str(source.get("content") or ""))
    sender = sender or tag_sender
    # An outside caller is stamped ``mcp:<label>`` / ``a2a:<label>``; the rail
    # already badges the protocol beside EXTERNAL, so the sender shows the label.
    if origin == ORIGIN_EXTERNAL:
        for prefix in ("mcp:", "a2a:"):
            if sender.lower().startswith(prefix):
                sender = sender[len(prefix) :].strip()
                break

    # A scheduler-run thread is named by its task, not by the prompt body: the
    # name is what the operator wrote and what the Scheduler view shows.
    task_name = str(source.get("task_name") or "")
    if not task_name and task_names and origin == ORIGIN_SCHEDULED:
        task_name = _scheduler_task_name(thread_id, task_names)
    if task_name:
        title = task_name

    fallback = _ORIGIN_FALLBACK.get(origin, "")
    if origin == ORIGIN_SCHEDULED and strip_human_namespace(thread_id).startswith("issues_"):
        fallback = "Issue sweep"
    title = condense(title, max_len) or fallback
    return ThreadLabel(title=title, origin=origin, channel=channel, sender=sender)


def _provenance_entry(entries: Sequence[Mapping]) -> Mapping | None:
    """The entry whose structured fields describe where this thread came from.

    NOT simply ``entries[0]``: the runner prepends ``system`` rows ahead of the
    first human turn (a prompt-change notice, a recorded context note), and an
    auto-compaction successor is seeded with a ``[Conversation compacted]`` row
    — none of which carry a channel. Prefer the first entry that actually has
    provenance, then the first human turn, and only then the head of the file.
    """
    for e in entries:
        if e.get("channel") or e.get("origin"):
            return e
    for e in entries:
        if e.get("role") in {"user", "peer_inbound"}:
            return e
    return entries[0] if entries else None


def _scheduler_task_name(thread_id: str, task_names: Mapping[str, str]) -> str:
    """Recover a task name from a ``scheduled_<agent>_<task8>@<stamp>`` id.

    The agent name itself can contain underscores, so match on the *last*
    underscore-delimited segment before the ``@`` stamp rather than splitting
    left-to-right.
    """
    stem = strip_human_namespace(thread_id).split("@", 1)[0]
    if not stem.startswith("scheduled_"):
        return ""
    candidate = stem.rsplit("_", 1)[-1]
    return task_names.get(candidate, "")
