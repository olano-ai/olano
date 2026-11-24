"""
Google Services tools — Gmail, Calendar, Drive, Docs, Sheets.

Authentication: OAuth2 with token cached in the active Olano home.
Connect from the dashboard Connections tab, or run ``olano auth google``
(one-time browser flow). The OAuth client is a Client ID/Secret pair like
every other connector — vault ``GOOGLE_OAUTH_CLIENT`` blob or the
``GOOGLE_CLIENT_ID``/``GOOGLE_CLIENT_SECRET`` pair; the legacy
``GOOGLE_OAUTH_CREDENTIALS`` path to a client_secret.json still works
(see ``_resolve_client_source``).

Scopes used:
  - gmail.readonly, gmail.send, gmail.modify  (read + send + label/draft/trash)
  - calendar  (full read/write)
  - drive     (full read/write)
  - documents (Google Docs read/write)
  - spreadsheets (Google Sheets read/write)
  - youtube.readonly  (read-only OAuth fallback for the YouTube toolkit when
    YOUTUBE_API_KEY is unset — the toolkit only reads)

``_SCOPES`` is what **new** consent flows request. Existing tokens are loaded
and refreshed with the scopes they were actually granted (a refresh token can
never be up-scoped — asking Google for more at refresh time fails with
``invalid_scope``), so tokens from before a scope was added keep working for
everything their grant covers. In practice that means only the YouTube tools'
OAuth fallback needs a one-time re-auth (``olano auth google``, or reconnect
Google in the dashboard Connections tab) to pick up youtube.readonly. The
Sheets tools need **no** re-auth: the Sheets API also accepts the full ``drive``
scope every existing token already carries — ``spreadsheets`` is listed so new
authorizations declare the narrower intent explicitly.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, cast

from langchain_core.tools import tool

from olano.config.home import get_olano_paths
from olano.security.tool_metadata import tool_metadata

logger = logging.getLogger(__name__)

# Requested by NEW consent flows only — never pass this when loading a saved
# token (its own granted scopes must drive refresh; see module docstring).
_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/youtube.readonly",
]

# OAuth client sources, in stored-resolution order (see _resolve_client_source).
_CLIENT_BLOB_KEY = "GOOGLE_OAUTH_CLIENT"  # {"client_id": …, "client_secret": …}
_CLIENT_ID_ENV = "GOOGLE_CLIENT_ID"
_CLIENT_SECRET_ENV = "GOOGLE_CLIENT_SECRET"
_CREDS_PATH_ENV = "GOOGLE_OAUTH_CREDENTIALS"  # legacy: path to client_secret.json

# Set by AgentRunner before each invocation so tools use the correct token scope.
# None → fall through to system token. Defined in the lightweight _agent_ctx
# module (re-exported here) so setting it never requires importing this module.
from olano.tools._agent_ctx import AGENT_CTX

# ── Connection metadata helpers ────────────────────────────────────────────────


def _load_connections_meta(agent_name: str) -> dict:
    path = get_olano_paths().agent_connections_meta_path(agent_name)
    if not path.exists():
        return {}
    try:
        return cast(dict, json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return {}


def _save_connections_meta(agent_name: str, meta: dict) -> None:
    paths = get_olano_paths()
    path = paths.agent_connections_meta_path(agent_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def is_google_shared(agent_name: str) -> bool:
    """Return True if this agent's Google token is shared with other agents."""
    meta = _load_connections_meta(agent_name)
    return bool(meta.get("google", {}).get("shared", False))


def set_google_shared(agent_name: str, shared: bool) -> None:
    meta = _load_connections_meta(agent_name)
    meta.setdefault("google", {})["shared"] = shared
    _save_connections_meta(agent_name, meta)


def clear_google_meta(agent_name: str) -> None:
    """Drop the Google entry from the agent's connections meta.

    Called on disconnect: a leftover ``shared: true`` would silently re-share
    the *next* token connected for this agent, even though the reconnect asked
    for a private one.
    """
    meta = _load_connections_meta(agent_name)
    if meta.pop("google", None) is not None:
        _save_connections_meta(agent_name, meta)


# ── Token path resolution (three-tier) ────────────────────────────────────────
# Priority: own token → any shared agent token → system token


def _token_path() -> Path:
    """Resolve the Google token path for the current agent context."""
    agent = AGENT_CTX.get()
    paths = get_olano_paths()

    if agent:
        own = paths.agent_google_token_path(agent)
        if own.exists():
            return own

        # Fall through to any other agent whose token is marked shared
        shared_token = _find_shared_token(exclude=agent)
        if shared_token:
            return shared_token

    return paths.google_token_path


def _find_shared_token(exclude: str | None = None) -> Path | None:
    """Return the first agent token marked shared=True, skipping `exclude`."""
    paths = get_olano_paths()
    agents_dir = paths.agents_dir
    if not agents_dir.exists():
        return None
    for agent_dir in sorted(agents_dir.iterdir()):
        if not agent_dir.is_dir():
            continue
        name = agent_dir.name
        if name == exclude:
            continue
        token = paths.agent_google_token_path(name)
        if token.exists() and is_google_shared(name):
            return token
    return None


def _resolve_token_path(agent: str | None) -> Path:
    """Resolve token path for a given agent name (used by status/auth APIs)."""
    paths = get_olano_paths()
    if agent is None:
        return paths.google_token_path
    own = paths.agent_google_token_path(agent)
    if own.exists():
        return own
    shared = _find_shared_token(exclude=agent)
    if shared:
        return shared
    return paths.google_token_path


def _get_credentials() -> Any:
    """Return valid Google OAuth2 credentials (refresh if needed)."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = None
    token_path = _token_path()
    if token_path.exists():
        # No scopes arg: google-auth then adopts the token file's own granted
        # scopes. Pinning _SCOPES here would make refresh request scopes the
        # refresh token never consented to → Google rejects with invalid_scope.
        creds = Credentials.from_authorized_user_file(str(token_path))

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _save_token(creds, token_path)
        else:
            raise RuntimeError("Google credentials not found. Run: olano auth google")

    return creds


def _save_token(creds: Any, path: Path | None = None) -> None:
    # ``to_json`` writes only the credential fields, so a successful refresh
    # also drops any ``auth_failed_at`` mark: the grant just proved itself.
    if path is None:
        path = _token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json(), encoding="utf-8")


# ── Refusal reports (the toolkit-side seam, see tools/_connector_auth.py) ────
# The Google token file is google-auth's own JSON; the two extra keys below ride
# in the same file (``from_authorized_user_file`` ignores unknown keys), so the
# Connections card can say "Needs reconnecting" for a Google grant the API
# refused, exactly as the connector broker does for every other service.
_AUTH_FAILED_AT = "auth_failed_at"
_AUTH_ERROR = "auth_error"


def _read_token_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def refresh_credentials_now() -> tuple[bool, str]:
    """Renew the current agent's Google token regardless of what the file says.

    A Google API answering 401 with a token the file still calls valid means
    the session was revoked or the grant died with a password change; one
    refresh repairs the alive-grant case with nobody involved. Returns
    ``(True, "")`` when a fresh token was saved and ``(False, why)`` otherwise
    (no token, no refresh token, or Google refused: ``invalid_grant``).
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        path = _token_path()
        if not path.exists():
            return False, "no Google connection is stored at this scope"
        creds = Credentials.from_authorized_user_file(str(path))
        if not creds.refresh_token:
            return False, "Google issued no refresh token for this connection"
        creds.refresh(Request())
        _save_token(creds, path)
        return True, ""
    except Exception as exc:
        return False, f"token refresh failed: {exc}"


def mark_google_auth_failed(error: str) -> bool:
    """Stamp the current agent's Google token as refused by Google.

    The credentials stay in place (a spurious refusal must not strand a
    working token); :func:`google_auth_status` reads the mark as
    ``connected: False`` + ``needs_reauth`` and a new sign-in, or the next
    honoured refresh, clears it. Returns whether a token file was marked.
    """
    path = _token_path()
    data = _read_token_json(path)
    if not data:
        return False
    data[_AUTH_FAILED_AT] = time.time()
    data[_AUTH_ERROR] = " ".join(str(error).split())[:240]
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return True


# ── Auth status helper (used by API) ──────────────────────────────────────────


def google_auth_status(agent: str | None = None) -> dict:
    """Return connection status for a given scope.

    ``agent=None`` checks the system token only.
    ``agent="name"`` checks own → shared → system.
    """
    from google.oauth2.credentials import Credentials

    paths = get_olano_paths()

    if agent is None:
        token_path = paths.google_token_path
        scope = "system"
        shared = None
    else:
        own = paths.agent_google_token_path(agent)
        if own.exists():
            token_path = own
            scope = "agent"
            shared = is_google_shared(agent)
        else:
            fallback = _find_shared_token(exclude=agent)
            if fallback:
                token_path = fallback
                scope = "shared"
                shared = True
            else:
                system = paths.google_token_path
                if system.exists():
                    token_path = system
                    scope = "system"
                    shared = None
                else:
                    return {"connected": False, "scope": "none", "token_path": None, "shared": None}

    if not token_path.exists():
        return {"connected": False, "scope": scope, "token_path": str(token_path), "shared": shared}

    try:
        creds = Credentials.from_authorized_user_file(str(token_path))
        connected = creds.valid or (creds.expired and bool(creds.refresh_token))
    except Exception:
        connected = False

    status: dict[str, Any] = {
        "connected": connected,
        "scope": scope,
        "token_path": str(token_path),
        "shared": shared,
    }
    marked = _read_token_json(token_path)
    if marked.get(_AUTH_FAILED_AT) is not None:
        # Google refused this token after a refresh attempt: the card must not
        # read "Connected" off a file that only says the token exists.
        status["connected"] = False
        status["needs_reauth"] = True
        status["auth_error"] = str(marked.get(_AUTH_ERROR) or "")
        status["auth_failed_at"] = float(marked[_AUTH_FAILED_AT])
    return status


def _gmail_service() -> Any:
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=_get_credentials())


def _calendar_service() -> Any:
    from googleapiclient.discovery import build

    return build("calendar", "v3", credentials=_get_credentials())


def _drive_service() -> Any:
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=_get_credentials())


def _docs_service() -> Any:
    from googleapiclient.discovery import build

    return build("docs", "v1", credentials=_get_credentials())


def _sheets_service() -> Any:
    from googleapiclient.discovery import build

    return build("sheets", "v4", credentials=_get_credentials())


def _decode_email_body(payload: dict) -> str:
    """Extract the text body from a Gmail message payload.

    Prefers a ``text/plain`` part; an HTML-only message (common for broker and
    bank notifications) falls back to its ``text/html`` part rendered to plain
    text, so the agent never reads an empty body where the mail has one.
    """
    return _find_body_part(payload, "text/plain") or _html_to_plaintext(
        _find_body_part(payload, "text/html")
    )


def _find_body_part(payload: dict, mime: str) -> str:
    """The first inline (non-attachment) part of ``mime``, decoded."""
    import base64

    if payload.get("mimeType", "").startswith(mime) and not payload.get("filename"):
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")

    for part in payload.get("parts", []):
        text = _find_body_part(part, mime)
        if text:
            return text

    return ""


def _attachment_parts(payload: dict) -> list[dict]:
    """Every part of a Gmail payload that carries a named file, depth first.

    A part is a file when it has a ``filename``; its bytes are either inline
    (``body.data``, small files) or fetched separately by ``body.attachmentId``.
    """
    found: list[dict] = []
    if payload.get("filename") and (
        payload.get("body", {}).get("attachmentId") or payload.get("body", {}).get("data")
    ):
        found.append(payload)
    for part in payload.get("parts", []) or []:
        found.extend(_attachment_parts(part))
    return found


