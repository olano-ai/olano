"""Per-chat output verbosity for messaging platforms.

A chat user on any platform (Telegram, WhatsApp, Discord, Slack, Signal, the
Chinese platforms, …) can choose how much of the agent's work is surfaced in
replies:

- ``quiet``  — only the agent's final answer (nothing if the turn produced no
  text). Best for noisy group chats.
- ``normal`` — the final answer. If a turn ran tools but produced no visible
  text, a one-line note is appended so the user isn't left hanging. This is
  the default and matches the platforms' historical behaviour for ordinary
  question/answer turns.
- ``verbose`` — the full play-by-play: each tool call (name + abbreviated
  args) and tool result (truncated), peer/subagent conversation cards, then
  the answer.

Multi-agent turns get special treatment so concurrent agents don't blur into
one stream of text (the "marcus talks to three agents and the chat is a mess"
problem):

- ``talk_to_agent`` calls render as a conversation card — ``💬 → kevin: …``
  followed by ``💬 kevin replied: …`` — instead of raw JSON args.
- ``task`` (subagent) calls render as ``🤖 → subagent research-agent: …``.
- Raw interleaved ``subagent_text`` chunks are NEVER spliced into the reply;
  they collapse into a single "talking with …" indicator per agent, and the
  actual replies arrive attributed via the tool-result cards above.

Rendering is **markup-dialect aware** so each platform gets its native look
instead of raw ``**`` / ``_`` artifacts:

- ``markdown`` — standard markdown. Used for platform adapters that convert
  standard markdown to their native format themselves (Telegram → MarkdownV2,
  WhatsApp → ``*bold*``, Slack → mrkdwn) and those that render it natively
  (Mattermost, Matrix, WeCom, DingTalk, Feishu, Weixin, webhook).
- ``discord`` — native Discord markdown, plus Discord-only niceties:
  ``-# subtext`` for the "talking with …" liveness indicators and ``>``
  block quotes for peer replies.
- ``mrkdwn`` — Slack's raw syntax (``*bold*``, ``_italic_``), for the classic
  Slack channel which posts text without conversion.
- ``telegram-legacy`` — Telegram Bot-API legacy Markdown (``*bold*``, no
  nested entities, no block quotes), for the classic Telegram channel which
  sends with ``parse_mode="Markdown"``.
- ``plain`` — no markup at all, for Signal, SMS, iMessage/BlueBubbles, email,
  QQ and Home Assistant, where markup characters would show up literally.

This module is intentionally pure (no I/O, no olano imports) so it is cheap
to import and trivial to unit-test. The runtime handlers collect the typed
events emitted by :meth:`AgentRunner.astream_events` and hand them to
:func:`render_events` together with the resolved verbosity and the platform's
dialect (via :func:`dialect_for_platform` / :func:`dialect_for_channel`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# Canonical modes, ordered low → high.
VERBOSITY_MODES: tuple[str, ...] = ("quiet", "normal", "verbose")
DEFAULT_VERBOSITY = "normal"

# Accepted spellings/aliases → canonical mode.  ``/verbose on|off`` is handled
# by the command layer (on → verbose, off → normal) and deliberately not mapped
# here, so ``normalize_verbosity`` stays unambiguous for ``/verbosity <mode>``.
_ALIASES: dict[str, str] = {
    "quiet": "quiet",
    "q": "quiet",
    "silent": "quiet",
    "min": "quiet",
    "minimal": "quiet",
    "normal": "normal",
    "n": "normal",
    "default": "normal",
    "standard": "normal",
    "verbose": "verbose",
    "v": "verbose",
    "full": "verbose",
    "debug": "verbose",
    "all": "verbose",
    "max": "verbose",
}

# Tool names that mean "conversation with a peer agent" (see
# olano.core.runner._PEER_TOOL_NAMES — kept in sync by test).
PEER_TOOL_NAMES: tuple[str, ...] = ("talk_to_agent", "talk_to")
# deepagents' built-in subagent delegation tool.
SUBAGENT_TOOL_NAME = "task"

# Caps so a chatty tool turn can't produce a multi-megabyte platform message.
_MAX_ARGS_CHARS = 300
_MAX_RESULT_CHARS = 600
_MAX_PEER_MSG_CHARS = 280


# ───────────────────────────── markup dialects ──────────────────────────────


@dataclass(frozen=True)
class MarkupDialect:
    """How to decorate names/args/results for one platform family.

    Wrappers are (open, close) pairs; empty pairs mean "no markup" so the
    plain dialect degrades to clean prose. ``subtext_prefix`` is Discord's
    ``-# `` small-print; dialects without it fall back to italics for the
    liveness indicators.
    """

    key: str
    bold: tuple[str, str] = ("**", "**")
    italic: tuple[str, str] = ("_", "_")
    code: tuple[str, str] = ("`", "`")
    fences: bool = True
    quote_prefix: str = "> "
    subtext_prefix: str = ""

    def b(self, s: str) -> str:
        return f"{self.bold[0]}{s}{self.bold[1]}"

    def i(self, s: str) -> str:
        return f"{self.italic[0]}{s}{self.italic[1]}"

    def c(self, s: str) -> str:
        return f"{self.code[0]}{s}{self.code[1]}"

    def block(self, s: str) -> str:
        """Multi-line preformatted block (tool output)."""
        if self.fences:
            return f"```\n{s}\n```"
        return s

    def quote(self, s: str) -> str:
        """Block-quote *s* (peer replies) when the platform supports it."""
        if not self.quote_prefix:
            return s
        return "\n".join(f"{self.quote_prefix}{line}" for line in s.splitlines())

    def subtext(self, line: str) -> str:
        """Small-print liveness line (``-# …`` on Discord, italics elsewhere)."""
        if self.subtext_prefix:
            return f"{self.subtext_prefix}{line}"
        return self.i(line)


# Standard markdown — the default. Platform adapters that need a different
# native syntax (Telegram MarkdownV2, WhatsApp, Slack mrkdwn) convert FROM
# standard markdown in their own format_message step, so this is the correct
# input dialect for them too.
MARKDOWN = MarkupDialect(key="markdown")

# Discord renders standard markdown natively and adds `-# subtext`.
DISCORD = MarkupDialect(key="discord", subtext_prefix="-# ")

# Slack's raw mrkdwn (classic channel posts text with no conversion).
MRKDWN = MarkupDialect(key="mrkdwn", bold=("*", "*"))

# Telegram Bot-API legacy Markdown (classic channel: parse_mode="Markdown").
# Single-char entities, NO nesting, no block quotes — a stray unbalanced or
# nested entity makes the API reject the whole message.
TELEGRAM_LEGACY = MarkupDialect(key="telegram-legacy", bold=("*", "*"), quote_prefix="")

# No markup at all: Signal, SMS, iMessage, email, QQ, Home Assistant.
PLAIN = MarkupDialect(
    key="plain",
    bold=("", ""),
    italic=("", ""),
    code=("", ""),
    fences=False,
    quote_prefix="",
)

_DIALECTS: dict[str, MarkupDialect] = {
    d.key: d for d in (MARKDOWN, DISCORD, MRKDWN, TELEGRAM_LEGACY, PLAIN)
}

# Platform adapters (olano/platforms/*) keyed by Platform enum value.
# Unlisted platforms default to standard markdown — their adapters either
# render it natively or convert it themselves.
_PLATFORM_DIALECTS: dict[str, str] = {
    "discord": "discord",
    "signal": "plain",  # signal-cli sends raw text; markup shows literally
    "sms": "plain",  # adapter strips markdown; emit none to begin with
    "bluebubbles": "plain",  # iMessage has no markup
    "email": "plain",  # text/plain MIME body
    "homeassistant": "plain",
    "qqbot": "plain",
}

# Classic channels (olano/channels/*) send text verbatim, so they need the
# platform's raw syntax.
_CHANNEL_DIALECTS: dict[str, str] = {
    "telegram": "telegram-legacy",
    "discord": "discord",
    "slack": "mrkdwn",
    "email": "plain",
}


def dialect_for_platform(platform: str | None) -> MarkupDialect:
    """Dialect for a platform-adapter handler (``platform:*`` integrations)."""
    key = _PLATFORM_DIALECTS.get(str(platform or "").strip().lower(), "markdown")
    return _DIALECTS[key]


def dialect_for_channel(channel: str | None) -> MarkupDialect:
    """Dialect for a classic-channel handler (``olano/channels/*``)."""
    key = _CHANNEL_DIALECTS.get(str(channel or "").strip().lower(), "markdown")
    return _DIALECTS[key]


def _resolve_dialect(dialect: MarkupDialect | str | None) -> MarkupDialect:
    if isinstance(dialect, MarkupDialect):
        return dialect
    if isinstance(dialect, str):
        return _DIALECTS.get(dialect.strip().lower(), MARKDOWN)
    return MARKDOWN


# ───────────────────────────── event helpers ────────────────────────────────


def normalize_verbosity(value: Any) -> str | None:
    """Map *value* to a canonical mode, or ``None`` if unrecognized."""
    if value is None:
        return None
    return _ALIASES.get(str(value).strip().lower())


def _tool_name(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("name") or "tool")
    return "tool"


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"… (+{len(text) - limit} chars)"


def _compact(text: str) -> str:
    """Collapse internal whitespace so args/results render on tidy lines."""
    return " ".join(str(text).split())


def _parse_args(payload: Any) -> dict[str, Any]:
    """Best-effort structured view of a tool_call payload's ``args``."""
    if not isinstance(payload, dict):
        return {}
    args = payload.get("args")
    if isinstance(args, dict):
        return args
    if isinstance(args, str) and args.strip():
        try:
            parsed = json.loads(args)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}


