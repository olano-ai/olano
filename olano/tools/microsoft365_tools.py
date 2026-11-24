"""Microsoft 365 tools backed by Microsoft Graph (v1.0).

Authentication is a delegated Graph token under ``MS365_ACCESS_TOKEN`` (or
``MICROSOFT_GRAPH_ACCESS_TOKEN``); the connector broker
(``olano/core/connectors/providers/microsoft365.py``) mints and refreshes it
after a user connects Microsoft 365. Every tool addresses ``/me`` by default;
``user_id`` reaches another account only with a token allowed to (an app-only
token, or a mailbox shared with the signed-in user).

The toolkit covers mail (drafts, replies, forwards, folders, attachments),
calendar (invitations, Teams links, recurrence, availability, responses),
people lookup, contacts, automatic replies, OneDrive and SharePoint files,
Excel workbooks, Teams channels and chats, and Microsoft To Do.

Conventions that matter when extending this file:

* **Every request goes through ``_request``** (or ``_stream_to_file`` /
  ``_upload_session_put`` for bytes). It adds ``Prefer: IdType="ImmutableId"``
  so a message or event keeps its id when it moves between folders, refuses a
  URL that is not ``https://graph.microsoft.com`` (a ``page_token`` comes back
  from the model, and the bearer token must never follow it anywhere else), and
  turns Graph's error body into a readable ``[tool-error]`` line.
* **A 403 is a missing permission, not a dead sign-in.** Connections made
  before this toolkit grew were granted fewer scopes, and the broker's refresh
  never asks for new ones, so a new feature on an old connection answers 403.
  ``_graph_error`` says so and names the fix (a fresh sign-in through
  ``magic_link_connect_service("microsoft365")``). A 401 keeps its status in
  the text so the central error boundary (``tools/_error_boundary.py``) can
  refresh the token and retry.
* **A send never echoes an id or a subject.** The error boundary reads a short
  result that mentions "401" or "unauthorized" as a refused sign-in and
  retries the call, which for a send would deliver the message twice. Send-type
  results name recipients only; event creation carries a ``transactionId`` so
  Graph drops a retried duplicate.
* **Times are local.** Tools resolve an empty ``time_zone`` to the mailbox's
  own zone (``mailboxSettings``), falling back to UTC for a grant that cannot
  read it, and ask Graph to answer in that zone.
* **Files stay in the workspace.** Local paths resolve against the running
  agent's workspace; downloads land in ``<workspace>/downloads/`` (or a folder
  inside the workspace), get a sanitized name, and are handed to the sandbox
  user.
* **Every body is HTML, and the model is told so.** Outlook renders what it is
  given, so a body goes out as ``contentType: HTML`` on every composing tool
  (``_html_body`` wraps tag-less text into paragraphs; Markdown is never
  rendered). The body descriptions (``_BODY_HELP`` and friends) spell out the
  tags, the two image routes and the attachment routes, because an agent that
  is not told writes Markdown and the recipient reads literal asterisks.
* **Pictures inside the body ride as inline attachments.** ``inline_images``
  attaches each local image with ``isInline: true`` and ``contentId`` = its
  file name, which the HTML references as ``<img src="cid:<file name>">``
  (``_inline_specs`` + ``_check_inline_refs``: an image the body never
  references, or a ``cid:`` the images never supply, is refused before any
  request, since either renders as a broken picture or a stray attachment).
  Regular ``attachments`` never set ``isInline``. On the reading side an
  inline attachment is listed and downloadable like any other: a screenshot
  pasted into an Outlook message *is* the message, and Graph's
  ``hasAttachments`` is ``false`` for a message whose attachments are all
  inline, so ``ms365_get_message`` always asks ``/attachments``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone, tzinfo
from email.utils import getaddresses
from html import escape as _html_escape
from html import unescape as _html_unescape
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlparse

import httpx
from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._toolkit import make_toolkit_api

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_GRAPH_HOST = "graph.microsoft.com"
_MS_CREDS_MSG = (
    "[missing-credentials] Microsoft 365 not configured. Set MS365_ACCESS_TOKEN "
    "(or MICROSOFT_GRAPH_ACCESS_TOKEN) in the vault."
)
_RECONNECT = 'magic_link_connect_service("microsoft365")'

# Graph's documented split for attachments: a single POST under 3 MB, an upload
# session from 3 MB to 150 MB (a session for a smaller file is refused).
_INLINE_ATTACHMENT_MAX = 3 * 1024 * 1024
_ATTACHMENT_MAX = 150 * 1024 * 1024
# sendMail carries attachments base64-encoded inside a JSON body capped at 4 MB,
# so past this total (leaving room for the body) a send goes draft-attach-send.
_SENDMAIL_INLINE_TOTAL = int(2.5 * 1024 * 1024)
# OneDrive's simple PUT accepts up to 250 MB but reads the whole file into
# memory here; past 4 MB an upload session streams it in chunks instead.
_SIMPLE_UPLOAD_MAX = 4 * 1024 * 1024
# Drive sessions need chunks in multiples of 320 KiB, Outlook attachment
# sessions need them under 4 MB; ten multiples satisfies both.
_UPLOAD_CHUNK = 320 * 1024 * 10

_MAX_TOP = 100

# Friendly names → Outlook well-known folder names.
_WELL_KNOWN_FOLDERS: dict[str, str] = {
    "inbox": "inbox",
    "drafts": "drafts",
    "draft": "drafts",
    "sent": "sentitems",
    "sent items": "sentitems",
    "sentitems": "sentitems",
    "deleted": "deleteditems",
    "deleted items": "deleteditems",
    "deleteditems": "deleteditems",
    "trash": "deleteditems",
    "bin": "deleteditems",
    "archive": "archive",
    "junk": "junkemail",
    "junk email": "junkemail",
    "junkemail": "junkemail",
    "spam": "junkemail",
    "outbox": "outbox",
}

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


# ── shared argument descriptions ────────────────────────────────────────────

# Repeated on most of the 51 tools and bound into every request, so kept short.
UserArg = Annotated[str, "'me' (default) = the connected account."]
TimeZoneArg = Annotated[
    str, "e.g. 'America/Costa_Rica'. Empty = the mailbox's own zone (UTC if unknown)."
]
PageTokenArg = Annotated[str, "Empty for page one; else the page_token a previous call printed."]
DriveArg = Annotated[str, "Empty = your OneDrive; else a SharePoint/search-result drive id."]
ItemArg = Annotated[str, "Item id, or a path from the drive root like '/Documents/Q3.xlsx'."]


# ── HTTP plumbing ───────────────────────────────────────────────────────────


def _client(**kwargs: Any) -> httpx.Client:
    """Every request is made from a client built here (tests swap the transport)."""
    return httpx.Client(**kwargs)


def _token() -> str:
    return get_credential("MS365_ACCESS_TOKEN") or get_credential("MICROSOFT_GRAPH_ACCESS_TOKEN")


def _is_graph_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and (parsed.hostname or "").lower() == _GRAPH_HOST


def _seg(value: str) -> str:
    """One URL path segment (an id, a name) with everything reserved escaped."""
    return quote(str(value).strip(), safe="")


def _user(user_id: str = "me") -> str:
    uid = (user_id or "me").strip()
    return "me" if uid.lower() == "me" else f"users/{_seg(uid)}"


def _short(path: str) -> str:
    """A request path for an error line: no query string, long ids elided."""
    bare = path.split("?", 1)[0]
    if bare.startswith(_GRAPH_BASE):
        bare = bare[len(_GRAPH_BASE) :]
    parts = [p if len(p) <= 40 else "…" for p in bare.strip("/").split("/")]
    return "/".join(parts)


def _clip(text: Any, limit: int = 300) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _graph_error(method: str, path: str, response: httpx.Response) -> str:
    """Graph's error body as one actionable ``[tool-error]`` line."""
    status = response.status_code
    code = message = ""
    try:
        err = response.json().get("error") or {}
        if isinstance(err, dict):
            code = str(err.get("code") or "")
            message = str(err.get("message") or "")
    except Exception:
        message = response.text or ""
    where = f"MS365 {method} {_short(path)}"
    detail = _clip(f"{code}: {message}" if code else message) or response.reason_phrase

    if status == 403:
        # Never let a permission problem read as a refused sign-in: the error
        # boundary would refresh, retry and then mark a healthy connection as
        # needing a reconnect. Keep Graph's code, drop wording that trips it.
        from olano.tools._connector_auth import is_auth_refusal

        if is_auth_refusal(detail):
            detail = code or "access denied"
        return (
            f"[tool-error] {where}: 403 {detail}. Microsoft 365 denied this request. "
            "Most often the connection was made before this feature existed and was "
            "never granted the permission for it (mail drafts and folders, availability, "
            "people search, Teams chats, To Do, contacts, SharePoint and shared files "
            f"came later). Offer the user a fresh sign-in with {_RECONNECT}: it replaces "
            "the connection in place, then retry. If they signed in again recently, "
            "their organization may require an admin to approve the permission, or the "
            "item may belong to someone who has not shared it. Personal Microsoft "
            "accounts have no Teams, directory, availability or SharePoint."
        )
    if status in (429, 503):
        wait = response.headers.get("Retry-After", "")
        pause = f" Wait {wait}s before retrying." if wait.isdigit() else ""
        return (
            f"[tool-error] {where}: {status} Microsoft 365 is throttling requests.{pause} {detail}"
        )
    if status == 404:
        return (
            f"[tool-error] {where}: 404 {detail}. Nothing was found at that id or path; "
            "list or search first to get a current one."
        )
    return f"[tool-error] {where}: {status} {detail}"


def _request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: Any = None,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
    prefer: tuple[str, ...] = (),
    timeout: float = 30,
) -> tuple[Any, str | None]:
    """One Graph call. Returns ``(json_or_{}, None)`` or ``(None, error_text)``."""
    token = _token()
    if not token:
        return None, _MS_CREDS_MSG
    url = path if path.startswith("https://") else f"{_GRAPH_BASE}/{path.lstrip('/')}"
    if not _is_graph_url(url):
        return None, (
            "[tool-error] MS365: refusing a URL outside graph.microsoft.com. Pass a "
            "page_token exactly as a previous call printed it."
        )
    hdrs = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Prefer": ", ".join(('IdType="ImmutableId"', *prefer)),
    }
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    elif content is None and method.upper() in ("POST", "PUT", "PATCH"):
        # Action endpoints (send, cancel, …) document a required Content-Length: 0.
        content = b""
    hdrs.update(headers or {})
    try:
        with _client(timeout=timeout) as client:
            response = client.request(
                method,
                url,
                params=params,
                content=json.dumps(body).encode() if body is not None else content,
                headers=hdrs,
            )
    except Exception as exc:
        reason = str(exc) or type(exc).__name__
        return None, f"[tool-error] MS365 {method} {_short(path)}: {type(exc).__name__}: {reason}"
    if response.status_code >= 400:
        return None, _graph_error(method, path, response)
    if not response.content:
        return {}, None
    try:
        return response.json(), None
    except ValueError:
        return {}, None


def _stream_to_file(
    url: str, target: Path, *, authenticated: bool, params: dict[str, Any] | None = None
) -> tuple[int, str | None]:
    """Download ``url`` into ``target``. Returns ``(bytes_written, error)``.

    ``authenticated`` requests carry the bearer token and must be Graph URLs;
    a pre-authenticated download URL (``@microsoft.graph.downloadUrl``) is
    fetched bare. Redirects are followed, and httpx drops the Authorization
    header when one leaves the origin.
    """
    headers: dict[str, str] = {}
    if authenticated:
        token = _token()
        if not token:
            return 0, _MS_CREDS_MSG
        if not _is_graph_url(url):
            return 0, "[tool-error] MS365: refusing to send the token outside graph.microsoft.com."
        headers = {"Authorization": f"Bearer {token}", "Prefer": 'IdType="ImmutableId"'}
    written = 0
    try:
        with _client(timeout=120, follow_redirects=True) as client:
            with client.stream("GET", url, headers=headers, params=params) as response:
                if response.status_code >= 400:
                    response.read()
                    return 0, _graph_error("GET", url, response)
                with target.open("wb") as fh:
                    for chunk in response.iter_bytes():
                        fh.write(chunk)
                        written += len(chunk)
    except Exception as exc:
        target.unlink(missing_ok=True)
        reason = str(exc) or type(exc).__name__
        return 0, f"[tool-error] MS365 download: {type(exc).__name__}: {reason}"
    return written, None