def _gmail_received_utc(msg: dict) -> str:
    """Gmail's own receive time for a message, as ISO-8601 UTC.

    ``internalDate`` is set by Gmail's servers when the message arrived (or was
    sent, for the mailbox's own mail). The ``Date`` header is whatever the
    sender's client wrote, so it is the weaker timestamp for evidence.
    """
    from datetime import UTC, datetime

    raw = msg.get("internalDate")
    if raw is None:
        return "?"
    try:
        return (
            datetime.fromtimestamp(int(raw) / 1000, tz=UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def _gmail_workspace_root() -> Path:
    """The running agent's workspace (not the legacy global one)."""
    from olano.config.workspace import get_workspace
    from olano.security.request_context import get_request_context

    root = get_request_context().metadata.get("workspace_root")
    return get_workspace(str(root) if root else None).root


# ── Gmail ────────────────────────────────────────────────────────────────────


def _attach_file(message: Any, path: str) -> None:
    """Attach one local file to a multipart email message.

    Image files are attached with an ``image/*`` MIME type so Gmail renders a
    preview; everything else is base64-encoded as a generic attachment. Raises
    ``FileNotFoundError`` if the path does not point at a real file.
    """
    import mimetypes
    from email import encoders
    from email.mime.base import MIMEBase
    from email.mime.image import MIMEImage

    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(path)

    mime_type = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
    maintype, _, subtype = mime_type.partition("/")
    data = p.read_bytes()

    if maintype == "image":
        part: Any = MIMEImage(data, _subtype=subtype or "octet-stream")
    else:
        part = MIMEBase(maintype or "application", subtype or "octet-stream")
        part.set_payload(data)
        encoders.encode_base64(part)

    part.add_header("Content-Disposition", "attachment", filename=p.name)
    message.attach(part)


def _html_to_plaintext(html: str) -> str:
    """Best-effort plain-text fallback derived from an HTML email body."""
    import re
    from html import unescape

    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|h[1-6]|li|tr|table|ul|ol)>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = unescape(text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _build_email_message(body: str, attachments: str = "") -> Any:
    """Build the MIME payload for an outgoing email.

    ``body`` is treated as **HTML**: every message ships as a
    ``multipart/alternative`` carrying an auto-derived ``text/plain`` fallback
    (see :func:`_html_to_plaintext`) plus the rich ``text/html`` part, so clients
    that can't render HTML still show readable text. When ``attachments`` is a
    non-empty comma-separated list of local paths, the alternative is wrapped in
    a ``multipart/mixed`` with one part per file (images get an ``image/*`` type;
    see :func:`_attach_file`).

    Callers set the ``to`` / ``subject`` / ``cc`` (and any threading) headers on
    the returned message.
    """
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    content: Any = MIMEMultipart("alternative")
    content.attach(MIMEText(_html_to_plaintext(body), "plain", "utf-8"))
    content.attach(MIMEText(body, "html", "utf-8"))

    paths = [p.strip() for p in attachments.split(",") if p.strip()]
    if not paths:
        return content

    message = MIMEMultipart()  # multipart/mixed: HTML alternative + file parts
    message.attach(content)
    for path in paths:
        _attach_file(message, path)
    return message


@tool
def gmail_read(
    max_results: Annotated[int, "Maximum number of emails to return (1-50)"] = 10,
    query: Annotated[str, "Gmail search query (e.g., 'is:unread', 'from:boss@company.com')"] = "",
) -> str:
    """
    Read recent emails from Gmail. Optionally filter with a Gmail search query.
    """
    try:
        service = _gmail_service()
    except Exception as e:
        return f"Error connecting to Gmail: {e}"

    max_results = max(1, min(50, max_results))
    params: dict[str, Any] = {"userId": "me", "maxResults": max_results}
    if query:
        params["q"] = query

    try:
        result = service.users().messages().list(**params).execute()
        messages = result.get("messages", [])
    except Exception as e:
        return f"Error fetching Gmail messages: {e}"

    if not messages:
        return "No emails found."

    lines = [f"Gmail ({len(messages)} messages):\n"]
    for msg_ref in messages:
        try:
            msg = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=msg_ref["id"],
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                )
                .execute()
            )
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            snippet = msg.get("snippet", "")[:150]
            lines.append(
                f"ID: {msg_ref['id']}\n"
                f"From: {headers.get('From', '?')}\n"
                f"Subject: {headers.get('Subject', '(no subject)')}\n"
                f"Date: {headers.get('Date', '?')}\n"
                f"Received (UTC): {_gmail_received_utc(msg)}\n"
                f"Preview: {snippet}\n"
            )
        except Exception:
            lines.append(f"(error reading message {msg_ref['id']})\n")

    return "\n".join(lines)


@tool
def gmail_search(
    query: Annotated[str, "Gmail search query (e.g., 'from:alice@example.com subject:invoice')"],
    max_results: Annotated[int, "Maximum results (1-20)"] = 5,
) -> str:
    """
    Search Gmail with a query string and return matching email summaries.
    """
    return str(gmail_read.invoke({"max_results": max_results, "query": query}))


@tool
def gmail_send(
    to: Annotated[str, "Recipient email address"],
    subject: Annotated[str, "Email subject line"],
    body: Annotated[
        str,
        "Email body as HTML. You MUST provide well-formed HTML markup (e.g. '<p>Hi <b>there</b></p>' with <p>, <ul>, <a>, inline styles, etc.), never plain text. The message is sent as a rich HTML email and a plain-text fallback is generated automatically.",
    ] = "",
    cc: Annotated[str, "CC email addresses (comma-separated, optional)"] = "",
    attachments: Annotated[
        str,
        "Local file paths to attach (comma-separated, optional). Images are sent with an image/* type so Gmail shows a preview.",
    ] = "",
) -> str:
    """
    Send a rich HTML email via Gmail.

    The ``body`` must be HTML — compose emails with HTML markup (headings,
    paragraphs, lists, links, inline styles) rather than plain text; a
    text/plain fallback is generated automatically for non-HTML clients.
    Optionally attach files or images via ``attachments`` (comma-separated
    local paths).
    """
    try:
        service = _gmail_service()
    except Exception as e:
        return f"Error connecting to Gmail: {e}"

    import base64

    try:
        message = _build_email_message(body, attachments=attachments)
    except FileNotFoundError as e:
        return f"Error: attachment not found: {e}"
    message["to"] = to.strip()
    message["subject"] = subject
    if cc:
        message["cc"] = cc.strip()

    try:
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return f"Email sent successfully. Message ID: {result.get('id', '?')}"
    except Exception as e:
        return f"Error sending email: {e}"


@tool
def gmail_get_message(
    message_id: Annotated[str, "Gmail message ID (from gmail_read / gmail_search)"],
) -> str:
    """Fetch one Gmail message by ID: headers, body and the list of its attachments.

    ``Received (UTC)`` is Gmail's own server timestamp for the message, the one
    to cite as evidence of when it was sent or arrived; ``Date`` is what the
    sender's mail client wrote. Save attachments with gmail_download_attachments.
    """
    from olano.uploads import human_size

    try:
        service = _gmail_service()
    except Exception as e:
        return f"Error connecting to Gmail: {e}"

    try:
        msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
        payload = msg.get("payload", {})
        headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
        body = _decode_email_body(payload) or msg.get("snippet", "")
        lines = [
            f"From: {headers.get('From', '?')}",
            f"To: {headers.get('To', '?')}",
        ]
        if headers.get("Cc"):
            lines.append(f"Cc: {headers['Cc']}")
        lines += [
            f"Date: {headers.get('Date', '?')}",
            f"Received (UTC): {_gmail_received_utc(msg)}",
            f"Subject: {headers.get('Subject', '(no subject)')}",
            f"Message-ID: {headers.get('Message-ID') or headers.get('Message-Id') or '?'}",
            f"Thread ID: {msg.get('threadId', '?')}",
            f"Labels: {', '.join(msg.get('labelIds', [])) or '(none)'}",
        ]
        files = _attachment_parts(payload)
        if files:
            lines.append(f"Attachments ({len(files)}), save with gmail_download_attachments:")
            for part in files:
                size = part.get("body", {}).get("size")
                lines.append(
                    f"- {part.get('filename')} "
                    f"({part.get('mimeType') or '?'}, "
                    f"{human_size(int(size)) if isinstance(size, int) else '?'})"
                )
        return "\n".join(lines) + f"\n\n{body}"
    except Exception as e:
        return f"Error fetching message {message_id}: {e}"


@tool
def gmail_download_attachments(
    message_id: Annotated[str, "Gmail message ID (from gmail_read / gmail_search)"],
    filenames: Annotated[
        str,
        "Attachment filenames to save, comma-separated, exactly as gmail_get_message "
        "lists them. Empty saves every attachment on the message.",
    ] = "",
    save_dir: Annotated[
        str,
        "Folder inside the workspace to save into. Empty uses downloads/gmail/<message_id>.",
    ] = "",
) -> str:
    """Save a Gmail message's attachments into the workspace, where other tools can open them.

    Returns one ``Saved: <path>`` line per file. Use it to keep a PDF
    confirmation, a spreadsheet or a statement that arrived by email as a file
    the agent can read, parse or attach elsewhere.
    """
    import base64
    import re

    from olano.tools._sandbox_exec import handoff_to_sandbox, missing_parents
    from olano.uploads import human_size, sanitize_filename

    try:
        service = _gmail_service()
    except Exception as e:
        return f"Error connecting to Gmail: {e}"

    try:
        msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    except Exception as e:
        return f"Error fetching message {message_id}: {e}"

    files = _attachment_parts(msg.get("payload", {}))
    wanted = [f.strip() for f in filenames.split(",") if f.strip()]
    if wanted:
        unknown = [w for w in wanted if w not in {p.get("filename") for p in files}]
        if unknown:
            listed = ", ".join(str(p.get("filename")) for p in files) or "none"
            return (
                f"[tool-error] no attachment named {', '.join(unknown)} on this message. "
                f"Its attachments: {listed}."
            )
        files = [p for p in files if p.get("filename") in wanted]
    if not files:
        return "The message has no attachments."

    root = _gmail_workspace_root().resolve()
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", message_id)[:64] or "message"
    base = (
        Path(save_dir.strip()).expanduser()
        if save_dir.strip()
        else Path("downloads/gmail") / safe_id
    )
    if not base.is_absolute():
        base = root / base
    base = base.resolve()
    if base != root and not base.is_relative_to(root):
        return f"[tool-error] save_dir must be inside the workspace ({root}); got {save_dir}."
    created: list[Path] = missing_parents(base / "_")
    base.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    for part in files:
        body = part.get("body", {}) or {}
        try:
            data = body.get("data")
            if not data:
                fetched = (
                    service.users()
                    .messages()
                    .attachments()
                    .get(userId="me", messageId=message_id, id=body["attachmentId"])
                    .execute()
                )
                data = fetched.get("data", "")
            raw = base64.urlsafe_b64decode(str(data) + "==")
        except Exception as e:
            return f"Error downloading {part.get('filename')}: {e}"
        name = sanitize_filename(
            str(part.get("filename") or ""), str(part.get("mimeType") or ""), "file"
        )
        target = base / name
        stem, suffix, n = target.stem, target.suffix, 1
        while target.exists():
            if target.read_bytes() == raw:
                break  # the same file saved earlier: reuse it
            target = base / f"{stem}-{n}{suffix}"
            n += 1
        if not target.exists():
            target.write_bytes(raw)
        handoff_to_sandbox(target, created=created)
        created = []  # the new folders are handed over once, with the first file
        lines.append(f"Saved: {target} ({human_size(len(raw))})")
    return "\n".join(lines)


@tool
def gmail_reply(
    message_id: Annotated[str, "Gmail message ID to reply to"],
    body: Annotated[
        str,
        "Reply body as HTML. You MUST provide well-formed HTML markup (e.g. '<p>Thanks!</p>'), never plain text. The reply is sent as a rich HTML email and a plain-text fallback is generated automatically.",
    ] = "",
    attachments: Annotated[
        str,
        "Local file paths to attach (comma-separated, optional). Images are sent with an image/* type so Gmail shows a preview.",
    ] = "",
) -> str:
    """Reply to a Gmail message as rich HTML, preserving the thread and subject.

    The ``body`` must be HTML; a text/plain fallback is generated automatically.
    Optionally attach files or images via ``attachments``.
    """
    try:
        service = _gmail_service()
    except Exception as e:
        return f"Error connecting to Gmail: {e}"

    import base64

    try:
        message = _build_email_message(body, attachments=attachments)
    except FileNotFoundError as e:
        return f"Error: attachment not found: {e}"

    try:
        original = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Message-ID", "References"],
            )
            .execute()
        )
        headers = {h["name"]: h["value"] for h in original.get("payload", {}).get("headers", [])}
        thread_id = original.get("threadId")
        subject = headers.get("Subject", "")
        if subject and not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        message["to"] = headers.get("From", "").strip()
        message["subject"] = subject
        if mid := headers.get("Message-ID"):
            message["In-Reply-To"] = mid
            existing_refs = headers.get("References", "")
            message["References"] = (existing_refs + " " + mid).strip()

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        result = (
            service.users()
            .messages()
            .send(userId="me", body={"raw": raw, "threadId": thread_id})
            .execute()
        )
        return f"Reply sent. Message ID: {result.get('id', '?')}, thread {thread_id}"
    except Exception as e:
        return f"Error sending reply: {e}"


