"""Request and response rules for proxying a viewer to a page's server.

Pure functions, no I/O: the route (``runtime/pages_routes.py``) applies them,
the tests pin them. A page server is agent-written code reached on the
dashboard's own origin, so these rules are what keep it from becoming a way
around the dashboard's security:

* **The viewer's credentials never reach the server.** ``Authorization`` (the
  page's Basic Auth, which is the agent owner's password) and the dashboard's
  session cookies are removed from every forwarded request. The server learns
  *who* is viewing from ``X-Olano-Viewer``, never *how they proved it*.
* **The server cannot speak for the runtime.** Incoming ``X-Olano-*`` and
  forwarding headers are dropped and re-set by the proxy, so a viewer cannot
  spoof them. On the way back, headers that act on the whole origin are
  removed (``Clear-Site-Data`` would log the viewer out of the dashboard,
  ``Service-Worker-Allowed`` would let a page register a worker over ``/``,
  ``WWW-Authenticate`` would hijack the password prompt, CORS headers would
  open the page to other sites), cookies are confined to the page's path and
  may not use the dashboard's names, and redirects stay under the page.
* **Unsafe methods must come from the page itself.** A browser re-sends cached
  Basic Auth credentials automatically, which makes a ``POST`` from another
  site a forged request; it is refused when the fetch metadata or ``Origin``
  says the request is cross-site.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from urllib.parse import urlsplit

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
PROXIED_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")

# RFC 9110 §7.6.1 connection-specific headers, plus the ones proxies
# conventionally never forward.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

_REQUEST_DROP = frozenset(
    {
        "authorization",
        "host",
        "forwarded",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "x-forwarded-prefix",
        "x-forwarded-port",
        "x-real-ip",
        "cookie",  # re-added filtered below
    }
)

_RESPONSE_DROP = frozenset(
    {
        "www-authenticate",
        "clear-site-data",
        "service-worker-allowed",
        "strict-transport-security",
        "alt-svc",
        "public-key-pins",
        "public-key-pins-report-only",
        "expect-ct",
        "set-cookie",  # re-added rewritten below
        "location",  # re-added rewritten below
        "server",
        "x-powered-by",
    }
)

#: The dashboard's own cookies (``runtime/auth.py``). Spelled here as well as
#: there because this module must not import the runtime; a test pins them.
RESERVED_COOKIES = frozenset({"lc_session", "__host-lc_session"})

_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


def page_prefix(slug: str) -> str:
    return f"/pages/{slug}"


def _connection_tokens(headers: Iterable[tuple[str, str]]) -> set[str]:
    tokens: set[str] = set()
    for name, value in headers:
        if name.lower() == "connection":
            tokens.update(t.strip().lower() for t in value.split(",") if t.strip())
    return tokens


def _filter_cookie_header(value: str) -> str:
    kept = []
    for part in value.split(";"):
        name, _, _ = part.strip().partition("=")
        if name and name.strip().lower() not in RESERVED_COOKIES:
            kept.append(part.strip())
    return "; ".join(kept)


def request_headers(
    headers: Iterable[tuple[str, str]],
    *,
    slug: str,
    port: int,
    viewer: str,
    client_ip: str,
    scheme: str,
    host: str,
) -> list[tuple[bytes, bytes]]:
    """The headers forwarded to the page server for one viewer request."""
    items = list(headers)
    drop = HOP_BY_HOP | _REQUEST_DROP | _connection_tokens(items)
    out: list[tuple[bytes, bytes]] = [(b"Host", f"127.0.0.1:{port}".encode())]
    chunked = False
    has_length = False
    cookies: list[str] = []
    for name, value in items:
        lower = name.lower()
        if lower == "transfer-encoding" and "chunked" in value.lower():
            chunked = True
        if lower == "content-length":
            has_length = True
        if lower == "cookie":
            filtered = _filter_cookie_header(value)
            if filtered:
                cookies.append(filtered)
            continue
        if lower in drop or lower.startswith("x-olano-"):
            continue
        out.append((name.encode("latin-1", "replace"), value.encode("latin-1", "replace")))
    if cookies:
        out.append((b"Cookie", "; ".join(cookies).encode("latin-1", "replace")))
    if chunked and not has_length:
        # The client streamed its body; the server gets it streamed too.
        out.append((b"Transfer-Encoding", b"chunked"))
    # A client-supplied X-Forwarded-For chain is never trusted: this proxy is
    # the first hop the page server can believe.
    forwarded_for = client_ip or "unknown"
    out += [
        (b"X-Forwarded-For", forwarded_for.encode("latin-1", "replace")),
        (b"X-Forwarded-Proto", (scheme or "http").encode()),
        (b"X-Forwarded-Host", host.encode("latin-1", "replace")),
        (b"X-Forwarded-Prefix", page_prefix(slug).encode()),
        (b"X-Olano-Page", slug.encode()),
        (b"X-Olano-Viewer", viewer.encode("utf-8", "replace")),
    ]
    return out


def rewrite_location(value: str, *, slug: str, port: int) -> str:
    """Keep a redirect under the page's prefix (and off the internal address)."""
    prefix = page_prefix(slug)
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        if parts.hostname in ("127.0.0.1", "localhost", "::1") and (parts.port in (None, port)):
            path = parts.path or "/"
            value = (
                path
                + (f"?{parts.query}" if parts.query else "")
                + (f"#{parts.fragment}" if parts.fragment else "")
            )
        else:
            return value  # an external redirect is the page's business
    # An app that already honours X-Forwarded-Prefix (or runs with a base
    # path) redirects under the prefix itself; anything else is re-rooted.
    if (
        value.startswith("/")
        and not value.startswith("//")
        and not (
            value == prefix or value.startswith(prefix + "/") or value.startswith(prefix + "?")
        )
    ):
        return prefix + value
    return value


