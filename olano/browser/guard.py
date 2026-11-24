"""The browser's egress guard: every request a page makes, checked.

Chromium does not ride httpx, so the egress chokepoints in
:mod:`olano.security.egress_guard` never see it. The browser tools used to
check only the URL the model passed to ``browser_navigate``: a click that
navigated elsewhere, a server redirect, a ``location =`` in a script, every
image, script and XHR a page loaded and every WebSocket it opened went out
unchecked. On a box whose agent is confined to an allowlist that was the whole
allowlist bypassed by one link; on any box it was the cloud metadata address
and the runtime's own private network one redirect away.

:func:`install_guards` puts one interceptor on the agent's ``BrowserContext``
(``route`` for HTTP) that asks two questions of every request. WebSockets get
the same questions from the egress proxy every local context's connections go
through (:mod:`olano.browser.egress_proxy`); Playwright's ``route_web_socket``
puts a binding on every page, which bot checks pick up, and remains only for
the remote provider's browser and the HTML renderer:

1. the agent's egress policy (allow/deny lists), captured from the governed
   tool call that last touched the session (:class:`GuardPolicy` is refreshed
   on every call, so an edited allowlist applies to the next request);
2. the private-network block: a host that is, or resolves to, a private,
   loopback, link-local or metadata address is refused. Resolution runs off
   the event loop and is cached briefly per host, because a page makes
   hundreds of requests to a handful of hosts.

**Redirect hops are checked before they are followed, by Chromium.** Playwright
hands a route handler the first request only and follows a ``3xx`` on its own,
so the guard used to fetch every navigation itself (``route.fetch`` with
redirects off, ``Location`` checked, response fulfilled). That fetch is
Playwright's own HTTP client, not the browser: measured against a TLS server
here, its ClientHello offered 52 ciphers, 10 extensions and no ALPN, where
Chromium's offers 16, 17 and ``h2``, so every page an agent opened reached the
site with a fingerprint no browser has, which is the first thing bot
protection looks at. Now Chromium fetches every page, and :func:`guard_page`
attaches a DevTools ``Fetch`` interception of its own to each page, for
document requests at the request stage: a redirected request pauses there
*before it leaves the browser* (Playwright sees the same pause and continues
it unexamined), and one to a refused host is failed, so the hop is never
sent. That covers every hop of a chain, where the fetch covered the first
one only. Two hops escape it and are caught after the fact: a popup's first
navigation (it starts before the popup can be armed) and a frame in its own
process, whose requests the page's session does not see. Those, and any
page a browser without that interception lands on, are recorded from the
context's ``request`` event (:func:`landed_refusal` then discards the page
before anything reads it).

A site that turned the agent's browser away is refused at the same chokepoint
(``GuardState.site_refusal``, :mod:`olano.browser.blockpage`): a top-level
navigation to it, whether typed, clicked or scripted, is aborted before it is
sent.

Service workers are blocked on every context the sessions create (their
fetches would not pass the interceptor), and non-network schemes other than
``data:``/``blob:``/``about:`` are refused outright. ``file://`` is refused
unless the state carries a :class:`olano.browser.files.FileAccess` (set only by
a tool that opened an allowed local file on a browser on this host), and then
only inside its roots.

Set ``LIONCLAW_BROWSER_ALLOW_PRIVATE_NETWORK=1`` to let the browser reach
private addresses, for a developer pointing an agent at a dev server on
localhost. The allowlist still applies.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_PASS_SCHEMES = frozenset({"data", "blob", "about"})
_CHECKED_SCHEMES = frozenset({"http", "https", "ws", "wss"})
_DNS_TTL_S = 60.0
_DNS_CACHE_MAX = 512


# Refused even when the private-network override is on: the cloud metadata
# endpoints hand out the machine's credentials, and link-local is where they live.
_ALWAYS_BLOCKED_HOSTS = frozenset(
    {"metadata.google.internal", "metadata", "169.254.169.254", "fd00:ec2::254", "100.100.100.200"}
)


def metadata_refusal(host: str) -> str | None:
    lowered = (host or "").strip().lower().strip("[]").rstrip(".")
    if lowered in _ALWAYS_BLOCKED_HOSTS:
        return f"The browser never opens the cloud metadata address '{host}'."
    try:
        ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(lowered)
    except ValueError:
        return None
    # ``[::ffff:169.254.169.254]`` is the same address in another spelling.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
        if str(ip) in _ALWAYS_BLOCKED_HOSTS:
            return f"The browser never opens the cloud metadata address '{host}'."
    if ip.is_link_local:
        return f"The browser never opens link-local addresses ('{host}')."
    return None


def private_network_allowed() -> bool:
    return os.environ.get("LIONCLAW_BROWSER_ALLOW_PRIVATE_NETWORK", "").strip() in ("1", "true")


@dataclass
class GuardPolicy:
    """What one agent's browser may reach. Captured in the sync tool body."""

    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    block_private: bool = True

    @classmethod
    def capture(cls) -> GuardPolicy:
        """The policy of the governed tool call this runs in."""
        from olano.security.egress_guard import active_policy

        policy = active_policy()
        allow, deny = policy if policy is not None else ([], [])
        return cls(allow=list(allow), deny=list(deny), block_private=not private_network_allowed())


