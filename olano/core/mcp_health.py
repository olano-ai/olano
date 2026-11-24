"""Periodic MCP connection health: keep OAuth grants alive, recover dead servers.

Everything else in :mod:`olano.core.mcp_setup` is **reactive**. Tokens are
renewed by whichever request first finds one expired; a server that was
unreachable at boot is retried when a human clicks *Reload MCP*. For a busy
connection that is enough. For the rest it produces exactly the complaint this
module exists to answer — "the agent keeps losing its MCP server and I have to
connect it again by hand":

- **Idle agents rot.** An agent whose Zoho tools go untouched overnight has an
  hour-old access token by morning. Nothing renews it until someone asks for a
  tool, and providers that expire refresh tokens on disuse may by then have
  nothing left to renew from.
- **A restart makes it permanent.** :func:`builder._build_mcp_tools` *skips* an
  OAuth server whose stored token reads ``expired``, so the tools are gone for
  the whole process lifetime — no retry, no backoff, no notice.
- **A transient outage is indistinguishable from a dead server.** A 30-second
  timeout during a boot sweep (slow container start, DNS blip, provider
  incident) drops that server's tools until the next manual reload.

The health loop closes all three: it renews stored grants *before* they lapse
(directly against the OAuth token endpoint, so it works while the MCP server
itself is down), and it asks the runtime to re-connect servers that are
degraded — with exponential backoff, so a permanently dead server costs one
attempt per backoff window instead of a rebuild every tick.

The decision logic here is deliberately free of runtime imports: it takes the
config entries plus the last build summary and returns *what should happen*.
:meth:`olano.runtime.server.RuntimeServer._mcp_health_loop` performs it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from olano.core.mcp_setup import (
    TOKEN_REFRESH_WINDOW,
    is_remote_transport,
    normalize_transport,
    oauth_token_state,
    refresh_oauth_token,
    token_needs_refresh,
)

logger = logging.getLogger(__name__)

# Server statuses (from the build summary) a re-connect could plausibly fix.
# `disabled`/`misconfigured` are the user's own doing; `connected` is fine.
RECOVERABLE_STATUSES = frozenset({"timeout", "error", "auth_required"})

# Backoff for re-connect attempts on a degraded server: first retry one tick
# later, then doubling to a daily floor. A server that is down for good must not
# rebuild its agent's graph every few minutes forever.
RECOVERY_BASE_DELAY = 300.0
RECOVERY_MAX_DELAY = 6 * 3600.0


@dataclass
class ServerReport:
    """One configured MCP server's health at the end of a tick."""

    url: str
    transport: str
    enabled: bool
    auth: str
    connected: bool
    status: str
    message: str
    token_state: str = "none"
    token_expires_at: float | None = None
    # What this tick did about it: none / refreshed / refresh_failed /
    # reload_requested / backoff.
    action: str = "none"
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "transport": self.transport,
            "enabled": self.enabled,
            "auth": self.auth,
            "connected": self.connected,
            "status": self.status,
            "message": self.message,
            "token_state": self.token_state,
            "token_expires_at": self.token_expires_at,
            "action": self.action,
            "detail": self.detail,
        }


@dataclass
class AgentReport:
    """One agent's MCP health, plus the reload the caller should perform."""

    agent: str
    checked_at: float
    servers: list[ServerReport] = field(default_factory=list)
    # URLs whose degraded state a re-connect may fix *now* (backoff elapsed).
    reload_urls: list[str] = field(default_factory=list)

    @property
    def needs_reload(self) -> bool:
        return bool(self.reload_urls)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "checked_at": self.checked_at,
            "servers": [s.to_dict() for s in self.servers],
            "reload_urls": list(self.reload_urls),
            "connected": sum(1 for s in self.servers if s.connected),
            "degraded": sum(1 for s in self.servers if s.enabled and not s.connected),
        }


@dataclass
class _Recovery:
    """Per-(agent, url) backoff state for re-connect attempts."""

    attempts: int = 0
    last_attempt: float = 0.0

    def due(self, now: float) -> bool:
        if self.attempts == 0:
            return True
        delay = min(RECOVERY_BASE_DELAY * (2 ** (self.attempts - 1)), RECOVERY_MAX_DELAY)
        return bool(now - self.last_attempt >= delay)


_RECOVERY: dict[tuple[str, str], _Recovery] = {}


def reset_recovery(agent: str | None = None) -> int:
    """Forget recorded backoff — an explicit retry must always be immediate.

    Called when the user reloads MCP by hand, and when a server comes back, so
    the next outage starts from the short delay again. Returns how many entries
    were dropped.
    """
    if agent is None:
        count = len(_RECOVERY)
        _RECOVERY.clear()
        return count
    keys = [key for key in _RECOVERY if key[0] == agent]
    for key in keys:
        del _RECOVERY[key]
    return len(keys)


def reset_recovery_for(agent: str, url: str) -> None:
    """Forget one server's backoff — it just came back, so the next outage
    starts from the short delay instead of wherever this one left off."""
    _RECOVERY.pop((agent, url), None)


def _note_attempt(agent: str, url: str, now: float) -> None:
    entry = _RECOVERY.setdefault((agent, url), _Recovery())
    entry.attempts += 1
    entry.last_attempt = now


def _clear_attempt(agent: str, url: str) -> None:
    _RECOVERY.pop((agent, url), None)