def rewrite_set_cookie(value: str, *, slug: str) -> str | None:
    """Confine one ``Set-Cookie`` to the page, or ``None`` to drop it.

    The cookie's ``Path`` is forced under ``/pages/<slug>/`` and its ``Domain``
    removed, so a page can neither read nor overwrite another page's or the
    dashboard's cookies; a cookie named like the dashboard session, or using
    the ``__Host-`` prefix (which requires ``Path=/``), is dropped outright.
    """
    prefix = page_prefix(slug)
    pieces = [p.strip() for p in value.split(";")]
    if not pieces or "=" not in pieces[0]:
        return None
    name = pieces[0].split("=", 1)[0].strip()
    if not name or not _TOKEN.match(name):
        return None
    if name.lower() in RESERVED_COOKIES or name.lower().startswith("__host-"):
        return None
    attrs: list[str] = []
    path = ""
    for attr in pieces[1:]:
        if not attr:
            continue
        key, _, val = attr.partition("=")
        key_l = key.strip().lower()
        if key_l == "domain":
            continue
        if key_l == "path":
            path = val.strip()
            continue
        attrs.append(attr)
    if not path.startswith("/"):
        path = "/"
    if not (path == prefix or path.startswith(prefix + "/")):
        path = prefix + (path if path != "/" else "/")
    return "; ".join([pieces[0], f"Path={path}", *attrs])


def _private_cache_control(value: str) -> str:
    directives = [d.strip() for d in value.split(",") if d.strip()]
    out: list[str] = []
    for directive in directives:
        key = directive.split("=", 1)[0].strip().lower()
        if key == "public":
            out.append("private")
        elif key in ("s-maxage", "proxy-revalidate"):
            continue
        else:
            out.append(directive)
    return ", ".join(dict.fromkeys(out)) or "private"


def response_headers(
    headers: Iterable[tuple[bytes, bytes]],
    *,
    slug: str,
    port: int,
) -> list[tuple[str, str]]:
    """The page server's response headers, made safe to hand to the viewer."""
    items = [(k.decode("latin-1", "replace"), v.decode("latin-1", "replace")) for k, v in headers]
    drop = HOP_BY_HOP | _RESPONSE_DROP | _connection_tokens(items)
    out: list[tuple[str, str]] = []
    saw_cache_control = False
    for name, value in items:
        lower = name.lower()
        if lower == "set-cookie":
            cookie = rewrite_set_cookie(value, slug=slug)
            if cookie is not None:
                out.append(("set-cookie", cookie))
            continue
        if lower == "location":
            out.append(("location", rewrite_location(value, slug=slug, port=port)))
            continue
        if lower == "cache-control":
            saw_cache_control = True
            out.append(("cache-control", _private_cache_control(value)))
            continue
        if lower in drop or lower.startswith(("x-olano-", "access-control-")):
            continue
        out.append((name, value))
    if not saw_cache_control:
        # Behind a login: no shared cache may store it for the next viewer.
        out.append(("cache-control", "private"))
    out.append(("x-content-type-options", "nosniff"))
    return out


def cross_site_unsafe(method: str, headers: dict[str, str], host: str) -> bool:
    """A state-changing request a browser sent on another site's behalf.

    ``headers`` keys are lowercase. Fetch metadata decides when present (every
    current browser sends it); otherwise a present ``Origin`` must match the
    host. A request with neither is a non-browser client, which had to supply
    the Basic Auth credentials itself, so it is not a forgery.
    """
    if method.upper() in SAFE_METHODS:
        return False
    site = headers.get("sec-fetch-site", "").strip().lower()
    if site:
        return site not in ("same-origin", "none")
    origin = headers.get("origin", "").strip()
    if not origin:
        return False
    if origin == "null":
        return True
    return (urlsplit(origin).netloc or "").lower() != (host or "").lower()


def upstream_target(
    raw_path: bytes, query: bytes, *, slug: str, strip_prefix: bool
) -> bytes | None:
    """The request target sent to the server, or ``None`` for a non-canonical URL."""
    prefix = page_prefix(slug).encode()
    if not (raw_path == prefix or raw_path.startswith(prefix + b"/")):
        return None
    path = raw_path[len(prefix) :] if strip_prefix else raw_path
    if not path.startswith(b"/"):
        path = b"/" + path
    return path + (b"?" + query if query else b"")


_ASSET_EXT = re.compile(r"\.[A-Za-z0-9]{1,8}$")


def is_navigation(path: str, accept: str) -> bool:
    """A request for a client-side route (SPA fallback), not a missing asset."""
    last = path.rstrip("/").rsplit("/", 1)[-1]
    if _ASSET_EXT.search(last):
        return "text/html" in accept.lower()
    return True


def status_page(title: str, message: str, *, refresh: int | None = None) -> str:
    """A small self-contained HTML page for 503/502/504 answers."""
    meta = f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh else ""
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"{meta}<title>{html.escape(title)}</title>"
        "<style>body{font:16px/1.5 system-ui,sans-serif;margin:0;display:grid;"
        "place-items:center;min-height:100vh;background:#f6f6f4;color:#222}"
        "main{max-width:34rem;padding:2rem}h1{font-size:1.25rem;margin:0 0 .5rem}"
        "p{margin:0;color:#555;white-space:pre-line}"
        "@media(prefers-color-scheme:dark){body{background:#161616;color:#eee}p{color:#aaa}}"
        f"</style></head><body><main><h1>{html.escape(title)}</h1>"
        f"<p>{html.escape(message)}</p></main></body></html>"
    )
