"""Server-side request forgery protections for outbound HTTP."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

_BLOCKED_HOSTS = {
    "localhost",
    "metadata.google.internal",
}
_BLOCKED_IPS = {
    "169.254.169.254",
    "127.0.0.1",
    "::1",
}


class SSRFBlockedError(ValueError):
    """Raised when an outbound URL resolves to a blocked destination."""


@dataclass(frozen=True)
class SSRFDecision:
    allowed: bool
    reason: str = ""


def _host_matches_rules(host: str, rules: list[str]) -> bool:
    # One definition of "a domain covers its subdomains", shared with
    # security/egress_guard so the URL gate and the socket guard agree.
    from olano.security.egress import host_matches

    return host_matches(host, rules)


def _is_blocked_ip(ip_text: str) -> bool:
    ip = ipaddress.ip_address(ip_text)
    return any(
        [
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
            ip_text in _BLOCKED_IPS,
        ]
    )


def validate_outbound_url(
    url: str,
    *,
    allowlist: list[str] | None = None,
    blocklist: list[str] | None = None,
) -> SSRFDecision:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return SSRFDecision(False, f"Unsupported URL scheme '{parsed.scheme}'.")
    host = parsed.hostname
    if not host:
        return SSRFDecision(False, "URL is missing a hostname.")
    lowered = host.lower()
    if lowered in _BLOCKED_HOSTS:
        return SSRFDecision(False, f"Blocked outbound host '{host}'.")
    if blocklist and _host_matches_rules(lowered, blocklist):
        return SSRFDecision(False, f"Host '{host}' is blocked by policy.")
    if allowlist and not _host_matches_rules(lowered, allowlist):
        return SSRFDecision(False, f"Host '{host}' is not in the allowlist.")

    try:
        if _is_blocked_ip(lowered):
            return SSRFDecision(False, f"Blocked private or special-use IP '{host}'.")
        return SSRFDecision(True)
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror:
        # Allow unresolved public hostnames to proceed so the underlying HTTP client can
        # surface the network error. Private IPs and blocked hostnames are rejected above.
        return SSRFDecision(True)

    for info in infos:
        resolved_ip = str(info[4][0])
        if _is_blocked_ip(resolved_ip):
            return SSRFDecision(
                False,
                f"Blocked outbound URL '{url}': DNS resolved to private or metadata IP '{resolved_ip}'.",
            )

    return SSRFDecision(True)


def private_host_refusal(host: str, port: int | None = None) -> str | None:
    """Why *host* is a private or special-use destination, or ``None``.

    The private-network half of :func:`validate_outbound_url` on its own, for a
    caller that already applied its own allow/deny lists (the browser's request
    guard). Resolves the name, so it blocks: call it off the event loop. An
    unresolvable name passes, as above: the client surfaces the DNS error.
    """
    lowered = (host or "").strip().lower().rstrip(".")
    if not lowered:
        return None
    if lowered in _BLOCKED_HOSTS:
        return f"Blocked outbound host '{host}'."
    literal = lowered.strip("[]")
    try:
        if _is_blocked_ip(literal):
            return f"Blocked private or special-use IP '{host}'."
        return None
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(lowered, port or 443)
    except (socket.gaierror, UnicodeError):
        return None
    for info in infos:
        resolved_ip = str(info[4][0])
        if _is_blocked_ip(resolved_ip):
            return f"Host '{host}' resolves to private or metadata IP '{resolved_ip}'."
    return None


def resolve_public(host: str, port: int | None = None) -> tuple[str | None, list[str]]:
    """``(refusal, addresses)`` for *host*: the private-network check of
    :func:`private_host_refusal`, and, when it passes, every address it
    checked (IPv4 first), for a caller that must connect to exactly those (the
    browser's egress proxy) so a name cannot resolve elsewhere in between.
    An unresolvable name is ``(None, [])``. Blocks: call it off the event loop.
    """
    lowered = (host or "").strip().lower().rstrip(".")
    if not lowered:
        return "No host.", []
    if lowered in _BLOCKED_HOSTS:
        return f"Blocked outbound host '{host}'.", []
    literal = lowered.strip("[]")
    try:
        if _is_blocked_ip(literal):
            return f"Blocked private or special-use IP '{host}'.", []
        return None, [literal]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(lowered, port or 443, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        return None, []
    addresses: list[str] = []
    for info in infos:
        resolved_ip = str(info[4][0])
        if _is_blocked_ip(resolved_ip):
            return f"Host '{host}' resolves to private or metadata IP '{resolved_ip}'.", []
        if resolved_ip not in addresses:
            addresses.append(resolved_ip)
    addresses.sort(key=lambda ip: ":" in ip)
    return None, addresses


def assert_safe_outbound_url(
    url: str,
    *,
    allowlist: list[str] | None = None,
    blocklist: list[str] | None = None,
) -> None:
    decision = validate_outbound_url(url, allowlist=allowlist, blocklist=blocklist)
    if not decision.allowed:
        raise SSRFBlockedError(decision.reason)


def redirect_guard(
    *,
    allowlist: list[str] | None = None,
    blocklist: list[str] | None = None,
) -> Callable[[Any], None]:
    """An httpx ``request`` event hook that re-checks every hop of a redirect.

    ``assert_safe_outbound_url`` on the URL the model asked for is not enough:
    with ``follow_redirects=True`` the client then follows a ``302`` to
    wherever the public server points, ``http://127.0.0.1/`` or the cloud
    metadata address included, and that hop was never checked. httpx runs
    request hooks for the redirected requests too, so this hook makes the
    same decision on each of them; raising aborts the chain and surfaces as
    the tool's usual ``Error:`` string.

    Install with ``httpx.Client(event_hooks={"request": [redirect_guard(...)]})``.
    Sync clients only (an async client needs an ``async def`` hook).
    """

    def _check(request: Any) -> None:
        assert_safe_outbound_url(str(request.url), allowlist=allowlist, blocklist=blocklist)

    return _check