def _peer_target(payload: Any) -> str:
    args = _parse_args(payload)
    return str(args.get("agent_name") or args.get("agent") or "").strip()


def _subagent_type(payload: Any) -> str:
    args = _parse_args(payload)
    return str(args.get("subagent_type") or "").strip()


def _format_tool_call(payload: Any, d: MarkupDialect) -> str:
    """One block per tool call. Peer/subagent calls get conversation cards."""
    name = _tool_name(payload)

    if name in PEER_TOOL_NAMES:
        target = _peer_target(payload) or "agent"
        args = _parse_args(payload)
        message = _compact(str(args.get("message") or args.get("content") or ""))
        head = f"💬 → {d.b(target)}"
        if message:
            return f"{head}: {_truncate(message, _MAX_PEER_MSG_CHARS)}"
        return f"{head} {d.i(f'(talking with {target}…)')}"

    if name == SUBAGENT_TOOL_NAME:
        sub = _subagent_type(payload) or "subagent"
        args = _parse_args(payload)
        description = _compact(str(args.get("description") or ""))
        head = f"🤖 → subagent {d.b(sub)}"
        if description:
            return f"{head}: {_truncate(description, _MAX_PEER_MSG_CHARS)}"
        return f"{head} {d.i('(delegating…)')}"

    args_str = ""
    if isinstance(payload, dict):
        args_str = _compact(str(payload.get("args") or ""))
    # Code-span the tool name: names like web_search contain underscores, which
    # break Telegram's legacy Markdown parser when wrapped in _/*/** entities
    # (the whole message then falls back to unformatted plain text).
    head = f"🔧 {d.c(name)}"
    if args_str:
        return f"{head}\n{d.c(_truncate(args_str, _MAX_ARGS_CHARS))}"
    return head


