"""Per-turn record of failing tool calls, shared by the runner and the middleware.

The runner's circuit breaker (``AgentRunner.astream_events``) stops a turn once the
*identical* tool call has failed ``tool_error_retry_limit`` times, so an agent that
keeps re-issuing a call nothing has changed cannot loop forever. Two things used
to make that cut-off harsher than it needed to be:

- The model was never told the breaker was close. Its third identical failure
  ended the turn with a canned message in place of the result it was waiting
  for, so the one step where it would have read the error most carefully is
  the step it never got.
- The count never reset. An agent that edited a file between two runs of a
  failing test command was in a new situation each time, and still got cut off
  on the third red run, which is simply how a fix-and-rerun cycle goes.

This module holds what both sides need to agree on so neither can drift:

- :func:`tool_call_signature`: the one spelling of "the identical call".
- :func:`is_failed_tool_result`: the one definition of "this result failed",
  by status or by the markers a guarded tool and a block leave on the content.
- :data:`WRITE_TOOL_NAMES`: the tools whose success means the workspace moved,
  which resets every count (the runner's breaker and this ledger alike).
- :class:`TurnToolFailures`: the ledger the runner binds for the turn
  (:func:`bind_turn_tool_failures`) and the security middleware reads
  (:func:`current_turn_tool_failures`) to append a notice to a failing result
  once one more identical failure would end the turn. The middleware counts
  results and the runner counts model steps, so on parallel identical calls
  the notice can arrive a step early; it is advice, the runner's count decides.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

# A successful call to one of these means the workspace changed since the last
# failure, so repeating a previously failing command is a new attempt, not a
# loop. A write through the shell (``sed -i``) is not detected; the breaker
# then behaves as before.
WRITE_TOOL_NAMES = frozenset({"write_file", "edit_file"})

# Content prefixes that mark a failed result carried on a ``status="success"``
# message: the runner's own shell block, deepagents' validation errors, the
# central error boundary and the connector-auth repair.
FAILED_RESULT_PREFIXES = ("[blocked]", "Error:", "[connector-auth]")

REPEAT_FAILURE_MARKER = "[repeat-failure]"


def tool_call_signature(tool_name: str, args_str: str) -> str:
    """The key the breaker counts on: tool name plus the serialized arguments."""
    return f"{tool_name}\x00{args_str or ''}"


def args_signature_text(args: Any) -> str:
    """Serialize tool args the way the runner does for the signature."""
    if not args:
        return ""
    if isinstance(args, str):
        return args
    try:
        return json.dumps(args)
    except (TypeError, ValueError):
        return str(args)


def is_failed_tool_result(status: Any, content: str) -> bool:
    """True when a tool result is a failure the breaker should count."""
    if status == "error":
        return True
    text = (content or "").strip()
    if text.startswith(FAILED_RESULT_PREFIXES):
        return True
    from olano.tools._error_boundary import TOOL_ERROR_MARKER

    return text.startswith(TOOL_ERROR_MARKER)


def strip_repeat_failure_notice(content: str) -> str:
    """Remove the notice this ledger appended, for messages that quote a result."""
    idx = content.find(REPEAT_FAILURE_MARKER)
    if idx < 0:
        return content
    return content[:idx].rstrip()


@dataclass
class TurnToolFailures:
    """Failures the middleware has returned this turn, by call signature."""

    limit: int
    failures: dict[str, int] = field(default_factory=dict)

    def record_failure(self, signature: str) -> int:
        count = self.failures.get(signature, 0) + 1
        self.failures[signature] = count
        return count

    def record_success(self, tool_name: str) -> None:
        """A successful write resets every count: the situation changed."""
        if tool_name in WRITE_TOOL_NAMES and self.failures:
            self.failures.clear()

    def notice(self, tool_name: str, count: int) -> str | None:
        """The warning to append once one more identical failure ends the turn.

        ``None`` while the breaker is off (``limit`` 0), when it would fire on
        the first failure anyway (``limit`` 1, nothing to warn ahead of), or
        while there is still more than one attempt left.
        """
        if self.limit <= 1 or count < self.limit - 1:
            return None
        return (
            f"{REPEAT_FAILURE_MARKER} This exact `{tool_name}` call has now failed "
            f"{count} time{'s' if count != 1 else ''} in this turn. One more identical "
            "failing call ends the turn. Before calling it again: read the full "
            "output or log, fix the cause (edit the file, change the command or "
            "its arguments), then re-run. A successful file edit resets this count."
        )


TURN_TOOL_FAILURES: ContextVar[TurnToolFailures | None] = ContextVar(
    "olano_turn_tool_failures", default=None
)


def current_turn_tool_failures() -> TurnToolFailures | None:
    return TURN_TOOL_FAILURES.get()


@contextmanager
def bind_turn_tool_failures(ledger: TurnToolFailures | None) -> Iterator[None]:
    token = TURN_TOOL_FAILURES.set(ledger)
    try:
        yield
    finally:
        TURN_TOOL_FAILURES.reset(token)
