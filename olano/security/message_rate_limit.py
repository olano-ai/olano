"""Per-agent and per-platform inbound-message rate limiting.

Ingress-only. This is called from the platform / channel handlers and the
dashboard / API chat endpoints (external inbound). Scheduled, heartbeat,
cognition and other autonomous turns run through
:meth:`olano.core.runner.AgentRunner.ainvoke` and never reach these call
sites, so they are exempt by construction.

Two independent scopes are enforced per inbound message:

- **agent scope** — every inbound message for this agent, across all platforms
  (``msg:{agent}:agent``)
- **platform scope** — inbound messages for this agent on one platform
  (``msg:{agent}:{platform}``)

If *either* scope is over its cap the message is rejected. Config precedence per
scope mirrors :func:`olano.config.system_settings.effective_heartbeat_interval`:
a per-agent cap wins; otherwise the home-wide default; otherwise unlimited. The
feature is **opt-in** — with nothing configured every scope is unlimited, so
existing deployments are unaffected.

The caps span minute / hour / day windows and are enforced with the shared
process-local :data:`~olano.security.rate_limit.RUNTIME_RATE_LIMITER`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from olano.config.system_settings import (
    MessageWindows,
    RateLimitSettings,
    load_rate_limit_settings,
)
from olano.security.rate_limit import RUNTIME_RATE_LIMITER, RateLimitDecision

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig

logger = logging.getLogger(__name__)


def _parse_windows(raw: Any) -> MessageWindows | None:
    """Coerce a loose mapping (from ``security.rate_limits``) into
    :class:`MessageWindows`. Returns ``None`` when missing/empty/malformed."""
    if not isinstance(raw, dict) or not raw:
        return None
    try:
        return MessageWindows.model_validate(raw)
    except Exception:
        logger.debug("message_rate_limit: ignoring malformed windows: %r", raw)
        return None


def _agent_rate_limits(config: AgentConfig | None) -> tuple[dict[str, Any], bool]:
    """Return ``(rate_limits_dict, explicitly_configured)``.

    ``explicitly_configured`` is True only when the agent set a ``rate_limits``
    block in ``agent.json5`` (i.e. it is in ``model_fields_set``) — this lets the
    legacy ``requests_per_minute`` slot be honored *only* when the author opted
    in, never from the schema default of 60.
    """
    security = getattr(config, "security", None)
    rate_limits = dict(getattr(security, "rate_limits", {}) or {})
    explicit = bool(
        security is not None and "rate_limits" in getattr(security, "model_fields_set", set())
    )
    return rate_limits, explicit


def _resolve_agent_windows(
    rate_limits: dict[str, Any], explicit: bool, home: RateLimitSettings
) -> MessageWindows:
    """Agent-scope caps: per-agent ``messages`` (with legacy ``requests_per_minute``
    fallback) → home-wide ``messages`` → unlimited."""
    windows = _parse_windows(rate_limits.get("messages"))
    if windows is not None:
        return windows
    if explicit:
        legacy = rate_limits.get("requests_per_minute")
        if isinstance(legacy, int) and legacy > 0:
            return MessageWindows(per_minute=legacy)
    return home.messages


def _resolve_platform_windows(
    rate_limits: dict[str, Any], platform: str, home: RateLimitSettings
) -> MessageWindows:
    """Platform-scope caps: per-agent ``per_platform[platform]`` →
    home-wide ``per_platform[platform]`` → unlimited."""
    per_platform = rate_limits.get("per_platform")
    if isinstance(per_platform, dict):
        windows = _parse_windows(per_platform.get(platform))
        if windows is not None:
            return windows
    home_windows = home.per_platform.get(platform)
    if home_windows is not None:
        return home_windows
    return MessageWindows()


def check_message_rate_limit(
    agent_name: str,
    platform: str,
    config: AgentConfig | None,
    *,
    home: RateLimitSettings | None = None,
) -> RateLimitDecision:
    """Decide whether an inbound message for ``agent_name`` on ``platform`` is
    allowed under the per-agent and per-platform caps.

    Returns an allowed decision immediately when nothing is configured (the
    common case). Otherwise the agent and platform scopes are peeked first and
    only committed together once both pass, so a message rejected by one scope
    never inflates the other's counters. ``home`` is loaded on demand but may be
    injected (tests / hot paths).
    """
    platform = (platform or "unknown").lower()
    home = home or load_rate_limit_settings()
    rate_limits, explicit = _agent_rate_limits(config)

    agent_limits = _resolve_agent_windows(rate_limits, explicit, home).to_limits()
    platform_limits = _resolve_platform_windows(rate_limits, platform, home).to_limits()

    if not agent_limits and not platform_limits:
        return RateLimitDecision(allowed=True)

    agent_key = f"msg:{agent_name}:agent"
    platform_key = f"msg:{agent_name}:{platform}"

    # Peek both scopes; commit only when both pass.
    if agent_limits:
        decision = RUNTIME_RATE_LIMITER.check_windows(agent_key, agent_limits, record=False)
        if not decision.allowed:
            return decision
    if platform_limits:
        decision = RUNTIME_RATE_LIMITER.check_windows(platform_key, platform_limits, record=False)
        if not decision.allowed:
            return decision
    if agent_limits:
        RUNTIME_RATE_LIMITER.check_windows(agent_key, agent_limits, record=True)
    if platform_limits:
        RUNTIME_RATE_LIMITER.check_windows(platform_key, platform_limits, record=True)
    return RateLimitDecision(allowed=True)


def rate_limit_notice(decision: RateLimitDecision) -> str:
    """User-facing 'slow down' reply for a rejected inbound message."""
    return (
        "⏳ Rate limit reached — too many messages in a short window. "
        f"Please try again in {decision.retry_after}s."
    )