_PENDING_MAX = 200


@dataclass
class GuardState:
    """Per-context guard state: the live policy, the local files the context
    may load, the recent refusals (for the dashboard) and the refusals not yet
    reported to a tool (:meth:`drain`)."""

    policy: GuardPolicy = field(default_factory=GuardPolicy)
    files: Any = None  # olano.browser.files.FileAccess | None
    # Why a top-level navigation to a URL must not be sent because its site
    # turned this browser away a moment ago, or ``None`` (blockpage).
    site_refusal: Callable[[str], str | None] | None = None
    blocked: deque = field(default_factory=lambda: deque(maxlen=20))
    pending: list[dict[str, Any]] = field(default_factory=list)

    def note(
        self,
        url: str,
        reason: str,
        *,
        navigation: bool = False,
        redirect: bool = False,
        sent: bool = False,
        site_block: bool = False,
    ) -> None:
        """Record a refusal. *navigation*: the page itself (or a frame's
        document), not a subresource. *redirect*: the target of a redirect.
        *sent*: the request left the browser before it could be refused.
        *site_block*: the site itself refused the browser earlier and
        *reason* is the whole message for the agent."""
        entry = {
            "url": url[:300],
            "reason": reason,
            "at": time.time(),
            "navigation": navigation,
            "redirect": redirect,
            "sent": sent,
            "site_block": site_block,
        }
        self.blocked.append(entry)
        # Bounded: a page hammering a refused host must not grow this forever.
        if len(self.pending) < _PENDING_MAX:
            self.pending.append(entry)

    def drain(self) -> list[dict[str, Any]]:
        """The refusals since the last drain (each reported to one tool call)."""
        out, self.pending = self.pending, []
        return out

    def last_block_for(self, url: str) -> str | None:
        """The refusal recorded for a navigation to *url*, newest first."""
        host = (urlsplit(url).hostname or "").lower()
        for item in reversed(self.blocked):
            if item["url"] == url[:300] or (host and urlsplit(item["url"]).hostname == host):
                return str(item["reason"])
        return None


_dns_cache: dict[str, tuple[float, str | None]] = {}


def _private_refusal_cached(host: str, port: int | None) -> str | None:
    key = f"{host}:{port or ''}"
    now = time.monotonic()
    hit = _dns_cache.get(key)
    if hit is not None and now - hit[0] < _DNS_TTL_S:
        return hit[1]
    from olano.security.ssrf import private_host_refusal

    reason = private_host_refusal(host, port)
    if len(_dns_cache) >= _DNS_CACHE_MAX:
        _dns_cache.clear()
    _dns_cache[key] = (now, reason)
    return reason


def clear_dns_cache() -> None:
    _dns_cache.clear()


def scheme_refusal(url: str) -> str | None:
    """Refusal for a URL's scheme alone (no network work), or ``None``."""
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError:
        return "Unparseable URL."
    if scheme in _PASS_SCHEMES or scheme in _CHECKED_SCHEMES:
        return None
    return f"The browser does not open '{scheme}:' addresses."


def file_refusal(url: str, files: Any) -> str | None:
    """Refusal for a ``file:`` URL: none may load without a file boundary."""
    if files is None:
        return "Local files cannot be opened from this page."
    reason = files.refusal(url)
    return str(reason) if reason else None


def _is_file(url: str) -> bool:
    try:
        return urlsplit(url).scheme.lower() == "file"
    except ValueError:
        return False