@tool
def gmail_create_draft(
    to: Annotated[str, "Recipient email address"],
    subject: Annotated[str, "Subject line"],
    body: Annotated[
        str,
        "Draft body as HTML. You MUST provide well-formed HTML markup (e.g. '<p>Hi <b>there</b></p>'), never plain text. The draft is a rich HTML email and a plain-text fallback is generated automatically.",
    ] = "",
    cc: Annotated[str, "CC addresses (comma-separated, optional)"] = "",
    attachments: Annotated[
        str,
        "Local file paths to attach (comma-separated, optional). Images are sent with an image/* type so Gmail shows a preview.",
    ] = "",
) -> str:
    """Create a rich HTML Gmail draft without sending it.

    The ``body`` must be HTML; a text/plain fallback is generated automatically.
    Optionally attach files or images via ``attachments``.
    """
    try:
        service = _gmail_service()
    except Exception as e:
        return f"Error connecting to Gmail: {e}"

    import base64

    try:
        message = _build_email_message(body, attachments=attachments)
    except FileNotFoundError as e:
        return f"Error: attachment not found: {e}"
    message["to"] = to.strip()
    message["subject"] = subject
    if cc:
        message["cc"] = cc.strip()
    try:
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        result = (
            service.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
        )
        return f"Draft created. ID: {result.get('id', '?')}"
    except Exception as e:
        return f"Error creating draft: {e}"


@tool
def gmail_modify_labels(
    message_id: Annotated[str, "Gmail message ID"],
    add_labels: Annotated[str, "Comma-separated label IDs to add (e.g. 'STARRED,IMPORTANT')"] = "",
    remove_labels: Annotated[str, "Comma-separated label IDs to remove (e.g. 'UNREAD,INBOX')"] = "",
) -> str:
    """Add or remove labels on a Gmail message. Common system labels: UNREAD, STARRED, INBOX, IMPORTANT."""
    try:
        service = _gmail_service()
    except Exception as e:
        return f"Error connecting to Gmail: {e}"

    body = {
        "addLabelIds": [s.strip() for s in add_labels.split(",") if s.strip()],
        "removeLabelIds": [s.strip() for s in remove_labels.split(",") if s.strip()],
    }
    if not body["addLabelIds"] and not body["removeLabelIds"]:
        return "No labels specified."
    try:
        result = service.users().messages().modify(userId="me", id=message_id, body=body).execute()
        return f"Labels updated. Now: {', '.join(result.get('labelIds', [])) or '(none)'}"
    except Exception as e:
        return f"Error modifying labels: {e}"


@tool
def gmail_trash(
    message_id: Annotated[str, "Gmail message ID to move to trash"],
) -> str:
    """Move a Gmail message to the Trash folder (recoverable for 30 days)."""
    try:
        service = _gmail_service()
    except Exception as e:
        return f"Error connecting to Gmail: {e}"
    try:
        service.users().messages().trash(userId="me", id=message_id).execute()
        return f"Message {message_id} moved to trash."
    except Exception as e:
        return f"Error trashing message: {e}"


@tool
def gmail_list_labels() -> str:
    """List all Gmail labels (system + user-defined) with their IDs."""
    try:
        service = _gmail_service()
    except Exception as e:
        return f"Error connecting to Gmail: {e}"
    try:
        labels = service.users().labels().list(userId="me").execute().get("labels", [])
        if not labels:
            return "No labels found."
        system = [l for l in labels if l.get("type") == "system"]
        user = [l for l in labels if l.get("type") != "system"]
        lines = ["System labels:"]
        for lbl in system:
            lines.append(f"  {lbl['id']:<20} ({lbl['name']})")
        if user:
            lines.append("\nUser labels:")
            for lbl in user:
                lines.append(f"  {lbl['id']:<30} ({lbl['name']})")
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing labels: {e}"


# ── Calendar ─────────────────────────────────────────────────────────────────


@tool
def calendar_list_events(
    days: Annotated[int, "Number of days ahead to look for events (1-30)"] = 7,
    calendar_id: Annotated[str, "Calendar ID (leave empty for primary)"] = "primary",
) -> str:
    """
    List upcoming calendar events from Google Calendar.
    """
    try:
        service = _calendar_service()
    except Exception as e:
        return f"Error connecting to Calendar: {e}"

    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    time_min = now.isoformat()
    time_max = (now + timedelta(days=max(1, min(30, days)))).isoformat()

    try:
        result = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=50,
            )
            .execute()
        )
        events = result.get("items", [])
    except Exception as e:
        return f"Error fetching calendar events: {e}"

    if not events:
        return f"No events in the next {days} days."

    lines = [f"Upcoming events (next {days} days):\n"]
    for event in events:
        start = event.get("start", {})
        start_dt = start.get("dateTime") or start.get("date", "?")
        summary = event.get("summary", "(no title)")
        location = event.get("location", "")
        eid = event.get("id", "")
        lines.append(
            f"• {start_dt} — {summary}" + (f" @ {location}" if location else "") + f"\n  ID: {eid}"
        )

    return "\n".join(lines)


@tool
def calendar_create_event(
    title: Annotated[str, "Event title"],
    start: Annotated[str, "Start datetime in ISO format (e.g., '2026-04-01T09:00:00')"],
    end: Annotated[str, "End datetime in ISO format (e.g., '2026-04-01T10:00:00')"],
    description: Annotated[str, "Event description (optional)"] = "",
    location: Annotated[str, "Event location (optional)"] = "",
    timezone: Annotated[str, "Timezone (e.g., 'America/New_York', default 'UTC')"] = "UTC",
    calendar_id: Annotated[str, "Calendar ID (leave empty for primary)"] = "primary",
) -> str:
    """
    Create a new event in Google Calendar.
    """
    try:
        service = _calendar_service()
    except Exception as e:
        return f"Error connecting to Calendar: {e}"

    event: dict[str, Any] = {
        "summary": title,
        "start": {"dateTime": start, "timeZone": timezone},
        "end": {"dateTime": end, "timeZone": timezone},
    }
    if description:
        event["description"] = description
    if location:
        event["location"] = location

    try:
        result = service.events().insert(calendarId=calendar_id, body=event).execute()
        link = result.get("htmlLink", "")
        return f"Event created: '{title}'\nID: {result['id']}\nLink: {link}"
    except Exception as e:
        return f"Error creating calendar event: {e}"


@tool
def calendar_delete_event(
    event_id: Annotated[str, "Event ID to delete"],
    calendar_id: Annotated[str, "Calendar ID (leave empty for primary)"] = "primary",
) -> str:
    """
    Delete a calendar event by its ID.
    """
    try:
        service = _calendar_service()
    except Exception as e:
        return f"Error connecting to Calendar: {e}"

    try:
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        return f"Event {event_id} deleted successfully."
    except Exception as e:
        return f"Error deleting event: {e}"


@tool
def calendar_get_event(
    event_id: Annotated[str, "Event ID"],
    calendar_id: Annotated[str, "Calendar ID (leave empty for primary)"] = "primary",
) -> str:
    """Get full details of a single Google Calendar event by ID."""
    try:
        service = _calendar_service()
    except Exception as e:
        return f"Error connecting to Calendar: {e}"
    try:
        event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        attendees = ", ".join(a.get("email", "") for a in event.get("attendees", []) or [])
        start = event.get("start", {})
        end = event.get("end", {})
        return (
            f"Title: {event.get('summary', '(no title)')}\n"
            f"ID: {event.get('id')}\n"
            f"Start: {start.get('dateTime') or start.get('date', '?')}\n"
            f"End: {end.get('dateTime') or end.get('date', '?')}\n"
            f"Status: {event.get('status', '?')}\n"
            f"Location: {event.get('location', '(none)')}\n"
            f"Attendees: {attendees or '(none)'}\n"
            f"Description: {event.get('description', '(none)')}\n"
            f"Link: {event.get('htmlLink', '')}"
        )
    except Exception as e:
        return f"Error fetching event {event_id}: {e}"


@tool
def calendar_update_event(
    event_id: Annotated[str, "Event ID to update"],
    title: Annotated[str, "New title (leave empty to keep)"] = "",
    start: Annotated[str, "New start ISO datetime (leave empty to keep)"] = "",
    end: Annotated[str, "New end ISO datetime (leave empty to keep)"] = "",
    description: Annotated[str, "New description (leave empty to keep)"] = "",
    location: Annotated[str, "New location (leave empty to keep)"] = "",
    timezone: Annotated[str, "Timezone for new start/end (default UTC)"] = "UTC",
    calendar_id: Annotated[str, "Calendar ID (leave empty for primary)"] = "primary",
) -> str:
    """Update fields on an existing Google Calendar event. Only non-empty fields are changed."""
    try:
        service = _calendar_service()
    except Exception as e:
        return f"Error connecting to Calendar: {e}"
    try:
        event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        if title:
            event["summary"] = title
        if description:
            event["description"] = description
        if location:
            event["location"] = location
        if start:
            event["start"] = {"dateTime": start, "timeZone": timezone}
        if end:
            event["end"] = {"dateTime": end, "timeZone": timezone}
        result = (
            service.events().update(calendarId=calendar_id, eventId=event_id, body=event).execute()
        )
        return f"Event updated: '{result.get('summary')}'\nLink: {result.get('htmlLink', '')}"
    except Exception as e:
        return f"Error updating event: {e}"


@tool
def calendar_search_events(
    query: Annotated[str, "Free-text search across event title, description, attendees"],
    days_ahead: Annotated[int, "Search window in days (1-365)"] = 90,
    calendar_id: Annotated[str, "Calendar ID (leave empty for primary)"] = "primary",
) -> str:
    """Search Google Calendar for events matching a query string."""
    try:
        service = _calendar_service()
    except Exception as e:
        return f"Error connecting to Calendar: {e}"
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    window = max(1, min(365, days_ahead))
    try:
        result = (
            service.events()
            .list(
                calendarId=calendar_id,
                q=query,
                timeMin=now.isoformat(),
                timeMax=(now + timedelta(days=window)).isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=50,
            )
            .execute()
        )
        events = result.get("items", [])
    except Exception as e:
        return f"Error searching calendar: {e}"
    if not events:
        return f"No events match '{query}' in next {window} days."
    lines = [f"Search results for '{query}' (next {window} days):"]
    for ev in events:
        start = ev.get("start", {})
        when = start.get("dateTime") or start.get("date", "?")
        lines.append(f"• {when} — {ev.get('summary', '(no title)')}  [id={ev.get('id', '')}]")
    return "\n".join(lines)


@tool
def calendar_quick_add(
    text: Annotated[str, "Natural language description (e.g. 'Lunch with Sam tomorrow at noon')"],
    calendar_id: Annotated[str, "Calendar ID (leave empty for primary)"] = "primary",
) -> str:
    """Create a Google Calendar event from a natural-language phrase. Uses Google's parser."""
    try:
        service = _calendar_service()
    except Exception as e:
        return f"Error connecting to Calendar: {e}"
    try:
        result = service.events().quickAdd(calendarId=calendar_id, text=text).execute()
        return (
            f"Event created via quick-add: '{result.get('summary')}'\n"
            f"ID: {result.get('id')}\n"
            f"Link: {result.get('htmlLink', '')}"
        )
    except Exception as e:
        return f"Error in quick-add: {e}"