async def check_agent(
    agent: str,
    entries: list[dict[str, Any]],
    summary: list[dict[str, Any]],
    *,
    refresh_tokens: bool = True,
    refresh_window: float = TOKEN_REFRESH_WINDOW,
    recover: bool = True,
    now: float | None = None,
) -> AgentReport:
    """Assess (and maintain) one agent's MCP servers.

    *entries* are the agent's ``mcp_servers`` config dicts; *summary* is the
    per-server result of its last build (:func:`builder.get_mcp_summary`).

    Renews any OAuth grant close to expiry, then reports which degraded servers
    are due for a re-connect. Never raises: a failure on one server is recorded
    in that server's row and the sweep continues, because the loop that calls
    this must not be able to die on a single bad config entry.
    """
    stamp = time.time() if now is None else now
    report = AgentReport(agent=agent, checked_at=stamp)
    by_url = {row.get("url", ""): row for row in summary}

    for entry in entries:
        url = str(entry.get("url") or "")
        transport = normalize_transport(
            entry.get("transport"), command=entry.get("command") or None
        )
        enabled = entry.get("enabled", True) is not False
        live = by_url.get(url, {})
        server = ServerReport(
            url=url,
            transport=transport,
            enabled=enabled,
            auth=(entry.get("auth") or "none").strip().lower(),
            connected=bool(live.get("ok")),
            # The config wins for a disabled entry: the live row is from the
            # last build, which predates a disable that did not rebuild, and
            # its `auth_required` kept the bell ringing for a server the
            # operator had already switched off.
            status="disabled" if not enabled else str(live.get("status") or "unknown"),
            message=str(live.get("message") or ""),
        )
        remote = bool(url) and is_remote_transport(transport)
        if remote:
            state = oauth_token_state(agent, url)
            server.token_state = state["state"]
            server.token_expires_at = state["expires_at"]

        if not enabled:
            report.servers.append(server)
            continue

        if refresh_tokens and remote and token_needs_refresh(agent, url, window=refresh_window):
            await _maintain_token(agent, entry, server)

        if recover:
            _plan_recovery(agent, server, report, stamp)

        report.servers.append(server)

    return report


async def _maintain_token(agent: str, entry: dict[str, Any], server: ServerReport) -> None:
    """Renew one server's access token in place, recording the outcome."""
    from olano.core.mcp_setup import resolve_client_credentials

    try:
        # A static client's refresh sends the secret, which the entry holds as
        # a ${vault:…} reference — resolve it per agent like every other reader.
        client_id, client_secret = resolve_client_credentials(entry, agent)
        result = await refresh_oauth_token(
            agent,
            server.url,
            client_id=client_id,
            client_secret=client_secret,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("MCP token refresh raised for %s/%s: %s", agent, server.url, exc)
        server.action = "refresh_failed"
        server.detail = str(exc)[:200]
        return

    server.detail = str(result.get("message") or "")[:200]
    if result.get("ok"):
        server.action = "refreshed"
        state = oauth_token_state(agent, server.url)
        server.token_state = state["state"]
        server.token_expires_at = state["expires_at"]
        logger.info("MCP token renewed for %s → %s", agent, server.url)
        return
    server.action = "refresh_failed"
    # `rejected` is the only outcome that means the stored grant is actually
    # dead; the rest (unreachable token endpoint, unparseable response) are
    # this-tick problems and the next tick simply tries again.
    level = logger.warning if result.get("status") == "rejected" else logger.debug
    level("MCP token refresh failed for %s → %s: %s", agent, server.url, server.detail)


def _plan_recovery(agent: str, server: ServerReport, report: AgentReport, now: float) -> None:
    """Decide whether *server* should be re-connected on this tick."""
    if server.connected:
        _clear_attempt(agent, server.url)
        return
    if server.status not in RECOVERABLE_STATUSES:
        return
    if server.status == "auth_required" and server.token_state in {"none", "expired"}:
        # Genuinely unauthorized — reconnecting cannot invent consent, and
        # retrying would just rebuild the graph on a timer for nothing. The
        # dashboard already shows this as "Authorize".
        server.action = server.action if server.action != "none" else "needs_authorization"
        return
    entry = _RECOVERY.setdefault((agent, server.url), _Recovery())
    if not entry.due(now):
        server.action = server.action if server.action != "none" else "backoff"
        return
    _note_attempt(agent, server.url, now)
    server.action = "reload_requested"
    report.reload_urls.append(server.url)


async def maintain(
    agents: dict[str, list[dict[str, Any]]],
    summaries: dict[str, list[dict[str, Any]]],
    *,
    refresh_tokens: bool = True,
    refresh_window: float = TOKEN_REFRESH_WINDOW,
    recover: bool = True,
) -> dict[str, AgentReport]:
    """Run :func:`check_agent` across a fleet, one agent at a time.

    Serial on purpose: the expensive follow-up (a graph rebuild) is serialized
    by the runtime anyway, and a fleet-wide token stampede against one
    provider's token endpoint is exactly the thing that gets an OAuth client
    rate-limited.
    """
    reports: dict[str, AgentReport] = {}
    for name, entries in agents.items():
        try:
            reports[name] = await check_agent(
                name,
                entries,
                summaries.get(name, []),
                refresh_tokens=refresh_tokens,
                refresh_window=refresh_window,
                recover=recover,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("MCP health check failed for %s: %s", name, exc)
    return reports