async def url_refusal(url: str, policy: GuardPolicy, files: Any = None) -> str | None:
    """Why the browser must not fetch *url* under *policy*, or ``None``."""
    if _is_file(url):
        return file_refusal(url, files)
    reason = scheme_refusal(url)
    if reason:
        return reason
    try:
        parts = urlsplit(url)
    except ValueError:
        return "Unparseable URL."
    if parts.scheme.lower() not in _CHECKED_SCHEMES:
        return None
    host = parts.hostname or ""
    if not host:
        return "URL has no host."
    reason = metadata_refusal(host)
    if reason:
        return reason
    from olano.security.egress_guard import check_host_against

    reason = check_host_against(host, (policy.allow, policy.deny))
    if reason:
        return reason
    if policy.block_private:
        try:
            port = parts.port
        except ValueError:
            port = None
        return await asyncio.get_running_loop().run_in_executor(
            None, _private_refusal_cached, host, port
        )
    return None


def url_refusal_sync(url: str, policy: GuardPolicy, files: Any = None) -> str | None:
    """:func:`url_refusal` for a synchronous caller (a tool body checking the
    URL the model asked for before any browser work starts)."""
    if _is_file(url):
        return file_refusal(url, files)
    reason = scheme_refusal(url)
    if reason:
        return reason
    parts = urlsplit(url)
    if parts.scheme.lower() not in _CHECKED_SCHEMES:
        return None
    host = parts.hostname or ""
    if not host:
        return "URL has no host."
    reason = metadata_refusal(host)
    if reason:
        return reason
    from olano.security.egress_guard import check_host_against

    reason = check_host_against(host, (policy.allow, policy.deny))
    if reason:
        return reason
    if policy.block_private:
        try:
            port = parts.port
        except ValueError:
            port = None
        return _private_refusal_cached(host, port)
    return None


def _is_top_level_navigation(request: Any) -> bool:
    try:
        if not request.is_navigation_request():
            return False
        return request.frame.parent_frame is None
    except Exception:
        # Service-worker requests have no frame.
        return False


def _is_navigation(request: Any) -> bool:
    try:
        return bool(request.is_navigation_request())
    except Exception:
        return False


async def install_guards(
    context: Any, state: GuardState, *, requests: bool = True, websockets: bool = True
) -> None:
    """Route every HTTP request and WebSocket of *context* through the guard.

    *requests* / *websockets* exist for the plain browser test
    (browser/plain_check.py), which installs one half at a time.
    """

    async def _refuse(url: str) -> str | None:
        try:
            return await url_refusal(url, state.policy, state.files)
        except Exception as exc:  # fail closed: an undecidable request is refused
            return f"Request check failed: {exc}"

    async def _on_request(route: Any, request: Any) -> None:
        url = str(request.url)
        reason = await _refuse(url)
        top_level = _is_top_level_navigation(request)
        site_block = False
        if not reason and top_level and state.site_refusal is not None:
            try:
                reason = state.site_refusal(url)
            except Exception:  # fail closed, as above
                reason = "Request check failed."
            site_block = bool(reason)
        if reason:
            state.note(url, reason, navigation=top_level, site_block=site_block)
            try:
                await route.abort("blockedbyclient")
            except Exception:
                pass
            return
        try:
            await route.continue_()
        except Exception:
            # The page navigated away or closed mid-request; nothing to do.
            pass

    async def _on_redirect(request: Any) -> None:
        # Redirect hops never reach a route handler; this is the only place
        # they are visible at all.
        if getattr(request, "redirected_from", None) is None:
            return
        url = str(request.url)
        reason = await _refuse(url)
        if reason:
            state.note(
                url, reason, navigation=_is_top_level_navigation(request), redirect=True, sent=True
            )

    async def _on_websocket(ws: Any) -> None:
        url = str(ws.url)
        reason = await _refuse(url)
        if reason:
            state.note(url, reason)
            try:
                await ws.close(code=1008, reason="blocked by browser egress policy")
            except Exception:
                pass
            return
        ws.connect_to_server()

    if requests:
        await context.route("**/*", _on_request)
        context.on("request", _on_redirect)
    route_ws = getattr(context, "route_web_socket", None)
    if websockets and route_ws is not None:
        await route_ws(re.compile(r".*"), _on_websocket)


_HOP_PATTERNS = [{"urlPattern": "*", "resourceType": "Document", "requestStage": "Request"}]


