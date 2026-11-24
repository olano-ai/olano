"""Keep a nested model call out of the caller's live stream.

Every model call this process makes inside a running LangGraph turn inherits the
turn's streaming callbacks, because ``langchain_core.ensure_config()`` merges the
ambient ``var_child_runnable_config`` into it. That ambient config carries
LangGraph's ``StreamMessagesHandler``, so a model invoked *inside a tool* pushes
its tokens into the caller's ``stream_mode="messages"`` stream — where
:meth:`olano.core.runner.AgentRunner.astream_events` appends them to the turn's
answer and the channel renders them as the agent's reply.

Two observed shapes of that bug:

- ``talk_to_agent`` invoking a peer mid-turn, so a caller's summary and a peer's
  raw reply arrived shuffled together in one Telegram message;
- ``image_describe`` transcribing two photos from one merged photo burst.
  LangGraph runs the two tool calls concurrently, both vision models streamed
  into the parent, and the user got two transcriptions interleaved
  chunk-by-chunk into a single unreadable answer.

The rule this module exists to make easy: **a model call made inside the graph is
never part of the answer, so it must never reach the stream.** Reach for
:func:`detached_runnable_config` around it and hand the call
:func:`nostream_config`. The two are independent belts — the first stops the
handler from being inherited at all, the second marks the run so LangGraph's
messages mode skips it even if a future langchain resolves the config
differently — and the runner drops nostream-tagged chunks as a third
(see ``AgentRunner._is_internal_chunk``).

Deliberately dependency-light: stdlib plus ``langchain_core.runnables.config``.
Tools import it at call time, and ``olano.core.__init__`` pulls builder+runner,
so anything heavier here would be paid by every toolkit that needs isolation.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:  # runtime-free: callers import this module on the hot path
    from langchain_core.runnables import RunnableConfig

# LangGraph's own marker for "do not stream this run's tokens":
# ``langgraph.constants.TAG_NOSTREAM``. Its ``_messages.StreamMessagesHandler``
# checks it in ``on_chat_model_start`` and, when present, never registers the
# run — so not a single token of that call reaches ``stream_mode="messages"``.
#
# Written as a literal rather than imported so this module stays cheap for the
# toolkits that import it at call time. ``tests/test_stream_isolation.py`` pins
# the two in sync, so a rename upstream fails in CI instead of silently
# un-suppressing every nested call. Note the value is a bare ``"nostream"`` —
# NOT ``"langsmith:nostream"``, which is a different (hidden-from-tracing) tag.
NOSTREAM_TAG = "nostream"


@contextlib.contextmanager
def detached_runnable_config() -> Iterator[None]:
    """Run a nested call detached from the caller's LangChain run context.

    Resetting ``var_child_runnable_config`` for the duration makes the nested
    call a clean top-level run whose tokens stay inside its own invocation. It is
    a **no-op** for an already-top-level caller (scheduler, API, CLI, ACP): their
    ambient config is already empty, so the reset simply restores that state.
    """
    from langchain_core.runnables.config import var_child_runnable_config

    token = var_child_runnable_config.set({})
    try:
        yield
    finally:
        var_child_runnable_config.reset(token)


def nostream_config(base: RunnableConfig | dict[str, Any] | None = None) -> RunnableConfig:
    """*base* (or a fresh config) carrying LangGraph's ``nostream`` tag.

    Merges rather than replaces: a caller that already passes ``configurable``,
    a thread id or its own tags keeps all of them, and the caller's own dict is
    left alone. Idempotent — tagging an already-tagged config changes nothing.
    """
    config: dict[str, Any] = dict(base or {})
    tags = list(config.get("tags") or [])
    if NOSTREAM_TAG not in tags:
        tags.append(NOSTREAM_TAG)
    config["tags"] = tags
    return cast("RunnableConfig", config)


def has_nostream_tag(obj: Any) -> bool:
    """True when *obj* (a config dict, or stream metadata) carries the tag.

    Accepts anything dict-like so the runner can ask the same question of
    LangGraph's per-chunk stream metadata without knowing where the tag landed.
    """
    if not isinstance(obj, dict):
        return False
    tags = obj.get("tags")
    if isinstance(tags, str):
        return tags == NOSTREAM_TAG
    if isinstance(tags, list | tuple | set):
        return NOSTREAM_TAG in tags
    return False
