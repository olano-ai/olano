"""Baseline browser security headers for the dashboard and its API.

Applied by ``security_headers_middleware`` in ``runtime/api.py`` to every HTTP
response. Deliberately small: no Content-Security-Policy (the SPA and the
tools it embeds have not been audited against one) and no HSTS (a self-hosted
box may terminate TLS with a self-signed certificate, and HSTS would lock its
owner out). Every header is set with ``setdefault``, so a route that already
sends its own value (``files-raw``, the magic-link pages) keeps it.

``X-Frame-Options`` is ``SAMEORIGIN`` rather than ``DENY`` because the Files
view previews documents in same-origin iframes.

Agent-published pages (``/pages/<slug>/``) are skipped entirely: they are the
owner's content, may be embedded on the owner's own site, and their assets are
served with guessed MIME types that ``nosniff`` would refuse.
"""

from __future__ import annotations

from collections.abc import MutableMapping

SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}

_SKIP_PREFIXES: tuple[str, ...] = ("/pages/",)


def should_apply(path: str) -> bool:
    return not path.startswith(_SKIP_PREFIXES)


def apply_security_headers(headers: MutableMapping[str, str], path: str) -> None:
    """Add the baseline headers to ``headers`` unless the route opted out."""
    if not should_apply(path):
        return
    for name, value in SECURITY_HEADERS.items():
        headers.setdefault(name, value)