async def guard_page(
    context: Any,
    page: Any,
    state: GuardState,
    *,
    proxy_auth: tuple[str, str] | None = None,
) -> Any:
    """Stop *page*'s refused redirect hops before they are sent (module docstring).

    *proxy_auth* is the context's proxy sign-in, when it has one: a proxy's
    ``407`` reaches this session first and Playwright's own answer never gets
    its turn (measured: handing the challenge on with ``Default`` left the
    page on the 407), so this session answers it. A site's own challenge is
    handed on, as it was.

    Returns the DevTools session, or ``None`` when one could not be attached:
    the page then keeps the after-the-fact checks, and the warning says so.
    """
    try:
        cdp = await context.new_cdp_session(page)
        tree = await cdp.send("Page.getFrameTree")
        main_frame = str(tree["frameTree"]["frame"]["id"])
    except Exception as exc:
        logger.warning("browser: redirect interception unavailable for this page: %s", exc)
        return None

    async def _on_paused(event: dict[str, Any]) -> None:
        request_id = event.get("requestId")
        url = str((event.get("request") or {}).get("url") or "")
        if event.get("redirectedRequestId") and url:
            reason: str | None
            try:
                reason = await url_refusal(url, state.policy, state.files)
            except Exception as exc:  # fail closed
                reason = f"Request check failed: {exc}"
            if reason:
                state.note(
                    url,
                    reason,
                    navigation=event.get("frameId") == main_frame,
                    redirect=True,
                    sent=False,
                )
                try:
                    await cdp.send(
                        "Fetch.failRequest",
                        {"requestId": request_id, "errorReason": "BlockedByClient"},
                    )
                except Exception:
                    pass
                return
        try:
            await cdp.send("Fetch.continueRequest", {"requestId": request_id})
        except Exception:
            # The page is gone, or Playwright's session already settled it.
            pass

    async def _on_auth(event: dict[str, Any]) -> None:
        challenge = event.get("authChallenge") or {}
        answer: dict[str, str] = {"response": "Default"}
        if challenge.get("source") == "Proxy" and proxy_auth is not None:
            answer = {
                "response": "ProvideCredentials",
                "username": proxy_auth[0],
                "password": proxy_auth[1],
            }
        try:
            await cdp.send(
                "Fetch.continueWithAuth",
                {"requestId": event.get("requestId"), "authChallengeResponse": answer},
            )
        except Exception:
            pass

    cdp.on("Fetch.requestPaused", _on_paused)
    cdp.on("Fetch.authRequired", _on_auth)
    try:
        await cdp.send("Fetch.enable", {"patterns": _HOP_PATTERNS, "handleAuthRequests": True})
    except Exception as exc:
        logger.warning("browser: redirect interception unavailable for this page: %s", exc)
        return None
    return cdp


async def landed_refusal(page: Any, state: GuardState) -> str | None:
    """Why the page *page* is showing must be discarded, or ``None``.

    A redirect hop the route could not stop lands before anything can refuse
    it; the page is then replaced with ``about:blank`` so no tool reads it, and
    every request it would make is refused anyway.
    """
    url = str(getattr(page, "url", "") or "")
    if urlsplit(url).scheme.lower() not in ("http", "https"):
        return None
    reason = await url_refusal(url, state.policy, state.files)
    if not reason:
        return None
    try:
        await page.goto("about:blank")
    except Exception:
        pass
    return (
        f"the page redirected to {url[:300]}, which this agent may not reach. {reason} "
        "The redirected page was discarded and not read."
    )


def _blocked_label(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return url[:80]
    if parts.hostname:
        return parts.hostname
    if parts.scheme.lower() == "file":
        from olano.browser.files import file_url_path

        path = file_url_path(url)
        if path is not None:
            return f"local file {path}"
    return url[:80]


def blocked_note(records: list[dict[str, Any]]) -> str:
    """A note naming what a page was refused, or ``""``."""
    labels = sorted({_blocked_label(e["url"]) for e in records})
    if not labels:
        return ""
    shown = ", ".join(labels[:5])
    more = f" and {len(labels) - 5} more" if len(labels) > 5 else ""
    return (
        f"\n\n[egress] Blocked {len(records)} request(s) this agent may not make: "
        f"{shown}{more}. Parts of the page may be missing."
    )


def blocked_message(entry: dict[str, Any]) -> str:
    """The tool error for a refused navigation."""
    if entry.get("site_block"):
        return str(entry["reason"])
    if entry.get("redirect") and entry.get("sent"):
        return (
            f"Error: the page redirected to {entry['url']}, which this agent may not reach. "
            f"{entry['reason']} The redirected page was discarded and not read."
        )
    if entry.get("redirect"):
        return (
            f"Error: the page redirected to {entry['url']}, which this agent may not reach. "
            f"{entry['reason']} The redirect was not followed."
        )
    return f"Error: navigation to {entry['url']} was blocked. {entry['reason']}"