def _upload_session_put(upload_url: str, path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Send ``path`` to a Graph upload session in 320 KiB-aligned chunks.

    The upload URL is pre-authenticated; the bearer token is deliberately not
    sent to it.
    """
    if urlparse(upload_url).scheme != "https":
        return None, "[tool-error] MS365 upload: the upload session URL is not https."
    size = path.stat().st_size
    last: httpx.Response | None = None
    try:
        with _client(timeout=120) as client, path.open("rb") as fh:
            offset = 0
            while offset < size:
                chunk = fh.read(_UPLOAD_CHUNK)
                end = offset + len(chunk) - 1
                last = client.put(
                    upload_url,
                    content=chunk,
                    headers={
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {offset}-{end}/{size}",
                        "Content-Type": "application/octet-stream",
                    },
                )
                if last.status_code >= 400:
                    return None, _graph_error("PUT", "upload session", last)
                offset = end + 1
    except Exception as exc:
        reason = str(exc) or type(exc).__name__
        return None, f"[tool-error] MS365 upload: {type(exc).__name__}: {reason}"
    if last is None or not last.content:
        return {}, None
    try:
        return last.json(), None
    except ValueError:
        return {}, None


def _values(data: Any) -> list[dict[str, Any]]:
    found = (data or {}).get("value") if isinstance(data, dict) else None
    return [v for v in found if isinstance(v, dict)] if isinstance(found, list) else []


def _page_hint(data: Any) -> str:
    link = (data or {}).get("@odata.nextLink") if isinstance(data, dict) else None
    return f'\nMore results: call again with page_token="{link}"' if link else ""


def _top(value: Any, default: int = 10, cap: int = _MAX_TOP) -> int:
    try:
        return max(1, min(int(value), cap))
    except (TypeError, ValueError):
        return default


def _odata_str(value: str) -> str:
    """A literal for an OData ``$filter`` string (single quotes doubled)."""
    return value.replace("'", "''")


def _search_str(value: str) -> str:
    """A value for a quoted ``$search`` parameter."""
    return " ".join(value.replace("\\", " ").replace('"', " ").split())


# ── formatting ──────────────────────────────────────────────────────────────


def _addr(entry: Any) -> str:
    email = (entry or {}).get("emailAddress") if isinstance(entry, dict) else None
    if not isinstance(email, dict):
        return "?"
    address = str(email.get("address") or "")
    name = str(email.get("name") or "")
    if name and address and name.lower() != address.lower():
        return f"{name} <{address}>"
    return address or name or "?"


def _addrs(entries: Any) -> str:
    return ", ".join(_addr(e) for e in (entries or []) if isinstance(e, dict)) or "(none)"


def _human_size(n: Any) -> str:
    try:
        size = int(n)
    except (TypeError, ValueError):
        return "?"
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _fmt_dt(value: Any) -> str:
    """``2026-10-05T09:00:00.0000000`` → ``2026-10-05 09:00``."""
    text = str(value or "")
    return text[:16].replace("T", " ") if text else "?"


def _fmt_span(start: Any, end: Any, all_day: bool = False) -> str:
    s = (start or {}).get("dateTime") if isinstance(start, dict) else None
    e = (end or {}).get("dateTime") if isinstance(end, dict) else None
    if not s:
        return "?"
    try:
        day = datetime.fromisoformat(str(s)[:19]).strftime("%a")
    except ValueError:
        day = ""
    if all_day:
        return f"{day} {str(s)[:10]} (all day)".strip()
    first = f"{day} {_fmt_dt(s)}".strip()
    if e and str(e)[:10] == str(s)[:10]:
        return f"{first}-{str(e)[11:16]}"
    return f"{first} → {_fmt_dt(e)}" if e else first


# Real HTML tags only: plain text often carries "<ana@x.com>" or "<name>".
_TAG_RE = re.compile(
    r"</?(?:p|br|div|span|b|i|u|s|strong|em|a|ul|ol|li|table|thead|tbody|tr|td|th|"
    r"h[1-6]|html|body|head|img|hr|pre|code|blockquote|font|center|sup|sub|small)\b[^>]*>",
    re.IGNORECASE,
)


def _html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html or "")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|h[1-6]|li|tr|table|ul|ol)>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = _html_unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _html_body(text: str) -> str:
    """HTML for a message body: passed through when it already is HTML, else built."""
    raw = str(text or "")
    if _TAG_RE.search(raw):
        return raw
    paragraphs = [p for p in re.split(r"\n\s*\n", raw.strip()) if p.strip()]
    return "".join(f"<p>{_html_escape(p).replace(chr(10), '<br>')}</p>" for p in paragraphs)


def _prepend_html(new_html: str, existing: str) -> str:
    """Put ``new_html`` above a draft's quoted thread, inside its ``<body>``."""
    if not existing:
        return new_html
    match = re.search(r"<body[^>]*>", existing, re.IGNORECASE)
    if match:
        return existing[: match.end()] + new_html + existing[match.end() :]
    return new_html + existing


# ── recipients ──────────────────────────────────────────────────────────────


def _recipients(value: str | None, field: str) -> tuple[list[dict[str, Any]], str | None]:
    """``"Ana <ana@x.com>, bo@y.com"`` → Graph recipient objects."""
    if not value or not str(value).strip():
        return [], None
    out: list[dict[str, Any]] = []
    for name, address in getaddresses([str(value).replace(";", ",")]):
        address = address.strip()
        if not address:
            continue
        if "@" not in address:
            return [], (
                f"[tool-error] {field}: '{address}' is not an email address. Look the "
                "person up with ms365_find_people first, then pass their address."
            )
        email: dict[str, str] = {"address": address}
        if name.strip():
            email["name"] = name.strip()
        out.append({"emailAddress": email})
    return out, None


def _addresses(entries: list[dict[str, Any]]) -> str:
    return ", ".join(e["emailAddress"]["address"] for e in entries)


# ── time zones and dates ────────────────────────────────────────────────────

_TZ_CACHE: dict[str, tuple[float, str]] = {}
_TZ_TTL = 600.0


def _mailbox_timezone(user_id: str = "me") -> str:
    """The mailbox's configured time zone; ``UTC`` when it cannot be read.

    Cached per token and user for ten minutes, so a calendar tool costs one
    extra call at most once per token lifetime. A grant without
    ``MailboxSettings`` (a connection older than this toolkit) reads as UTC.
    """
    token = _token()
    key = hashlib.sha256(f"{token}|{user_id}".encode()).hexdigest()[:24]
    hit = _TZ_CACHE.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    data, err = _request("GET", f"{_user(user_id)}/mailboxSettings/timeZone")
    zone = "" if err else str((data or {}).get("value") or "")
    zone = zone or "UTC"
    if len(_TZ_CACHE) > 256:
        _TZ_CACHE.clear()
    _TZ_CACHE[key] = (time.monotonic() + _TZ_TTL, zone)
    return zone


def _resolve_tz(time_zone: str, user_id: str = "me") -> str:
    return (time_zone or "").strip() or _mailbox_timezone(user_id)


def _zoneinfo(name: str) -> tzinfo | None:
    """A ``tzinfo`` for an IANA or Windows zone name, or ``None``."""
    from zoneinfo import ZoneInfo

    label = (name or "").strip()
    if not label or label.upper() in ("UTC", "GMT", "Z"):
        return timezone.utc
    try:
        return ZoneInfo(label)
    except Exception:
        pass
    try:
        from tzlocal.windows_tz import win_tz

        iana = win_tz.get(label)
        if iana:
            return ZoneInfo(iana)
    except Exception:
        pass
    return None


def _parse_when(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    if len(text) > 10 and text[10] == " ":
        text = text[:10] + "T" + text[11:]
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _is_date_only(value: str) -> bool:
    return bool(re.fullmatch(r"\s*\d{4}-\d{2}-\d{2}\s*", value or ""))


def _bad_when(field: str, value: str) -> str:
    return (
        f"[tool-error] {field}: could not read '{value}' as a date or time. Use ISO 8601, "
        "e.g. '2026-10-05T09:30' (local to time_zone) or '2026-10-05'."
    )


def _graph_dt(field: str, value: str, zone_name: str) -> tuple[dict[str, str] | None, str | None]:
    """A Graph ``dateTimeTimeZone`` for ``value``.

    A naive value is local to ``zone_name``. A value with an offset is
    converted into ``zone_name`` when that zone is known here, else to UTC.
    """
    moment = _parse_when(value)
    if moment is None:
        return None, _bad_when(field, value)
    if moment.tzinfo is not None:
        zone = _zoneinfo(zone_name)
        if zone is None:
            moment, zone_name = moment.astimezone(timezone.utc), "UTC"
        else:
            moment = moment.astimezone(zone)
        moment = moment.replace(tzinfo=None)
    return {"dateTime": moment.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": zone_name}, None


def _instant(field: str, value: str, zone_name: str) -> tuple[datetime | None, str | None]:
    """An aware UTC datetime for ``value`` (a naive value is local to the zone)."""
    moment = _parse_when(value)
    if moment is None:
        return None, _bad_when(field, value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_zoneinfo(zone_name) or timezone.utc)
    return moment.astimezone(timezone.utc), None


def _utc_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _start_of_today(zone_name: str) -> datetime:
    zone = _zoneinfo(zone_name) or timezone.utc
    now = datetime.now(zone)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _tz_prefer(zone_name: str) -> str:
    return f'outlook.timezone="{zone_name}"'


# ── local files ─────────────────────────────────────────────────────────────


def _workspace_root() -> Path:
    """The running agent's workspace (not the legacy global one)."""
    from olano.config.workspace import get_workspace
    from olano.security.request_context import get_request_context

    root = get_request_context().metadata.get("workspace_root")
    return get_workspace(str(root) if root else None).root


def _local_files(value: str, what: str = "attachment") -> tuple[list[Path], str | None]:
    """Comma-separated local paths; relative ones resolve against the workspace."""
    paths: list[Path] = []
    for raw in [p.strip() for p in str(value or "").split(",") if p.strip()]:
        path = Path(raw.removeprefix("file://")).expanduser()
        if not path.is_absolute():
            path = _workspace_root() / path
        if not path.is_file():
            return [], f"[tool-error] {what} not found: {raw}"
        paths.append(path)
    return paths, None


# ── inline images ───────────────────────────────────────────────────────────

# The Content-ID an inline image is referenced by is its file name, so the
# model writes ``<img src="cid:logo.png">`` for ``inline_images="logo.png"``.
_CID_RE = re.compile(r"""(?i)\bcid:\s*([^"'\s<>)]+)""")


def _inline_specs(value: str) -> tuple[list[tuple[Path, str]], str | None]:
    """``(path, content id)`` per inline image; refuses a non-image or a name clash."""
    paths, err = _local_files(value, what="inline image")
    if err:
        return [], err
    specs: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for path in paths:
        mime = mimetypes.guess_type(str(path))[0] or ""
        if not mime.startswith("image/"):
            return [], (
                f"[tool-error] inline_images: {path.name} is not an image ({mime or 'unknown type'}); "
                "pass documents in attachments instead."
            )
        if path.name in seen:
            return [], (
                f"[tool-error] inline_images: two files are named {path.name}; the file name is "
                "the cid, so rename one."
            )
        seen.add(path.name)
        specs.append((path, path.name))
    return specs, None


def _check_inline_refs(html: str, specs: list[tuple[Path, str]]) -> str | None:
    """Every inline image must be referenced by the body, and every cid supplied."""
    referenced = {ref.strip() for ref in _CID_RE.findall(html or "")}
    supplied = {cid for _, cid in specs}
    missing = sorted(referenced - supplied)
    if missing:
        return (
            f"[tool-error] the body references cid:{missing[0]} but no inline image with that "
            f"file name was given; pass it in inline_images (supplied: "
            f"{', '.join(sorted(supplied)) or 'none'})."
        )
    unused = sorted(supplied - referenced)
    if unused:
        return (
            f"[tool-error] inline image {unused[0]} is not shown anywhere in the body; add "
            f'<img src="cid:{unused[0]}"> where it should appear, or pass it in attachments '
            "to send it as a regular file."
        )
    return None


def _download_target(
    filename: str, save_dir: str, mime: str = ""
) -> tuple[Path | None, list[Path], str | None]:
    """``(target, directories created, error)`` for a file saved into the workspace."""
    from olano.tools._sandbox_exec import missing_parents
    from olano.uploads import sanitize_filename

    root = _workspace_root().resolve()
    base = Path(save_dir.strip()).expanduser() if save_dir.strip() else Path("downloads")
    if not base.is_absolute():
        base = root / base
    base = base.resolve()
    if base != root and not base.is_relative_to(root):
        return (
            None,
            [],
            (f"[tool-error] save_dir must be inside the workspace ({root}); got {save_dir}."),
        )
    name = sanitize_filename(filename or "download", mime, "file")
    target = base / name
    stem, suffix, n = target.stem, target.suffix, 1
    while target.exists():
        target = base / f"{stem}-{n}{suffix}"
        n += 1
    created = missing_parents(target)
    base.mkdir(parents=True, exist_ok=True)
    return target, created, None


def _handoff(target: Path, created: list[Path]) -> None:
    from olano.tools._sandbox_exec import handoff_to_sandbox

    handoff_to_sandbox(target, created=created)


# ═══════════════════════════════════════════════════════════════════════════
# Mail
# ═══════════════════════════════════════════════════════════════════════════

# Shared on every composing tool: the body is always sent as HTML, and the model
# has to be told, or it writes Markdown that the recipient reads as asterisks.
_BODY_HELP = (
    "Message body in HTML; it is always sent as HTML, so write HTML tags (<p>, <br>, "
    "<b>, <ul><li>, <a href>, <table>, <h2>). Markdown is NOT rendered: do not write "
    "**bold** or '- ' bullets. Plain text with no tags is wrapped into paragraphs at "
    'blank lines. Pictures: <img src="https://..."> shows a hosted image, '
    '<img src="cid:FILENAME"> shows a local image passed in inline_images.'
)
_ATTACH_HELP = (
    "Local files to attach as regular attachments, comma-separated (workspace-relative "
    "or absolute), any type, up to 150 MB each. For a picture that must appear inside "
    "the message text use inline_images instead."
)
_INLINE_HELP = (
    "Local image files to embed in the body, comma-separated (png, jpg, gif, webp, "
    '...). Reference each one in the HTML as <img src="cid:FILENAME"> where FILENAME '
    "is its file name, e.g. inline_images='logo.png' with <img src=\"cid:logo.png\">. "
    "Header banners and signature logos go here; they are not listed as attachments."
)

_MESSAGE_LIST_FIELDS = (
    "id,subject,from,receivedDateTime,isRead,hasAttachments,bodyPreview,flag,importance,isDraft"
)


def _mail_folder_id(value: str, user_id: str) -> tuple[str | None, str | None]:
    """A folder id for a well-known name, a folder id, or a display name."""
    raw = (value or "").strip()
    if not raw:
        return None, "[tool-error] a folder is required."
    alias = _WELL_KNOWN_FOLDERS.get(raw.lower())
    if alias:
        return alias, None
    if len(raw) > 60 and " " not in raw:
        return raw, None
    data, err = _request(
        "GET",
        f"{_user(user_id)}/mailFolders",
        params={"$top": 100, "$select": "id,displayName,childFolderCount"},
    )
    if err:
        return None, err
    folders = _values(data)
    wanted = raw.lower()
    for folder in folders:
        if str(folder.get("displayName") or "").lower() == wanted:
            return str(folder["id"]), None
    for parent in [f for f in folders if f.get("childFolderCount")][:25]:
        kids, kid_err = _request(
            "GET",
            f"{_user(user_id)}/mailFolders/{_seg(parent['id'])}/childFolders",
            params={"$top": 100, "$select": "id,displayName"},
        )
        if kid_err:
            continue
        for folder in _values(kids):
            if str(folder.get("displayName") or "").lower() == wanted:
                return str(folder["id"]), None
    names = ", ".join(str(f.get("displayName")) for f in folders[:30])
    return None, (
        f"[tool-error] no mail folder named '{raw}'. Folders: {names or '(none)'}. "
        "ms365_list_mail_folders lists nested ones with their ids."
    )


def _message_line(index: int, m: dict[str, Any]) -> str:
    flags = []
    if not m.get("isRead", True):
        flags.append("unread")
    if m.get("hasAttachments"):
        flags.append("attachment")
    if (m.get("flag") or {}).get("flagStatus") == "flagged":
        flags.append("flagged")
    if m.get("importance") == "high":
        flags.append("high importance")
    if m.get("isDraft"):
        flags.append("draft")
    marks = f"  [{', '.join(flags)}]" if flags else ""
    preview = _clip(m.get("bodyPreview"), 160)
    return (
        f"{index}. {_fmt_dt(m.get('receivedDateTime'))}  From: {_addr(m.get('from'))}  "
        f"Subject: {m.get('subject') or '(no subject)'}{marks}\n"
        f"   id: {m.get('id')}" + (f"\n   Preview: {preview}" if preview else "")
    )


@tool
def ms365_list_messages(
    folder: Annotated[
        str,
        "Folder to read: inbox (default), sent, drafts, archive, deleted, junk, a folder "
        "name, a folder id, or 'all' for every folder.",
    ] = "inbox",
    query: Annotated[
        str,
        "Free-text search over sender, subject and body, e.g. 'invoice' or "
        "'from:ana subject:q3'. Searches return at most 1,000 matches.",
    ] = "",
    unread_only: Annotated[bool, "Only unread messages."] = False,
    from_address: Annotated[str, "Only messages from this email address."] = "",
    received_after: Annotated[
        str, "Only messages received on or after this date (YYYY-MM-DD)."
    ] = "",
    received_before: Annotated[str, "Only messages received before this date (YYYY-MM-DD)."] = "",
    top: Annotated[int, "How many messages to return (1-100)."] = 10,
    page_token: PageTokenArg = "",
    user_id: UserArg = "me",
) -> str:
    """List Outlook messages, newest first, with sender, subject, flags and a preview.

    Use ms365_get_message for a full message and its attachments.
    """
    if page_token:
        data, err = _request("GET", page_token)
    else:
        box = (folder or "inbox").strip()
        if box.lower() in ("", "all", "*"):
            path = f"{_user(user_id)}/messages"
        else:
            folder_id, err = _mail_folder_id(box, user_id)
            if err:
                return err
            path = f"{_user(user_id)}/mailFolders/{_seg(folder_id or '')}/messages"
        params: dict[str, Any] = {"$top": _top(top), "$select": _MESSAGE_LIST_FIELDS}
        if query.strip():
            # $search cannot be combined with $filter or $orderby on messages,
            # so the other filters travel inside the search string (KQL).
            terms = [_search_str(query)]
            if from_address.strip():
                terms.append(f"from:{_search_str(from_address)}")
            if received_after.strip():
                terms.append(f"received>={_search_str(received_after)[:10]}")
            if received_before.strip():
                terms.append(f"received<{_search_str(received_before)[:10]}")
            params["$search"] = f'"{" ".join(t for t in terms if t)}"'
        else:
            # With $orderby, Graph requires the ordered property to lead the
            # $filter, so receivedDateTime always comes first.
            after = received_after.strip()[:10] or "1900-01-01"
            clauses = [f"receivedDateTime ge {after}T00:00:00Z"]
            if received_before.strip():
                clauses.append(f"receivedDateTime lt {received_before.strip()[:10]}T00:00:00Z")
            if unread_only:
                clauses.append("isRead eq false")
            if from_address.strip():
                clauses.append(f"from/emailAddress/address eq '{_odata_str(from_address.strip())}'")
            params["$filter"] = " and ".join(clauses)
            params["$orderby"] = "receivedDateTime desc"
        data, err = _request("GET", path, params=params)
    if err:
        return err
    messages = _values(data)
    if query.strip() and unread_only:
        messages = [m for m in messages if not m.get("isRead", True)]
    if not messages:
        return "No messages found." + _page_hint(data)
    lines = [f"{len(messages)} message(s):"]
    lines += [_message_line(i, m) for i, m in enumerate(messages, start=1)]
    return "\n".join(lines) + _page_hint(data)


@tool
def ms365_get_message(
    message_id: Annotated[str, "Message id from ms365_list_messages."],
    max_chars: Annotated[int, "Longest body to return, in characters."] = 20000,
    user_id: UserArg = "me",
) -> str:
    """Read one Outlook message in full: headers, body as text, and its attachments.

    Attachments (documents and pictures pasted into the body alike) are listed with
    ids; save them with ms365_download_attachment to open them.
    """
    base = f"{_user(user_id)}/messages/{_seg(message_id)}"
    data, err = _request(
        "GET",
        base,
        params={
            "$select": (
                "id,subject,from,toRecipients,ccRecipients,bccRecipients,replyTo,"
                "receivedDateTime,sentDateTime,body,hasAttachments,isRead,isDraft,flag,"
                "importance,categories,conversationId,webLink"
            )
        },
        prefer=('outlook.body-content-type="text"',),
    )
    if err:
        return err
    m = data or {}
    body = str((m.get("body") or {}).get("content") or "")
    limit = max(500, int(max_chars or 20000))
    if len(body) > limit:
        body = body[:limit] + f"\n[truncated at {limit} characters]"
    lines = [
        f"Subject: {m.get('subject') or '(no subject)'}",
        f"From: {_addr(m.get('from'))}",
        f"To: {_addrs(m.get('toRecipients'))}",
    ]
    if m.get("ccRecipients"):
        lines.append(f"Cc: {_addrs(m.get('ccRecipients'))}")
    if m.get("bccRecipients"):
        lines.append(f"Bcc: {_addrs(m.get('bccRecipients'))}")
    if m.get("replyTo"):
        lines.append(f"Reply-To: {_addrs(m.get('replyTo'))}")
    lines.append(f"Received: {_fmt_dt(m.get('receivedDateTime'))} UTC")
    status = ["read" if m.get("isRead") else "unread"]
    if m.get("isDraft"):
        status.append("draft")
    if (m.get("flag") or {}).get("flagStatus") == "flagged":
        status.append("flagged")
    if m.get("importance") and m.get("importance") != "normal":
        status.append(f"{m.get('importance')} importance")
    lines.append(f"Status: {', '.join(status)}")
    if m.get("categories"):
        lines.append(f"Categories: {', '.join(m['categories'])}")
    lines.append(f"id: {m.get('id')}")
    if m.get("conversationId"):
        lines.append(f"Conversation id: {m.get('conversationId')}")
    if m.get("webLink"):
        lines.append(f"Open in Outlook: {m.get('webLink')}")
    # Always asked, never gated on hasAttachments: Graph reports false for a
    # message whose only attachments are inline (a pasted screenshot).
    att, att_err = _request(
        "GET",
        f"{base}/attachments",
        params={"$select": "id,name,contentType,size,isInline"},
    )
    if att_err:
        lines.append(f"Attachments: could not list them ({_clip(att_err, 160)})")
    else:
        items = _values(att)
        if items:
            lines.append(f"Attachments ({len(items)}), save with ms365_download_attachment:")
            lines += [_attachment_line(a) for a in items]
    lines.append("")
    lines.append(body or "(empty body)")
    return "\n".join(lines)


def _attachment_line(a: dict[str, Any]) -> str:
    kind = "inline image, shown inside the body" if a.get("isInline") else "attachment"
    return (
        f"  - {a.get('name')} ({a.get('contentType')}, {_human_size(a.get('size'))}, {kind}) "
        f"attachment_id: {a.get('id')}"
    )


def _message_payload(
    subject: str | None,
    body: str | None,
    to: str | None,
    cc: str | None,
    bcc: str | None,
    importance: str = "",
) -> tuple[dict[str, Any], str | None]:
    payload: dict[str, Any] = {}
    if subject is not None:
        payload["subject"] = subject
    if body is not None:
        payload["body"] = {"contentType": "HTML", "content": _html_body(body)}
    for field, value, key in (
        ("to", to, "toRecipients"),
        ("cc", cc, "ccRecipients"),
        ("bcc", bcc, "bccRecipients"),
    ):
        if value is None:
            continue
        parsed, err = _recipients(value, field)
        if err:
            return {}, err
        payload[key] = parsed
    level = (importance or "").strip().lower()
    if level:
        if level not in ("low", "normal", "high"):
            return {}, "[tool-error] importance must be low, normal or high."
        payload["importance"] = level
    return payload, None


def _file_attachment(path: Path, content_id: str | None = None) -> dict[str, Any]:
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    item: dict[str, Any] = {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": path.name,
        "contentType": mime,
        "contentBytes": base64.b64encode(path.read_bytes()).decode(),
    }
    if content_id:
        item["isInline"] = True
        item["contentId"] = content_id
    return item


def _attachment_items(
    files: list[Path], inline: list[tuple[Path, str]]
) -> list[tuple[Path, str | None]]:
    """Regular files first, then inline images, each with its content id."""
    return [(p, None) for p in files] + [(p, cid) for p, cid in inline]


def _attach(
    message_path: str, files: list[Path], inline: list[tuple[Path, str]] | None = None
) -> str | None:
    """Attach local files to a draft: one POST up to 3 MB, an upload session past it."""
    for path, content_id in _attachment_items(files, inline or []):
        size = path.stat().st_size
        if size > _ATTACHMENT_MAX:
            return f"[tool-error] {path.name} is {_human_size(size)}; Outlook's limit is 150 MB."
        if size < _INLINE_ATTACHMENT_MAX:
            _, err = _request(
                "POST", f"{message_path}/attachments", body=_file_attachment(path, content_id)
            )
        else:
            mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            item: dict[str, Any] = {
                "attachmentType": "file",
                "name": path.name,
                "size": size,
                "contentType": mime,
            }
            if content_id:
                item["isInline"] = True
                item["contentId"] = content_id
            session, err = _request(
                "POST",
                f"{message_path}/attachments/createUploadSession",
                body={"AttachmentItem": item},
            )
            if not err:
                upload_url = str((session or {}).get("uploadUrl") or "")
                if not upload_url:
                    return "[tool-error] MS365: Graph returned no upload URL for the attachment."
                _, err = _upload_session_put(upload_url, path)
        if err:
            return err
    return None


def _discard_draft(message_path: str) -> None:
    """Best effort: remove a draft a failed compose left behind."""
    _request("DELETE", message_path)


def _draft_from(
    user_id: str,
    message_id: str,
    mode: str,
    body: str,
    extra: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """A reply, reply-all or forward draft that keeps the thread quoted below ``body``."""
    action = {"reply": "createReply", "reply_all": "createReplyAll", "forward": "createForward"}[
        mode
    ]
    draft, err = _request("POST", f"{_user(user_id)}/messages/{_seg(message_id)}/{action}", body={})
    if err:
        return None, err
    draft = draft or {}
    draft_path = f"{_user(user_id)}/messages/{_seg(str(draft.get('id') or ''))}"
    patch = dict(extra)
    if body.strip():
        current = draft.get("body") or {}
        existing = str(current.get("content") or "")
        if existing and str(current.get("contentType") or "").lower() == "text":
            existing = _html_body(existing)
        patch["body"] = {
            "contentType": "HTML",
            "content": _prepend_html(_html_body(body), existing),
        }
    if patch:
        updated, err = _request("PATCH", draft_path, body=patch)
        if err:
            _discard_draft(draft_path)
            return None, err
        draft.update(
            {k: v for k, v in (updated or {}).items() if k in ("toRecipients", "ccRecipients")}
        )
    return draft, None


def _mode(reply_all: bool) -> str:
    return "reply_all" if reply_all else "reply"


@tool
def ms365_send_mail(
    to: Annotated[str, "Recipients, comma-separated: 'ana@x.com, Bo Li <bo@y.com>'."],
    subject: Annotated[str, "Subject line."],
    body: Annotated[str, _BODY_HELP],
    cc: Annotated[str, "Cc recipients, comma-separated."] = "",
    bcc: Annotated[str, "Bcc recipients, comma-separated."] = "",
    attachments: Annotated[str, _ATTACH_HELP] = "",
    inline_images: Annotated[str, _INLINE_HELP] = "",
    importance: Annotated[str, "low, normal (default) or high."] = "normal",
    user_id: UserArg = "me",
) -> str:
    """Send an Outlook email now, as HTML, with optional attachments and inline images.

    To let the user review it first, use ms365_create_draft.
    """
    payload, err = _message_payload(subject, body, to, cc, bcc, importance)
    if err:
        return err
    if not payload.get("toRecipients"):
        return "[tool-error] to: at least one recipient is required."
    files, err = _local_files(attachments)
    if err:
        return err
    inline, err = _inline_specs(inline_images)
    if err:
        return err
    err = _check_inline_refs(payload["body"]["content"], inline)
    if err:
        return err
    items = _attachment_items(files, inline)
    total = sum(p.stat().st_size for p, _ in items)
    who = _addresses(payload["toRecipients"])
    extras = []
    if payload.get("ccRecipients"):
        extras.append(f"cc {_addresses(payload['ccRecipients'])}")
    if payload.get("bccRecipients"):
        extras.append(f"bcc {_addresses(payload['bccRecipients'])}")
    if files:
        extras.append(f"{len(files)} attachment(s)")
    if inline:
        extras.append(f"{len(inline)} inline image(s)")
    suffix = f" ({'; '.join(extras)})" if extras else ""

    if total <= _SENDMAIL_INLINE_TOTAL:
        if items:
            payload["attachments"] = [_file_attachment(p, cid) for p, cid in items]
        _, err = _request(
            "POST",
            f"{_user(user_id)}/sendMail",
            body={"message": payload, "saveToSentItems": True},
        )
        return err or f"Sent Microsoft 365 email to {who}{suffix}."

    # Large attachments cannot ride in sendMail's 4 MB body: draft, attach, send.
    draft, err = _request("POST", f"{_user(user_id)}/messages", body=payload)
    if err:
        return err
    draft_path = f"{_user(user_id)}/messages/{_seg(str((draft or {}).get('id') or ''))}"
    err = _attach(draft_path, files, inline)
    if not err:
        _, err = _request("POST", f"{draft_path}/send")
    if err:
        _discard_draft(draft_path)
        return err
    return f"Sent Microsoft 365 email to {who}{suffix}."


@tool
def ms365_create_draft(
    to: Annotated[
        str,
        "Recipients, comma-separated. On a reply, leave empty to answer the original sender(s).",
    ] = "",
    subject: Annotated[
        str, "Subject line. On a reply or forward, empty keeps 'RE:'/'FW:' + the original."
    ] = "",
    body: Annotated[str, _BODY_HELP + " On a reply it sits above the quoted thread."] = "",
    cc: Annotated[str, "Cc recipients, comma-separated."] = "",
    bcc: Annotated[str, "Bcc recipients, comma-separated."] = "",
    attachments: Annotated[str, _ATTACH_HELP] = "",
    inline_images: Annotated[str, _INLINE_HELP] = "",
    importance: Annotated[str, "low, normal or high."] = "",
    reply_to_message_id: Annotated[
        str,
        "To draft a reply or forward of an existing message, its id. Empty drafts a new message.",
    ] = "",
    mode: Annotated[
        str, "With reply_to_message_id: reply (default), reply_all or forward."
    ] = "reply",
    user_id: UserArg = "me",
) -> str:
    """Save an Outlook draft (HTML body, attachments, inline images) without sending it, so
    the user can review it in Outlook.

    Send it later with ms365_send_draft or change it with ms365_update_draft.
    """
    files, err = _local_files(attachments)
    if err:
        return err
    inline, err = _inline_specs(inline_images)
    if err:
        return err
    err = _check_inline_refs(_html_body(body), inline)
    if err:
        return err
    base = _user(user_id)
    if reply_to_message_id.strip():
        kind = (mode or "reply").strip().lower().replace("-", "_").replace(" ", "_")
        if kind not in ("reply", "reply_all", "forward"):
            return "[tool-error] mode must be reply, reply_all or forward."
        if kind == "forward" and not to.strip():
            return "[tool-error] to: a forward needs at least one recipient."
        extra, err = _message_payload(
            subject if subject.strip() else None,
            None,
            to if to.strip() else None,
            cc if cc.strip() else None,
            bcc if bcc.strip() else None,
            importance,
        )
        if err:
            return err
        draft, err = _draft_from(user_id, reply_to_message_id, kind, body, extra)
    else:
        payload, err = _message_payload(subject, body, to, cc, bcc, importance)
        if err:
            return err
        draft, err = _request("POST", f"{base}/messages", body=payload)
    if err:
        return err
    draft = draft or {}
    draft_path = f"{base}/messages/{_seg(str(draft.get('id') or ''))}"
    err = _attach(draft_path, files, inline)
    if err:
        _discard_draft(draft_path)
        return err
    lines = [
        "Draft saved in Drafts (not sent).",
        f"Draft id: {draft.get('id')}",
        f"To: {_addrs(draft.get('toRecipients'))}",
    ]
    if draft.get("webLink"):
        lines.append(f"Review it in Outlook: {draft.get('webLink')}")
    lines.append("Send it with ms365_send_draft; change it with ms365_update_draft.")
    return "\n".join(lines)


@tool
def ms365_update_draft(
    draft_id: Annotated[
        str, "Draft id from ms365_create_draft or ms365_list_messages(folder='drafts')."
    ],
    subject: Annotated[str | None, "New subject. Omit to keep."] = None,
    body: Annotated[
        str | None,
        "New body, replacing the whole body (a quoted earlier message included). Omit to "
        "keep. " + _BODY_HELP,
    ] = None,
    to: Annotated[str | None, "New To list (replaces it). Omit to keep."] = None,
    cc: Annotated[str | None, "New Cc list; '' clears it. Omit to keep."] = None,
    bcc: Annotated[str | None, "New Bcc list; '' clears it. Omit to keep."] = None,
    importance: Annotated[str, "low, normal or high. Empty keeps it."] = "",
    add_attachments: Annotated[str, _ATTACH_HELP] = "",
    add_inline_images: Annotated[
        str,
        _INLINE_HELP + " The body (new or kept) must reference each one as cid:FILENAME.",
    ] = "",
    remove_attachment_ids: Annotated[
        str, "Attachment ids to remove, comma-separated (ms365_get_message lists them)."
    ] = "",
    user_id: UserArg = "me",
) -> str:
    """Change an unsent Outlook draft: recipients, subject, body, attachments or inline
    images."""
    payload, err = _message_payload(subject, body, to, cc, bcc, importance)
    if err:
        return err
    files, err = _local_files(add_attachments)
    if err:
        return err
    inline, err = _inline_specs(add_inline_images)
    if err:
        return err
    if body is not None:
        # A kept body is not fetched: the cids it references may already be
        # attached, so only a body given here is checked against the images.
        err = _check_inline_refs(payload["body"]["content"], inline)
        if err:
            return err
    removals = [a.strip() for a in remove_attachment_ids.split(",") if a.strip()]
    if not payload and not files and not inline and not removals:
        return "[tool-error] nothing to change: pass at least one field."
    draft_path = f"{_user(user_id)}/messages/{_seg(draft_id)}"
    changed = []
    if payload:
        _, err = _request("PATCH", draft_path, body=payload)
        if err:
            return err
        changed.append(", ".join(sorted(payload)))
    for attachment_id in removals:
        _, err = _request("DELETE", f"{draft_path}/attachments/{_seg(attachment_id)}")
        if err:
            return err
    if removals:
        changed.append(f"removed {len(removals)} attachment(s)")
    err = _attach(draft_path, files, inline)
    if err:
        return err
    if files:
        changed.append(f"added {len(files)} attachment(s)")
    if inline:
        changed.append(f"added {len(inline)} inline image(s)")
    return f"Draft updated ({'; '.join(changed)}). It is still unsent."


@tool
def ms365_send_draft(
    draft_id: Annotated[str, "Draft id to send."],
    user_id: UserArg = "me",
) -> str:
    """Send an existing Outlook draft."""
    draft_path = f"{_user(user_id)}/messages/{_seg(draft_id)}"
    draft, err = _request(
        "GET", draft_path, params={"$select": "isDraft,toRecipients,ccRecipients,bccRecipients"}
    )
    if err:
        return err
    draft = draft or {}
    if not draft.get("isDraft", True):
        return "[tool-error] that message is not a draft (it was already sent)."
    recipients = (
        list(draft.get("toRecipients") or [])
        + list(draft.get("ccRecipients") or [])
        + list(draft.get("bccRecipients") or [])
    )
    if not recipients:
        return "[tool-error] the draft has no recipients; add them with ms365_update_draft."
    _, err = _request("POST", f"{draft_path}/send")
    return err or f"Sent the draft to {_addrs(recipients)}."


def _compose_and_send(
    user_id: str,
    message_id: str,
    kind: str,
    body: str,
    extra: dict[str, Any],
    attachments: str,
    inline_images: str = "",
) -> tuple[dict[str, Any] | None, str | None]:
    files, err = _local_files(attachments)
    if err:
        return None, err
    inline, err = _inline_specs(inline_images)
    if err:
        return None, err
    err = _check_inline_refs(_html_body(body), inline)
    if err:
        return None, err
    draft, err = _draft_from(user_id, message_id, kind, body, extra)
    if err:
        return None, err
    draft = draft or {}
    draft_path = f"{_user(user_id)}/messages/{_seg(str(draft.get('id') or ''))}"
    err = _attach(draft_path, files, inline)
    if not err:
        _, err = _request("POST", f"{draft_path}/send")
    if err:
        _discard_draft(draft_path)
        return None, err
    return draft, None


@tool
def ms365_reply(
    message_id: Annotated[str, "Id of the message to answer."],
    body: Annotated[str, "Reply text; it sits above the quoted thread. " + _BODY_HELP],
    reply_all: Annotated[bool, "Reply to everyone on the message, not only the sender."] = False,
    cc: Annotated[str, "Extra Cc recipients, comma-separated (replaces the Cc list)."] = "",
    attachments: Annotated[str, _ATTACH_HELP] = "",
    inline_images: Annotated[str, _INLINE_HELP] = "",
    user_id: UserArg = "me",
) -> str:
    """Reply to an Outlook message now, in the same thread, with an HTML body and optional
    attachments or inline images. For a reviewable draft use ms365_create_draft with
    reply_to_message_id."""
    if not body.strip():
        return "[tool-error] body: the reply needs some text."
    extra, err = _message_payload(None, None, None, cc if cc.strip() else None, None)
    if err:
        return err
    draft, err = _compose_and_send(
        user_id, message_id, _mode(reply_all), body, extra, attachments, inline_images
    )
    if err:
        return err
    return f"Reply sent to {_addrs((draft or {}).get('toRecipients'))}."


@tool
def ms365_forward(
    message_id: Annotated[str, "Id of the message to forward."],
    to: Annotated[str, "Recipients, comma-separated."],
    comment: Annotated[str, "Note above the forwarded message. " + _BODY_HELP] = "",
    cc: Annotated[str, "Cc recipients, comma-separated."] = "",
    attachments: Annotated[str, "Extra files to add. " + _ATTACH_HELP] = "",
    inline_images: Annotated[str, _INLINE_HELP] = "",
    user_id: UserArg = "me",
) -> str:
    """Forward an Outlook message now, with its attachments, an optional HTML note, and
    optional extra attachments or inline images."""
    extra, err = _message_payload(None, None, to, cc if cc.strip() else None, None)
    if err:
        return err
    if not extra.get("toRecipients"):
        return "[tool-error] to: at least one recipient is required."
    _, err = _compose_and_send(
        user_id, message_id, "forward", comment, extra, attachments, inline_images
    )
    return err or f"Forwarded the message to {_addresses(extra['toRecipients'])}."


@tool
def ms365_update_message(
    message_id: Annotated[str, "Message id."],
    is_read: Annotated[bool | None, "true marks it read, false unread. Omit to keep."] = None,
    flag: Annotated[str, "flagged, complete, or clear. Empty keeps the flag."] = "",
    flag_due_date: Annotated[str, "Due date for the flag (YYYY-MM-DD); implies flagged."] = "",
    categories: Annotated[
        str | None, "Outlook categories, comma-separated (replaces them); '' clears. Omit to keep."
    ] = None,
    importance: Annotated[str, "low, normal or high. Empty keeps it."] = "",
    user_id: UserArg = "me",
) -> str:
    """Mark an Outlook message read or unread, flag or unflag it, or set its categories."""
    patch: dict[str, Any] = {}
    done: list[str] = []
    if is_read is not None:
        patch["isRead"] = bool(is_read)
        done.append("marked read" if is_read else "marked unread")
    state = (flag or "").strip().lower()
    if flag_due_date.strip():
        state = state or "flagged"
    if state:
        mapping = {
            "flagged": "flagged",
            "flag": "flagged",
            "complete": "complete",
            "completed": "complete",
            "done": "complete",
            "clear": "notFlagged",
            "none": "notFlagged",
            "unflag": "notFlagged",
            "notflagged": "notFlagged",
        }
        status = mapping.get(state.replace(" ", ""))
        if status is None:
            return "[tool-error] flag must be flagged, complete or clear."
        flag_body: dict[str, Any] = {"flagStatus": status}
        if flag_due_date.strip() and status == "flagged":
            if not _is_date_only(flag_due_date):
                return _bad_when("flag_due_date", flag_due_date)
            zone = _mailbox_timezone(user_id)
            day = flag_due_date.strip()
            today = _start_of_today(zone).strftime("%Y-%m-%d")
            flag_body["startDateTime"] = {
                "dateTime": f"{min(today, day)}T00:00:00",
                "timeZone": zone,
            }
            flag_body["dueDateTime"] = {"dateTime": f"{day}T00:00:00", "timeZone": zone}
        patch["flag"] = flag_body
        done.append(
            {"flagged": "flagged", "complete": "flag completed", "notFlagged": "flag cleared"}[
                status
            ]
        )
    if categories is not None:
        patch["categories"] = [c.strip() for c in categories.split(",") if c.strip()]
        done.append("categories set")
    level = importance.strip().lower()
    if level:
        if level not in ("low", "normal", "high"):
            return "[tool-error] importance must be low, normal or high."
        patch["importance"] = level
        done.append(f"importance {level}")
    if not patch:
        return "[tool-error] nothing to change: pass is_read, flag, categories or importance."
    _, err = _request("PATCH", f"{_user(user_id)}/messages/{_seg(message_id)}", body=patch)
    return err or f"Updated the message: {', '.join(done)}."


@tool
def ms365_move_message(
    message_id: Annotated[str, "Message id."],
    destination: Annotated[
        str,
        "Target folder: inbox, archive, deleted (trash), junk, drafts, a folder name, or a folder id.",
    ],
    user_id: UserArg = "me",
) -> str:
    """Move an Outlook message to another folder, e.g. Archive or Deleted Items."""
    folder_id, err = _mail_folder_id(destination, user_id)
    if err:
        return err
    data, err = _request(
        "POST",
        f"{_user(user_id)}/messages/{_seg(message_id)}/move",
        body={"destinationId": folder_id},
    )
    if err:
        return err
    return f"Moved the message to {destination.strip()}. Its id is now: {(data or {}).get('id') or message_id}"


@tool
def ms365_list_mail_folders(
    parent_folder: Annotated[
        str, "Empty for top-level folders, or a folder id/name to list its subfolders."
    ] = "",
    user_id: UserArg = "me",
) -> str:
    """List Outlook mail folders with unread and total counts."""
    base = f"{_user(user_id)}/mailFolders"
    if parent_folder.strip():
        folder_id, err = _mail_folder_id(parent_folder, user_id)
        if err:
            return err
        base = f"{base}/{_seg(folder_id or '')}/childFolders"
    data, err = _request(
        "GET",
        base,
        params={
            "$top": 100,
            "$select": "id,displayName,unreadItemCount,totalItemCount,childFolderCount",
        },
    )
    if err:
        return err
    folders = _values(data)
    if not folders:
        return "No folders found."
    return "\n".join(
        f"- {f.get('displayName')}: {f.get('unreadItemCount', 0)} unread / "
        f"{f.get('totalItemCount', 0)} total"
        + (f", {f.get('childFolderCount')} subfolder(s)" if f.get("childFolderCount") else "")
        + f"\n  id: {f.get('id')}"
        for f in folders
    )


@tool
def ms365_download_attachment(
    message_id: Annotated[str, "Message id."],
    attachment_id: Annotated[
        str, "One attachment id (ms365_get_message lists them). Empty saves all."
    ] = "",
    save_dir: Annotated[str, "Folder inside the workspace. Empty uses 'downloads'."] = "",
    include_inline: Annotated[
        bool,
        "When saving all: also save the pictures embedded in the body (pasted screenshots, "
        "logos). False saves only the regular attachments.",
    ] = True,
    user_id: UserArg = "me",
) -> str:
    """Save an Outlook message's attachments (documents and inline pictures) into the
    workspace, where other tools can open or describe them."""
    base = f"{_user(user_id)}/messages/{_seg(message_id)}/attachments"
    data, err = _request("GET", base, params={"$select": "id,name,contentType,size,isInline"})
    if err:
        return err
    items = _values(data)
    if attachment_id.strip():
        items = [a for a in items if a.get("id") == attachment_id.strip()]
        if not items:
            return "[tool-error] no attachment with that id on this message."
    elif not include_inline:
        items = [a for a in items if not a.get("isInline")]
    if not items:
        return "The message has no attachments."
    lines: list[str] = []
    for item in items:
        kind = str(item.get("@odata.type") or "")
        name = str(item.get("name") or "attachment")
        if kind.endswith("referenceAttachment"):
            lines.append(f"- {name}: a link to a cloud file, not stored in the message; skipped.")
            continue
        if kind.endswith("itemAttachment") and not name.lower().endswith(".eml"):
            name += ".eml"
        target, created, err = _download_target(name, save_dir, str(item.get("contentType") or ""))
        if err or target is None:
            return err or "[tool-error] could not choose a download path."
        size, err = _stream_to_file(
            f"{_GRAPH_BASE}/{base}/{_seg(str(item.get('id')))}/$value",
            target,
            authenticated=True,
        )
        if err:
            return err
        _handoff(target, created)
        note = ", inline image from the body" if item.get("isInline") else ""
        lines.append(f"Saved: {target} ({_human_size(size)}{note})")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# Calendar
# ═══════════════════════════════════════════════════════════════════════════

_EVENT_LIST_FIELDS = (
    "id,subject,start,end,location,organizer,attendees,isOnlineMeeting,onlineMeeting,"
    "isAllDay,isCancelled,showAs,responseStatus,isOrganizer,type,webLink,bodyPreview"
)

_RESPONSE_LABELS = {
    "accepted": "accepted",
    "tentativelyAccepted": "tentative",
    "declined": "declined",
    "notResponded": "no response",
    "none": "no response",
    "organizer": "organizer",
}


def _attendee_summary(attendees: Any, limit: int = 6) -> str:
    people = [a for a in (attendees or []) if isinstance(a, dict)]
    if not people:
        return ""
    shown = []
    for a in people[:limit]:
        response = _RESPONSE_LABELS.get(str((a.get("status") or {}).get("response") or ""), "")
        who = (a.get("emailAddress") or {}).get("address") or _addr(a)
        shown.append(f"{who} ({response})" if response else str(who))
    more = f", +{len(people) - limit} more" if len(people) > limit else ""
    return ", ".join(shown) + more


def _event_line(e: dict[str, Any]) -> str:
    when = _fmt_span(e.get("start"), e.get("end"), bool(e.get("isAllDay")))
    parts = [f"- {when}  {e.get('subject') or '(no subject)'}"]
    if e.get("isCancelled"):
        parts[0] += "  [cancelled]"
    location = (e.get("location") or {}).get("displayName")
    if location:
        parts.append(f"  Where: {location}")
    if not e.get("isOrganizer"):
        parts.append(f"  Organizer: {_addr(e.get('organizer'))}")
    response = str((e.get("responseStatus") or {}).get("response") or "")
    if response and response not in ("organizer", "none"):
        parts.append(f"  Your response: {_RESPONSE_LABELS.get(response, response)}")
    attendees = _attendee_summary(e.get("attendees"))
    if attendees:
        parts.append(f"  Attendees: {attendees}")
    join = (e.get("onlineMeeting") or {}).get("joinUrl")
    if join:
        parts.append(f"  Join: {join}")
    parts.append(f"  id: {e.get('id')}")
    return "\n".join(parts)


def _recurrence(
    kind: str,
    interval: int,
    days_of_week: str,
    end_date: str,
    occurrences: int,
    first_day: date,
) -> tuple[dict[str, Any] | None, str | None]:
    """A Graph ``patternedRecurrence`` from the tool's plain arguments."""
    pattern_kind = (kind or "").strip().lower()
    if pattern_kind in ("", "none", "no"):
        return None, None
    step = max(1, int(interval or 1))
    days: list[str] = []
    for raw in [d.strip().lower() for d in (days_of_week or "").split(",") if d.strip()]:
        match = next((w for w in _WEEKDAYS if w.startswith(raw[:3])), None)
        if match is None:
            return None, f"[tool-error] recurrence_days: '{raw}' is not a weekday."
        days.append(match)
    if pattern_kind == "daily":
        pattern: dict[str, Any] = {"type": "daily", "interval": step}
    elif pattern_kind in ("weekdays", "workdays", "every weekday"):
        pattern = {"type": "weekly", "interval": 1, "daysOfWeek": list(_WEEKDAYS[:5])}
    elif pattern_kind == "weekly":
        pattern = {
            "type": "weekly",
            "interval": step,
            "daysOfWeek": days or [_WEEKDAYS[first_day.weekday()]],
            "firstDayOfWeek": "sunday",
        }
    elif pattern_kind == "monthly":
        pattern = {"type": "absoluteMonthly", "interval": step, "dayOfMonth": first_day.day}
    elif pattern_kind in ("yearly", "annually"):
        pattern = {
            "type": "absoluteYearly",
            "interval": step,
            "dayOfMonth": first_day.day,
            "month": first_day.month,
        }
    else:
        return None, "[tool-error] recurrence must be daily, weekdays, weekly, monthly or yearly."
    rng: dict[str, Any] = {"startDate": first_day.isoformat()}
    if end_date.strip():
        if not _is_date_only(end_date):
            return None, _bad_when("recurrence_end_date", end_date)
        rng.update({"type": "endDate", "endDate": end_date.strip()})
    elif occurrences and int(occurrences) > 0:
        rng.update({"type": "numbered", "numberOfOccurrences": int(occurrences)})
    else:
        rng["type"] = "noEnd"
    return {"pattern": pattern, "range": rng}, None


def _event_times(
    start: str, end: str, zone: str, all_day: bool
) -> tuple[dict[str, str] | None, dict[str, str] | None, str | None]:
    if all_day:
        first = _parse_when(start)
        if first is None:
            return None, None, _bad_when("start_datetime", start)
        last = _parse_when(end) if end.strip() else None
        if end.strip() and last is None:
            return None, None, _bad_when("end_datetime", end)
        day_one = first.date()
        # Graph wants the midnight after the last day (exclusive). A date names
        # the last day itself; a date-time at midnight is already exclusive.
        if last is None:
            day_end = day_one + timedelta(days=1)
        elif _is_date_only(end) or last.time() != datetime.min.time():
            day_end = last.date() + timedelta(days=1)
        else:
            day_end = last.date()
        if day_end <= day_one:
            day_end = day_one + timedelta(days=1)
        return (
            {"dateTime": f"{day_one.isoformat()}T00:00:00", "timeZone": zone},
            {"dateTime": f"{day_end.isoformat()}T00:00:00", "timeZone": zone},
            None,
        )
    s, err = _graph_dt("start_datetime", start, zone)
    if err:
        return None, None, err
    e, err = _graph_dt("end_datetime", end, zone)
    if err:
        return None, None, err
    return s, e, None


@tool
def ms365_list_events(
    start: Annotated[
        str, "Window start, e.g. '2026-10-05' or '2026-10-05T13:00'. Empty = start of today."
    ] = "",
    end: Annotated[str, "Window end (a date includes that whole day). Empty = start + days."] = "",
    days: Annotated[int, "Window length in days when end is empty."] = 7,
    query: Annotated[
        str, "Only events whose subject, location, organizer or preview contains this text."
    ] = "",
    calendar_id: Annotated[
        str, "A calendar id from ms365_list_calendars. Empty = the default calendar."
    ] = "",
    time_zone: TimeZoneArg = "",
    top: Annotated[int, "Most events to return (1-100)."] = 50,
    page_token: PageTokenArg = "",
    user_id: UserArg = "me",
) -> str:
    """List calendar events in a time window, in order, with each occurrence of a
    recurring meeting, attendees and their responses, and Teams join links."""
    zone = _resolve_tz(time_zone, user_id)
    if page_token:
        data, err = _request("GET", page_token, prefer=(_tz_prefer(zone),))
        window = ""
    else:
        if start.strip():
            begin, err = _instant("start", start, zone)
            if err:
                return err
        else:
            begin = _start_of_today(zone).astimezone(timezone.utc)
        if end.strip():
            finish, err = _instant("end", end, zone)
            if err:
                return err
            if _is_date_only(end) and finish is not None:
                finish += timedelta(days=1)
        else:
            finish = (begin or datetime.now(timezone.utc)) + timedelta(days=max(1, int(days or 7)))
        if begin is None or finish is None or finish <= begin:
            return "[tool-error] end must be after start."
        base = _user(user_id)
        path = (
            f"{base}/calendars/{_seg(calendar_id)}/calendarView"
            if calendar_id.strip()
            else f"{base}/calendarView"
        )
        data, err = _request(
            "GET",
            path,
            params={
                "startDateTime": _utc_iso(begin),
                "endDateTime": _utc_iso(finish),
                "$orderby": "start/dateTime",
                "$top": _top(top, 50),
                "$select": _EVENT_LIST_FIELDS,
            },
            prefer=(_tz_prefer(zone),),
        )
        window = f" from {_utc_iso(begin)} to {_utc_iso(finish)} (UTC bounds)"
    if err:
        return err
    events = _values(data)
    needle = query.strip().lower()
    if needle:
        events = [
            e
            for e in events
            if needle
            in " ".join(
                [
                    str(e.get("subject") or ""),
                    str((e.get("location") or {}).get("displayName") or ""),
                    _addr(e.get("organizer")),
                    str(e.get("bodyPreview") or ""),
                ]
            ).lower()
        ]
    if not events:
        return f"No events{window}." + _page_hint(data)
    header = f"{len(events)} event(s){window}, times in {zone}:"
    return header + "\n" + "\n".join(_event_line(e) for e in events) + _page_hint(data)


@tool
def ms365_get_event(
    event_id: Annotated[str, "Event id from ms365_list_events."],
    time_zone: TimeZoneArg = "",
    user_id: UserArg = "me",
) -> str:
    """Read one calendar event in full: description, attendees and responses,
    recurrence, Teams link."""
    zone = _resolve_tz(time_zone, user_id)
    data, err = _request(
        "GET",
        f"{_user(user_id)}/events/{_seg(event_id)}",
        params={
            "$select": (
                "id,subject,start,end,location,organizer,attendees,isOnlineMeeting,"
                "onlineMeeting,isAllDay,isCancelled,showAs,responseStatus,isOrganizer,"
                "type,webLink,body,recurrence,seriesMasterId,importance,sensitivity,"
                "reminderMinutesBeforeStart,isReminderOn,allowNewTimeProposals,categories"
            )
        },
        prefer=(_tz_prefer(zone), 'outlook.body-content-type="text"'),
    )
    if err:
        return err
    e = data or {}
    lines = [
        f"Subject: {e.get('subject') or '(no subject)'}",
        f"When: {_fmt_span(e.get('start'), e.get('end'), bool(e.get('isAllDay')))} ({zone})",
    ]
    if e.get("isCancelled"):
        lines.append("Status: cancelled")
    location = (e.get("location") or {}).get("displayName")
    if location:
        lines.append(f"Where: {location}")
    lines.append(
        f"Organizer: {_addr(e.get('organizer'))}" + (" (you)" if e.get("isOrganizer") else "")
    )
    response = str((e.get("responseStatus") or {}).get("response") or "")
    if response and not e.get("isOrganizer"):
        lines.append(f"Your response: {_RESPONSE_LABELS.get(response, response)}")
    attendees = [a for a in (e.get("attendees") or []) if isinstance(a, dict)]
    if attendees:
        lines.append(f"Attendees ({len(attendees)}):")
        for a in attendees:
            status = _RESPONSE_LABELS.get(str((a.get("status") or {}).get("response") or ""), "")
            lines.append(f"  - {_addr(a)} [{a.get('type', 'required')}] {status}".rstrip())
    join = (e.get("onlineMeeting") or {}).get("joinUrl")
    if join:
        lines.append(f"Teams join link: {join}")
    recurrence = e.get("recurrence") or {}
    if recurrence:
        pattern = recurrence.get("pattern") or {}
        rng = recurrence.get("range") or {}
        days = ", ".join(pattern.get("daysOfWeek") or [])
        until = (
            rng.get("endDate")
            if rng.get("type") == "endDate"
            else (
                f"{rng.get('numberOfOccurrences')} times"
                if rng.get("type") == "numbered"
                else "no end"
            )
        )
        lines.append(
            f"Repeats: {pattern.get('type')} every {pattern.get('interval', 1)}"
            + (f" on {days}" if days else "")
            + f", from {rng.get('startDate')} until {until}"
        )
    if e.get("type") == "occurrence" and e.get("seriesMasterId"):
        lines.append(f"Part of a series; series id: {e.get('seriesMasterId')}")
    if e.get("isReminderOn"):
        lines.append(f"Reminder: {e.get('reminderMinutesBeforeStart')} minutes before")
    if e.get("showAs"):
        lines.append(f"Show as: {e.get('showAs')}")
    lines.append(f"id: {e.get('id')}")
    if e.get("webLink"):
        lines.append(f"Open in Outlook: {e.get('webLink')}")
    body = str((e.get("body") or {}).get("content") or "").strip()
    if body:
        lines += ["", _clip(body, 4000) if len(body) > 4000 else body]
    return "\n".join(lines)


@tool
def ms365_create_event(
    subject: Annotated[str, "Event title."],
    start_datetime: Annotated[
        str, "Start, e.g. '2026-10-05T09:30' (local to time_zone). A date for all-day events."
    ],
    end_datetime: Annotated[str, "End, e.g. '2026-10-05T10:00'. For all-day events, the last day."],
    time_zone: TimeZoneArg = "",
    attendees: Annotated[
        str,
        "Required attendees' emails, comma-separated. Outlook emails them an invitation. "
        "Resolve names with ms365_find_people first.",
    ] = "",
    optional_attendees: Annotated[str, "Optional attendees' emails, comma-separated."] = "",
    body: Annotated[str, "Description / agenda, HTML or plain text."] = "",
    location: Annotated[str, "Room or address."] = "",
    online_meeting: Annotated[bool, "Add a Teams meeting link (work or school accounts)."] = False,
    is_all_day: Annotated[bool, "An all-day event (start and end are dates)."] = False,
    recurrence: Annotated[
        str, "Repeat: daily, weekdays, weekly, monthly, yearly. Empty = once."
    ] = "",
    recurrence_interval: Annotated[int, "Repeat every N days/weeks/months/years."] = 1,
    recurrence_days: Annotated[
        str, "For weekly: days, e.g. 'monday,wednesday'. Empty = the start's weekday."
    ] = "",
    recurrence_end_date: Annotated[str, "Last date of the series (YYYY-MM-DD)."] = "",
    recurrence_count: Annotated[int, "Or: number of occurrences. Both empty = no end."] = 0,
    reminder_minutes: Annotated[
        int, "Reminder this many minutes before; -1 keeps Outlook's default."
    ] = -1,
    show_as: Annotated[str, "free, tentative, busy, oof or workingElsewhere. Empty = busy."] = "",
    calendar_id: Annotated[
        str, "Calendar id from ms365_list_calendars. Empty = default calendar."
    ] = "",
    user_id: UserArg = "me",
) -> str:
    """Create a calendar event or meeting. With attendees, Outlook sends the invitations;
    online_meeting adds a Teams link."""
    zone = _resolve_tz(time_zone, user_id)
    start_obj, end_obj, err = _event_times(start_datetime, end_datetime, zone, is_all_day)
    if err or start_obj is None or end_obj is None:
        return err or "[tool-error] could not read the event times."
    required, err = _recipients(attendees, "attendees")
    if err:
        return err
    optional, err = _recipients(optional_attendees, "optional_attendees")
    if err:
        return err
    payload: dict[str, Any] = {
        "subject": subject,
        "start": start_obj,
        "end": end_obj,
        "isAllDay": bool(is_all_day),
        "attendees": [dict(a, type="required") for a in required]
        + [dict(a, type="optional") for a in optional],
    }
    if body.strip():
        payload["body"] = {"contentType": "HTML", "content": _html_body(body)}
    if location.strip():
        payload["location"] = {"displayName": location.strip()}
    if online_meeting:
        payload["isOnlineMeeting"] = True
        payload["onlineMeetingProvider"] = "teamsForBusiness"
    if reminder_minutes is not None and int(reminder_minutes) >= 0:
        payload["isReminderOn"] = True
        payload["reminderMinutesBeforeStart"] = int(reminder_minutes)
    if show_as.strip():
        payload["showAs"] = show_as.strip()
    first_day = date.fromisoformat(start_obj["dateTime"][:10])
    pattern, err = _recurrence(
        recurrence,
        recurrence_interval,
        recurrence_days,
        recurrence_end_date,
        recurrence_count,
        first_day,
    )
    if err:
        return err
    if pattern:
        payload["recurrence"] = pattern
    # A retried create (the error boundary retries on what looks like a refused
    # sign-in) must not book the meeting twice; Graph drops a repeated
    # transactionId. The 15-minute bucket keeps a deliberate re-create possible.
    basis = json.dumps(
        [
            user_id,
            calendar_id,
            subject,
            start_obj,
            end_obj,
            _addresses(required + optional),
            int(time.time() // 900),
        ],
        sort_keys=True,
    )
    payload["transactionId"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"olano-ms365-event:{basis}"))
    base = _user(user_id)
    path = (
        f"{base}/calendars/{_seg(calendar_id)}/events" if calendar_id.strip() else f"{base}/events"
    )
    data, err = _request("POST", path, body=payload, prefer=(_tz_prefer(zone),))
    if err and ": 409 " in err:
        return (
            "An identical event (same title, time and attendees) was created moments ago, so "
            "this request was not booked again. Find it with ms365_list_events."
        )
    if err:
        return err
    d = data or {}
    lines = [
        f"Created event: {d.get('subject') or subject}",
        f"When: {_fmt_span(d.get('start') or start_obj, d.get('end') or end_obj, bool(is_all_day))} ({zone})",
    ]
    if required or optional:
        lines.append(f"Invitations sent to: {_addresses(required + optional)}")
    join = (d.get("onlineMeeting") or {}).get("joinUrl")
    if join:
        lines.append(f"Teams join link: {join}")
    elif online_meeting:
        lines.append("No Teams link came back (personal accounts cannot create Teams meetings).")
    lines.append(f"id: {d.get('id')}")
    if d.get("webLink"):
        lines.append(f"Open in Outlook: {d.get('webLink')}")
    return "\n".join(lines)


@tool
def ms365_update_event(
    event_id: Annotated[str, "Event id."],
    subject: Annotated[str | None, "New title. Omit to keep."] = None,
    start_datetime: Annotated[str, "New start. If end is empty, the event keeps its length."] = "",
    end_datetime: Annotated[str, "New end."] = "",
    time_zone: TimeZoneArg = "",
    location: Annotated[str | None, "New location. Omit to keep."] = None,
    body: Annotated[str | None, "New description (replaces it). Omit to keep."] = None,
    add_attendees: Annotated[str, "Required attendees to add, comma-separated emails."] = "",
    add_optional_attendees: Annotated[str, "Optional attendees to add."] = "",
    remove_attendees: Annotated[str, "Attendee emails to remove."] = "",
    online_meeting: Annotated[bool | None, "true adds a Teams link. Omit to keep."] = None,
    show_as: Annotated[str, "free, tentative, busy, oof or workingElsewhere. Empty keeps it."] = "",
    reminder_minutes: Annotated[int | None, "Reminder minutes before start. Omit to keep."] = None,
    user_id: UserArg = "me",
) -> str:
    """Change a calendar event: reschedule, rename, move, edit attendees or add a Teams link.
    Attendees receive the update."""
    zone = _resolve_tz(time_zone, user_id)
    event_path = f"{_user(user_id)}/events/{_seg(event_id)}"
    current, err = _request(
        "GET",
        event_path,
        params={"$select": "start,end,attendees,isOrganizer,isAllDay"},
        prefer=(_tz_prefer("UTC"),),
    )
    if err:
        return err
    current = current or {}
    patch: dict[str, Any] = {}
    changed: list[str] = []
    if subject is not None:
        patch["subject"] = subject
        changed.append("title")
    if location is not None:
        patch["location"] = {"displayName": location}
        changed.append("location")
    if body is not None:
        patch["body"] = {"contentType": "HTML", "content": _html_body(body)}
        changed.append("description")
    if start_datetime.strip() and not end_datetime.strip():
        # Keep the length: compute both ends in UTC from the current event.
        try:
            old_start = datetime.fromisoformat(str(current["start"]["dateTime"])[:19])
            old_end = datetime.fromisoformat(str(current["end"]["dateTime"])[:19])
        except (KeyError, TypeError, ValueError):
            return "[tool-error] could not read the event's current times; pass end_datetime too."
        new_start, err = _instant("start_datetime", start_datetime, zone)
        if err or new_start is None:
            return err or _bad_when("start_datetime", start_datetime)
        new_end = new_start + (old_end - old_start)
        patch["start"] = {"dateTime": new_start.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"}
        patch["end"] = {"dateTime": new_end.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"}
        changed.append("time")
    elif start_datetime.strip() or end_datetime.strip():
        for field, value, key in (
            ("start_datetime", start_datetime, "start"),
            ("end_datetime", end_datetime, "end"),
        ):
            if value.strip():
                obj, err = _graph_dt(field, value, zone)
                if err:
                    return err
                patch[key] = obj
        changed.append("time")
    add_req, err = _recipients(add_attendees, "add_attendees")
    if err:
        return err
    add_opt, err = _recipients(add_optional_attendees, "add_optional_attendees")
    if err:
        return err
    removing = {
        a["emailAddress"]["address"].lower()
        for a in _recipients(remove_attendees, "remove_attendees")[0]
    }
    if add_req or add_opt or removing:
        if not current.get("isOrganizer", True):
            return "[tool-error] only the organizer can change the attendee list."
        people = [
            {"emailAddress": a.get("emailAddress"), "type": a.get("type", "required")}
            for a in (current.get("attendees") or [])
            if isinstance(a, dict)
            and str((a.get("emailAddress") or {}).get("address") or "").lower() not in removing
        ]
        present = {str((a.get("emailAddress") or {}).get("address") or "").lower() for a in people}
        for entry, kind in [(a, "required") for a in add_req] + [(a, "optional") for a in add_opt]:
            if entry["emailAddress"]["address"].lower() not in present:
                people.append(dict(entry, type=kind))
        patch["attendees"] = people
        changed.append(f"attendees (+{len(add_req) + len(add_opt)}, -{len(removing)})")
    if online_meeting is not None:
        patch["isOnlineMeeting"] = bool(online_meeting)
        if online_meeting:
            patch["onlineMeetingProvider"] = "teamsForBusiness"
        changed.append("Teams link" if online_meeting else "online meeting off")
    if show_as.strip():
        patch["showAs"] = show_as.strip()
        changed.append("show as")
    if reminder_minutes is not None:
        patch["isReminderOn"] = int(reminder_minutes) >= 0
        if int(reminder_minutes) >= 0:
            patch["reminderMinutesBeforeStart"] = int(reminder_minutes)
        changed.append("reminder")
    if not patch:
        return "[tool-error] nothing to change: pass at least one field."
    data, err = _request("PATCH", event_path, body=patch, prefer=(_tz_prefer(zone),))
    if err:
        return err
    d = data or {}
    notice = " Attendees get the update." if d.get("attendees") else ""
    join = (d.get("onlineMeeting") or {}).get("joinUrl")
    return (
        f"Updated the event ({', '.join(changed)}). Now: "
        f"{_fmt_span(d.get('start'), d.get('end'), bool(d.get('isAllDay')))} ({zone})."
        f"{notice}" + (f"\nTeams join link: {join}" if join else "")
    )


@tool
def ms365_delete_event(
    event_id: Annotated[str, "Event id."],
    cancellation_message: Annotated[
        str, "For a meeting you organize: a note sent to attendees with the cancellation."
    ] = "",
    user_id: UserArg = "me",
) -> str:
    """Delete a calendar event. For a meeting you organize, attendees get a cancellation
    (with your note). For someone else's meeting, prefer ms365_respond_to_event to decline."""
    event_path = f"{_user(user_id)}/events/{_seg(event_id)}"
    current, err = _request("GET", event_path, params={"$select": "isOrganizer,attendees"})
    if err:
        return err
    current = current or {}
    attendees = [a for a in (current.get("attendees") or []) if isinstance(a, dict)]
    if current.get("isOrganizer", True) and attendees:
        _, err = _request(
            "POST", f"{event_path}/cancel", body={"Comment": cancellation_message.strip()}
        )
        return err or f"Cancelled the meeting; {len(attendees)} attendee(s) were notified."
    _, err = _request("DELETE", event_path)
    if err:
        return err
    if not current.get("isOrganizer", True):
        return (
            "Removed the event from this calendar. The organizer was not told; use "
            "ms365_respond_to_event to decline instead when they should know."
        )
    return "Deleted the event."


@tool
def ms365_respond_to_event(
    event_id: Annotated[str, "Id of the meeting invitation."],
    response: Annotated[str, "accept, tentative or decline."],
    comment: Annotated[str, "Optional message to the organizer."] = "",
    send_response: Annotated[bool, "Tell the organizer (default). False answers silently."] = True,
    propose_new_start: Annotated[str, "With tentative/decline: a new start time to propose."] = "",
    propose_new_end: Annotated[str, "With tentative/decline: the proposed end."] = "",
    time_zone: TimeZoneArg = "",
    user_id: UserArg = "me",
) -> str:
    """Accept, tentatively accept or decline a meeting invitation, optionally proposing a new time."""
    action = {
        "accept": "accept",
        "accepted": "accept",
        "yes": "accept",
        "tentative": "tentativelyAccept",
        "tentatively": "tentativelyAccept",
        "maybe": "tentativelyAccept",
        "decline": "decline",
        "declined": "decline",
        "no": "decline",
    }.get((response or "").strip().lower())
    if action is None:
        return "[tool-error] response must be accept, tentative or decline."
    payload: dict[str, Any] = {"comment": comment, "sendResponse": bool(send_response)}
    if propose_new_start.strip() or propose_new_end.strip():
        if action == "accept":
            return "[tool-error] a new time can only be proposed with tentative or decline."
        if not (propose_new_start.strip() and propose_new_end.strip()):
            return "[tool-error] pass both propose_new_start and propose_new_end."
        if not send_response:
            return "[tool-error] proposing a new time needs send_response=true."
        zone = _resolve_tz(time_zone, user_id)
        s, err = _graph_dt("propose_new_start", propose_new_start, zone)
        if err:
            return err
        e, err = _graph_dt("propose_new_end", propose_new_end, zone)
        if err:
            return err
        payload["proposedNewTime"] = {"start": s, "end": e}
    _, err = _request("POST", f"{_user(user_id)}/events/{_seg(event_id)}/{action}", body=payload)
    if err:
        return err
    verb = {
        "accept": "Accepted",
        "tentativelyAccept": "Tentatively accepted",
        "decline": "Declined",
    }[action]
    told = "the organizer was notified" if send_response else "the organizer was not notified"
    proposal = " and proposed a new time" if "proposedNewTime" in payload else ""
    return f"{verb} the invitation{proposal}; {told}."


@tool
def ms365_find_meeting_times(
    attendees: Annotated[
        str, "Emails of the people who must attend, comma-separated (you are included)."
    ],
    duration_minutes: Annotated[int, "Meeting length in minutes."] = 30,
    start: Annotated[str, "Search from (date or date-time). Empty = now."] = "",
    end: Annotated[str, "Search until. Empty = 7 days after start."] = "",
    time_zone: TimeZoneArg = "",
    working_hours_only: Annotated[bool, "Only suggest times inside working hours."] = True,
    max_candidates: Annotated[int, "Most suggestions to return (1-20)."] = 5,
    minimum_attendee_percentage: Annotated[
        int, "Accept slots where at least this % can attend (default 100)."
    ] = 100,
    user_id: UserArg = "me",
) -> str:
    """Suggest meeting times when the given people are free (work or school accounts).

    Book one with ms365_create_event.
    """
    people, err = _recipients(attendees, "attendees")
    if err:
        return err
    if not people:
        return "[tool-error] attendees: at least one email is required."
    zone = _resolve_tz(time_zone, user_id)
    begin = _parse_when(start) if start.strip() else datetime.now(_zoneinfo(zone) or timezone.utc)
    if begin is None:
        return _bad_when("start", start)
    if end.strip():
        finish = _parse_when(end)
        if finish is None:
            return _bad_when("end", end)
        if _is_date_only(end):
            finish += timedelta(days=1)
    else:
        finish = begin + timedelta(days=7)
    s, err = _graph_dt("start", begin.isoformat(), zone)
    if err:
        return err
    e, err = _graph_dt("end", finish.isoformat(), zone)
    if err:
        return err
    payload = {
        "attendees": [dict(p, type="required") for p in people],
        "timeConstraint": {
            "activityDomain": "work" if working_hours_only else "unrestricted",
            "timeSlots": [{"start": s, "end": e}],
        },
        "meetingDuration": f"PT{max(5, int(duration_minutes or 30))}M",
        "maxCandidates": _top(max_candidates, 5, 20),
        "isOrganizerOptional": False,
        "returnSuggestionReasons": True,
        "minimumAttendeePercentage": max(0, min(100, int(minimum_attendee_percentage or 100))),
    }
    data, err = _request(
        "POST", f"{_user(user_id)}/findMeetingTimes", body=payload, prefer=(_tz_prefer(zone),)
    )
    if err:
        return err
    suggestions = [
        s for s in ((data or {}).get("meetingTimeSuggestions") or []) if isinstance(s, dict)
    ]
    if not suggestions:
        reason = (data or {}).get("emptySuggestionsReason") or "no free slot in that window"
        return f"No meeting times found ({reason}). Try a wider window or fewer required attendees."
    lines = [f"{len(suggestions)} suggestion(s), times in {zone}:"]
    for sug in suggestions:
        slot = sug.get("meetingTimeSlot") or {}
        busy = [
            f"{(a.get('attendee') or {}).get('emailAddress', {}).get('address')}: {a.get('availability')}"
            for a in (sug.get("attendeeAvailability") or [])
            if isinstance(a, dict) and a.get("availability") not in (None, "free")
        ]
        line = f"- {_fmt_span(slot.get('start'), slot.get('end'))}  confidence {sug.get('confidence', '?')}%"
        if busy:
            line += f"  (not free: {', '.join(busy)})"
        if sug.get("suggestionReason"):
            line += f"\n  {sug.get('suggestionReason')}"
        lines.append(line)
    return "\n".join(lines)


@tool
def ms365_get_availability(
    people: Annotated[str, "Emails to check, comma-separated (yours included if you want it)."],
    start: Annotated[str, "Window start (date or date-time). Empty = now."] = "",
    end: Annotated[str, "Window end. Empty = 24 hours after start."] = "",
    interval_minutes: Annotated[int, "Slot size for the availability strip (5-1440)."] = 30,
    time_zone: TimeZoneArg = "",
    user_id: UserArg = "me",
) -> str:
    """Show when people are busy or free (free/busy blocks and working hours), for
    work or school accounts."""
    targets, err = _recipients(people, "people")
    if err:
        return err
    if not targets:
        return "[tool-error] people: at least one email is required."
    if len(targets) > 20:
        return "[tool-error] people: at most 20 per call."
    zone = _resolve_tz(time_zone, user_id)
    begin = _parse_when(start) if start.strip() else datetime.now(_zoneinfo(zone) or timezone.utc)
    if begin is None:
        return _bad_when("start", start)
    if end.strip():
        finish = _parse_when(end)
        if finish is None:
            return _bad_when("end", end)
        if _is_date_only(end):
            finish += timedelta(days=1)
    else:
        finish = begin + timedelta(days=1)
    s, err = _graph_dt("start", begin.isoformat(), zone)
    if err or s is None:
        return err or _bad_when("start", start)
    e, err = _graph_dt("end", finish.isoformat(), zone)
    if err or e is None:
        return err or _bad_when("end", end)
    step = max(5, min(1440, int(interval_minutes or 30)))
    data, err = _request(
        "POST",
        f"{_user(user_id)}/calendar/getSchedule",
        body={
            "schedules": [t["emailAddress"]["address"] for t in targets],
            "startTime": s,
            "endTime": e,
            "availabilityViewInterval": step,
        },
        prefer=(_tz_prefer(zone),),
    )
    if err:
        return err
    lines = [
        f"Availability {_fmt_dt(s['dateTime'])} → {_fmt_dt(e['dateTime'])} ({zone}); "
        f"strip is one digit per {step} min: 0 free, 1 tentative, 2 busy, 3 away, 4 elsewhere."
    ]
    for sched in _values(data):
        who = sched.get("scheduleId")
        if sched.get("error"):
            lines.append(
                f"- {who}: unavailable ({_clip((sched['error'] or {}).get('message'), 160)})"
            )
            continue
        lines.append(f"- {who}: {sched.get('availabilityView') or '(no data)'}")
        for item in (sched.get("scheduleItems") or [])[:20]:
            label = item.get("subject") or item.get("location") or ""
            lines.append(
                f"    {item.get('status')}: {_fmt_span(item.get('start'), item.get('end'))}"
                + (f" {label}" if label else "")
            )
        hours = sched.get("workingHours") or {}
        if hours.get("startTime"):
            lines.append(
                f"    Working hours: {', '.join(hours.get('daysOfWeek') or [])} "
                f"{str(hours.get('startTime'))[:5]}-{str(hours.get('endTime'))[:5]} "
                f"({(hours.get('timeZone') or {}).get('name', '')})".rstrip()
            )
    return "\n".join(lines)


@tool
def ms365_list_calendars(user_id: UserArg = "me") -> str:
    """List the calendars this account can see (own and shared), with ids for other calendar tools."""
    data, err = _request(
        "GET",
        f"{_user(user_id)}/calendars",
        params={"$select": "id,name,owner,canEdit,isDefaultCalendar", "$top": 100},
    )
    if err:
        return err
    calendars = _values(data)
    if not calendars:
        return "No calendars found."
    return "\n".join(
        f"- {c.get('name')}"
        + (" (default)" if c.get("isDefaultCalendar") else "")
        + ("" if c.get("canEdit") else " (read-only)")
        + f" owner: {(c.get('owner') or {}).get('address') or '?'}\n  id: {c.get('id')}"
        for c in calendars
    )


# ═══════════════════════════════════════════════════════════════════════════
# People, profile, contacts, automatic replies
# ═══════════════════════════════════════════════════════════════════════════


@tool
def ms365_find_people(
    query: Annotated[str, "A name, part of a name, or email, e.g. 'maria' or 'lopez'."],
    top: Annotated[int, "Most results (1-25)."] = 10,
) -> str:
    """Find a person's email address by name: people you work with, your organization's
    directory, and your Outlook contacts. Use it before inviting or emailing someone by name."""
    needle = _search_str(query)
    if not needle:
        return "[tool-error] query is required."
    limit = _top(top, 10, 25)
    found: dict[str, dict[str, str]] = {}
    failures: list[str] = []

    def add(name: str, email: str, detail: str, source: str) -> None:
        key = email.strip().lower()
        if key and key not in found:
            found[key] = {"name": name, "email": email.strip(), "detail": detail, "source": source}

    data, err = _request(
        "GET",
        "me/people",
        params={
            "$search": f'"{needle}"',
            "$top": limit,
            "$select": "displayName,scoredEmailAddresses,jobTitle,department,companyName",
        },
    )
    if err:
        failures.append(err)
    for p in _values(data):
        detail = " · ".join(
            str(p.get(k)) for k in ("jobTitle", "department", "companyName") if p.get(k)
        )
        for scored in p.get("scoredEmailAddresses") or []:
            add(
                str(p.get("displayName") or ""),
                str(scored.get("address") or ""),
                detail,
                "people you work with",
            )

    data, err = _request(
        "GET",
        "users",
        params={
            "$search": f'"displayName:{needle}" OR "mail:{needle}"',
            "$top": limit,
            "$select": "displayName,mail,userPrincipalName,jobTitle,department",
        },
        headers={"ConsistencyLevel": "eventual"},
    )
    if err:
        failures.append(err)
    for u in _values(data):
        detail = " · ".join(str(u.get(k)) for k in ("jobTitle", "department") if u.get(k))
        add(
            str(u.get("displayName") or ""),
            str(u.get("mail") or u.get("userPrincipalName") or ""),
            detail,
            "directory",
        )

    data, err = _request(
        "GET",
        "me/contacts",
        params={"$top": 250, "$select": "displayName,emailAddresses,companyName,jobTitle"},
    )
    if err:
        failures.append(err)
    terms = needle.lower().split()
    for c in _values(data):
        emails = [
            str(e.get("address") or "")
            for e in (c.get("emailAddresses") or [])
            if isinstance(e, dict)
        ]
        haystack = " ".join(
            [str(c.get("displayName") or ""), str(c.get("companyName") or ""), *emails]
        ).lower()
        if all(t in haystack for t in terms):
            detail = " · ".join(str(c.get(k)) for k in ("jobTitle", "companyName") if c.get(k))
            for email in emails:
                add(str(c.get("displayName") or ""), email, detail, "contacts")

    if not found:
        if len(failures) == 3:
            return failures[0]
        return f"No one matched '{query}'. Try part of the name, or ask the user for the address."
    rows = list(found.values())[:limit]
    lines = [f"{len(rows)} match(es):"]
    for r in rows:
        extra = f" ({r['detail']})" if r["detail"] else ""
        lines.append(f"- {r['name'] or '?'} <{r['email']}>{extra} [{r['source']}]")
    return "\n".join(lines)


@tool
def ms365_get_profile(user_id: UserArg = "me") -> str:
    """The connected account's name, email, job details, time zone and working hours."""
    data, err = _request(
        "GET",
        _user(user_id),
        params={
            "$select": (
                "displayName,mail,userPrincipalName,jobTitle,department,officeLocation,"
                "mobilePhone,businessPhones,preferredLanguage"
            )
        },
    )
    if err:
        return err
    u = data or {}
    lines = [
        f"Name: {u.get('displayName') or '?'}",
        f"Email: {u.get('mail') or u.get('userPrincipalName') or '?'}",
    ]
    for label, key in (
        ("Job title", "jobTitle"),
        ("Department", "department"),
        ("Office", "officeLocation"),
        ("Mobile", "mobilePhone"),
        ("Language", "preferredLanguage"),
    ):
        if u.get(key):
            lines.append(f"{label}: {u.get(key)}")
    if u.get("businessPhones"):
        lines.append(f"Business phones: {', '.join(u['businessPhones'])}")
    settings, err = _request("GET", f"{_user(user_id)}/mailboxSettings")
    if err:
        lines.append("Mailbox settings: not readable with this connection.")
    else:
        s = settings or {}
        if s.get("timeZone"):
            lines.append(f"Time zone: {s.get('timeZone')}")
        hours = s.get("workingHours") or {}
        if hours.get("startTime"):
            lines.append(
                f"Working hours: {', '.join(hours.get('daysOfWeek') or [])} "
                f"{str(hours.get('startTime'))[:5]}-{str(hours.get('endTime'))[:5]}"
            )
        auto = (s.get("automaticRepliesSetting") or {}).get("status")
        if auto:
            lines.append(f"Automatic replies: {auto}")
    return "\n".join(lines)


@tool
def ms365_set_auto_reply(
    mode: Annotated[str, "off, on (until turned off) or scheduled (needs start and end)."],
    internal_message: Annotated[
        str, "Reply sent to people in your organization (HTML or plain)."
    ] = "",
    external_message: Annotated[
        str, "Reply sent to outside senders. Empty reuses the internal one."
    ] = "",
    external_audience: Annotated[
        str, "Who outside gets it: all (default), contacts or none."
    ] = "all",
    start: Annotated[str, "For scheduled: when replies begin."] = "",
    end: Annotated[str, "For scheduled: when replies stop."] = "",
    time_zone: TimeZoneArg = "",
    user_id: UserArg = "me",
) -> str:
    """Turn Outlook automatic replies (out of office) on, off, or on for a scheduled period."""
    status = {
        "off": "disabled",
        "disabled": "disabled",
        "disable": "disabled",
        "on": "alwaysEnabled",
        "enabled": "alwaysEnabled",
        "alwaysenabled": "alwaysEnabled",
        "scheduled": "scheduled",
        "schedule": "scheduled",
    }.get((mode or "").strip().lower().replace(" ", ""))
    if status is None:
        return "[tool-error] mode must be off, on or scheduled."
    setting: dict[str, Any] = {"status": status}
    if status != "disabled":
        if not internal_message.strip() and not external_message.strip():
            return "[tool-error] pass the reply text in internal_message."
        audience = {
            "all": "all",
            "contacts": "contactsOnly",
            "contactsonly": "contactsOnly",
            "none": "none",
        }.get((external_audience or "all").strip().lower().replace(" ", "").replace("_", ""))
        if audience is None:
            return "[tool-error] external_audience must be all, contacts or none."
        internal = internal_message.strip() or external_message.strip()
        external = external_message.strip() or internal
        setting.update(
            {
                "internalReplyMessage": _html_body(internal),
                "externalReplyMessage": _html_body(external),
                "externalAudience": audience,
            }
        )
    if status == "scheduled":
        if not (start.strip() and end.strip()):
            return "[tool-error] scheduled replies need both start and end."
        zone = _resolve_tz(time_zone, user_id)
        s, err = _graph_dt("start", start, zone)
        if err:
            return err
        e, err = _graph_dt("end", end, zone)
        if err:
            return err
        setting["scheduledStartDateTime"] = s
        setting["scheduledEndDateTime"] = e
    _, err = _request(
        "PATCH", f"{_user(user_id)}/mailboxSettings", body={"automaticRepliesSetting": setting}
    )
    if err:
        return err
    if status == "disabled":
        return "Automatic replies are off."
    if status == "scheduled":
        return (
            f"Automatic replies scheduled from {setting['scheduledStartDateTime']['dateTime'][:16]} "
            f"to {setting['scheduledEndDateTime']['dateTime'][:16]} "
            f"({setting['scheduledStartDateTime']['timeZone']})."
        )
    return "Automatic replies are on until turned off."


@tool
def ms365_list_contacts(
    query: Annotated[str, "Only contacts whose name, company or email contains this text."] = "",
    top: Annotated[int, "Most contacts to return (1-100)."] = 25,
    page_token: PageTokenArg = "",
    user_id: UserArg = "me",
) -> str:
    """List Outlook contacts with emails, phones and company."""
    fields = "id,displayName,emailAddresses,businessPhones,mobilePhone,companyName,jobTitle"
    if page_token:
        data, err = _request("GET", page_token)
    else:
        data, err = _request(
            "GET",
            f"{_user(user_id)}/contacts",
            params={"$top": 250 if query.strip() else _top(top, 25), "$select": fields},
        )
    if err:
        return err
    # Contacts document no $search and no name filter, so matching is local.
    contacts = sorted(_values(data), key=lambda c: str(c.get("displayName") or "").lower())
    terms = query.strip().lower().split()
    if terms:
        contacts = [
            c
            for c in contacts
            if all(
                t
                in " ".join(
                    [
                        str(c.get("displayName") or ""),
                        str(c.get("companyName") or ""),
                        *[str(e.get("address") or "") for e in (c.get("emailAddresses") or [])],
                    ]
                ).lower()
                for t in terms
            )
        ][: _top(top, 25)]
    if not contacts:
        return "No contacts found." + ("" if terms else _page_hint(data))
    lines = []
    for c in contacts:
        emails = ", ".join(
            str(e.get("address")) for e in (c.get("emailAddresses") or []) if e.get("address")
        )
        phones = ", ".join(
            [
                *(c.get("businessPhones") or []),
                *([c["mobilePhone"]] if c.get("mobilePhone") else []),
            ]
        )
        extra = " · ".join(str(c.get(k)) for k in ("jobTitle", "companyName") if c.get(k))
        lines.append(
            f"- {c.get('displayName') or '?'}"
            + (f" <{emails}>" if emails else "")
            + (f" ({extra})" if extra else "")
            + (f" tel: {phones}" if phones else "")
            + f"\n  id: {c.get('id')}"
        )
    return "\n".join(lines) + ("" if terms else _page_hint(data))


@tool
def ms365_create_contact(
    given_name: Annotated[str, "First name."] = "",
    surname: Annotated[str, "Last name."] = "",
    email: Annotated[str, "Email address(es), comma-separated."] = "",
    mobile_phone: Annotated[str, "Mobile number."] = "",
    business_phone: Annotated[str, "Work number."] = "",
    company: Annotated[str, "Company name."] = "",
    job_title: Annotated[str, "Job title."] = "",
    notes: Annotated[str, "Free-text notes."] = "",
    user_id: UserArg = "me",
) -> str:
    """Add a contact to Outlook."""
    if not (given_name.strip() or surname.strip() or email.strip()):
        return "[tool-error] give at least a name or an email."
    emails, err = _recipients(email, "email")
    if err:
        return err
    display = " ".join(p for p in (given_name.strip(), surname.strip()) if p) or (
        emails[0]["emailAddress"]["address"] if emails else ""
    )
    payload: dict[str, Any] = {"displayName": display}
    if given_name.strip():
        payload["givenName"] = given_name.strip()
    if surname.strip():
        payload["surname"] = surname.strip()
    if emails:
        payload["emailAddresses"] = [
            {"address": e["emailAddress"]["address"], "name": display} for e in emails
        ]
    if mobile_phone.strip():
        payload["mobilePhone"] = mobile_phone.strip()
    if business_phone.strip():
        payload["businessPhones"] = [business_phone.strip()]
    if company.strip():
        payload["companyName"] = company.strip()
    if job_title.strip():
        payload["jobTitle"] = job_title.strip()
    if notes.strip():
        payload["personalNotes"] = notes.strip()
    data, err = _request("POST", f"{_user(user_id)}/contacts", body=payload)
    if err:
        return err
    return f"Added contact {display}. id: {(data or {}).get('id')}"


# ═══════════════════════════════════════════════════════════════════════════
# OneDrive and SharePoint
# ═══════════════════════════════════════════════════════════════════════════


def _drive(drive_id: str, user_id: str) -> str:
    return f"drives/{_seg(drive_id)}" if drive_id.strip() else f"{_user(user_id)}/drive"


def _item(item: str) -> str:
    """Graph addressing for an item id or a ``/path`` from the drive root."""
    ref = (item or "").strip()
    if ref in ("", "/", "root"):
        return "root"
    if ref.startswith("/"):
        return f"root:{quote(ref.rstrip('/'), safe='/')}:"
    return f"items/{_seg(ref)}"


def _child(folder: str, name: str) -> str:
    """Addressing for ``name`` inside ``folder`` (id, path or root)."""
    ref = (folder or "").strip()
    encoded = quote(name, safe="")
    if ref in ("", "/", "root"):
        return f"root:/{encoded}:"
    if ref.startswith("/"):
        return f"root:{quote(ref.rstrip('/'), safe='/')}/{encoded}:"
    return f"items/{_seg(ref)}:/{encoded}:"


def _item_line(i: dict[str, Any]) -> str:
    kind = (
        f"folder, {(i.get('folder') or {}).get('childCount', 0)} item(s)"
        if i.get("folder") is not None
        else _human_size(i.get("size"))
    )
    modified = _fmt_dt(i.get("lastModifiedDateTime"))
    who = ((i.get("lastModifiedBy") or {}).get("user") or {}).get("displayName")
    line = f"- {i.get('name')} ({kind}, modified {modified}" + (f" by {who}" if who else "") + ")"
    return line + f"\n  id: {i.get('id')}" + (f"\n  {i.get('webUrl')}" if i.get("webUrl") else "")


_CONFLICT = {"rename": "rename", "replace": "replace", "overwrite": "replace", "fail": "fail"}


@tool
def ms365_list_drive_items(
    folder_item_id: Annotated[
        str, "Folder id, a path like '/Documents/Reports', or 'root' (default)."
    ] = "root",
    drive_id: DriveArg = "",
    top: Annotated[int, "Most items to return (1-100)."] = 50,
    page_token: PageTokenArg = "",
    user_id: UserArg = "me",
) -> str:
    """List the files and folders in a OneDrive or SharePoint folder."""
    if page_token:
        data, err = _request("GET", page_token)
    else:
        data, err = _request(
            "GET",
            f"{_drive(drive_id, user_id)}/{_item(folder_item_id)}/children",
            params={
                "$top": _top(top, 50),
                "$select": "id,name,size,folder,file,lastModifiedDateTime,lastModifiedBy,webUrl",
            },
        )
    if err:
        return err
    items = _values(data)
    if not items:
        return "No drive items found." + _page_hint(data)
    return "\n".join(_item_line(i) for i in items) + _page_hint(data)


@tool
def ms365_search_drive(
    query: Annotated[str, "Words in the file name or content."],
    drive_id: DriveArg = "",
    top: Annotated[int, "Most results (1-100)."] = 25,
    user_id: UserArg = "me",
) -> str:
    """Search one OneDrive (or a SharePoint library) by file name and content.
    ms365_search_files also covers SharePoint and files shared with you."""
    literal = quote(query.replace("'", "''"), safe="")
    data, err = _request(
        "GET",
        f"{_drive(drive_id, user_id)}/root/search(q='{literal}')",
        params={"$top": _top(top, 25)},
    )
    if err:
        return err
    items = _values(data)
    if not items:
        return "No drive search results found."
    return "\n".join(_item_line(i) for i in items) + _page_hint(data)


@tool
def ms365_search_files(
    query: Annotated[str, "Search words, e.g. 'budget 2026'. KQL works: 'author:ana'."],
    file_types: Annotated[
        str, "Only these extensions, comma-separated, e.g. 'docx,xlsx,pdf'."
    ] = "",
    top: Annotated[int, "Most results (1-50)."] = 25,
) -> str:
    """Search files across OneDrive, SharePoint and files shared with you (work or school
    accounts). Results carry the drive id and item id for the other file tools."""
    text = query.strip()
    if not text:
        return "[tool-error] query is required."
    kinds = [k.strip().lstrip(".") for k in file_types.split(",") if k.strip()]
    if kinds:
        text += " (" + " OR ".join(f"filetype:{k}" for k in kinds) + ")"
    data, err = _request(
        "POST",
        "search/query",
        body={
            "requests": [
                {
                    "entityTypes": ["driveItem"],
                    "query": {"queryString": text},
                    "from": 0,
                    "size": _top(top, 25, 50),
                }
            ]
        },
    )
    if err:
        return err
    hits: list[dict[str, Any]] = []
    for response in _values(data):
        for container in response.get("hitsContainers") or []:
            hits += [h for h in (container.get("hits") or []) if isinstance(h, dict)]
    if not hits:
        return "No files found."
    lines = []
    for hit in hits:
        r = hit.get("resource") or {}
        parent = r.get("parentReference") or {}
        who = ((r.get("lastModifiedBy") or {}).get("user") or {}).get("displayName")
        summary = _clip(_html_to_text(str(hit.get("summary") or "")), 160)
        lines.append(
            f"- {r.get('name')} ({_human_size(r.get('size'))}, modified "
            f"{_fmt_dt(r.get('lastModifiedDateTime'))}"
            + (f" by {who}" if who else "")
            + ")"
            + f"\n  item id: {r.get('id')}  drive id: {parent.get('driveId')}"
            + (f"\n  {r.get('webUrl')}" if r.get("webUrl") else "")
            + (f"\n  {summary}" if summary else "")
        )
    return "\n".join(lines)


@tool
def ms365_upload_drive_file(
    local_path: Annotated[str, "File to upload (workspace-relative or absolute)."],
    remote_name: Annotated[str, "Name in OneDrive. Empty keeps the local name."] = "",
    folder: Annotated[
        str, "Target folder id or path like '/Documents/Reports'. Empty = root."
    ] = "",
    conflict: Annotated[str, "If the name exists: rename (default), replace or fail."] = "rename",
    drive_id: DriveArg = "",
    user_id: UserArg = "me",
) -> str:
    """Upload a local file to OneDrive or a SharePoint library (any size)."""
    files, err = _local_files(local_path)
    if err:
        return err
    if not files:
        return "[tool-error] local_path is required."
    path = files[0]
    behaviour = _CONFLICT.get((conflict or "rename").strip().lower())
    if behaviour is None:
        return "[tool-error] conflict must be rename, replace or fail."
    name = remote_name.strip() or path.name
    target = f"{_drive(drive_id, user_id)}/{_child(folder, name)}"
    size = path.stat().st_size
    if size <= _SIMPLE_UPLOAD_MAX:
        data, err = _request(
            "PUT",
            f"{target}/content",
            params={"@microsoft.graph.conflictBehavior": behaviour},
            content=path.read_bytes(),
            headers={"Content-Type": "application/octet-stream"},
            timeout=120,
        )
    else:
        session, err = _request(
            "POST",
            f"{target}/createUploadSession",
            body={"item": {"@microsoft.graph.conflictBehavior": behaviour}},
        )
        if not err:
            upload_url = str((session or {}).get("uploadUrl") or "")
            if not upload_url:
                return "[tool-error] MS365: Graph returned no upload URL."
            data, err = _upload_session_put(upload_url, path)
    if err:
        return err
    d = data or {}
    return f"Uploaded {d.get('name') or name} ({_human_size(size)}).\nid: {d.get('id')}" + (
        f"\nLink: {d.get('webUrl')}" if d.get("webUrl") else ""
    )


@tool
def ms365_download_drive_file(
    item: ItemArg,
    save_dir: Annotated[str, "Folder inside the workspace. Empty uses 'downloads'."] = "",
    filename: Annotated[str, "Save under this name. Empty keeps the OneDrive name."] = "",
    convert_to_pdf: Annotated[
        bool, "Save a PDF conversion (Word, Excel, PowerPoint and more)."
    ] = False,
    drive_id: DriveArg = "",
    user_id: UserArg = "me",
) -> str:
    """Download a OneDrive or SharePoint file into the workspace (optionally as PDF)."""
    item_path = f"{_drive(drive_id, user_id)}/{_item(item)}"
    meta, err = _request("GET", item_path)
    if err:
        return err
    meta = meta or {}
    if meta.get("folder") is not None:
        return "[tool-error] that is a folder; list it with ms365_list_drive_items."
    name = filename.strip() or str(meta.get("name") or "download")
    mime = str((meta.get("file") or {}).get("mimeType") or "")
    if convert_to_pdf:
        name = Path(name).stem + ".pdf"
        mime = "application/pdf"
    target, created, err = _download_target(name, save_dir, mime)
    if err or target is None:
        return err or "[tool-error] could not choose a download path."
    download_url = str(meta.get("@microsoft.graph.downloadUrl") or "")
    if convert_to_pdf:
        size, err = _stream_to_file(
            f"{_GRAPH_BASE}/{item_path}/content",
            target,
            authenticated=True,
            params={"format": "pdf"},
        )
    elif download_url.startswith("https://"):
        size, err = _stream_to_file(download_url, target, authenticated=False)
    else:
        size, err = _stream_to_file(
            f"{_GRAPH_BASE}/{item_path}/content", target, authenticated=True
        )
    if err:
        return err
    _handoff(target, created)
    return f"Saved: {target} ({_human_size(size)})"


@tool
def ms365_create_folder(
    name: Annotated[str, "New folder name."],
    parent_folder: Annotated[str, "Parent folder id or path. Empty = root."] = "",
    conflict: Annotated[str, "If the name exists: rename (default), replace or fail."] = "rename",
    drive_id: DriveArg = "",
    user_id: UserArg = "me",
) -> str:
    """Create a folder in OneDrive or a SharePoint library."""
    behaviour = _CONFLICT.get((conflict or "rename").strip().lower())
    if behaviour is None:
        return "[tool-error] conflict must be rename, replace or fail."
    if not name.strip():
        return "[tool-error] name is required."
    data, err = _request(
        "POST",
        f"{_drive(drive_id, user_id)}/{_item(parent_folder)}/children",
        body={"name": name.strip(), "folder": {}, "@microsoft.graph.conflictBehavior": behaviour},
    )
    if err:
        return err
    d = data or {}
    return f"Created folder {d.get('name')}.\nid: {d.get('id')}" + (
        f"\nLink: {d.get('webUrl')}" if d.get("webUrl") else ""
    )


@tool
def ms365_share_drive_item(
    item: ItemArg,
    access: Annotated[str, "view (default) or edit."] = "view",
    scope: Annotated[
        str,
        "Without recipients, who the link works for: organization (default, people in your "
        "company) or anyone (no sign-in).",
    ] = "organization",
    recipients: Annotated[
        str, "Emails to invite directly, comma-separated. They get an email with the link."
    ] = "",
    message: Annotated[str, "Note included in the invitation email."] = "",
    drive_id: DriveArg = "",
    user_id: UserArg = "me",
) -> str:
    """Share a OneDrive or SharePoint file or folder: invite specific people, or create a view
    or edit link."""
    role = {"view": "read", "read": "read", "edit": "write", "write": "write"}.get(
        (access or "view").strip().lower()
    )
    if role is None:
        return "[tool-error] access must be view or edit."
    item_path = f"{_drive(drive_id, user_id)}/{_item(item)}"
    people, err = _recipients(recipients, "recipients")
    if err:
        return err
    if people:
        data, err = _request(
            "POST",
            f"{item_path}/invite",
            body={
                "recipients": [{"email": p["emailAddress"]["address"]} for p in people],
                "message": message,
                "requireSignIn": True,
                "sendInvitation": True,
                "roles": [role],
            },
        )
        if err:
            return err
        links = {
            str((p.get("link") or {}).get("webUrl"))
            for p in _values(data)
            if (p.get("link") or {}).get("webUrl")
        }
        verb = "edit" if role == "write" else "view"
        return f"Invited {_addresses(people)} to {verb}; they were emailed a link." + (
            f"\nLink: {', '.join(sorted(links))}" if links else ""
        )
    link_scope = {
        "organization": "organization",
        "org": "organization",
        "anyone": "anonymous",
        "anonymous": "anonymous",
    }.get((scope or "organization").strip().lower())
    if link_scope is None:
        return "[tool-error] scope must be organization or anyone."
    data, err = _request(
        "POST",
        f"{item_path}/createLink",
        body={"type": "edit" if role == "write" else "view", "scope": link_scope},
    )
    if err:
        return err
    url = ((data or {}).get("link") or {}).get("webUrl")
    who = "anyone with the link" if link_scope == "anonymous" else "people in your organization"
    return f"Sharing link ({'edit' if role == 'write' else 'view'}, {who}): {url}"


@tool
def ms365_move_drive_item(
    item: ItemArg,
    destination_folder: Annotated[
        str, "Folder id or path to move it into. Empty keeps the folder."
    ] = "",
    new_name: Annotated[str, "New name. Empty keeps the name."] = "",
    drive_id: DriveArg = "",
    user_id: UserArg = "me",
) -> str:
    """Move or rename a OneDrive or SharePoint file or folder (within the same drive)."""
    if not destination_folder.strip() and not new_name.strip():
        return "[tool-error] pass destination_folder, new_name, or both."
    drive = _drive(drive_id, user_id)
    patch: dict[str, Any] = {}
    if new_name.strip():
        patch["name"] = new_name.strip()
    if destination_folder.strip():
        folder, err = _request(
            "GET", f"{drive}/{_item(destination_folder)}", params={"$select": "id,name,folder"}
        )
        if err:
            return err
        folder = folder or {}
        if folder.get("folder") is None and _item(destination_folder) != "root":
            return "[tool-error] destination_folder is not a folder."
        patch["parentReference"] = {"id": folder.get("id")}
    data, err = _request("PATCH", f"{drive}/{_item(item)}", body=patch)
    if err:
        return err
    d = data or {}
    where = (d.get("parentReference") or {}).get("path") or ""
    return (
        f"Moved/renamed to {d.get('name')}"
        + (f" in {where}" if where else "")
        + f".\nid: {d.get('id')}"
    )


@tool
def ms365_delete_drive_item(
    item: ItemArg,
    drive_id: DriveArg = "",
    user_id: UserArg = "me",
) -> str:
    """Delete a OneDrive or SharePoint file or folder (it goes to the recycle bin, restorable)."""
    _, err = _request("DELETE", f"{_drive(drive_id, user_id)}/{_item(item)}")
    return err or "Deleted; it is in the recycle bin and can be restored from there."


@tool
def ms365_find_sites(
    query: Annotated[
        str, "Words in the SharePoint site name. Empty lists sites you can reach."
    ] = "",
    top: Annotated[int, "Most sites (1-10)."] = 10,
) -> str:
    """Find SharePoint sites and their document libraries; a library's drive id works
    with every OneDrive file tool (work or school accounts)."""
    data, err = _request(
        "GET",
        "sites",
        params={"search": _search_str(query) or "*", "$select": "id,displayName,webUrl,name"},
    )
    if err:
        return err
    sites = _values(data)[: _top(top, 10, 10)]
    if not sites:
        return "No SharePoint sites found."
    lines = []
    for site in sites:
        lines.append(
            f"- {site.get('displayName') or site.get('name')}  {site.get('webUrl') or ''}".rstrip()
        )
        drives, drive_err = _request(
            "GET", f"sites/{_seg(str(site.get('id')))}/drives", params={"$select": "id,name,webUrl"}
        )
        if drive_err:
            lines.append("  libraries: could not list them")
            continue
        for d in _values(drives):
            lines.append(f"  library '{d.get('name')}': drive id {d.get('id')}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# Excel workbooks
# ═══════════════════════════════════════════════════════════════════════════


def _workbook(item: str, drive_id: str, user_id: str) -> str:
    return f"{_drive(drive_id, user_id)}/{_item(item)}/workbook"


def _range_seg(address: str) -> str:
    return quote(address.strip().replace("'", "''"), safe=":$")


def _parse_rows(field: str, value: str) -> tuple[list[list[Any]] | None, str | None]:
    try:
        rows = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        return (
            None,
            f'[tool-error] {field} must be a JSON array of rows, e.g. [["a", 1], ["b", 2]]: {exc}',
        )
    if isinstance(rows, list) and rows and not any(isinstance(r, list) for r in rows):
        rows = [rows]
    if not isinstance(rows, list) or not rows or not all(isinstance(r, list) for r in rows):
        return None, f"[tool-error] {field} must be a non-empty JSON array of rows (arrays)."
    return rows, None


def _markdown(values: list[list[Any]], max_rows: int) -> str:
    rows = [
        [("" if c is None else str(c)).replace("|", "\\|").replace("\n", " ") for c in r]
        for r in values
    ]
    if not rows:
        return "(empty)"
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    shown = rows[: max_rows + 1]
    out = ["| " + " | ".join(shown[0]) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(r) + " |" for r in shown[1:]]
    if len(rows) > len(shown):
        out.append(f"[{len(rows) - len(shown)} more row(s); read a narrower address to see them]")
    return "\n".join(out)


@tool
def ms365_excel_list_sheets(
    item: ItemArg,
    drive_id: DriveArg = "",
    user_id: UserArg = "me",
) -> str:
    """List an Excel workbook's worksheets (with the range holding data) and tables.
    Excel tools work on work or school OneDrive and SharePoint, not personal OneDrive."""
    book = _workbook(item, drive_id, user_id)
    data, err = _request(
        "GET", f"{book}/worksheets", params={"$select": "id,name,position,visibility"}
    )
    if err:
        return err
    lines = ["Worksheets:"]
    for sheet in _values(data)[:25]:
        used, used_err = _request(
            "GET",
            f"{book}/worksheets/{_seg(str(sheet.get('name')))}/usedRange(valuesOnly=true)",
            params={"$select": "address,rowCount,columnCount"},
        )
        extent = ""
        if not used_err and used:
            extent = f" data in {used.get('address')} ({used.get('rowCount')} rows x {used.get('columnCount')} cols)"
        hidden = "" if sheet.get("visibility") in (None, "Visible") else " [hidden]"
        lines.append(f"- {sheet.get('name')}{hidden}{extent}")
    tables, err = _request("GET", f"{book}/tables", params={"$select": "id,name"})
    if not err and _values(tables):
        lines.append("Tables (append with ms365_excel_add_rows):")
        for table in _values(tables)[:25]:
            rng, rng_err = _request(
                "GET",
                f"{book}/tables/{_seg(str(table.get('name')))}/range",
                params={"$select": "address"},
            )
            where = f" at {rng.get('address')}" if not rng_err and rng else ""
            lines.append(f"- {table.get('name')}{where}")
    return "\n".join(lines)


@tool
def ms365_excel_read_range(
    item: ItemArg,
    sheet: Annotated[str, "Worksheet name. Empty = the first sheet."] = "",
    address: Annotated[
        str, "Cells like 'A1:F50' (or 'Sheet1!A1:F50'). Empty = all used cells."
    ] = "",
    table: Annotated[str, "Or: an Excel table name, read whole."] = "",
    max_rows: Annotated[int, "Most rows to show."] = 200,
    drive_id: DriveArg = "",
    user_id: UserArg = "me",
) -> str:
    """Read cells from an Excel workbook in OneDrive or SharePoint (work or school), as a table."""
    book = _workbook(item, drive_id, user_id)
    addr, sheet_name = address.strip(), sheet.strip()
    if "!" in addr and not sheet_name:
        sheet_name, addr = (p.strip().strip("'") for p in addr.rsplit("!", 1))
    if table.strip():
        path = f"{book}/tables/{_seg(table)}/range"
    else:
        if not sheet_name:
            sheets, err = _request("GET", f"{book}/worksheets", params={"$select": "name,position"})
            if err:
                return err
            first = sorted(_values(sheets), key=lambda s: int(s.get("position") or 0))
            if not first:
                return "[tool-error] the workbook has no worksheets."
            sheet_name = str(first[0].get("name"))
        sheet_path = f"{book}/worksheets/{_seg(sheet_name)}"
        path = (
            f"{sheet_path}/range(address='{_range_seg(addr)}')"
            if addr
            else f"{sheet_path}/usedRange(valuesOnly=true)"
        )
    data, err = _request("GET", path, params={"$select": "address,values"})
    if err:
        return err
    values = (data or {}).get("values") or []
    return f"{(data or {}).get('address')}:\n{_markdown(values, max(1, int(max_rows or 200)))}"


@tool
def ms365_excel_write_range(
    item: ItemArg,
    sheet: Annotated[str, "Worksheet name."],
    address: Annotated[str, "Top-left cell or full range, e.g. 'B2' or 'B2:D4'."],
    values: Annotated[
        str,
        'JSON array of rows, e.g. [["Name", "Total"], ["Ana", 120]]. Strings starting '
        "with '=' are formulas.",
    ],
    drive_id: DriveArg = "",
    user_id: UserArg = "me",
) -> str:
    """Write values or formulas into cells of an Excel workbook in OneDrive or SharePoint
    (work or school)."""
    rows, err = _parse_rows("values", values)
    if err or rows is None:
        return err or "[tool-error] values is required."
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    addr = address.strip().split("!")[-1]
    if ":" not in addr:
        # Expand a top-left cell to the block the values cover.
        match = re.fullmatch(r"\$?([A-Za-z]{1,3})\$?(\d+)", addr)
        if not match:
            return "[tool-error] address must be a cell like 'B2' or a range like 'B2:D4'."
        col = 0
        for ch in match.group(1).upper():
            col = col * 26 + (ord(ch) - 64)
        last_col, name = col + width - 1, ""
        while last_col:
            last_col, rem = divmod(last_col - 1, 26)
            name = chr(65 + rem) + name
        addr = f"{addr}:{name}{int(match.group(2)) + len(rows) - 1}"
    path = f"{_workbook(item, drive_id, user_id)}/worksheets/{_seg(sheet)}/range(address='{_range_seg(addr)}')"
    data, err = _request("PATCH", path, body={"values": rows})
    if err:
        return err
    return f"Wrote {len(rows)} x {width} cells to {(data or {}).get('address') or addr}."


@tool
def ms365_excel_add_rows(
    item: ItemArg,
    table: Annotated[str, "Excel table name (ms365_excel_list_sheets lists them)."],
    rows: Annotated[
        str, 'JSON array of rows, one value per table column, e.g. [["2026-10-01", "Ana", 120]].'
    ],
    drive_id: DriveArg = "",
    user_id: UserArg = "me",
) -> str:
    """Append rows to the end of an Excel table in OneDrive or SharePoint (work or school)."""
    parsed, err = _parse_rows("rows", rows)
    if err or parsed is None:
        return err or "[tool-error] rows is required."
    _, err = _request(
        "POST",
        f"{_workbook(item, drive_id, user_id)}/tables/{_seg(table)}/rows",
        body={"values": parsed, "index": None},
    )
    return err or f"Added {len(parsed)} row(s) to table {table}."


# ═══════════════════════════════════════════════════════════════════════════
# Teams
# ═══════════════════════════════════════════════════════════════════════════


@tool
def ms365_list_teams() -> str:
    """List Microsoft Teams the signed-in user has joined."""
    data, err = _request("GET", "me/joinedTeams")
    if err:
        return err
    values = _values(data)
    if not values:
        return "No joined teams found."
    return "\n".join(f"- [{t.get('id')}] {t.get('displayName')}" for t in values)


@tool
def ms365_list_channels(team_id: Annotated[str, "Team id from ms365_list_teams."]) -> str:
    """List channels in a Microsoft Team."""
    data, err = _request("GET", f"teams/{_seg(team_id)}/channels")
    if err:
        return err
    values = _values(data)
    if not values:
        return "No channels found."
    return "\n".join(f"- [{c.get('id')}] {c.get('displayName')}" for c in values)


@tool
def ms365_send_channel_message(
    team_id: Annotated[str, "Team id."],
    channel_id: Annotated[str, "Channel id."],
    message: Annotated[str, "Message text, HTML or plain."],
    reply_to_message_id: Annotated[
        str, "To answer inside an existing thread, the thread's message id."
    ] = "",
    subject: Annotated[str, "Subject line for a new post (ignored on replies)."] = "",
) -> str:
    """Post a message to a Microsoft Teams channel, or reply inside one of its threads."""
    if not message.strip():
        return "[tool-error] message is empty."
    path = f"teams/{_seg(team_id)}/channels/{_seg(channel_id)}/messages"
    payload: dict[str, Any] = {"body": {"contentType": "html", "content": _html_body(message)}}
    if reply_to_message_id.strip():
        path += f"/{_seg(reply_to_message_id)}/replies"
    elif subject.strip():
        payload["subject"] = subject.strip()
    _, err = _request("POST", path, body=payload)
    if err:
        return err
    return (
        "Replied in the Teams thread."
        if reply_to_message_id.strip()
        else "Posted to the Teams channel."
    )


def _my_id() -> tuple[str | None, str | None]:
    data, err = _request("GET", "me", params={"$select": "id"})
    if err:
        return None, err
    return str((data or {}).get("id") or ""), None


@tool
def ms365_list_chats(
    query: Annotated[str, "Only chats whose topic or member names contain this text."] = "",
    top: Annotated[int, "Most chats (1-50), most recent first."] = 20,
) -> str:
    """List your Teams chats (one-on-one, group and meeting chats), most recent first,
    with members and the last message."""
    data, err = _request(
        "GET",
        "me/chats",
        params={
            "$expand": "members,lastMessagePreview",
            "$orderby": "lastMessagePreview/createdDateTime desc",
            "$top": _top(top, 20, 50),
        },
    )
    if err:
        return err
    me, _ = _my_id()
    chats = _values(data)
    lines: list[str] = []
    needle = query.strip().lower()
    for chat in chats:
        members = [
            str(m.get("displayName") or m.get("email") or "?")
            for m in (chat.get("members") or [])
            if isinstance(m, dict) and m.get("userId") != me
        ]
        title = chat.get("topic") or ", ".join(members) or "(no members)"
        if needle and needle not in f"{title} {' '.join(members)}".lower():
            continue
        last = chat.get("lastMessagePreview") or {}
        sender = ((last.get("from") or {}).get("user") or {}).get("displayName") or ""
        preview = _clip(_html_to_text(str((last.get("body") or {}).get("content") or "")), 100)
        line = f"- {title} [{chat.get('chatType')}]"
        if preview:
            line += f"\n  last: {_fmt_dt(last.get('createdDateTime'))} {sender}: {preview}"
        lines.append(line + f"\n  chat id: {chat.get('id')}")
    return "\n".join(lines) if lines else "No chats found."


@tool
def ms365_read_chat(
    chat_id: Annotated[str, "Chat id from ms365_list_chats."],
    top: Annotated[int, "How many recent messages (1-50)."] = 20,
) -> str:
    """Read the most recent messages of a Teams chat, oldest first."""
    data, err = _request(
        "GET",
        f"chats/{_seg(chat_id)}/messages",
        params={"$top": _top(top, 20, 50), "$orderby": "createdDateTime desc"},
    )
    if err:
        return err
    messages = list(reversed(_values(data)))
    lines = []
    for m in messages:
        if m.get("messageType") != "message" or m.get("deletedDateTime"):
            continue
        sender = ((m.get("from") or {}).get("user") or {}).get("displayName") or "?"
        text = _html_to_text(str((m.get("body") or {}).get("content") or ""))
        files = [a.get("name") for a in (m.get("attachments") or []) if a.get("name")]
        line = f"[{_fmt_dt(m.get('createdDateTime'))}] {sender}: {_clip(text, 1000)}"
        if files:
            line += f" (files: {', '.join(files)})"
        lines.append(line)
    return "\n".join(lines) if lines else "No messages in this chat."


@tool
def ms365_send_chat_message(
    message: Annotated[str, "Message text, HTML or plain."],
    chat_id: Annotated[str, "An existing chat's id (ms365_list_chats)."] = "",
    to: Annotated[str, "Or: one person's email, to message them one-on-one."] = "",
) -> str:
    """Send a Teams chat message to an existing chat, or one-on-one to a person by email."""
    if not message.strip():
        return "[tool-error] message is empty."
    target = chat_id.strip()
    person = ""
    if not target:
        people, err = _recipients(to, "to")
        if err:
            return err
        if len(people) != 1:
            return "[tool-error] pass chat_id, or exactly one email in to (group chats: use their chat_id)."
        person = people[0]["emailAddress"]["address"]
        me, err = _my_id()
        if err:
            return err
        member = "#microsoft.graph.aadUserConversationMember"
        chat, err = _request(
            "POST",
            "chats",
            body={
                "chatType": "oneOnOne",
                "members": [
                    {
                        "@odata.type": member,
                        "roles": ["owner"],
                        "user@odata.bind": f"{_GRAPH_BASE}/users('{_odata_str(me or '')}')",
                    },
                    {
                        "@odata.type": member,
                        "roles": ["owner"],
                        "user@odata.bind": f"{_GRAPH_BASE}/users('{_odata_str(person)}')",
                    },
                ],
            },
        )
        if err:
            return err
        target = str((chat or {}).get("id") or "")
    _, err = _request(
        "POST",
        f"chats/{_seg(target)}/messages",
        body={"body": {"contentType": "html", "content": _html_body(message)}},
    )
    if err:
        return err
    return f"Sent a Teams chat message to {person}." if person else "Sent the Teams chat message."


# ═══════════════════════════════════════════════════════════════════════════
# Microsoft To Do
# ═══════════════════════════════════════════════════════════════════════════

TaskListArg = Annotated[str, "Task list name or id. Empty = the default Tasks list."]


def _task_list_id(ref: str, user_id: str) -> tuple[str | None, str | None]:
    data, err = _request("GET", f"{_user(user_id)}/todo/lists")
    if err:
        return None, err
    lists = _values(data)
    wanted = (ref or "").strip()
    for entry in lists:
        if not wanted and entry.get("wellknownListName") == "defaultList":
            return str(entry.get("id")), None
        if wanted and (
            entry.get("id") == wanted
            or str(entry.get("displayName") or "").lower() == wanted.lower()
        ):
            return str(entry.get("id")), None
    if not wanted and lists:
        return str(lists[0].get("id")), None
    names = ", ".join(str(e.get("displayName")) for e in lists) or "(none)"
    return None, f"[tool-error] no To Do list named '{wanted}'. Lists: {names}."


def _task_status(value: str) -> str | None:
    return {
        "open": "notStarted",
        "notstarted": "notStarted",
        "not started": "notStarted",
        "todo": "notStarted",
        "reopen": "notStarted",
        "inprogress": "inProgress",
        "in progress": "inProgress",
        "started": "inProgress",
        "done": "completed",
        "complete": "completed",
        "completed": "completed",
        "waiting": "waitingOnOthers",
        "waitingonothers": "waitingOnOthers",
        "deferred": "deferred",
    }.get((value or "").strip().lower())


@tool
def ms365_list_task_lists(user_id: UserArg = "me") -> str:
    """List Microsoft To Do task lists."""
    data, err = _request("GET", f"{_user(user_id)}/todo/lists")
    if err:
        return err
    lists = _values(data)
    if not lists:
        return "No task lists found."
    return "\n".join(
        f"- {e.get('displayName')}"
        + (" (default)" if e.get("wellknownListName") == "defaultList" else "")
        + (" (shared)" if e.get("isShared") else "")
        + f"\n  id: {e.get('id')}"
        for e in lists
    )


@tool
def ms365_list_tasks(
    task_list: TaskListArg = "",
    status: Annotated[str, "open (default), completed or all."] = "open",
    top: Annotated[int, "Most tasks (1-100)."] = 25,
    user_id: UserArg = "me",
) -> str:
    """List Microsoft To Do tasks with status, importance and due date."""
    list_id, err = _task_list_id(task_list, user_id)
    if err:
        return err
    params: dict[str, Any] = {"$top": _top(top, 25)}
    state = (status or "open").strip().lower()
    if state == "open":
        params["$filter"] = "status ne 'completed'"
    elif state in ("completed", "done"):
        params["$filter"] = "status eq 'completed'"
    elif state != "all":
        return "[tool-error] status must be open, completed or all."
    data, err = _request(
        "GET", f"{_user(user_id)}/todo/lists/{_seg(list_id or '')}/tasks", params=params
    )
    if err:
        return err
    tasks = _values(data)
    if not tasks:
        return "No tasks found."
    lines = []
    for t in tasks:
        due = (t.get("dueDateTime") or {}).get("dateTime")
        bits = [str(t.get("status"))]
        if t.get("importance") == "high":
            bits.append("high importance")
        if due:
            bits.append(f"due {str(due)[:10]}")
        lines.append(f"- {t.get('title')} [{', '.join(bits)}]\n  id: {t.get('id')}")
    return "\n".join(lines) + _page_hint(data)


@tool
def ms365_create_task(
    title: Annotated[str, "What needs doing."],
    task_list: TaskListArg = "",
    due_date: Annotated[str, "Due date (YYYY-MM-DD)."] = "",
    notes: Annotated[str, "Details."] = "",
    importance: Annotated[str, "low, normal (default) or high."] = "normal",
    reminder: Annotated[str, "Reminder time, e.g. '2026-10-05T09:00' (local to time_zone)."] = "",
    time_zone: TimeZoneArg = "",
    user_id: UserArg = "me",
) -> str:
    """Add a task to Microsoft To Do."""
    if not title.strip():
        return "[tool-error] title is required."
    list_id, err = _task_list_id(task_list, user_id)
    if err:
        return err
    level = (importance or "normal").strip().lower()
    if level not in ("low", "normal", "high"):
        return "[tool-error] importance must be low, normal or high."
    payload: dict[str, Any] = {"title": title.strip(), "importance": level}
    if notes.strip():
        payload["body"] = {"content": notes.strip(), "contentType": "text"}
    zone = ""
    if due_date.strip() or reminder.strip():
        zone = _resolve_tz(time_zone, user_id)
    if due_date.strip():
        if not _is_date_only(due_date):
            return _bad_when("due_date", due_date)
        payload["dueDateTime"] = {"dateTime": f"{due_date.strip()}T00:00:00", "timeZone": zone}
    if reminder.strip():
        when, err = _graph_dt("reminder", reminder, zone)
        if err:
            return err
        payload["isReminderOn"] = True
        payload["reminderDateTime"] = when
    data, err = _request(
        "POST", f"{_user(user_id)}/todo/lists/{_seg(list_id or '')}/tasks", body=payload
    )
    if err:
        return err
    return (
        f"Added task '{title.strip()}'"
        + (f", due {due_date.strip()}" if due_date.strip() else "")
        + (f".\nid: {(data or {}).get('id')}")
    )


@tool
def ms365_update_task(
    task_id: Annotated[str, "Task id from ms365_list_tasks."],
    task_list: TaskListArg = "",
    title: Annotated[str | None, "New title. Omit to keep."] = None,
    status: Annotated[
        str, "completed (done), notStarted (reopen), inProgress, waitingOnOthers or deferred."
    ] = "",
    due_date: Annotated[
        str | None, "New due date (YYYY-MM-DD); '' clears it. Omit to keep."
    ] = None,
    notes: Annotated[str | None, "New details (replaces them). Omit to keep."] = None,
    importance: Annotated[str, "low, normal or high. Empty keeps it."] = "",
    time_zone: TimeZoneArg = "",
    user_id: UserArg = "me",
) -> str:
    """Update a Microsoft To Do task: complete or reopen it, rename it, change its due date."""
    list_id, err = _task_list_id(task_list, user_id)
    if err:
        return err
    patch: dict[str, Any] = {}
    if title is not None:
        patch["title"] = title
    if status.strip():
        state = _task_status(status)
        if state is None:
            return "[tool-error] status must be completed, notStarted, inProgress, waitingOnOthers or deferred."
        patch["status"] = state
    if due_date is not None:
        if not due_date.strip():
            patch["dueDateTime"] = None
        elif not _is_date_only(due_date):
            return _bad_when("due_date", due_date)
        else:
            patch["dueDateTime"] = {
                "dateTime": f"{due_date.strip()}T00:00:00",
                "timeZone": _resolve_tz(time_zone, user_id),
            }
    if notes is not None:
        patch["body"] = {"content": notes, "contentType": "text"}
    level = importance.strip().lower()
    if level:
        if level not in ("low", "normal", "high"):
            return "[tool-error] importance must be low, normal or high."
        patch["importance"] = level
    if not patch:
        return "[tool-error] nothing to change: pass at least one field."
    data, err = _request(
        "PATCH",
        f"{_user(user_id)}/todo/lists/{_seg(list_id or '')}/tasks/{_seg(task_id)}",
        body=patch,
    )
    if err:
        return err
    return f"Updated task '{(data or {}).get('title') or task_id}' (now {(data or {}).get('status', '?')})."


# ═══════════════════════════════════════════════════════════════════════════
# Misc
# ═══════════════════════════════════════════════════════════════════════════


@tool
def ms365_decode_file_content(encoded_content: str, output_path: str) -> str:
    """Decode base64 content into a local file for Microsoft 365 upload prep."""
    try:
        Path(output_path).expanduser().write_bytes(base64.b64decode(encoded_content))
        return f"Wrote decoded file to {output_path}"
    except Exception as exc:
        return f"[tool-error] ms365_decode_file_content: {exc}"


get_tools, get_tool = make_toolkit_api(globals())