def _format_tool_result(payload: Any, peer_by_id: dict[str, str], d: MarkupDialect) -> str:
    name = _tool_name(payload)
    content = ""
    if isinstance(payload, dict):
        content = str(payload.get("content") or "")
    content = _truncate(content, _MAX_RESULT_CHARS)
    if not content:
        return ""

    if name in PEER_TOOL_NAMES:
        call_id = str(payload.get("id") or "") if isinstance(payload, dict) else ""
        target = peer_by_id.get(call_id) or "agent"
        return f"💬 {d.b(target)} replied:\n{d.quote(content)}"

    if name == SUBAGENT_TOOL_NAME:
        return f"🤖 subagent finished:\n{d.block(content)}"

    return f"↳ {d.c(name)}\n{d.block(content)}"


def _subagent_indicator(payload: Any, d: MarkupDialect) -> tuple[str, str]:
    """Return ``(dedup_key, indicator_line)`` for a subagent_text event.

    The raw interleaved text of peers/subagents is intentionally dropped —
    concurrent agents streaming into one chat is unreadable. Their actual
    replies surface through the attributed tool-result cards; here we only
    show a one-line "talking with …" indicator, once per agent (not per
    chunk), as a liveness signal. Names are NOT nested in extra entities:
    Telegram's legacy parser rejects nested markup wholesale.
    """
    agents: list[str] = []
    namespace: Any = ()
    if isinstance(payload, dict):
        raw_agents = payload.get("agents")
        if isinstance(raw_agents, list | tuple):
            agents = [str(a).strip() for a in raw_agents if str(a).strip()]
        namespace = payload.get("namespace") or ()
    if agents:
        names = ", ".join(dict.fromkeys(agents))
        return (
            f"peer:{','.join(sorted(set(agents)))}",
            d.subtext(f"💬 talking with {names}…"),
        )
    key = ":".join(str(part) for part in namespace) if namespace else "subagent"
    return f"ns:{key}", d.subtext("🤖 talking with subagent…")


