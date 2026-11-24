"""Who may flip an agent's external exposure or mint an access token.

Both actions hand people **outside** the deployment a way to drive an agent, so
the bar is higher than the ownership rule the ``self_*`` tools use: the turn must
be rooted at an **administrator who is present** — not a peer agent, not a
scheduled or Cortex run, not an inbound MCP/A2A caller, and (on a multi-user
deployment) not an agent owner who is merely a member.

Shared by the admin toolkit (``olano_set_agent_external_access``), the
self-configuration toolkit (``self_set_external_access``) and the magic-link
minting tool, so the three surfaces cannot drift apart.
"""

from __future__ import annotations

import json

_NO_HUMAN_SOURCES = ("peer", "scheduled", "cognition", "webhook", "mcp", "a2a", "agent_test")


def admin_human_denial() -> str | None:
    """``None`` when an administrator is on this turn, else a JSON refusal."""
    from olano.security.request_context import current_origin, get_request_context

    ctx = get_request_context()
    if ctx.source in _NO_HUMAN_SOURCES:
        return _deny(
            f"this is a {ctx.source} turn with no administrator in the conversation; "
            "exposing an agent or minting an access token needs an admin human"
        )
    origin = current_origin()
    if str(origin.get("root_source") or "human") != "human" or not origin.get("interactive", True):
        return _deny(
            "this turn did not start with a person; exposing an agent or minting an access "
            "token needs an admin human"
        )
    try:
        from olano.runtime.auth import multi_user_enabled
    except Exception:  # pragma: no cover - runtime package unavailable (CLI-only)
        multi_user_enabled = None  # type: ignore[assignment]
    if multi_user_enabled is not None and multi_user_enabled():
        if not ctx.is_admin:
            return _deny(
                "only an administrator may expose an agent to outside callers or mint an "
                "access token; ask an admin to do it from the dashboard's External access tab"
            )
        return None
    # Single-user install: the operator is the only human. A dashboard / CLI /
    # local turn is theirs by construction; a messenger turn counts only when the
    # sender is on the channel's trusted (owner) list.
    if ctx.channel in ("", "local", "cli", "dashboard", "api"):
        return None
    if ctx.is_owner:
        return None
    return _deny(
        "only the deployment's owner may expose an agent to outside callers or mint an "
        "access token; this chat is not on the trusted-senders list"
    )


def _deny(why: str) -> str:
    return json.dumps(
        {
            "error": f"refused: {why}",
            "agent_guidance": (
                "Do not retry. Tell the person that external access (MCP / A2A exposure and "
                "access tokens) is changed by an administrator in the dashboard: agent → "
                "External access."
            ),
        },
        ensure_ascii=False,
    )
