"""Enforcement for the egress lists — every outbound call a tool makes.

:mod:`olano.security.egress` decides *what* an agent may reach. This module is
*where that decision bites*. The gate in :mod:`olano.security.ssrf` only runs in
the five tools that call ``assert_safe_outbound_url``; the other ~80 modules
that reach the network build their own ``httpx`` client and were never governed
by anything. A domain list that covers six tools out of eighty is not an egress
control — it is a setting that reads like one, which is worse than none.

So enforcement moves under the HTTP libraries themselves. Four chokepoints
cover every outbound request the runtime makes:

* ``httpx.Client.send`` / ``httpx.AsyncClient.send`` — every ``httpx.get``,
  ``httpx.post``, ``client.stream`` and SDK call funnels through these two.
* ``aiohttp.ClientSession._request`` — the one method every aiohttp verb calls.
* ``urllib.request.OpenerDirector.open`` — what ``urlopen`` drives.

Chromium does not ride any of them, so :mod:`olano.tools.browser` intercepts
every request its browser contexts make and asks :func:`check_host_against`
itself.

**The rule that makes this safe: only a call made *inside a governed tool* is
checked.** The tool wrapper (``security/pipeline`` and the deepagents
middleware) pushes the resolved policy into the request context for the
duration of one tool call, and :func:`active_policy` returns ``None`` anywhere
else. So the platform's own traffic — the agent's model turn, catalog sync,
credits and health reporting, connector token refresh, the relay, MCP connects
at build time — is exempt *by construction*, not by a hostname list that would
go stale the moment someone points a BYOK provider at a custom base URL or runs
ollama on another box. Nothing outside a tool call can be blocked by an
operator's typo.

Two narrower exemptions sit above that:

* :func:`exempt` — for a platform call that genuinely happens *inside* a tool,
  which is only ever a model invocation: ``tools/vision``, ``cognition/worker``
  and ``core/runner`` each call a model from within a governed context, and the
  user's domain list is about where the agent may *go*, not which model answers
  it. The same three sites the stream-isolation rule names, for the same
  reason: a model call inside the graph is not part of what the tool is doing.
* :data:`INFRA_HOSTS` — the box's own control plane. A belt for anything that
  nests under a tool without going through :func:`exempt`; an operator cannot
  deny-list the deployment out of contact with its own control plane, because
  the first thing that breaks is its ability to report that it broke.

Blocking raises :class:`EgressBlockedError`, a ``ValueError`` — tools that
already catch ``SSRFBlockedError`` (also a ``ValueError``) turn it into a
readable "Error: …" string the model can act on, and everything else surfaces
it through the normal tool-failure path rather than crashing a turn.

:func:`install` is idempotent and is called from
``egress.security_metadata()`` — the function every governed tool call already
invokes — so the patches are in place before any policy can exist, with no
import-time side effect and no boot-order requirement.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlsplit

from olano.security.egress import ALLOW_KEY, DENY_KEY, host_matches
from olano.security.request_context import get_request_context

logger = logging.getLogger(__name__)

#: The deployment's own control plane. Never blockable: catalog and credits
#: sync, health reporting, the connector/MCP auth relay and the managed model
#: gateway all live here, and a box that cannot reach them cannot be told to
#: stop, cannot report that it is failing, and cannot be recovered without SSH.
#: Subdomain-covering, like every other entry.
INFRA_HOSTS: frozenset[str] = frozenset(
    {
        "olano.ai",
        "olano.run",
    }
)

_EXEMPT: ContextVar[bool] = ContextVar("lionclaw_egress_exempt", default=False)

_install_lock = threading.Lock()
_installed = False
#: Set by :func:`uninstall` so the tests can restore the originals.
_originals: dict[str, Any] = {}


class EgressBlockedError(ValueError):
    """Raised when an outbound host is refused by the agent's egress lists."""


@contextmanager
def exempt() -> Iterator[None]:
    """Suppress enforcement for a platform call nested inside a tool call.

    Only for calls the *platform* makes on the agent's behalf — a model
    invocation from inside a tool. Never wrap a call whose destination the
    model chose; that is exactly what the lists exist to govern.

    **Safe to wrap a whole nested agent run**, not just a bare model call:
    ``push_request_context`` *merges* metadata, so a peer invoked through
    ``talk_to_agent`` or a Cortex worker would otherwise inherit the calling
    agent's lists and have its model calls refused by them. Wrapping the run
    exempts the model traffic, and :func:`governed` re-arms enforcement the
    moment that agent enters a tool of its own — under *its* policy, resolved
    from *its* config. So an exemption can never widen what a tool may reach.
    """
    token = _EXEMPT.set(True)
    try:
        yield
    finally:
        _EXEMPT.reset(token)


@contextmanager
def governed() -> Iterator[None]:
    """Clear any ambient exemption for the duration of one governed tool call.

    The counterpart to :func:`exempt`, taken by both tool wrappers alongside
    the policy push. Entering a tool is by definition the governed case, so an
    exemption set further up the stack — a nested agent run, a worker — must
    not survive into it.
    """
    token = _EXEMPT.set(False)
    try:
        yield
    finally:
        _EXEMPT.reset(token)