@tool
def calendar_list_calendars() -> str:
    """List all Google Calendars the authorized user can access, with IDs."""
    try:
        service = _calendar_service()
    except Exception as e:
        return f"Error connecting to Calendar: {e}"
    try:
        items = service.calendarList().list().execute().get("items", [])
        if not items:
            return "No calendars found."
        lines = ["Calendars:"]
        for cal in items:
            primary = " (primary)" if cal.get("primary") else ""
            lines.append(
                f"  {cal.get('summary', '?')}{primary}\n"
                f"    id: {cal.get('id')}  access: {cal.get('accessRole', '?')}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing calendars: {e}"


@tool
def calendar_freebusy(
    start: Annotated[str, "Start ISO datetime to check (e.g. '2026-04-01T09:00:00Z')"],
    end: Annotated[str, "End ISO datetime to check"],
    calendar_ids: Annotated[str, "Comma-separated calendar IDs (default 'primary')"] = "primary",
) -> str:
    """Check free/busy windows across one or more Google Calendars."""
    try:
        service = _calendar_service()
    except Exception as e:
        return f"Error connecting to Calendar: {e}"
    ids = [s.strip() for s in calendar_ids.split(",") if s.strip()]
    try:
        result = (
            service.freebusy()
            .query(body={"timeMin": start, "timeMax": end, "items": [{"id": i} for i in ids]})
            .execute()
        )
        cals = result.get("calendars", {})
        if not cals:
            return "No data returned."
        lines = [f"Free/busy {start} → {end}:"]
        for cal_id, info in cals.items():
            busy = info.get("busy", [])
            if not busy:
                lines.append(f"  {cal_id}: FREE")
            else:
                lines.append(f"  {cal_id}: BUSY")
                for slot in busy:
                    lines.append(f"    {slot.get('start')} → {slot.get('end')}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error in freebusy query: {e}"


# ── Drive ─────────────────────────────────────────────────────────────────────


@tool
def drive_list(
    folder_id: Annotated[str, "Folder ID to list (leave empty for root/My Drive)"] = "",
    max_results: Annotated[int, "Maximum number of files to list (1-100)"] = 20,
    query: Annotated[str, "Drive search query (e.g., 'mimeType=\\'application/pdf\\'')"] = "",
) -> str:
    """
    List files in Google Drive. Optionally filter by folder or search query.
    """
    try:
        service = _drive_service()
    except Exception as e:
        return f"Error connecting to Drive: {e}"

    q_parts = ["trashed = false"]
    if folder_id:
        q_parts.append(f"'{folder_id}' in parents")
    if query:
        q_parts.append(query)

    try:
        result = (
            service.files()
            .list(
                q=" and ".join(q_parts),
                pageSize=max(1, min(100, max_results)),
                fields="files(id, name, mimeType, size, modifiedTime)",
            )
            .execute()
        )
        files = result.get("files", [])
    except Exception as e:
        return f"Error listing Drive files: {e}"

    if not files:
        return "No files found."

    lines = [f"Google Drive files ({len(files)}):\n"]
    for f in files:
        size = f.get("size", "?")
        size_str = f"{int(size) // 1024} KB" if str(size).isdigit() else "folder"
        lines.append(
            f"• {f['name']}\n"
            f"  ID: {f['id']}  |  Type: {f['mimeType'].split('.')[-1]}  |  Size: {size_str}"
            f"  |  Modified: {f.get('modifiedTime', '?')[:10]}"
        )

    return "\n".join(lines)


@tool
def drive_download(
    file_id: Annotated[str, "Google Drive file ID to download"],
    filename: Annotated[str, "Filename to save as (optional, uses Drive filename if empty)"] = "",
) -> str:
    """
    Download a file from Google Drive to workspace/downloads/.
    """
    try:
        service = _drive_service()
    except Exception as e:
        return f"Error connecting to Drive: {e}"

    from olano.config.workspace import get_workspace
    from olano.security.request_context import get_request_context

    # Resolve the *running agent's* workspace from the request context; a bare
    # get_workspace() would save into the legacy/global workspace instead of
    # ~/.olano/agents/<name>/workspace/.
    _ws_root = get_request_context().metadata.get("workspace_root")
    downloads = get_workspace(str(_ws_root) if _ws_root else None).root / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)

    try:
        # Get file metadata
        meta = service.files().get(fileId=file_id, fields="name,mimeType").execute()
        fname = filename or meta.get("name", f"drive_{file_id[:8]}")
        out_path = downloads / fname

        # Export Google Workspace files as PDF, otherwise use regular download
        mime = meta.get("mimeType", "")
        if "google-apps" in mime:
            # Export as PDF
            request = service.files().export_media(fileId=file_id, mimeType="application/pdf")
            out_path = downloads / (fname + ".pdf")
        else:
            from googleapiclient.http import MediaIoBaseDownload

            request = service.files().get_media(fileId=file_id)

        import io

        from googleapiclient.http import MediaIoBaseDownload

        fh = io.FileIO(str(out_path), "wb")
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        fh.close()

        size_kb = out_path.stat().st_size / 1024
        return f"Downloaded '{meta['name']}' → {out_path} ({size_kb:.1f} KB)"
    except Exception as e:
        return f"Error downloading from Drive: {e}"


@tool
def drive_upload(
    path: Annotated[str, "Local file path to upload"],
    folder_id: Annotated[str, "Google Drive folder ID (leave empty for root)"] = "",
) -> str:
    """
    Upload a local file to Google Drive.
    """
    try:
        service = _drive_service()
    except Exception as e:
        return f"Error connecting to Drive: {e}"

    import mimetypes

    from googleapiclient.http import MediaFileUpload

    p = Path(path)
    if not p.exists():
        return f"Error: file not found: {path}"

    mime_type = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
    file_metadata: dict[str, Any] = {"name": p.name}
    if folder_id:
        file_metadata["parents"] = [folder_id]

    try:
        media = MediaFileUpload(str(p), mimetype=mime_type, resumable=True)
        result = (
            service.files()
            .create(body=file_metadata, media_body=media, fields="id, name, webViewLink")
            .execute()
        )
        return (
            f"Uploaded '{p.name}' to Google Drive\n"
            f"ID: {result['id']}\n"
            f"Link: {result.get('webViewLink', '(no link)')}"
        )
    except Exception as e:
        return f"Error uploading to Drive: {e}"


@tool
def drive_search(
    query: Annotated[str, "Free-text search (matches name and full text content)"],
    max_results: Annotated[int, "Maximum results (1-50)"] = 15,
) -> str:
    """Search Google Drive for files matching a query in name or content."""
    try:
        service = _drive_service()
    except Exception as e:
        return f"Error connecting to Drive: {e}"
    safe = query.replace("'", "\\'")
    q = f"(name contains '{safe}' or fullText contains '{safe}') and trashed = false"
    try:
        result = (
            service.files()
            .list(
                q=q,
                pageSize=max(1, min(50, max_results)),
                fields="files(id, name, mimeType, modifiedTime, webViewLink)",
            )
            .execute()
        )
        files = result.get("files", [])
    except Exception as e:
        return f"Error searching Drive: {e}"
    if not files:
        return f"No files match '{query}'."
    lines = [f"Drive search '{query}' ({len(files)} matches):"]
    for f in files:
        lines.append(
            f"• {f['name']}\n  ID: {f['id']}  Type: {f['mimeType']}  "
            f"Modified: {f.get('modifiedTime', '?')[:10]}\n  Link: {f.get('webViewLink', '')}"
        )
    return "\n".join(lines)


@tool
def drive_create_folder(
    name: Annotated[str, "Folder name"],
    parent_id: Annotated[str, "Parent folder ID (leave empty for root/My Drive)"] = "",
) -> str:
    """Create a new folder in Google Drive."""
    try:
        service = _drive_service()
    except Exception as e:
        return f"Error connecting to Drive: {e}"
    metadata: dict[str, Any] = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_id:
        metadata["parents"] = [parent_id]
    try:
        result = service.files().create(body=metadata, fields="id, name, webViewLink").execute()
        return (
            f"Folder created: '{result['name']}'\n"
            f"ID: {result['id']}\n"
            f"Link: {result.get('webViewLink', '')}"
        )
    except Exception as e:
        return f"Error creating folder: {e}"


@tool
def drive_share(
    file_id: Annotated[str, "Google Drive file or folder ID"],
    email: Annotated[str, "Email to share with, or empty for anyone-with-link"] = "",
    role: Annotated[str, "Permission role: 'reader', 'commenter', 'writer'"] = "reader",
) -> str:
    """Share a Drive file/folder. If email is empty, creates an anyone-with-link permission."""
    try:
        service = _drive_service()
    except Exception as e:
        return f"Error connecting to Drive: {e}"
    if role not in ("reader", "commenter", "writer", "owner"):
        return f"Invalid role '{role}'. Use reader, commenter, or writer."
    perm: dict[str, Any] = {"role": role}
    if email:
        perm["type"] = "user"
        perm["emailAddress"] = email
    else:
        perm["type"] = "anyone"
    try:
        service.permissions().create(
            fileId=file_id, body=perm, sendNotificationEmail=False
        ).execute()
        meta = service.files().get(fileId=file_id, fields="webViewLink, name").execute()
        target = email or "anyone with the link"
        return (
            f"Shared '{meta.get('name')}' with {target} as {role}.\n"
            f"Link: {meta.get('webViewLink', '')}"
        )
    except Exception as e:
        return f"Error sharing file: {e}"


@tool
def drive_delete(
    file_id: Annotated[str, "Google Drive file or folder ID"],
    permanent: Annotated[bool, "If True, permanently delete; otherwise move to trash"] = False,
) -> str:
    """Delete a Drive file. By default it's moved to Trash; set permanent=True to fully delete."""
    try:
        service = _drive_service()
    except Exception as e:
        return f"Error connecting to Drive: {e}"
    try:
        if permanent:
            service.files().delete(fileId=file_id).execute()
            return f"Permanently deleted file {file_id}."
        service.files().update(fileId=file_id, body={"trashed": True}).execute()
        return f"File {file_id} moved to trash."
    except Exception as e:
        return f"Error deleting file: {e}"


@tool
def drive_get_link(
    file_id: Annotated[str, "Google Drive file or folder ID"],
) -> str:
    """Get a shareable webViewLink for a Drive file."""
    try:
        service = _drive_service()
    except Exception as e:
        return f"Error connecting to Drive: {e}"
    try:
        meta = (
            service.files()
            .get(fileId=file_id, fields="name, mimeType, webViewLink, webContentLink")
            .execute()
        )
        return (
            f"{meta.get('name')} ({meta.get('mimeType')})\n"
            f"View: {meta.get('webViewLink', '(none)')}\n"
            f"Download: {meta.get('webContentLink', '(none)')}"
        )
    except Exception as e:
        return f"Error fetching link: {e}"


# ── Docs ──────────────────────────────────────────────────────────────────────


def _docs_extract_text(doc: dict) -> str:
    """Flatten a Docs document body into plain text."""
    out: list[str] = []
    for element in doc.get("body", {}).get("content", []):
        para = element.get("paragraph")
        if not para:
            continue
        for run in para.get("elements", []):
            text_run = run.get("textRun")
            if text_run:
                out.append(text_run.get("content", ""))
    return "".join(out)


@tool
def docs_create(
    title: Annotated[str, "Document title"],
    initial_text: Annotated[str, "Optional initial body text"] = "",
) -> str:
    """Create a new Google Doc with an optional initial body."""
    try:
        service = _docs_service()
    except Exception as e:
        return f"Error connecting to Docs: {e}"
    try:
        doc = service.documents().create(body={"title": title}).execute()
        doc_id = doc["documentId"]
        if initial_text:
            service.documents().batchUpdate(
                documentId=doc_id,
                body={
                    "requests": [{"insertText": {"location": {"index": 1}, "text": initial_text}}]
                },
            ).execute()
        return (
            f"Doc created: '{title}'\n"
            f"ID: {doc_id}\n"
            f"Link: https://docs.google.com/document/d/{doc_id}/edit"
        )
    except Exception as e:
        return f"Error creating doc: {e}"


@tool
def docs_read(
    document_id: Annotated[str, "Google Docs document ID"],
) -> str:
    """Read the plain-text contents of a Google Doc."""
    try:
        service = _docs_service()
    except Exception as e:
        return f"Error connecting to Docs: {e}"
    try:
        doc = service.documents().get(documentId=document_id).execute()
        text = _docs_extract_text(doc)
        title = doc.get("title", "(untitled)")
        return f"# {title}\n(id: {document_id})\n\n{text or '(empty document)'}"
    except Exception as e:
        return f"Error reading doc: {e}"


@tool
def docs_append_text(
    document_id: Annotated[str, "Google Docs document ID"],
    text: Annotated[str, "Text to append to the end of the document"],
) -> str:
    """Append plain text to the end of a Google Doc."""
    try:
        service = _docs_service()
    except Exception as e:
        return f"Error connecting to Docs: {e}"
    try:
        doc = service.documents().get(documentId=document_id).execute()
        end_index = doc.get("body", {}).get("content", [])[-1].get("endIndex", 1) - 1
        service.documents().batchUpdate(
            documentId=document_id,
            body={"requests": [{"insertText": {"location": {"index": end_index}, "text": text}}]},
        ).execute()
        return f"Appended {len(text)} chars to doc {document_id}."
    except Exception as e:
        return f"Error appending to doc: {e}"


@tool
def docs_insert_text(
    document_id: Annotated[str, "Google Docs document ID"],
    text: Annotated[str, "Text to insert"],
    index: Annotated[int, "Insertion index (1 = start of document body)"] = 1,
) -> str:
    """Insert text at a specific character index in a Google Doc."""
    try:
        service = _docs_service()
    except Exception as e:
        return f"Error connecting to Docs: {e}"
    try:
        service.documents().batchUpdate(
            documentId=document_id,
            body={
                "requests": [{"insertText": {"location": {"index": max(1, index)}, "text": text}}]
            },
        ).execute()
        return f"Inserted {len(text)} chars at index {index} in doc {document_id}."
    except Exception as e:
        return f"Error inserting into doc: {e}"


@tool
def docs_replace_text(
    document_id: Annotated[str, "Google Docs document ID"],
    find: Annotated[str, "Text to find"],
    replace: Annotated[str, "Replacement text"],
    match_case: Annotated[bool, "Case-sensitive match (default True)"] = True,
) -> str:
    """Find and replace all occurrences of a string in a Google Doc."""
    try:
        service = _docs_service()
    except Exception as e:
        return f"Error connecting to Docs: {e}"
    try:
        result = (
            service.documents()
            .batchUpdate(
                documentId=document_id,
                body={
                    "requests": [
                        {
                            "replaceAllText": {
                                "containsText": {"text": find, "matchCase": match_case},
                                "replaceText": replace,
                            }
                        }
                    ]
                },
            )
            .execute()
        )
        replies = result.get("replies", [{}])
        n = replies[0].get("replaceAllText", {}).get("occurrencesChanged", 0)
        return f"Replaced {n} occurrence(s) of '{find}' with '{replace}'."
    except Exception as e:
        return f"Error replacing text: {e}"


# ── Sheets ────────────────────────────────────────────────────────────────────
# A1 notation is the agent-facing address format ("Tab!A1:C10"); the Sheets
# batchUpdate API instead wants a numeric ``sheetId`` + a 0-based half-open
# ``GridRange``. The helpers below translate between the two so every tool can
# take plain A1 from the model.

_SHEETS_MIME = "application/vnd.google-apps.spreadsheet"
_A1_PART_RE = re.compile(r"^([A-Za-z]*)(\d*)$")
_MAX_CELL_WIDTH = 40


def _col_letters(index: int) -> str:
    """1 → ``A``, 26 → ``Z``, 27 → ``AA``."""
    out = ""
    while index > 0:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out or "A"


def _col_index(letters: str) -> int:
    """``A`` → 1, ``AA`` → 27. Returns 0 for an empty string."""
    idx = 0
    for ch in letters.upper():
        idx = idx * 26 + (ord(ch) - 64)
    return idx


def _quote_sheet(title: str) -> str:
    """Quote a tab name for A1 notation when it needs it (spaces, ``Q1``, …).

    A tab called ``Q1`` has to be quoted or it reads as the cell reference Q1.
    Columns top out at ``XFD``, so only 1-3 leading letters can collide — a name
    like ``Sheet1`` is unambiguous and stays bare.
    """
    if not title:
        return ""
    plain = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", title) and not re.fullmatch(
        r"[A-Za-z]{1,3}\d+", title
    )
    return title if plain else "'" + title.replace("'", "''") + "'"


