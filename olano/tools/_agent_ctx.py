"""Per-turn agent identity contextvar for the Google toolkit.

Lives in its own module so binding it (AgentRunner does this every turn, and
``_credentials.agent_runtime_context`` on every execution path) doesn't import
``google_tools`` — that module builds ~50 langchain tools at import time,
~0.35s the first turn would otherwise pay just to set this variable.
``google_tools`` re-exports it, so ``google_tools.AGENT_CTX`` remains the same
object for existing imports.
"""

from __future__ import annotations

import contextvars

# Set by AgentRunner before each invocation so tools use the correct token scope.
# None → fall through to system token.
AGENT_CTX: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "lionclaw_google_agent", default=None
)