def active_policy() -> tuple[list[str], list[str]] | None:
    """The ``(allow, deny)`` in force for this call, or ``None`` if ungoverned.

    ``None`` means "not inside a governed tool call" — the platform's own
    traffic — and is the common case for everything that is not an agent tool.
    """
    if _EXEMPT.get():
        return None
    security = get_request_context().metadata.get("security")
    if not isinstance(security, dict):
        return None
    allow = security.get(ALLOW_KEY)
    deny = security.get(DENY_KEY)
    if allow is None and deny is None:
        return None
    return list(allow or []), list(deny or [])


def check_host(host: str | None) -> str | None:
    """Return a refusal reason for *host*, or ``None`` when it may be reached.

    Deny is checked before allow, matching
    :func:`olano.security.ssrf.validate_outbound_url`; an empty allow list
    means "everything public", which is the default posture.
    """
    return check_host_against(host, active_policy())


def check_host_against(host: str | None, policy: tuple[list[str], list[str]] | None) -> str | None:
    """:func:`check_host` against an explicit ``(allow, deny)`` policy.

    For enforcement that runs where the request context is not: the browser's
    request interception fires on the browser's own event loop, long after (and
    between) the tool calls that pushed the policy, so the browser captures
    :func:`active_policy` in the tool body and evaluates every request against
    that copy.
    """
    if policy is None:
        return None
    allow, deny = policy
    if not allow and not deny:
        return None
    if not host:
        return None
    lowered = host.strip().lower().rstrip(".")
    if not lowered or host_matches(lowered, INFRA_HOSTS):
        return None
    if deny and host_matches(lowered, deny):
        return f"Host '{host}' is blocked by this deployment's egress deny list."
    if allow and not host_matches(lowered, allow):
        return (
            f"Host '{host}' is not in the allowlist. "
            "Add it under Config → Outbound access, or to this agent's egress_allow."
        )
    return None


def assert_host_allowed(host: str | None) -> None:
    """:func:`check_host`, raising :class:`EgressBlockedError` on refusal."""
    reason = check_host(host)
    if reason:
        raise EgressBlockedError(reason)


def assert_url_allowed(url: str | None) -> None:
    """:func:`assert_host_allowed` for a full URL. Unparseable URLs pass — the
    SSRF gate and the client itself both reject them with a better message."""
    if not url:
        return
    try:
        host = urlsplit(str(url)).hostname
    except ValueError:
        return
    assert_host_allowed(host)


# ── chokepoints ──────────────────────────────────────────────────────────────


def install() -> None:
    """Patch the HTTP libraries. Idempotent, thread-safe, cheap to re-call."""
    global _installed
    if _installed:
        return
    with _install_lock:
        if _installed:
            return
        _patch_httpx()
        _patch_aiohttp()
        _patch_urllib()
        _installed = True


def uninstall() -> None:
    """Restore the original methods. For tests; never called in production."""
    global _installed
    with _install_lock:
        if not _installed:
            return
        for key, original in _originals.items():
            module, attr = key.rsplit(".", 1)
            try:
                if module == "httpx.Client":
                    import httpx

                    setattr(httpx.Client, attr, original)
                elif module == "httpx.AsyncClient":
                    import httpx

                    setattr(httpx.AsyncClient, attr, original)
                elif module == "aiohttp.ClientSession":
                    import aiohttp

                    setattr(aiohttp.ClientSession, attr, original)
                elif module == "urllib.OpenerDirector":
                    import urllib.request

                    setattr(urllib.request.OpenerDirector, attr, original)
            except Exception:  # pragma: no cover - defensive
                logger.exception("could not restore %s", key)
        _originals.clear()
        _installed = False


def _patch_httpx() -> None:
    try:
        import httpx
    except Exception:  # pragma: no cover - httpx is a hard dependency
        return

    _originals["httpx.Client.send"] = httpx.Client.send
    _originals["httpx.AsyncClient.send"] = httpx.AsyncClient.send
    sync_send = httpx.Client.send
    async_send = httpx.AsyncClient.send

    def guarded_send(self: Any, request: Any, **kwargs: Any) -> Any:
        assert_host_allowed(getattr(request.url, "host", None))
        return sync_send(self, request, **kwargs)

    async def guarded_asend(self: Any, request: Any, **kwargs: Any) -> Any:
        assert_host_allowed(getattr(request.url, "host", None))
        return await async_send(self, request, **kwargs)

    httpx.Client.send = guarded_send  # type: ignore[method-assign]
    httpx.AsyncClient.send = guarded_asend  # type: ignore[method-assign]


def _patch_aiohttp() -> None:
    try:
        import aiohttp
    except Exception:
        return

    _originals["aiohttp.ClientSession._request"] = aiohttp.ClientSession._request
    original = aiohttp.ClientSession._request

    async def guarded_request(self: Any, method: str, str_or_url: Any, **kwargs: Any) -> Any:
        assert_url_allowed(str(str_or_url))
        return await original(self, method, str_or_url, **kwargs)

    aiohttp.ClientSession._request = guarded_request  # type: ignore[method-assign]


def _patch_urllib() -> None:
    import urllib.request

    _originals["urllib.OpenerDirector.open"] = urllib.request.OpenerDirector.open
    original = urllib.request.OpenerDirector.open

    def guarded_open(self: Any, fullurl: Any, *args: Any, **kwargs: Any) -> Any:
        # `fullurl` is a str or a Request; the latter carries `.full_url`.
        target = getattr(fullurl, "full_url", fullurl)
        assert_url_allowed(str(target))
        return original(self, fullurl, *args, **kwargs)

    urllib.request.OpenerDirector.open = guarded_open  # type: ignore[method-assign]