def _split_a1(range_a1: str) -> tuple[str, str, str]:
    """Split ``[Tab!]start[:end]`` into ``(tab_title, start, end)``.

    ``rpartition`` on ``!`` keeps quoted tab names containing ``!`` intact.
    """
    text = range_a1.strip()
    sheet = ""
    if "!" in text:
        sheet, _, text = text.rpartition("!")
        sheet = sheet.strip()
        if len(sheet) >= 2 and sheet.startswith("'") and sheet.endswith("'"):
            sheet = sheet[1:-1].replace("''", "'")
    start, _, end = text.partition(":")
    return sheet, start.strip(), end.strip()


def _a1_origin(range_a1: str) -> tuple[int, int]:
    """Return the 1-based ``(row, col)`` a resolved A1 range starts at."""
    _, start, _ = _split_a1(range_a1)
    match = _A1_PART_RE.match(start)
    if not match:
        return 1, 1
    letters, digits = match.groups()
    return (int(digits) if digits else 1), (_col_index(letters) or 1)


def _a1_to_grid_range(cells: str, sheet_id: int) -> dict[str, Any]:
    """Convert the cell part of an A1 range into a ``GridRange``.

    An omitted bound stays omitted, so ``A:C`` means "those columns, every row"
    and an empty string means the whole tab.
    """
    grid: dict[str, Any] = {"sheetId": sheet_id}
    start, _, end = cells.partition(":")
    start, end = start.strip(), end.strip()
    if not start:
        return grid

    ms = _A1_PART_RE.match(start)
    me = _A1_PART_RE.match(end) if end else ms
    if not ms or not me:
        raise ValueError(f"Could not parse A1 range: {cells!r}")

    start_col, start_row = _col_index(ms.group(1)), ms.group(2)
    end_col, end_row = _col_index(me.group(1)), me.group(2)
    if start_col:
        grid["startColumnIndex"] = start_col - 1
    if end_col:
        grid["endColumnIndex"] = end_col
    if start_row:
        grid["startRowIndex"] = int(start_row) - 1
    if end_row:
        grid["endRowIndex"] = int(end_row)
    return grid


def _sheet_tabs(service: Any, spreadsheet_id: str) -> list[dict]:
    """Return every tab's ``properties`` block, in spreadsheet order."""
    meta = (
        service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets.properties")
        .execute()
    )
    tabs = [s.get("properties", {}) for s in meta.get("sheets", [])]
    if not tabs:
        raise ValueError(f"Spreadsheet {spreadsheet_id} has no sheets.")
    return tabs


def _resolve_tab(service: Any, spreadsheet_id: str, sheet_name: str) -> dict:
    """Return the ``properties`` of a tab by name; empty name → the first tab."""
    tabs = _sheet_tabs(service, spreadsheet_id)
    if not sheet_name:
        return tabs[0]
    for props in tabs:
        if props.get("title") == sheet_name:
            return props
    available = ", ".join(repr(p.get("title")) for p in tabs)
    raise ValueError(f"No sheet named {sheet_name!r}. Available: {available}")


def _resolve_a1_target(service: Any, spreadsheet_id: str, range_a1: str) -> tuple[dict, dict]:
    """Resolve an A1 range to ``(tab_properties, GridRange)``.

    Handles the three shapes models actually pass: ``"Tab!A1:C10"``, a bare
    ``"A1:C10"`` (first tab), and a bare tab name (the whole tab — which would
    otherwise parse as the cell reference ``SHEET1``).
    """
    tabs = _sheet_tabs(service, spreadsheet_id)
    sheet_ref, _, _ = _split_a1(range_a1)
    cells = range_a1.strip()

    if sheet_ref:
        cells = cells.rpartition("!")[2].strip()
        for props in tabs:
            if props.get("title") == sheet_ref:
                return props, _a1_to_grid_range(cells, int(props["sheetId"]))
        available = ", ".join(repr(p.get("title")) for p in tabs)
        raise ValueError(f"No sheet named {sheet_ref!r}. Available: {available}")

    bare = (
        cells[1:-1].replace("''", "'") if cells.startswith("'") and cells.endswith("'") else cells
    )
    for props in tabs:
        if props.get("title") == bare:
            return props, {"sheetId": int(props["sheetId"])}
    return tabs[0], _a1_to_grid_range(cells, int(tabs[0]["sheetId"]))


def _parse_values(values_json: str) -> list[list[Any]]:
    """Parse a JSON grid argument into the Sheets ``values`` shape.

    Accepts a 2-D array (``[["Name", "Score"], ["Ada", 99]]``), a flat array
    (one row), or a bare scalar (one cell). Numbers/booleans survive as their
    JSON types so Sheets stores them as numbers rather than text.
    """
    text = (values_json or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"values_json is not valid JSON: {e}") from e

    if not isinstance(data, list):
        return [[data]]
    if not data:
        return []
    if all(isinstance(row, list) for row in data):
        return [list(row) for row in data]
    if any(isinstance(row, list) for row in data):
        raise ValueError("values_json must be one flat row or a list of rows, not a mix of both.")
    return [list(data)]


def _parse_color(value: str) -> dict[str, float]:
    """Parse ``#RRGGBB`` / ``RRGGBB`` / ``#RGB`` into a Sheets color dict."""
    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", text):
        raise ValueError(f"Invalid color {value!r} — use hex like '#4285F4'.")
    r, g, b = (int(text[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return {"red": r, "green": g, "blue": b}


def _sheet_url(spreadsheet_id: str, sheet_id: int | None = None) -> str:
    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
    return f"{url}#gid={sheet_id}" if sheet_id is not None else url


def _render_grid(values: list[list[Any]], origin: tuple[int, int], max_rows: int) -> str:
    """Render a values grid as an aligned table with real row/column labels.

    The labels are the spreadsheet's own coordinates (derived from the range the
    API echoed back), so the model can address any cell it sees without having
    to count offsets.
    """
    if not values:
        return "(empty range)"

    first_row, first_col = origin
    shown = values[: max(1, max_rows)]
    width = max((len(row) for row in shown), default=0)
    if not width:
        return "(empty range)"

    def cell(value: Any) -> str:
        text = "" if value is None else str(value)
        text = text.replace("\n", " ⏎ ")
        return text if len(text) <= _MAX_CELL_WIDTH else text[: _MAX_CELL_WIDTH - 1] + "…"

    header = [_col_letters(first_col + i) for i in range(width)]
    body = [[cell(row[i]) if i < len(row) else "" for i in range(width)] for row in shown]
    widths = [max(len(header[i]), *(len(row[i]) for row in body)) for i in range(width)]
    gutter = len(str(first_row + len(shown) - 1))

    lines = [" " * gutter + " | " + " | ".join(h.ljust(widths[i]) for i, h in enumerate(header))]
    lines.append("-" * len(lines[0]))
    for offset, row in enumerate(body):
        label = str(first_row + offset).rjust(gutter)
        lines.append(label + " | " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(row)))

    out = [line.rstrip() for line in lines]
    if len(values) > len(shown):
        out.append(f"… {len(values) - len(shown)} more row(s) not shown — raise max_rows.")
    return "\n".join(out)


@tool
def sheets_list(
    query: Annotated[str, "Filter by name substring (leave empty to list all)"] = "",
    max_results: Annotated[int, "Maximum number of spreadsheets to list (1-100)"] = 20,
) -> str:
    """List Google Sheets spreadsheets in Drive, most recently modified first.

    Use this to find a spreadsheet ID by name before calling the other sheets_* tools.
    """
    try:
        service = _drive_service()
    except Exception as e:
        return f"Error connecting to Drive: {e}"

    q_parts = [f"mimeType = '{_SHEETS_MIME}'", "trashed = false"]
    if query:
        q_parts.append("name contains '{}'".format(query.replace("\\", "\\\\").replace("'", "\\'")))

    try:
        result = (
            service.files()
            .list(
                q=" and ".join(q_parts),
                pageSize=max(1, min(100, max_results)),
                orderBy="modifiedTime desc",
                fields="files(id, name, modifiedTime, webViewLink)",
            )
            .execute()
        )
        files = result.get("files", [])
    except Exception as e:
        return f"Error listing spreadsheets: {e}"

    if not files:
        return "No spreadsheets found."

    lines = [f"Google Sheets spreadsheets ({len(files)}):\n"]
    for f in files:
        lines.append(
            f"• {f['name']}\n"
            f"  ID: {f['id']}  |  Modified: {f.get('modifiedTime', '?')[:10]}\n"
            f"  Link: {f.get('webViewLink', _sheet_url(f['id']))}"
        )
    return "\n".join(lines)


@tool
def sheets_create(
    title: Annotated[str, "Spreadsheet title"],
    sheet_names: Annotated[str, "Comma-separated tab names (default: one 'Sheet1')"] = "",
    values_json: Annotated[
        str,
        'Optional JSON rows for the first tab, e.g. \'[["Name","Score"],["Ada",99]]\'',
    ] = "",
) -> str:
    """Create a new Google Sheets spreadsheet, optionally with named tabs and initial rows."""
    try:
        rows = _parse_values(values_json)
    except ValueError as e:
        return f"Error creating spreadsheet: {e}"

    try:
        service = _sheets_service()
    except Exception as e:
        return f"Error connecting to Sheets: {e}"

    body: dict[str, Any] = {"properties": {"title": title}}
    tabs = [n.strip() for n in sheet_names.split(",") if n.strip()]
    if tabs:
        body["sheets"] = [{"properties": {"title": name}} for name in tabs]

    try:
        sheet = service.spreadsheets().create(body=body).execute()
        spreadsheet_id = sheet["spreadsheetId"]
        first_tab = sheet["sheets"][0]["properties"]["title"]
        if rows:
            service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"{_quote_sheet(first_tab)}!A1",
                valueInputOption="USER_ENTERED",
                body={"values": rows},
            ).execute()
        written = f"\nWrote {len(rows)} row(s) to '{first_tab}'." if rows else ""
        return (
            f"Spreadsheet created: '{title}'\n"
            f"ID: {spreadsheet_id}\n"
            f"Tabs: {', '.join(s['properties']['title'] for s in sheet['sheets'])}\n"
            f"Link: {_sheet_url(spreadsheet_id)}{written}"
        )
    except Exception as e:
        return f"Error creating spreadsheet: {e}"


@tool
def sheets_info(
    spreadsheet_id: Annotated[str, "Google Sheets spreadsheet ID"],
) -> str:
    """Get spreadsheet metadata: title, link, and every tab's name, ID, and size."""
    try:
        service = _sheets_service()
    except Exception as e:
        return f"Error connecting to Sheets: {e}"

    try:
        meta = (
            service.spreadsheets()
            .get(
                spreadsheetId=spreadsheet_id,
                fields="properties(title,locale,timeZone),sheets.properties",
            )
            .execute()
        )
    except Exception as e:
        return f"Error reading spreadsheet: {e}"

    props = meta.get("properties", {})
    lines = [
        f"# {props.get('title', '(untitled)')}",
        f"(id: {spreadsheet_id})",
        f"Locale: {props.get('locale', '?')}  |  Time zone: {props.get('timeZone', '?')}",
        f"Link: {_sheet_url(spreadsheet_id)}",
        "",
        "Tabs:",
    ]
    for sheet in meta.get("sheets", []):
        tab = sheet.get("properties", {})
        grid = tab.get("gridProperties", {})
        frozen = []
        if grid.get("frozenRowCount"):
            frozen.append(f"{grid['frozenRowCount']} frozen row(s)")
        if grid.get("frozenColumnCount"):
            frozen.append(f"{grid['frozenColumnCount']} frozen col(s)")
        lines.append(
            f"• {tab.get('title')}\n"
            f"  sheetId: {tab.get('sheetId')}  |  "
            f"{grid.get('rowCount', '?')} rows × {grid.get('columnCount', '?')} cols"
            + (f"  |  {', '.join(frozen)}" if frozen else "")
            + f"\n  Link: {_sheet_url(spreadsheet_id, tab.get('sheetId'))}"
        )
    return "\n".join(lines)


@tool
def sheets_read(
    spreadsheet_id: Annotated[str, "Google Sheets spreadsheet ID"],
    range_a1: Annotated[
        str, "A1 range: 'Tab!A1:D50', 'Tab' (whole tab), or 'A:C'. Empty = the first tab"
    ] = "",
    value_render: Annotated[
        str, "FORMATTED_VALUE (as displayed), UNFORMATTED_VALUE (raw), or FORMULA"
    ] = "FORMATTED_VALUE",
    max_rows: Annotated[int, "Maximum rows to render (1-500)"] = 200,
) -> str:
    """Read cell values from a Google Sheet as a labelled table.

    Rows and columns are labelled with their real spreadsheet coordinates, so any cell
    shown can be addressed directly in a follow-up sheets_write / sheets_format_cells call.
    """
    render = value_render.upper()
    if render not in ("FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA"):
        return (
            f"Invalid value_render '{value_render}'. "
            "Use FORMATTED_VALUE, UNFORMATTED_VALUE, or FORMULA."
        )

    try:
        service = _sheets_service()
    except Exception as e:
        return f"Error connecting to Sheets: {e}"

    try:
        target = range_a1.strip() or _quote_sheet(
            _resolve_tab(service, spreadsheet_id, "")["title"]
        )
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=target, valueRenderOption=render)
            .execute()
        )
    except Exception as e:
        return f"Error reading sheet: {e}"

    resolved = result.get("range", target)
    values = result.get("values", [])
    grid = _render_grid(values, _a1_origin(resolved), max(1, min(500, max_rows)))
    return f"{resolved} — {len(values)} row(s)\n\n{grid}"


