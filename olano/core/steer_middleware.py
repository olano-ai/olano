"""The graph half of mid-turn steering — see :mod:`olano.core.steering`.

``SteerMiddleware.before_model`` runs as its own LangGraph node right before
every model call of the main agent loop (langchain wires the first middleware
that defines ``before_model`` as the node the ``tools`` edge returns to, so
the builder lists this one ahead of the security middleware). It asks the
registry for steers accepted for the running turn and appends each as a
``HumanMessage`` — a real state write, so the note lands in the checkpoint
after the tool results it followed and stays in the thread's history for every
later turn, exactly where the user said it.

Three properties are load-bearing:

* **Never in a subgraph.** A ``task`` subagent runs under the same thread id
  and, with a shared request context, would otherwise drain the user's aside
  into a helper that returns a summary the main agent never questions. The
  hook only acts in the root namespace (``checkpoint_ns == ""``).
* **Keyed by the agent name captured at build.** A peer callee reached through
  ``talk_to_agent`` runs its own graph with its own middleware instance, so it
  can never consume the caller's steer even though it inherits the caller's
  request context.
* **Idempotent.** ``drain`` moves the steer to *delivered* before the node
  returns, and the message carries a deterministic id (``steer:<id>``): a
  replayed node finds nothing, and even a double write would replace rather
  than duplicate under the messages reducer.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

from olano.core.steering import REGISTRY, steer_message_content

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig

logger = logging.getLogger(__name__)


def _in_root_graph() -> bool:
    """True unless this hook is running inside a LangGraph subgraph."""
    try:
        from langgraph.config import get_config

        cfg = get_config()
    except Exception:
        # Outside any graph (unit tests, a direct call): treat as root.
        return True
    try:
        ns = (cfg.get("configurable") or {}).get("checkpoint_ns", "")
    except Exception:
        return True
    return not ns


def _current_thread_id() -> str:
    try:
        from olano.security.request_context import get_request_context

        return get_request_context().session_id or ""
    except Exception:
        return ""


class SteerMiddleware(AgentMiddleware):
    """Append the turn's pending steers to the state before each model call."""

    def __init__(self, agent_name: str) -> None:
        super().__init__()
        self.agent_name = agent_name

    @property
    def name(self) -> str:
        return "SteerMiddleware"

    def _drain(self) -> dict[str, Any] | None:
        try:
            thread_id = _current_thread_id()
            if not thread_id or not _in_root_graph():
                return None
            drained = REGISTRY.drain(self.agent_name, thread_id)
        except Exception:  # a steer must never cost the turn
            logger.debug("steer drain skipped", exc_info=True)
            return None
        if not drained:
            return None
        messages = [
            HumanMessage(
                content=steer_message_content(m.text),
                id=f"steer:{m.id}",
                additional_kwargs={"olano_steer_id": m.id},
            )
            for m in drained
        ]
        logger.debug(
            "steer: delivered %d message(s) to %s on thread %s",
            len(messages),
            self.agent_name,
            thread_id,
        )
        return {"messages": messages}

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self._drain()

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self._drain()


def build_steer_middleware(config: AgentConfig) -> list[AgentMiddleware]:
    """Always one instance — steering has nothing to configure; with no steer
    pending the hook is one contextvar read and one dict lookup per model call."""
    return [SteerMiddleware(config.name)]
