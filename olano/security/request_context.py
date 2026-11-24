"""Request-scoped security context for tool policy evaluation."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class RequestContext:
    """Execution context used by security middleware and tool wrappers."""

    source: str = "local"
    channel: str = "local"
    user_id: str = "owner"
    session_id: str = ""
    authenticated: bool = True
    is_owner: bool = True
    is_admin: bool = False
    is_group: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


_REQUEST_CONTEXT: ContextVar[RequestContext | None] = ContextVar(
    "lionclaw_request_context",
    default=None,
)


def get_request_context() -> RequestContext:
    """Return the active request context."""
    current = _REQUEST_CONTEXT.get()
    return current if current is not None else RequestContext()


# ── Call origin (for A2A / autonomous HITL resolution) ───────────────────────
# A small descriptor carried in ``metadata["origin"]`` that records where a call
# chain *started* (human chat / scheduled task / cognition run) and whether a
# human is reachable up the chain. Unlike ``source`` — which becomes ``"peer"``
# at every A2A hop — the origin is propagated forward by ``talk_to_agent`` so a
# delegated agent can resolve HITL correctly. See ``security/hitl_policy.py``.
ORIGIN_KEY = "origin"

# ``metadata[HIDDEN_PROMPT_KEY] = True`` marks a turn whose *prompt* is written
# for the model, not for the human reading the thread: the runner stamps the
# persisted ``user`` row ``hidden`` and the transcript readers skip it, while
# the checkpoint keeps it as context. The onboarding greeting is the case — the
# platform tells Agent Father "a new owner just signed up, say hi", and the
# owner should see the hello, not the instruction that produced it.
HIDDEN_PROMPT_KEY = "hidden_prompt"


def make_origin(
    root_source: str,
    interactive: bool,
    *,
    root_user_id: str = "",
    root_session_id: str = "",
    hop_count: int = 0,
    call_path: list[str] | None = None,
) -> dict[str, Any]:
    """Build an origin descriptor. ``root_source`` ∈ human|scheduled|cognition|peer-unknown."""
    return {
        "root_source": root_source,
        "interactive": bool(interactive),
        "root_user_id": root_user_id,
        "root_session_id": root_session_id,
        "hop_count": int(hop_count),
        "call_path": list(call_path or []),
    }


def _infer_origin(ctx: RequestContext) -> dict[str, Any]:
    """Best-effort origin for a context that didn't set one explicitly."""
    if ctx.source == "scheduled":
        return make_origin("scheduled", False, root_session_id=ctx.session_id)
    if ctx.source == "peer":
        # A peer hop with no propagated origin → unknown root; treat as headless.
        return make_origin("peer-unknown", False, root_session_id=ctx.session_id)
    # api / platform / channel / local → a human sits at the root of the chain.
    return make_origin("human", True, root_user_id=ctx.user_id, root_session_id=ctx.session_id)


def current_origin() -> dict[str, Any]:
    """Return the active call origin (explicit ``metadata['origin']`` or inferred)."""
    ctx = get_request_context()
    origin = (ctx.metadata or {}).get(ORIGIN_KEY)
    return origin if isinstance(origin, dict) else _infer_origin(ctx)


def propagated_origin(caller_name: str) -> dict[str, Any]:
    """Origin to carry into a peer (A2A) hop: inherit the root, bump the hop
    count, and append the caller to the call path."""
    origin = dict(current_origin())
    origin["hop_count"] = int(origin.get("hop_count", 0)) + 1
    path = list(origin.get("call_path", []))
    if caller_name:
        path.append(caller_name)
    origin["call_path"] = path
    return origin


# ── Per-agent ownership (for the self_config toolkit) ────────────────────────
# ``is_owner`` above is NOT per-agent: on the dashboard it is ``user.is_admin``
# and on a messenger it is channel-allowlist membership, so neither can answer
# "does this human own the agent they are talking to". That question needs both
# the human AND the agent name, and only the entry point holds both — by the
# time a tool runs, the HTTP request is long gone (the SSE chat body executes
# after Starlette's middleware has exited).
#
# So the entry point resolves it eagerly — ``runtime/auth.can_admin_agent``,
# the same predicate guarding raw config reads and connector mutations — and
# leaves the answer here as one bool. Absent means "nobody established it",
# which is not the same as False: the gate decides what to do with each,
# per surface (``self_config_tools._ownership_gate``).
#
# Metadata, not a dataclass field, so every existing reader of RequestContext
# keeps its shape — and because ``push_request_context`` merges metadata, a
# nested push inherits it. That inheritance is why the gate tests ``source``
# for peer/scheduled/cognition hops BEFORE reading this key.
AGENT_ADMIN_KEY = "agent_admin"


def context_agent_admin() -> bool | None:
    """Whether the human on this turn may administer the agent they addressed.

    ``None`` when no entry point established it (the CLI, a background sweep,
    or any path that never pushed a context).
    """
    value = (get_request_context().metadata or {}).get(AGENT_ADMIN_KEY)
    return value if isinstance(value, bool) else None


@contextmanager
def push_request_context(**updates: Any) -> Iterator[RequestContext]:
    """Temporarily override fields on the active request context."""
    current = get_request_context()
    metadata = updates.pop("metadata", None)
    merged_metadata = dict(current.metadata)
    if metadata:
        merged_metadata.update(metadata)
    next_ctx = replace(current, metadata=merged_metadata, **updates)
    token = _REQUEST_CONTEXT.set(next_ctx)
    try:
        yield next_ctx
    finally:
        # ``reset(token)`` is only valid in the same asyncio Context that created
        # the token. When this sync context manager wraps an ``await`` that is
        # cancelled (e.g. ``asyncio.wait_for`` timeout) or otherwise abandoned,
        # the generator can be GC-finalized in a *different* Context, where
        # ``reset`` raises ``ValueError: Token was created in a different
        # Context``. That original Context is being torn down anyway, so its copy
        # of the var needs no reset — swallow only that specific, benign error.
        try:
            _REQUEST_CONTEXT.reset(token)
        except ValueError:
            pass