@tool
def sheets_batch_read(
    spreadsheet_id: Annotated[str, "Google Sheets spreadsheet ID"],
    ranges: Annotated[str, 'Comma-separated A1 ranges, e.g. "Data!A1:C20, Totals!A1:B5"'],
    value_render: Annotated[
        str, "FORMATTED_VALUE (as displayed), UNFORMATTED_VALUE (raw), or FORMULA"
    ] = "FORMATTED_VALUE",
    max_rows: Annotated[int, "Maximum rows to render per range (1-500)"] = 100,
) -> str:
    """Read several ranges from one spreadsheet in a single call.

    Cheaper than repeated sheets_read calls when you need scattered ranges or tabs.
    """
    wanted = [r.strip() for r in ranges.split(",") if r.strip()]
    if not wanted:
        return 'No ranges given. Pass a comma-separated list like "Data!A1:C20, Totals!A1:B5".'

    render = value_render.upper()
    if render not in ("FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA"):
        return (
            f"Invalid value_render '{value_render}'. "
            "Use FORMATTED_VALUE, UNFORMATTED_VALUE, or FORMULA."
        )

    try:
        service = _sheets_service()
    except Exception as e:
        return f"Error connecting to Sheets: {e}"

    try:
        result = (
            service.spreadsheets()
            .values()
            .batchGet(spreadsheetId=spreadsheet_id, ranges=wanted, valueRenderOption=render)
            .execute()
        )
    except Exception as e:
        return f"Error reading ranges: {e}"

    cap = max(1, min(500, max_rows))
    blocks = []
    for value_range in result.get("valueRanges", []):
        resolved = value_range.get("range", "?")
        values = value_range.get("values", [])
        blocks.append(
            f"## {resolved} — {len(values)} row(s)\n"
            f"{_render_grid(values, _a1_origin(resolved), cap)}"
        )
    return "\n\n".join(blocks) or "No values returned."


@tool
def sheets_write(
    spreadsheet_id: Annotated[str, "Google Sheets spreadsheet ID"],
    range_a1: Annotated[str, "Where to write — a top-left anchor ('Tab!A1') or a full range"],
    values_json: Annotated[
        str, 'JSON rows, e.g. \'[["Name","Score"],["Ada",99]]\'. A flat array writes one row'
    ],
    value_input: Annotated[
        str, "USER_ENTERED (parse formulas/dates/numbers) or RAW (store verbatim)"
    ] = "USER_ENTERED",
) -> str:
    """Overwrite cells in a Google Sheet starting at a range.

    Existing values in the written block are replaced. To add rows to the end of a
    table without overwriting anything, use sheets_append instead.
    """
    option = value_input.upper()
    if option not in ("USER_ENTERED", "RAW"):
        return f"Invalid value_input '{value_input}'. Use USER_ENTERED or RAW."

    try:
        service = _sheets_service()
    except Exception as e:
        return f"Error connecting to Sheets: {e}"

    try:
        rows = _parse_values(values_json)
    except ValueError as e:
        return f"Error writing to sheet: {e}"
    if not rows:
        return "Nothing to write — values_json is empty. Use sheets_clear to blank a range."

    try:
        result = (
            service.spreadsheets()
            .values()
            .update(
                spreadsheetId=spreadsheet_id,
                range=range_a1,
                valueInputOption=option,
                body={"values": rows},
            )
            .execute()
        )
        return (
            f"Wrote {result.get('updatedCells', 0)} cell(s) "
            f"({result.get('updatedRows', 0)} row(s)) to {result.get('updatedRange', range_a1)}.\n"
            f"Link: {_sheet_url(spreadsheet_id)}"
        )
    except Exception as e:
        return f"Error writing to sheet: {e}"


@tool
def sheets_append(
    spreadsheet_id: Annotated[str, "Google Sheets spreadsheet ID"],
    values_json: Annotated[
        str, 'JSON rows to append, e.g. \'[["Ada",99],["Grace",97]]\'. A flat array appends one row'
    ],
    range_a1: Annotated[str, "Tab or table range to append after (empty = the first tab)"] = "",
    value_input: Annotated[
        str, "USER_ENTERED (parse formulas/dates/numbers) or RAW (store verbatim)"
    ] = "USER_ENTERED",
) -> str:
    """Append rows after the last used row of a table, without overwriting existing data."""
    option = value_input.upper()
    if option not in ("USER_ENTERED", "RAW"):
        return f"Invalid value_input '{value_input}'. Use USER_ENTERED or RAW."

    try:
        service = _sheets_service()
    except Exception as e:
        return f"Error connecting to Sheets: {e}"

    try:
        rows = _parse_values(values_json)
    except ValueError as e:
        return f"Error appending to sheet: {e}"
    if not rows:
        return "Nothing to append — values_json is empty."

    try:
        target = range_a1.strip() or _quote_sheet(
            _resolve_tab(service, spreadsheet_id, "")["title"]
        )
        result = (
            service.spreadsheets()
            .values()
            .append(
                spreadsheetId=spreadsheet_id,
                range=target,
                valueInputOption=option,
                insertDataOption="INSERT_ROWS",
                body={"values": rows},
            )
            .execute()
        )
        updates = result.get("updates", {})
        return (
            f"Appended {updates.get('updatedRows', len(rows))} row(s) to "
            f"{updates.get('updatedRange', target)}.\n"
            f"Link: {_sheet_url(spreadsheet_id)}"
        )
    except Exception as e:
        return f"Error appending to sheet: {e}"


@tool_metadata(capability="mutating", risk="medium", requires_hitl=False)
@tool
def sheets_clear(
    spreadsheet_id: Annotated[str, "Google Sheets spreadsheet ID"],
    range_a1: Annotated[str, "A1 range to blank, e.g. 'Tab!A2:D100' or 'Tab' for the whole tab"],
) -> str:
    """Clear the values in a range, keeping the cells, formatting, and tab itself."""
    try:
        service = _sheets_service()
    except Exception as e:
        return f"Error connecting to Sheets: {e}"
    try:
        result = (
            service.spreadsheets()
            .values()
            .clear(spreadsheetId=spreadsheet_id, range=range_a1, body={})
            .execute()
        )
        return f"Cleared values in {result.get('clearedRange', range_a1)}."
    except Exception as e:
        return f"Error clearing range: {e}"


@tool
def sheets_add_sheet(
    spreadsheet_id: Annotated[str, "Google Sheets spreadsheet ID"],
    title: Annotated[str, "Name for the new tab"],
    rows: Annotated[int, "Number of rows in the new tab"] = 1000,
    cols: Annotated[int, "Number of columns in the new tab"] = 26,
) -> str:
    """Add a new tab (worksheet) to an existing spreadsheet."""
    try:
        service = _sheets_service()
    except Exception as e:
        return f"Error connecting to Sheets: {e}"
    try:
        result = (
            service.spreadsheets()
            .batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "requests": [
                        {
                            "addSheet": {
                                "properties": {
                                    "title": title,
                                    "gridProperties": {
                                        "rowCount": max(1, rows),
                                        "columnCount": max(1, cols),
                                    },
                                }
                            }
                        }
                    ]
                },
            )
            .execute()
        )
        props = result.get("replies", [{}])[0].get("addSheet", {}).get("properties", {})
        sheet_id = props.get("sheetId")
        return (
            f"Added tab '{props.get('title', title)}' (sheetId: {sheet_id}).\n"
            f"Link: {_sheet_url(spreadsheet_id, sheet_id)}"
        )
    except Exception as e:
        return f"Error adding sheet: {e}"


@tool
def sheets_delete_sheet(
    spreadsheet_id: Annotated[str, "Google Sheets spreadsheet ID"],
    sheet_name: Annotated[str, "Name of the tab to delete"],
) -> str:
    """Delete a tab and all of its data from a spreadsheet. This cannot be undone."""
    try:
        service = _sheets_service()
    except Exception as e:
        return f"Error connecting to Sheets: {e}"
    try:
        props = _resolve_tab(service, spreadsheet_id, sheet_name)
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"deleteSheet": {"sheetId": int(props["sheetId"])}}]},
        ).execute()
        return f"Deleted tab '{props['title']}' (sheetId: {props['sheetId']})."
    except Exception as e:
        return f"Error deleting sheet: {e}"


@tool_metadata(capability="mutating", risk="medium", requires_hitl=False)
@tool
def sheets_rename_sheet(
    spreadsheet_id: Annotated[str, "Google Sheets spreadsheet ID"],
    sheet_name: Annotated[str, "Current tab name"],
    new_title: Annotated[str, "New tab name"],
) -> str:
    """Rename a tab in a spreadsheet."""
    try:
        service = _sheets_service()
    except Exception as e:
        return f"Error connecting to Sheets: {e}"
    try:
        props = _resolve_tab(service, spreadsheet_id, sheet_name)
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "updateSheetProperties": {
                            "properties": {"sheetId": int(props["sheetId"]), "title": new_title},
                            "fields": "title",
                        }
                    }
                ]
            },
        ).execute()
        return f"Renamed tab '{props['title']}' → '{new_title}'."
    except Exception as e:
        return f"Error renaming sheet: {e}"


