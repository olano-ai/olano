"""One page of a conversation, for a client that draws it a page at a time.

``GET /api/agents/{name}/threads/{id}`` returns the whole transcript by
default, which is what the dashboard reads. A phone opening a long
conversation paid for every message it had ever had before it could draw the
last one, so the endpoint also takes ``limit`` / ``before`` (newest page
first, then older ones as the person scrolls up) and ``tool_results=status``
(a tool step's output left out, since the app draws the step and never its
output).

Both are about what one read returns, never what is stored: the transcript
on disk and the agent's own history (the checkpoint) are untouched.

Positions are indexes into the thread's chat-visible rows. A transcript is
append-only, so an index names the same row for the life of the thread, and a
client can ask for the rows ``before`` the first one it holds.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from olano.core.thread_labels import (
    CHANNEL_A2A,
    CHANNEL_MCP,
    ORIGIN_AUTONOMOUS,
    ORIGIN_CORTEX,
    ORIGIN_EXTERNAL,
    ORIGIN_HEARTBEAT,
    ORIGIN_PEER,
    ORIGIN_SCHEDULED,
)

#: Roles that open a turn. A page starts at one so a reply is never split
#: from its question or its tool steps.
TURN_START_ROLES = frozenset({"user", "peer_inbound"})

#: ``tool_results`` values: the output as stored, or only that the step ran.
TOOL_RESULTS_FULL = "full"
TOOL_RESULTS_STATUS = "status"

_TOOL_PREFIX = re.compile(r"^([\w.-]+): ")


def is_turn_start(row: Mapping[str, Any]) -> bool:
    # A steered message is written mid-turn (core/steering.py): the turn it
    # joined continues after it.
    return row.get("role") in TURN_START_ROLES and not row.get("steer")


def page_bounds(
    rows: Sequence[Mapping[str, Any]], limit: int, before: int | None
) -> tuple[int, int]:
    """``(start, end)`` of the page of at most ``limit`` rows ending before ``before``.

    The start moves back to the turn that holds it, so a page can run past
    ``limit`` by the rest of one turn, never split it.
    """
    end = len(rows) if before is None else max(0, min(before, len(rows)))
    start = max(0, end - max(1, limit))
    while start > 0 and not is_turn_start(rows[start]):
        start -= 1
    return start, end


def tool_name(message: Mapping[str, Any]) -> str:
    name = str(message.get("name") or "")
    if name:
        return name
    m = _TOOL_PREFIX.match(str(message.get("content") or ""))
    return m.group(1) if m else ""


def keeps_tool_output(message: Mapping[str, Any]) -> bool:
    """Whether a client that draws no tool output still needs this one.

    The chat builds sign-in cards out of two kinds of result
    (``connectLinks.ts``): a ``magic_link_*`` tool's, and any result carrying
    an MCP ``authorization_url``. Those keep their output.
    """
    return tool_name(message).startswith("magic_link") or "authorization_url" in str(
        message.get("content") or ""
    )


def omit_tool_output(message: dict[str, Any]) -> dict[str, Any]:
    """The tool result without its output, still naming the tool that ran.

    A transcript row reads ``"<tool>: <output>"``; the name stays in the same
    shape so a client parsing it finds the tool and an empty output.
    """
    if message.get("role") != "tool_result" or keeps_tool_output(message):
        return message
    name = tool_name(message)
    message["content"] = "" if message.get("name") or not name else f"{name}: "
    message["output_omitted"] = True
    return message


# ── The thread list, a page at a time ────────────────────────────────────────
#
# ``GET /api/agents/{name}/threads?limit=N[&before=<cursor>][&kind=conversation]``
# lists the newest N threads by transcript mtime, reading only those N files;
# an agent with thousands of threads used to be read in full for every list.
# The cursor is the mtime (ns) of the last transcript the page looked at, as
# an opaque decimal string, so the next page starts strictly below it.

#: Threads per page when a client asks for pages without saying how many.
DEFAULT_PAGE = 20

#: Origins that are never a conversation a person had with the agent. Mirrors
#: the dashboard's ``threadPick.isHumanConversation`` + the app's
#: ``isConversation``: scheduled/heartbeat runs, agent-to-agent threads, Cortex
#: threads and outside (MCP/A2A) calls.
_NOT_CONVERSATION_ORIGINS = frozenset(
    {
        ORIGIN_SCHEDULED,
        ORIGIN_HEARTBEAT,
        ORIGIN_PEER,
        ORIGIN_AUTONOMOUS,
        ORIGIN_CORTEX,
        ORIGIN_EXTERNAL,
    }
)
_PROTOCOL_CHANNELS = frozenset({CHANNEL_MCP, CHANNEL_A2A})

KIND_CONVERSATION = "conversation"


def is_conversation(origin: str, channel: str) -> bool:
    """Whether a labelled thread is one a person had (chat, messenger)."""
    return origin not in _NOT_CONVERSATION_ORIGINS and channel not in _PROTOCOL_CHANNELS


def cursor_ns(before: str | None) -> int | None:
    """The mtime (ns) a ``before`` cursor names, or None for the newest page."""
    if not before:
        return None
    return int(before)


def cursor_of(mtime_ns: int) -> str:
    return str(mtime_ns)


# ── Keyword search ───────────────────────────────────────────────────────────

#: Transcripts one search request reads at most: the rest is reached with the
#: cursor it returns, so one request never runs for minutes on a long history.
SEARCH_MAX_SCAN = 400
SNIPPET_RADIUS = 60

_SEARCHED_ROLES = frozenset({"user", "peer_inbound", "assistant"})
_WS = re.compile(r"\s+")


def text_prefilter_applies(q: str) -> bool:
    """Whether ``q`` can be looked for in a transcript's raw JSON text first.

    Rows are written with ``json.dumps``, which escapes quotes, backslashes
    and non-ASCII, so only a plain ASCII needle is guaranteed to appear in the
    raw text the way it appears in a row's content.
    """
    return q.isascii() and '"' not in q and "\\" not in q


def find_match(entries: Sequence[Mapping[str, Any]], q: str) -> dict[str, str] | None:
    """The first visible row whose content holds ``q`` (case-insensitive), with a snippet."""
    needle = q.lower()
    for e in entries:
        if e.get("hidden") or e.get("role") not in _SEARCHED_ROLES:
            continue
        content = e.get("content")
        if not isinstance(content, str):
            continue
        at = content.lower().find(needle)
        if at < 0:
            continue
        start = max(0, at - SNIPPET_RADIUS)
        end = min(len(content), at + len(q) + SNIPPET_RADIUS)
        snippet = _WS.sub(" ", content[start:end]).strip()
        if start > 0:
            snippet = "…" + snippet
        if end < len(content):
            snippet += "…"
        return {"role": str(e.get("role")), "snippet": snippet}
    return None
