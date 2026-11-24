"""Resolve a thread's owning agent and its origin (interactive vs. autonomous).

The checkpoint store keys rows by an **agent-scoped** ``thread_id``
(``<agent>::<human_id>`` — see :func:`olano.core.checkpoints.scope_thread`) so
two agents the same human talks to never share a checkpoint lineage. This module's
identity is therefore the *scoped* key: :func:`build_thread_agent_index` emits scoped
keys (rebuilt from the per-agent transcript dirs
``<home>/sessions/<agent>/<stem>.jsonl``), so it joins directly against the scoped
keys physically present in ``threads.db``. Autonomous cognition threads bypass the
runner and keep a raw ``cog::<agent>`` id, handled by the fallback in
:func:`resolve_agent`.
"""

from __future__ import annotations

from pathlib import Path

from olano.config.home import get_olano_paths
from olano.core.checkpoints import scope_thread

ORIGIN_AUTONOMOUS = "autonomous"
ORIGIN_INTERACTIVE = "interactive"
ORIGIN_PEER = "peer"
ORIGIN_SCHEDULED = "scheduled"

# Origins that are not a human typing in the chat box.
NON_INTERACTIVE = {ORIGIN_AUTONOMOUS, ORIGIN_PEER, ORIGIN_SCHEDULED}


def build_thread_agent_index(home: str | Path | None = None) -> dict[str, str]:
    """Map **scoped** ``<agent>::<stem> -> agent`` by enumerating per-agent transcript dirs.

    The JSONL filename is the human-facing ``stem`` (e.g. a Telegram chat id); the
    checkpoint row for it lives under the agent-scoped key ``<agent>::<stem>``. We key
    the index by that scoped form so it joins directly against ``threads.db``. This
    also fixes a latent bug: previously two agents that both had a ``<stem>.jsonl``
    for the same human id collapsed (last writer wins); scoped keys keep them distinct.
    """
    sessions_dir = get_olano_paths(home).sessions_dir
    index: dict[str, str] = {}
    if not sessions_dir.exists():
        return index
    for agent_dir in sessions_dir.iterdir():
        if not agent_dir.is_dir():
            continue
        agent = agent_dir.name
        for f in agent_dir.glob("*.jsonl"):
            index[scope_thread(agent, f.stem)] = agent
    return index


def owning_agent_of(name: str) -> str:
    """Collapse an ephemeral cognition-worker name to the agent that owns it.

    Workers are named ``{parent}__cog__{label}__{hex6}`` (see
    ``cognition/worker._build_worker_config``); anything without the marker is
    already a real agent name and passes through unchanged.
    """
    return name.split("__cog__", 1)[0] if "__cog__" in name else name


def resolve_agent(thread_id: str, index: dict[str, str]) -> str:
    """Agent that owns a (scoped) thread; falls back to parsing the ``cog::`` id, else unknown.

    The cog fallback must collapse the worker name: ``cog::merli__cog__pulse_agent__ab12cd``
    belongs to ``merli`` — attributing it to the ephemeral child name would match
    no member grant and fragment every by-agent rollup into one-run buckets.
    """
    direct = index.get(thread_id)
    if direct:
        return direct
    if "cog::" in thread_id:
        tail = thread_id.split("cog::", 1)[1]
        return owning_agent_of(tail.split("::", 1)[0]) or "unknown"
    return "unknown"


def classify_origin(thread_id: str, first_entry: dict | None) -> str:
    """Classify how a thread was started, from its id + first transcript entry."""
    tid = thread_id or ""
    if "cog::" in tid:
        return ORIGIN_AUTONOMOUS
    entry = first_entry or {}
    if entry.get("role") == "peer_inbound":
        return ORIGIN_PEER
    content = str(entry.get("content") or "")
    head = content[:60].lower()
    if "scheduled task" in head or head.startswith("[autonomous"):
        return ORIGIN_SCHEDULED
    return ORIGIN_INTERACTIVE