@tool_metadata(capability="mutating", risk="medium", requires_hitl=False)
@tool
def sheets_insert_rows(
    spreadsheet_id: Annotated[str, "Google Sheets spreadsheet ID"],
    start_index: Annotated[int, "1-based row to insert before (2 inserts above row 2)"],
    count: Annotated[int, "How many rows to insert"] = 1,
    sheet_name: Annotated[str, "Tab name (empty = the first tab)"] = "",
    dimension: Annotated[
        str, "'ROWS' (default), or 'COLUMNS' to insert columns — start_index is then 1 = column A"
    ] = "ROWS",
) -> str:
    """Insert blank rows (or columns) into a tab, shifting existing cells down/right."""
    axis = dimension.upper()
    if axis not in ("ROWS", "COLUMNS"):
        return f"Invalid dimension '{dimension}'. Use ROWS or COLUMNS."
    if start_index < 1 or count < 1:
        return "start_index must be >= 1 (1-based) and count must be >= 1."

    try:
        service = _sheets_service()
    except Exception as e:
        return f"Error connecting to Sheets: {e}"

    try:
        props = _resolve_tab(service, spreadsheet_id, sheet_name)
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "insertDimension": {
                            "range": {
                                "sheetId": int(props["sheetId"]),
                                "dimension": axis,
                                "startIndex": start_index - 1,
                                "endIndex": start_index - 1 + count,
                            },
                            "inheritFromBefore": start_index > 1,
                        }
                    }
                ]
            },
        ).execute()
        label = "row(s)" if axis == "ROWS" else "column(s)"
        return f"Inserted {count} {label} at position {start_index} in '{props['title']}'."
    except Exception as e:
        return f"Error inserting {axis.lower()}: {e}"


@tool
def sheets_delete_rows(
    spreadsheet_id: Annotated[str, "Google Sheets spreadsheet ID"],
    start_index: Annotated[int, "1-based first row to delete (2 = row 2)"],
    count: Annotated[int, "How many rows to delete"] = 1,
    sheet_name: Annotated[str, "Tab name (empty = the first tab)"] = "",
    dimension: Annotated[
        str, "'ROWS' (default), or 'COLUMNS' to delete columns — start_index is then 1 = column A"
    ] = "ROWS",
) -> str:
    """Delete rows (or columns) from a tab, shifting the remaining cells up/left."""
    axis = dimension.upper()
    if axis not in ("ROWS", "COLUMNS"):
        return f"Invalid dimension '{dimension}'. Use ROWS or COLUMNS."
    if start_index < 1 or count < 1:
        return "start_index must be >= 1 (1-based) and count must be >= 1."

    try:
        service = _sheets_service()
    except Exception as e:
        return f"Error connecting to Sheets: {e}"

    try:
        props = _resolve_tab(service, spreadsheet_id, sheet_name)
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "deleteDimension": {
                            "range": {
                                "sheetId": int(props["sheetId"]),
                                "dimension": axis,
                                "startIndex": start_index - 1,
                                "endIndex": start_index - 1 + count,
                            }
                        }
                    }
                ]
            },
        ).execute()
        label = "row(s)" if axis == "ROWS" else "column(s)"
        return f"Deleted {count} {label} from position {start_index} in '{props['title']}'."
    except Exception as e:
        return f"Error deleting {axis.lower()}: {e}"


@tool_metadata(capability="mutating", risk="medium", requires_hitl=False)
@tool
def sheets_find_replace(
    spreadsheet_id: Annotated[str, "Google Sheets spreadsheet ID"],
    find: Annotated[str, "Text to find"],
    replace: Annotated[str, "Replacement text"],
    sheet_name: Annotated[str, "Tab to search (empty = every tab in the spreadsheet)"] = "",
    match_case: Annotated[bool, "Case-sensitive match (default True)"] = True,
    match_entire_cell: Annotated[bool, "Only replace when the whole cell equals `find`"] = False,
) -> str:
    """Find and replace text across one tab, or the whole spreadsheet."""
    try:
        service = _sheets_service()
    except Exception as e:
        return f"Error connecting to Sheets: {e}"

    request: dict[str, Any] = {
        "find": find,
        "replacement": replace,
        "matchCase": match_case,
        "matchEntireCell": match_entire_cell,
    }
    try:
        if sheet_name:
            props = _resolve_tab(service, spreadsheet_id, sheet_name)
            request["sheetId"] = int(props["sheetId"])
            scope = f"tab '{props['title']}'"
        else:
            request["allSheets"] = True
            scope = "all tabs"

        result = (
            service.spreadsheets()
            .batchUpdate(
                spreadsheetId=spreadsheet_id, body={"requests": [{"findReplace": request}]}
            )
            .execute()
        )
        reply = result.get("replies", [{}])[0].get("findReplace", {})
        return (
            f"Replaced {reply.get('occurrencesChanged', 0)} occurrence(s) of '{find}' with "
            f"'{replace}' in {scope} "
            f"({reply.get('rowsChanged', 0)} row(s), {reply.get('sheetsChanged', 0)} tab(s))."
        )
    except Exception as e:
        return f"Error in find/replace: {e}"


@tool_metadata(capability="mutating", risk="medium", requires_hitl=False)
@tool
def sheets_format_cells(
    spreadsheet_id: Annotated[str, "Google Sheets spreadsheet ID"],
    range_a1: Annotated[str, "A1 range to format, e.g. 'Tab!A1:D1' or 'Tab' for the whole tab"],
    bold: Annotated[bool | None, "Bold text (leave unset to keep as-is)"] = None,
    italic: Annotated[bool | None, "Italic text (leave unset to keep as-is)"] = None,
    font_size: Annotated[int, "Font size in points (0 = keep as-is)"] = 0,
    background: Annotated[str, "Cell fill as hex, e.g. '#4285F4' (empty = keep as-is)"] = "",
    text_color: Annotated[str, "Text color as hex, e.g. '#FFFFFF' (empty = keep as-is)"] = "",
    number_format: Annotated[
        str, "Number pattern, e.g. '#,##0.00', '$#,##0', '0.0%', 'yyyy-mm-dd' (empty = keep as-is)"
    ] = "",
    horizontal_align: Annotated[str, "LEFT, CENTER, or RIGHT (empty = keep as-is)"] = "",
    wrap: Annotated[str, "OVERFLOW_CELL, CLIP, or WRAP (empty = keep as-is)"] = "",
) -> str:
    """Apply cell formatting to a range — bold/italic headers, fills, number formats, alignment.

    Only the arguments you set are changed; everything else keeps its current formatting.
    """
    try:
        service = _sheets_service()
    except Exception as e:
        return f"Error connecting to Sheets: {e}"

    cell_format: dict[str, Any] = {}
    fields: list[str] = []
    text_format: dict[str, Any] = {}

    if bold is not None:
        text_format["bold"] = bold
        fields.append("userEnteredFormat.textFormat.bold")
    if italic is not None:
        text_format["italic"] = italic
        fields.append("userEnteredFormat.textFormat.italic")
    if font_size:
        text_format["fontSize"] = font_size
        fields.append("userEnteredFormat.textFormat.fontSize")

    try:
        if text_color:
            text_format["foregroundColor"] = _parse_color(text_color)
            fields.append("userEnteredFormat.textFormat.foregroundColor")
        if background:
            cell_format["backgroundColor"] = _parse_color(background)
            fields.append("userEnteredFormat.backgroundColor")
    except ValueError as e:
        return f"Error formatting cells: {e}"

    if number_format:
        cell_format["numberFormat"] = {"type": "NUMBER", "pattern": number_format}
        fields.append("userEnteredFormat.numberFormat")
    if horizontal_align:
        align = horizontal_align.upper()
        if align not in ("LEFT", "CENTER", "RIGHT"):
            return f"Invalid horizontal_align '{horizontal_align}'. Use LEFT, CENTER, or RIGHT."
        cell_format["horizontalAlignment"] = align
        fields.append("userEnteredFormat.horizontalAlignment")
    if wrap:
        strategy = wrap.upper()
        if strategy not in ("OVERFLOW_CELL", "CLIP", "WRAP"):
            return f"Invalid wrap '{wrap}'. Use OVERFLOW_CELL, CLIP, or WRAP."
        cell_format["wrapStrategy"] = strategy
        fields.append("userEnteredFormat.wrapStrategy")

    if text_format:
        cell_format["textFormat"] = text_format
    if not fields:
        return "Nothing to change — set at least one formatting argument."

    try:
        props, grid = _resolve_a1_target(service, spreadsheet_id, range_a1)
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "repeatCell": {
                            "range": grid,
                            "cell": {"userEnteredFormat": cell_format},
                            "fields": ",".join(fields),
                        }
                    }
                ]
            },
        ).execute()
        return (
            f"Formatted {range_a1 or props['title']} in '{props['title']}' "
            f"({len(fields)} propert{'y' if len(fields) == 1 else 'ies'} applied)."
        )
    except Exception as e:
        return f"Error formatting cells: {e}"


@tool_metadata(capability="mutating", risk="medium", requires_hitl=False)
@tool
def sheets_freeze(
    spreadsheet_id: Annotated[str, "Google Sheets spreadsheet ID"],
    rows: Annotated[int, "Number of header rows to freeze (0 = none)"] = 1,
    cols: Annotated[int, "Number of leading columns to freeze (0 = none)"] = 0,
    sheet_name: Annotated[str, "Tab name (empty = the first tab)"] = "",
) -> str:
    """Freeze header rows and/or leading columns so they stay visible while scrolling."""
    try:
        service = _sheets_service()
    except Exception as e:
        return f"Error connecting to Sheets: {e}"
    try:
        props = _resolve_tab(service, spreadsheet_id, sheet_name)
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "updateSheetProperties": {
                            "properties": {
                                "sheetId": int(props["sheetId"]),
                                "gridProperties": {
                                    "frozenRowCount": max(0, rows),
                                    "frozenColumnCount": max(0, cols),
                                },
                            },
                            "fields": (
                                "gridProperties.frozenRowCount,gridProperties.frozenColumnCount"
                            ),
                        }
                    }
                ]
            },
        ).execute()
        return f"Froze {max(0, rows)} row(s) and {max(0, cols)} column(s) in '{props['title']}'."
    except Exception as e:
        return f"Error freezing panes: {e}"


@tool
def sheets_export_csv(
    spreadsheet_id: Annotated[str, "Google Sheets spreadsheet ID"],
    sheet_name: Annotated[str, "Tab to export (empty = the first tab)"] = "",
    filename: Annotated[str, "Filename to save as (optional, defaults to '<tab>.csv')"] = "",
) -> str:
    """Export one tab of a spreadsheet to a CSV file in workspace/downloads/.

    Use this when you need the raw data on disk (for a script, an email attachment, or
    analysis) rather than rendered into the conversation.
    """
    from google.auth.transport.requests import AuthorizedSession

    from olano.config.workspace import get_workspace
    from olano.security.request_context import get_request_context

    try:
        service = _sheets_service()
        props = _resolve_tab(service, spreadsheet_id, sheet_name)
    except Exception as e:
        return f"Error connecting to Sheets: {e}"

    # Resolve the *running agent's* workspace (see drive_download for why a bare
    # get_workspace() would land the file in the legacy/global workspace).
    ws_root = get_request_context().metadata.get("workspace_root")
    downloads = get_workspace(str(ws_root) if ws_root else None).root / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)

    safe = re.sub(r"[^\w.\- ]+", "_", filename or f"{props['title']}.csv").strip() or "sheet.csv"
    if not safe.lower().endswith(".csv"):
        safe += ".csv"
    out_path = downloads / safe

    # The Drive export endpoint only ever returns the *first* tab as CSV, so go
    # through the per-gid spreadsheet export URL with an authorized session.
    url = (
        f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
        f"/export?format=csv&gid={props['sheetId']}"
    )
    try:
        response = AuthorizedSession(_get_credentials()).get(url, timeout=60)
        response.raise_for_status()
        if "csv" not in response.headers.get("content-type", ""):
            return (
                f"Error exporting sheet: Google returned "
                f"{response.headers.get('content-type', 'an unexpected content type')} "
                "instead of CSV — check that the spreadsheet ID is correct and readable."
            )
        out_path.write_bytes(response.content)
    except Exception as e:
        return f"Error exporting sheet: {e}"

    size_kb = out_path.stat().st_size / 1024
    return f"Exported tab '{props['title']}' → {out_path} ({size_kb:.1f} KB)"


