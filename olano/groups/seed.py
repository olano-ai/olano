"""Seeding a new group from an existing one-to-one conversation.

The gap this closes: you are talking to one bot, you want a second one in on
it. Creating an empty group and re-explaining is the thing people actually do
instead, so "add a bot to this chat" has to arrive carrying the conversation
or it does not answer the request it was asked for.

**What carries over is the transcript, never the model context.** Those are two
different stores and only one of them can be copied. A 1:1 chat's resumable
context lives in the checkpointer under that agent's thread id; a group gives
every member its own *group-scoped* thread id, so each one necessarily starts
fresh. What this module moves is the readable record — attributed rows in the
group's transcript — which is exactly what the addressed member is shown on its
turn (``groups_routes.compose_turn``). So the second bot reads the history as
history, and the first bot, which lived through it, reads it again the same
way. That symmetry is deliberate: the alternative is a group where one member
silently knows more than the transcript says it does.

**Bounded, because a transcript is not a context window.** A long-running 1:1
thread can hold hundreds of turns, and every one copied is a row the group
store keeps forever and every member re-reads on every turn. :data:`MAX_CARRY`
caps it and the tail is what survives — the end of a conversation is what the
new member needs to be useful in it.

**Attribution is preserved, not re-derived.** The human's rows stay the
human's, the agent's stay that agent's. A copied row is never re-authored to
the person who pressed the button: they may be reading someone else's thread
(an admin), and a transcript that lies about who said what is worse than no
transcript. Rows that are neither (``system`` notes, tool traffic) are dropped
rather than flattened into someone's voice.
"""

from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: How many of the conversation's most recent turns are copied into the new
#: group. A ceiling rather than a target: shorter threads carry entirely.
MAX_CARRY = 20

#: Per-message character cap. One pasted document should not become the whole
#: group transcript; the tail is trimmed with an ellipsis so the row still
#: reads as what it was.
MAX_BODY_CHARS = 4000

#: Roles worth carrying. Tool calls and tool results are on disk but are not
#: part of the readable conversation, and a `system` row is runtime bookkeeping
#: that would arrive in the group as a message nobody sent.
_CARRIED_ROLES = ("user", "assistant")


def read_session_rows(path: Path) -> list[dict[str, Any]]:
    """Parse a session JSONL (plain or gzipped), skipping unreadable lines.

    Mirrors the runtime's own reader: housekeeping compresses old sessions to
    ``<thread>.jsonl.gz``, and a truncated final line (a crash mid-write) must
    cost that line, never the file.
    """
    if not path.exists():
        return []
    try:
        if path.suffix == ".gz":
            text = gzip.decompress(path.read_bytes()).decode("utf-8")
        else:
            text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        logger.debug("read_session_rows: unreadable session %s: %s", path, e)
        return []
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _clip(body: str) -> str:
    text = str(body or "").strip()
    if len(text) <= MAX_BODY_CHARS:
        return text
    return text[:MAX_BODY_CHARS].rstrip() + "…"


def build_carry_over(
    rows: list[dict[str, Any]],
    *,
    agent: str,
    human: str,
    limit: int = MAX_CARRY,
) -> list[dict[str, str]]:
    """The attributed rows to append to a new group, oldest first.

    ``agent`` authors the assistant turns and ``human`` (a ``human-<slug>``
    principal) the user turns. Empty bodies are dropped — a turn that carried
    only an attachment or a tool call has nothing to say in a transcript, and
    the group store refuses a blank message anyway.
    """
    carried: list[dict[str, str]] = []
    for row in rows:
        role = row.get("role")
        if role not in _CARRIED_ROLES:
            continue
        body = _clip(row.get("content", ""))
        if not body:
            continue
        carried.append(
            {
                "author": human if role == "user" else agent,
                "author_kind": "human" if role == "user" else "agent",
                "body": body,
            }
        )
    # The tail: the end of a conversation is what a new member needs.
    bound = max(0, int(limit))
    return carried[-bound:] if bound else []


def carry_over_note(agent: str, carried: int, total: int) -> str:
    """The system row that opens a seeded group.

    Says where the history came from and whether it is all of it, so nobody
    reads a trimmed conversation as the whole one. Written as a ``system``
    message rather than in someone's voice — no participant said this.
    """
    if carried >= total:
        return (
            f"This group was started from a one-to-one conversation with {agent}. "
            f"The {carried} message(s) above came from it."
        )
    return (
        f"This group was started from a one-to-one conversation with {agent}. "
        f"The most recent {carried} of its {total} messages came from it; "
        "the earlier ones stayed in that conversation."
    )