def _peer_targets_by_call_id(events: list[tuple[str, Any]]) -> dict[str, str]:
    """Map tool_call id → peer agent name, so replies can be attributed."""
    out: dict[str, str] = {}
    for kind, payload in events:
        if kind == "tool_call" and isinstance(payload, dict):
            if _tool_name(payload) in PEER_TOOL_NAMES:
                target = _peer_target(payload)
                call_id = str(payload.get("id") or "")
                if call_id and target:
                    out[call_id] = target
    return out


def _friendly_tool_label(payload: Any, peer_by_id: dict[str, str]) -> str:
    """Short human label for the normal-mode "done" note."""
    name = _tool_name(payload)
    if name in PEER_TOOL_NAMES:
        target = _peer_target(payload)
        return f"talked with {target}" if target else "talked with a peer agent"
    if name == SUBAGENT_TOOL_NAME:
        sub = _subagent_type(payload)
        return f"ran subagent {sub}" if sub else "ran a subagent"
    return name


def render_events(
    events: list[tuple[str, Any]],
    verbosity: Any = DEFAULT_VERBOSITY,
    dialect: MarkupDialect | str | None = None,
    *,
    streamed_progress: bool = False,
) -> str:
    """Render collected ``(event_type, payload)`` tuples into outbound text.

    *events* are the items yielded by :meth:`AgentRunner.astream_events` for a
    single turn. The rendering is driven by *verbosity* (see module docstring)
    and decorated per *dialect* (a :class:`MarkupDialect`, its key, or ``None``
    for standard markdown).

    *streamed_progress* says the turn's tool calls already reached the chat
    live (see :mod:`olano.platforms.progress_reporter`). The play-by-play
    is then omitted here — repeating it under the answer would show the same
    work twice — leaving this to render the answer alone.
    """
    mode = normalize_verbosity(verbosity) or DEFAULT_VERBOSITY
    d = _resolve_dialect(dialect)

    answer_parts = [p for kind, p in events if kind == "text" and isinstance(p, str)]
    answer = "".join(answer_parts).strip()

    if mode == "quiet":
        return answer

    peer_by_id = _peer_targets_by_call_id(events)

    if mode == "normal" or streamed_progress:
        if answer:
            return answer
        # No visible text — surface a concise note when tools did the work so
        # the user isn't left staring at silence.
        tool_calls = [p for kind, p in events if kind == "tool_call"]
        if tool_calls:
            labels = ", ".join(
                dict.fromkeys(_friendly_tool_label(p, peer_by_id) for p in tool_calls)
            )
            return d.i(f"(done — {len(tool_calls)} tool call(s): {labels})")
        return ""

    # verbose — render in arrival order so tool calls/results sit next to the
    # text they relate to. Peer/subagent raw text collapses to one indicator
    # per agent; their replies arrive via the attributed tool-result cards.
    blocks: list[str] = []
    text_buf: list[str] = []
    seen_subagent_keys: set[str] = set()

    def _flush_text() -> None:
        if text_buf:
            joined = "".join(text_buf).strip()
            if joined:
                blocks.append(joined)
            text_buf.clear()

    for kind, payload in events:
        if kind == "text" and isinstance(payload, str):
            text_buf.append(payload)
        elif kind == "subagent_text":
            key, indicator = _subagent_indicator(payload, d)
            if key not in seen_subagent_keys:
                seen_subagent_keys.add(key)
                _flush_text()
                blocks.append(indicator)
        elif kind == "tool_call":
            _flush_text()
            blocks.append(_format_tool_call(payload, d))
        elif kind == "tool_result":
            _flush_text()
            block = _format_tool_result(payload, peer_by_id, d)
            if block:
                blocks.append(block)
        elif kind == "status":
            msg = payload.get("message") if isinstance(payload, dict) else str(payload)
            if msg:
                _flush_text()
                blocks.append(d.i(str(msg)))
    _flush_text()

    return "\n\n".join(b for b in blocks if b).strip()
