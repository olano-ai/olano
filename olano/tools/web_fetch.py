"""Lightweight readable-content web fetch tool modeled after OpenClaw."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from typing import Annotated, cast

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

from langchain_core.tools import tool

from olano.security.request_context import get_request_context
from olano.security.ssrf import SSRFBlockedError, assert_safe_outbound_url, redirect_guard
from olano.security.tool_metadata import tool_metadata

logger = logging.getLogger(__name__)

DEFAULT_FETCH_MAX_CHARS = 50_000
DEFAULT_FETCH_MAX_RESPONSE_BYTES = 2_000_000
DEFAULT_FETCH_TIMEOUT_SECONDS = 30
DEFAULT_FETCH_CACHE_TTL_SECONDS = 15 * 60
DEFAULT_FETCH_MAX_REDIRECTS = 3
DEFAULT_FETCH_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
DEFAULT_ERROR_MAX_CHARS = 4_000
INVISIBLE_UNICODE_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u206a-\u206f\ufeff]")
_HIDDEN_CLASS_NAMES = (
    "sr-only",
    "visually-hidden",
    "d-none",
    "hidden",
    "invisible",
    "screen-reader-only",
    "offscreen",
)
_BLOCK_TAGS = {
    "article",
    "aside",
    "blockquote",
    "br",
    "div",
    "footer",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "section",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
}
_SKIP_TAGS = {
    "canvas",
    "embed",
    "iframe",
    "meta",
    "noscript",
    "object",
    "script",
    "style",
    "svg",
    "template",
}
_FETCH_CACHE: dict[str, CacheEntry] = {}


@dataclass
class CacheEntry:
    value: dict[str, object]
    expires_at: float


def _normalize_cache_key(url: str, extract_mode: str, max_chars: int) -> str:
    return f"fetch:{url}:{extract_mode}:{max_chars}".strip().lower()


def _read_cache(key: str) -> dict[str, object] | None:
    entry = _FETCH_CACHE.get(key)
    if not entry:
        return None
    if time.time() > entry.expires_at:
        _FETCH_CACHE.pop(key, None)
        return None
    return dict(entry.value)


def _write_cache(key: str, value: dict[str, object], ttl_seconds: int) -> None:
    if ttl_seconds <= 0:
        return
    if len(_FETCH_CACHE) >= 100:
        oldest_key = next(iter(_FETCH_CACHE), None)
        if oldest_key is not None:
            _FETCH_CACHE.pop(oldest_key, None)
    _FETCH_CACHE[key] = CacheEntry(value=dict(value), expires_at=time.time() + ttl_seconds)


def _normalize_content_type(value: str | None) -> str:
    if not value:
        return "application/octet-stream"
    head = value.split(";", 1)[0].strip()
    return head or "application/octet-stream"


def _strip_invisible_unicode(text: str) -> str:
    return INVISIBLE_UNICODE_RE.sub("", text)


def _decode_entities(value: str) -> str:
    return unescape(value.replace("\xa0", " "))


def _normalize_whitespace(value: str) -> str:
    text = value.replace("\r", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _strip_tags(value: str) -> str:
    return _decode_entities(re.sub(r"<[^>]+>", "", value))


def _truncate_text(value: str, max_chars: int) -> tuple[str, bool]:
    if len(value) <= max_chars:
        return value, False
    return value[:max_chars], True


def _is_hidden_style(style: str) -> bool:
    lowered = style.lower()
    checks = (
        "display:none",
        "display: none",
        "visibility:hidden",
        "visibility: hidden",
        "opacity:0",
        "opacity: 0",
        "font-size:0",
        "font-size: 0",
        "color:transparent",
        "color: transparent",
    )
    return any(token in lowered for token in checks)


def _sanitize_html(html: str) -> str:
    cleaned = re.sub(r"<!--[\s\S]*?-->", "", html)
    for tag in _SKIP_TAGS:
        cleaned = re.sub(
            rf"<{tag}\b[^>]*>[\s\S]*?</{tag}>",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(rf"<{tag}\b[^>]*/?>", "", cleaned, flags=re.IGNORECASE)

    hidden_pattern = (
        r"<(?P<tag>[a-z0-9]+)\b"
        r"(?=[^>]*(?:"
        r'aria-hidden\s*=\s*["\']true["\']|'
        r"\bhidden\b|"
        r'class\s*=\s*["\'][^"\']*(?:sr-only|visually-hidden|d-none|hidden|invisible|'
        r'screen-reader-only|offscreen)[^"\']*["\']|'
        r'style\s*=\s*["\'][^"\']*(?:display\s*:\s*none|visibility\s*:\s*hidden|'
        r"opacity\s*:\s*0|font-size\s*:\s*0|color\s*:\s*transparent)[^\"\']*[\"']"
        r"))[^>]*>[\s\S]*?</(?P=tag)>"
    )
    cleaned = re.sub(hidden_pattern, "", cleaned, flags=re.IGNORECASE)
    return cleaned


class _HtmlToMarkdownParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.link_stack: list[str | None] = []
        self.skip_stack: list[str] = []
        self.title_parts: list[str] = []
        self.capture_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if tag == "title":
            self.capture_title = True
        if tag in _SKIP_TAGS:
            self.skip_stack.append(tag)
            return
        if self.skip_stack:
            return
        class_name = attrs_dict.get("class") or ""
        style = attrs_dict.get("style") or ""
        if attrs_dict.get("aria-hidden") == "true":
            self.skip_stack.append(tag)
            return
        if any(name in class_name.lower() for name in _HIDDEN_CLASS_NAMES) or _is_hidden_style(
            style
        ):
            self.skip_stack.append(tag)
            return
        if tag in {
            "article",
            "br",
            "div",
            "footer",
            "header",
            "hr",
            "ol",
            "p",
            "section",
            "table",
            "tr",
            "ul",
        }:
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            level = max(1, min(6, int(tag[1])))
            self.parts.append(f"\n{'#' * level} ")
        elif tag == "a":
            self.link_stack.append(attrs_dict.get("href"))

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.capture_title = False
        if self.skip_stack:
            if self.skip_stack[-1] == tag:
                self.skip_stack.pop()
            return
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")
        elif tag == "a" and self.link_stack:
            href = self.link_stack.pop()
            if href:
                recent = "".join(self.parts[-1:]).strip()
                if (
                    recent
                    and self.parts
                    and self.parts[-1].strip()
                    and not recent.endswith(f"]({href})")
                ):
                    self.parts[-1] = f"[{self.parts[-1].strip()}]({href})"

    def handle_data(self, data: str) -> None:
        if self.capture_title:
            self.title_parts.append(data)
        if self.skip_stack:
            return
        if data:
            self.parts.append(data)

    def render(self) -> tuple[str, str | None]:
        text = _normalize_whitespace(
            _strip_invisible_unicode(_decode_entities("".join(self.parts)))
        )
        title = _normalize_whitespace(
            _strip_invisible_unicode(_decode_entities("".join(self.title_parts)))
        )
        return text, title or None


def _html_to_markdown(html: str) -> tuple[str, str | None]:
    parser = _HtmlToMarkdownParser()
    parser.feed(html)
    parser.close()
    return parser.render()


def _markdown_to_text(markdown: str) -> str:
    text = re.sub(r"!\[[^\]]*]\([^)]+\)", "", markdown)
    text = re.sub(r"\[([^\]]+)]\([^)]+\)", r"\1", text)
    text = re.sub(r"```[\s\S]*?```", lambda m: m.group(0).replace("```", ""), text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)
    return _normalize_whitespace(_strip_invisible_unicode(text))


def _format_error_detail(detail: str, content_type: str | None, max_chars: int) -> str:
    if not detail:
        return ""
    if "text/html" in (content_type or "").lower() or detail.lstrip().lower().startswith("<html"):
        rendered, title = _html_to_markdown(_sanitize_html(detail))
        text = "\n".join(part for part in [title, rendered] if part)
    else:
        text = detail
    truncated, _ = _truncate_text(_normalize_whitespace(text), max_chars)
    return truncated


def _response_bytes(response: object) -> bytes:
    content = getattr(response, "content", b"")
    if isinstance(content, bytes):
        return content
    if isinstance(content, str):
        return content.encode("utf-8", errors="replace")
    return bytes(content or b"")


def _managed_fetch_payload(url: str, extract_mode: str, max_chars: int) -> dict[str, object] | None:
    """Fetch through the Olano web service, shaped like the local payload.

    Returns ``None`` on any refusal so the caller's local reader takes over.
    """
    try:
        from olano.deployment.services_client import (
            ServiceUnavailable,
            service_fetch,
            services_available,
        )

        if not services_available():
            return None
        start = time.perf_counter()
        try:
            data = service_fetch(url)
        except ServiceUnavailable:
            return None
        pages = data.get("pages") or []
        page = pages[0] if pages else {}
        text = str(page.get("content") or "")
        if not text:
            return None
        # Mirror the credit spend into the analytics service ledger — a managed
        # fetch bills without producing a token, so nothing else records it.
        try:
            from olano.analytics.service_ledger import record_service_event

            charged = data.get("credits_charged")
            record_service_event(
                "web_fetch",
                provider="olano",
                managed=True,
                credits=float(charged) if isinstance(charged, int | float) else None,
            )
        except Exception:
            logger.debug("service ledger: managed fetch not recorded", exc_info=True)
        if extract_mode == "text":
            text = _markdown_to_text(text)
        text = _normalize_whitespace(_strip_invisible_unicode(text))
        text, truncated = _truncate_text(text, max_chars)
        return {
            "url": url,
            "finalUrl": str(page.get("url") or url),
            "status": 200,
            "contentType": "text/markdown",
            "title": page.get("title"),
            "extractMode": extract_mode,
            "extractor": "olano-web-service",
            "externalContent": {"untrusted": True, "source": "web_fetch", "wrapped": False},
            "truncated": truncated,
            "length": len(text),
            "rawLength": len(text),
            "wrappedLength": len(text),
            "fetchedAt": datetime.now(timezone.utc).isoformat(),
            "tookMs": int((time.perf_counter() - start) * 1000),
            "text": text,
            "warning": None,
            "cached": False,
        }
    except Exception:  # pragma: no cover - any surprise degrades to local fetch
        return None


def _web_fetch_impl(
    url: Annotated[str, "HTTP or HTTPS URL to fetch."],
    extractMode: Annotated[str, 'Extraction mode ("markdown" or "text").'] = "markdown",
    maxChars: Annotated[int, "Maximum characters to return (truncates when exceeded)."] = 50_000,
) -> str:
    if httpx is None:
        return "Error: httpx not installed. Run: uv add httpx"

    try:
        parsed = httpx.URL(url)
    except Exception:
        return "Error: Invalid URL: must be http or https"
    if parsed.scheme not in {"http", "https"}:
        return "Error: Invalid URL: must be http or https"

    ctx = get_request_context()
    security = dict(ctx.metadata.get("security", {}) or {})
    ssrf_allowlist = list(security.get("ssrf_allowlist", []) or [])
    ssrf_blocklist = list(security.get("ssrf_blocklist", []) or [])
    try:
        assert_safe_outbound_url(url, allowlist=ssrf_allowlist, blocklist=ssrf_blocklist)
    except SSRFBlockedError as exc:
        return f"Error: {exc}"

    extract_mode = "text" if extractMode == "text" else "markdown"
    max_chars = max(100, min(int(maxChars), DEFAULT_FETCH_MAX_CHARS))
    cache_key = _normalize_cache_key(url, extract_mode, max_chars)
    cached = _read_cache(cache_key)
    if cached is not None:
        cached["cached"] = True
        return json.dumps(cached, ensure_ascii=False, indent=2)

    # Managed deployments prefer the Olano web service (Parallel Extract):
    # renders JS pages and survives bot walls this plain GET cannot, billed in
    # olano credits. Any refusal — disabled, out of credits, provider error —
    # falls through to the local reader below (unlike search, fetch has a free
    # local equivalent, so degrading beats erroring). Private/internal hosts
    # never leave the box: the SSRF gate above already vetted the URL, and the
    # service path is skipped for anything the public internet can't resolve
    # anyway (Parallel could not reach it).
    managed = _managed_fetch_payload(url, extract_mode, max_chars)
    if managed is not None:
        _write_cache(cache_key, managed, DEFAULT_FETCH_CACHE_TTL_SECONDS)
        return json.dumps(managed, ensure_ascii=False, indent=2)

    start = time.perf_counter()
    try:
        with httpx.Client(
            timeout=DEFAULT_FETCH_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={
                "Accept": "text/markdown, text/html;q=0.9, */*;q=0.1",
                "Accept-Language": "en-US,en;q=0.9",
                "User-Agent": DEFAULT_FETCH_USER_AGENT,
            },
            max_redirects=DEFAULT_FETCH_MAX_REDIRECTS,
            # Every redirect hop gets the same SSRF decision as the first URL.
            event_hooks={
                "request": [redirect_guard(allowlist=ssrf_allowlist, blocklist=ssrf_blocklist)]
            },
        ) as client:
            response = client.get(url)
    except httpx.TimeoutException:
        return f"Error: request timed out after {DEFAULT_FETCH_TIMEOUT_SECONDS}s"
    except Exception as exc:
        return f"Error: {exc}"

    content_type = _normalize_content_type(response.headers.get("content-type"))
    final_url = str(getattr(response, "url", url) or url)
    body_bytes = _response_bytes(response)
    body_truncated = len(body_bytes) > DEFAULT_FETCH_MAX_RESPONSE_BYTES
    body_bytes = body_bytes[:DEFAULT_FETCH_MAX_RESPONSE_BYTES]
    body = body_bytes.decode("utf-8", errors="replace")

    if response.status_code >= 400:
        detail = _format_error_detail(body, content_type, DEFAULT_ERROR_MAX_CHARS)
        return (
            f"Error: Web fetch failed ({response.status_code}): {detail or response.reason_phrase}"
        )

    title: str | None = None
    extractor = "raw"
    text = body
    warning: str | None = None

    if content_type == "text/markdown":
        extractor = "cf-markdown"
        if extract_mode == "text":
            text = _markdown_to_text(body)
    elif content_type == "text/html":
        sanitized = _sanitize_html(body)
        rendered, title = _html_to_markdown(sanitized)
        text = rendered
        extractor = "raw-html"
        if extract_mode == "text":
            text = _markdown_to_text(text)
    elif content_type == "application/json":
        try:
            text = json.dumps(json.loads(body), ensure_ascii=False, indent=2)
            extractor = "json"
        except Exception:
            extractor = "raw"

    text = _normalize_whitespace(_strip_invisible_unicode(text))
    text, truncated = _truncate_text(text, max_chars)
    if body_truncated:
        warning = f"Response body truncated after {DEFAULT_FETCH_MAX_RESPONSE_BYTES} bytes."

    took_ms = int((time.perf_counter() - start) * 1000)
    payload: dict[str, object] = {
        "url": url,
        "finalUrl": final_url,
        "status": response.status_code,
        "contentType": content_type,
        "title": title,
        "extractMode": extract_mode,
        "extractor": extractor,
        "externalContent": {
            "untrusted": True,
            "source": "web_fetch",
            "wrapped": False,
        },
        "truncated": truncated,
        "length": len(text),
        "rawLength": len(text),
        "wrappedLength": len(text),
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "tookMs": took_ms,
        "text": text,
        "warning": warning,
        "cached": False,
    }
    _write_cache(cache_key, payload, DEFAULT_FETCH_CACHE_TTL_SECONDS)
    return json.dumps(payload, ensure_ascii=False, indent=2)


@tool_metadata(capability="read_only", risk="medium", requires_hitl=False)
@tool
def page_read(
    url: Annotated[str, "HTTP or HTTPS URL to fetch."],
    extractMode: Annotated[str, 'Extraction mode ("markdown" or "text").'] = "markdown",
    maxChars: Annotated[int, "Maximum characters to return (truncates when exceeded)."] = 50_000,
) -> str:
    """
    Fast first-pass webpage reader for public URLs.
    Use this before browser automation when you want readable page content from a normal GET request.
    """
    return _web_fetch_impl(url=url, extractMode=extractMode, maxChars=maxChars)


@tool_metadata(capability="read_only", risk="medium", requires_hitl=False)
@tool
def web_fetch_fast(
    url: Annotated[str, "HTTP or HTTPS URL to fetch."],
    extractMode: Annotated[str, 'Extraction mode ("markdown" or "text").'] = "markdown",
    maxChars: Annotated[int, "Maximum characters to return (truncates when exceeded)."] = 50_000,
) -> str:
    """Compatibility alias for `page_read`."""
    return cast(str, page_read.func(url=url, extractMode=extractMode, maxChars=maxChars))


@tool_metadata(capability="read_only", risk="medium", requires_hitl=False)
@tool
def web_fetch(
    url: Annotated[str, "HTTP or HTTPS URL to fetch."],
    extractMode: Annotated[str, 'Extraction mode ("markdown" or "text").'] = "markdown",
    maxChars: Annotated[int, "Maximum characters to return (truncates when exceeded)."] = 50_000,
) -> str:
    """Compatibility alias for `page_read`."""
    return cast(str, page_read.func(url=url, extractMode=extractMode, maxChars=maxChars))


@tool_metadata(capability="mutating", risk="medium", requires_hitl=False)
@tool
def url_download(
    url: Annotated[str, "URL of the file to download"],
    filename: Annotated[str, "Filename to save as (optional, inferred from URL if empty)"] = "",
    timeout: Annotated[int, "Timeout in seconds (max 300)"] = 60,
) -> str:
    """Download a file from the web into the workspace downloads folder."""
    from olano.tools.http_tools import http_download

    return cast(str, http_download.func(url=url, filename=filename, timeout=timeout))


@tool_metadata(capability="mutating", risk="medium", requires_hitl=False)
@tool
def web_download_file(
    url: Annotated[str, "URL of the file to download"],
    filename: Annotated[str, "Filename to save as (optional, inferred from URL if empty)"] = "",
    timeout: Annotated[int, "Timeout in seconds (max 300)"] = 60,
) -> str:
    """Compatibility alias for `url_download`."""
    return cast(str, url_download.func(url=url, filename=filename, timeout=timeout))


@tool_metadata(capability="mutating", risk="medium", requires_hitl=False)
@tool
def web_download(
    url: Annotated[str, "URL of the file to download"],
    filename: Annotated[str, "Filename to save as (optional, inferred from URL if empty)"] = "",
    timeout: Annotated[int, "Timeout in seconds (max 300)"] = 60,
) -> str:
    """Compatibility alias for `url_download`."""
    return cast(str, url_download.func(url=url, filename=filename, timeout=timeout))


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