# ── OAuth client resolution ───────────────────────────────────────────────────
# Google historically took a *path* to a downloaded client_secret.json, unlike
# the connector broker where every provider is a {"client_id", "client_secret"}
# vault blob. Both now work, resolved in this order (explicit beats stored,
# blob beats legacy path): explicit Client ID+Secret → explicit path →
# GOOGLE_OAUTH_CLIENT vault/env blob → GOOGLE_CLIENT_ID/-SECRET pair →
# GOOGLE_OAUTH_CREDENTIALS vault/env path. Stored values resolve through
# ``get_credential`` (vault common → os.environ), the same seam the broker
# uses, so an operator-provisioned "official" client needs no UI input.


def _client_config(client_id: str, client_secret: str, *, kind: str = "web") -> dict:
    """google-auth ``client_config`` dict for a bare Client ID/Secret pair.

    ``kind`` is ``"web"`` for the headless redirect flow and ``"installed"``
    for the CLI's loopback flow — google_auth_oauthlib keys the config on it.
    """
    return {
        kind: {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def _stored_client_pair() -> tuple[str, str] | None:
    """(client_id, client_secret) from the vault/env, or ``None``.

    ``GOOGLE_OAUTH_CLIENT`` holds either a bare ``{"client_id", "client_secret"}``
    blob (the shape every other connector uses) or the full downloaded
    client_secret.json (a ``{"web": …}`` / ``{"installed": …}`` wrapper) — both
    are accepted so the file's contents can be pasted into the vault verbatim.
    Falls back to the ``GOOGLE_CLIENT_ID``/``GOOGLE_CLIENT_SECRET`` pair (the
    names the google ToolkitAgent already documents). A malformed blob falls
    through rather than raising, matching the connector broker.
    """
    from olano.tools._credentials import get_credential

    raw = get_credential(_CLIENT_BLOB_KEY)
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            inner = parsed
            for wrapper in ("web", "installed"):
                if isinstance(parsed.get(wrapper), dict):
                    inner = parsed[wrapper]
                    break
            client_id = str(inner.get("client_id") or "").strip()
            client_secret = str(inner.get("client_secret") or "").strip()
            if client_id and client_secret:
                return client_id, client_secret
    client_id = get_credential(_CLIENT_ID_ENV).strip()
    client_secret = get_credential(_CLIENT_SECRET_ENV).strip()
    if client_id and client_secret:
        return client_id, client_secret
    return None


def _resolve_client_source(
    creds_path: str | None,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> tuple[str, Any]:
    """Resolve the OAuth client for a new consent flow.

    Returns ``("pair", (client_id, client_secret))`` or ``("path", str)``.
    """
    if client_id or client_secret:
        if not (client_id and client_secret):
            raise ValueError(
                "Google: both a Client ID and a Client Secret are required — "
                "one of the two is missing."
            )
        return "pair", (client_id.strip(), client_secret.strip())
    if creds_path:
        return "path", creds_path
    pair = _stored_client_pair()
    if pair:
        return "pair", pair
    from olano.tools._credentials import get_credential

    path = get_credential(_CREDS_PATH_ENV)
    if path:
        return "path", path
    raise ValueError(
        "Google: no OAuth client configured. Enter a Client ID/Secret in the "
        f"connect panel, store them in the vault under {_CLIENT_BLOB_KEY} "
        "(JSON with client_id/client_secret — pasting the downloaded "
        f"client_secret.json verbatim also works), or set {_CREDS_PATH_ENV} "
        "to the path of a client_secret.json file."
    )


def browser_available() -> bool:
    """Best-effort check for a usable local browser.

    Returns ``False`` on headless servers (no DISPLAY, or an SSH session) so
    callers can fall back to a copy-the-link authorization flow instead of
    crashing with ``could not locate runnable browser``.
    """
    import webbrowser

    # An SSH session, or Linux without a display server, is effectively headless
    # even if ``webbrowser`` finds a stale ``$BROWSER`` entry.
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    if sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        return False
    try:
        webbrowser.get()
        return True
    except Exception:
        return False


def run_google_auth_flow(
    creds_path: str | None = None,
    agent: str | None = None,
    shared: bool = False,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> None:
    """Run the OAuth2 authorization flow on the local machine (used by the CLI).

    Opens a browser for consent when one is available; on a headless host it
    prints the authorization URL instead of crashing. For remote/headless
    setups prefer the dashboard's Connections flow, which surfaces a link the
    user can open on any device (see :func:`start_google_auth_flow`).

    A bare Client ID/Secret pair is run as an *installed-app* client: a
    Desktop-type OAuth client works out of the box; a Web-type client must
    have the loopback redirect registered or Google rejects the consent URL.

    ``agent=None``  → save to the system token (shared by all agents).
    ``agent="name"`` → save to that agent's private token.
    ``shared=True``  → also mark the agent token as shared with other agents.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    kind, source = _resolve_client_source(creds_path, client_id, client_secret)
    if kind == "pair":
        pair_id, pair_secret = source
        flow = InstalledAppFlow.from_client_config(
            _client_config(pair_id, pair_secret, kind="installed"), _SCOPES
        )
    else:
        flow = InstalledAppFlow.from_client_secrets_file(source, _SCOPES)
    open_browser = browser_available()
    if not open_browser:
        print(
            "No local browser detected. Open the URL printed below on any device "
            "to authorize (or use the dashboard's Connections page on a remote host)."
        )
    creds = flow.run_local_server(port=0, open_browser=open_browser)

    paths = get_olano_paths()
    token_path = paths.agent_google_token_path(agent) if agent else paths.google_token_path

    _save_token(creds, token_path)

    if agent and shared:
        set_google_shared(agent, True)

    print(f"Google auth successful! Token saved to: {token_path}")


# ── Headless OAuth flow (used by the dashboard Connections page) ───────────────
# Mirrors the MCP OAuth flow in olano/core/mcp_setup.py: the server never
# opens a browser. ``start_google_auth_flow`` builds the authorization URL and
# returns a session; the dashboard shows the link, the user opens it on any
# device, Google redirects back to ``redirect_uri`` (the dashboard's
# ``/api/auth/google/callback``), and ``resolve_google_auth_callback`` exchanges
# the code and saves the token. The dashboard polls ``poll_google_auth_flow``.


@dataclass
class GoogleAuthSession:
    session_id: str
    agent: str | None
    shared: bool
    token_path: Path
    redirect_uri: str
    flow: Any = None
    state: str | None = None
    authorization_url: str | None = None
    # awaiting_authorization | exchanging | authorized | error
    status: str = "awaiting_authorization"
    error: str | None = None
    created_at: float = 0.0
    logs: list[dict] = field(default_factory=list)

    def log(self, level: str, message: str) -> None:
        self.logs.append({"ts": time.time(), "level": level, "message": message})
        logger.log(
            logging.ERROR if level == "error" else logging.INFO,
            "google-oauth[%s]: %s",
            self.session_id,
            message,
        )

    def public(self) -> dict:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "authorization_url": self.authorization_url,
            "redirect_uri": self.redirect_uri,
            "error": self.error,
            "agent": self.agent,
            "shared": self.shared,
            "logs": self.logs,
        }


_GOOGLE_AUTH_SESSIONS: dict[str, GoogleAuthSession] = {}
_GOOGLE_AUTH_BY_STATE: dict[str, str] = {}


def _prune_google_auth_sessions(max_age: float = 1800.0) -> None:
    now = time.time()
    stale = [sid for sid, s in _GOOGLE_AUTH_SESSIONS.items() if now - s.created_at > max_age]
    for sid in stale:
        sess = _GOOGLE_AUTH_SESSIONS.pop(sid, None)
        if sess and sess.state:
            _GOOGLE_AUTH_BY_STATE.pop(sess.state, None)


def start_google_auth_flow(
    *,
    creds_path: str | None,
    agent: str | None,
    shared: bool,
    redirect_uri: str,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> GoogleAuthSession:
    """Begin a headless-friendly authorization-code flow.

    Builds the authorization URL (no browser is opened) and returns a session.
    ``redirect_uri`` must be registered on the Google OAuth client and is the
    dashboard's ``/api/auth/google/callback``. Because the redirect lands on the
    dashboard origin rather than ``localhost``, the OAuth client must be of type
    **Web application** (a Desktop-app client only allows loopback redirects).

    The client comes from ``client_id``+``client_secret`` (the connect panel's
    fields), else ``creds_path``, else the vault/env — see
    :func:`_resolve_client_source` for the stored-resolution order.
    """
    _prune_google_auth_sessions()
    kind, source = _resolve_client_source(creds_path, client_id, client_secret)

    # Google sometimes returns a superset of the requested scopes (e.g. it adds
    # ``openid``); relax the strict scope check so the token exchange succeeds.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

    from google_auth_oauthlib.flow import Flow

    if kind == "pair":
        pair_id, pair_secret = source
        flow = Flow.from_client_config(
            _client_config(pair_id, pair_secret), scopes=_SCOPES, redirect_uri=redirect_uri
        )
    else:
        flow = Flow.from_client_secrets_file(source, scopes=_SCOPES, redirect_uri=redirect_uri)
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )

    paths = get_olano_paths()
    token_path = paths.agent_google_token_path(agent) if agent else paths.google_token_path

    session = GoogleAuthSession(
        session_id=uuid.uuid4().hex,
        agent=agent,
        shared=shared,
        token_path=token_path,
        redirect_uri=redirect_uri,
        flow=flow,
        state=state,
        authorization_url=auth_url,
        created_at=time.time(),
    )
    _GOOGLE_AUTH_SESSIONS[session.session_id] = session
    if state:
        _GOOGLE_AUTH_BY_STATE[state] = session.session_id
    session.log("info", "Authorization URL ready — open it to grant access.")
    return session


def poll_google_auth_flow(session_id: str) -> GoogleAuthSession | None:
    return _GOOGLE_AUTH_SESSIONS.get(session_id)


def resolve_google_auth_callback(
    state: str | None, code: str | None, error: str | None = None
) -> GoogleAuthSession | None:
    """Feed the OAuth redirect back into the waiting flow (matched by ``state``).

    Exchanges the authorization code for tokens and saves them. Returns the
    session so the API can render a status page; ``None`` if no flow matches.
    """
    if not state:
        return None
    sid = _GOOGLE_AUTH_BY_STATE.get(state)
    if not sid:
        return None
    session = _GOOGLE_AUTH_SESSIONS.get(sid)
    if session is None:
        return None
    if session.status in ("authorized", "error"):
        return session

    if error:
        session.status = "error"
        session.error = error
        session.log("error", f"Authorization failed: {error}")
        _GOOGLE_AUTH_BY_STATE.pop(state, None)
        return session
    if not code:
        session.status = "error"
        session.error = "No authorization code was returned."
        session.log("error", session.error)
        _GOOGLE_AUTH_BY_STATE.pop(state, None)
        return session

    try:
        session.status = "exchanging"
        session.log("info", "Authorization code received — exchanging for tokens…")
        # Pass the code directly (not the redirect URL) so oauthlib does not
        # reject an http loopback/proxy redirect as insecure transport. The
        # exchange is a sync requests POST invoked from the API callback, so it
        # must have a timeout — a stuck TCP connection here hangs the server.
        session.flow.fetch_token(code=code, timeout=30)
        creds = session.flow.credentials
        _save_token(creds, session.token_path)
        if session.agent and session.shared:
            set_google_shared(session.agent, True)
        session.status = "authorized"
        session.log("info", f"Google auth successful — token saved to {session.token_path}")
    except Exception as exc:
        session.status = "error"
        session.error = str(exc)
        session.log("error", f"Token exchange failed: {exc}")
    finally:
        _GOOGLE_AUTH_BY_STATE.pop(state, None)
    return session


def disconnect_google(agent: str | None = None) -> bool:
    """Delete the Google token for the given scope. Returns True if deleted.

    Also drops the agent's connections-meta entry and any in-flight
    authorization for this scope, so a reconnect starts clean instead of
    inheriting the disconnected token's ``shared`` flag or letting a stale
    redirect land on the account that was just removed.
    """
    paths = get_olano_paths()
    token_path = paths.agent_google_token_path(agent) if agent else paths.google_token_path

    if agent:
        clear_google_meta(agent)
    for sid, session in list(_GOOGLE_AUTH_SESSIONS.items()):
        if (session.agent or None) != agent:
            continue
        _GOOGLE_AUTH_SESSIONS.pop(sid, None)
        if session.state:
            _GOOGLE_AUTH_BY_STATE.pop(session.state, None)

    if token_path.exists():
        token_path.unlink()
        return True
    return False


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
