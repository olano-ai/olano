"""Multi-bot group threads — one human-visible conversation, several agents.

Olano already had every primitive for agents talking to each other
(``talk_to_agent``, ``delegate_task``, the A2A activity log, the org chart);
what it had no notion of was a *thread a human watches* with more than one
agent in it. This package is that notion and nothing more: a roster of two to
six agents, an append-only message log with an author on every row, and a
turn-taking rule ("a posted message is answered by the addressed member").

Deliberately small, and deliberately not a second agent loop: a turn is
handed to the same :class:`~olano.core.runner.AgentRunner` the dashboard chat
uses, on a group-scoped thread id, so checkpointing, streaming isolation,
credentials and cost accounting all behave exactly as they do in a 1:1 chat.

``cascade.py`` adds the second half of the notion: a reply that addresses
another member by ``@name`` gives that member the next turn, and the agents
carry on without the human — under a hop cap, a wall-clock budget, ping-pong
detection and a per-hop access re-check, every one of which writes its reason
into the transcript when it fires.

Two independent switches in ``~/.olano/config.yaml``, **both default on**:
``groups.enabled`` (while it is off ``olano/runtime/groups_routes.py`` answers
404 on every path and nothing here is ever constructed) and
``groups.autonomous`` (while *it* is off, a human message is answered by
exactly one member and the thread stops there).
"""

from olano.groups.cascade import (
    REGISTRY,
    CascadeLimits,
    CascadeResult,
    CascadeState,
    next_speaker,
    run_cascade,
)
from olano.groups.store import GroupError, GroupStore, get_group_store

__all__ = [
    "REGISTRY",
    "CascadeLimits",
    "CascadeResult",
    "CascadeState",
    "GroupError",
    "GroupStore",
    "get_group_store",
    "next_speaker",
    "run_cascade",
]
