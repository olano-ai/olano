"""LinkedIn outreach on the owner's OWN Unipile workspace (BYOK).

The managed path (:mod:`olano.deployment.linkedin_client`) reaches Unipile
through Olano Cloud, which holds the provider key, the account bindings, the
daily caps and the add-on seats. An owner who already has a Unipile workspace
can skip all of that: when the agent's credentials resolve both
``UNIPILE_API_KEY`` and ``UNIPILE_DSN`` (``get_credential``: the agent's
``config.env``, then the vault, per-agent override first, then the
environment), every LinkedIn tool talks to that workspace directly from this
box. Nothing is billed by Olano: no add-on seat, no credits; Unipile bills the
owner. It works on any install, managed or self-hosted.

The functions here return exactly the JSON the cloud routes return
(``{url, intent_id, ...}`` for connect, ``{status, account, intent, limits,
addon}`` for status, the per-action payloads for actions) and raise
:class:`~olano.deployment.linkedin_client.LinkedinServiceError` with the same
codes, so the tools render both paths with one set of code.

What the cloud did that this box now does itself:

- **Account binding.** Which LinkedIn account of the workspace an agent uses.
  ``UNIPILE_ACCOUNT_ID`` pins it explicitly. Otherwise ``connect`` remembers
  which LinkedIn accounts already existed and binds the one the hosted sign-in
  adds; with no pending sign-in, a workspace with exactly one LinkedIn account
  no other agent uses is adopted. Stored in ``<home>/linkedin/byok.json``.
- **Daily caps.** The same per-account rolling-24 h limits as the managed
  service (``LINKEDIN_DAILY_CAP_<ACTION>`` overrides them here too), kept in
  the same state file, because LinkedIn restricts accounts, whoever pays.
- **Chat isolation.** One key reaches every account's inbox, so a chat is read
  or answered only when it belongs to the agent's bound account.

Disconnecting unbinds the agent; the account is deleted from the workspace
only when it was connected through this box's sign-in link (an account the
owner connected in their own Unipile dashboard is theirs to remove).
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

from olano.deployment.linkedin_client import LinkedinServiceError

logger = logging.getLogger(__name__)

KEY_NAME = "UNIPILE_API_KEY"
DSN_NAME = "UNIPILE_DSN"
ACCOUNT_NAME = "UNIPILE_ACCOUNT_ID"
TIMEOUT_SECONDS = 30.0
LINK_TTL = timedelta(hours=1)

NOTE_MAX_CHARS = 300
MESSAGE_MAX_CHARS = 8_000
COMMENT_MAX_CHARS = 1_250
REACTIONS = ("like", "celebrate", "support", "love", "insightful", "funny")

# Same pacing as the managed service (cloud lib/services/linkedin.ts): below
# what LinkedIn tolerates, invitations lowest because LinkedIn also caps them
# weekly.
DEFAULT_DAILY_CAPS: dict[str, int] = {
    "visit": 80,
    "follow": 50,
    "invite": 15,
    "message": 100,
    "comment": 30,
    "react": 60,
}
_NEEDS_RECONNECT = {"CREDENTIALS", "STOPPED", "ERROR", "DELETED"}
_STATE_LOCK = threading.Lock()


# ── credentials ─────────────────────────────────────────────────────────────


def byok_credentials() -> tuple[str, str] | None:
    """``(api_key, origin)`` when the agent has its own Unipile workspace, else None."""
    try:
        from olano.tools._credentials import get_credential

        key = get_credential(KEY_NAME).strip()
        dsn = get_credential(DSN_NAME).strip()
    except Exception:  # pragma: no cover - defensive
        return None
    if not key or not dsn:
        return None
    origin = unipile_origin(dsn)
    return (key, origin) if origin else None


def byok_configured() -> bool:
    return byok_credentials() is not None


def unipile_origin(dsn: str) -> str:
    """The tenant origin from a DSN as the dashboard shows it (``api42.unipile.com:17294``),
    with or without scheme or a trailing ``/api/v1``. Empty when unusable."""
    raw = dsn.strip().rstrip("/")
    if not raw:
        return ""
    if not re.match(r"^https?://", raw, re.I):
        raw = f"https://{raw}"
    raw = re.sub(r"/api/v1$", "", raw, flags=re.I)
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.hostname:
        return ""
    return raw


def _pinned_account_id() -> str:
    try:
        from olano.tools._credentials import get_credential

        return get_credential(ACCOUNT_NAME).strip()
    except Exception:  # pragma: no cover - defensive
        return ""


# ── HTTP ────────────────────────────────────────────────────────────────────


class UnipileError(Exception):
    def __init__(self, status: int, type_: str, detail: str) -> None:
        super().__init__(f"unipile {status} {type_}: {detail}")
        self.status = status
        self.type = type_
        self.detail = detail


def _call(
    method: str,
    path: str,
    *,
    query: dict[str, Any] | None = None,
    body: Any = None,
    form: dict[str, Any] | None = None,
) -> Any:
    import httpx

    creds = byok_credentials()
    if creds is None:
        raise LinkedinServiceError(
            "service_unavailable", "UNIPILE_API_KEY / UNIPILE_DSN are not set"
        )
    key, origin = creds
    url = f"{origin}/api/v1{path}"
    from olano.security.ssrf import validate_outbound_url

    decision = validate_outbound_url(url)
    if not decision.allowed:
        raise LinkedinServiceError("byok_config", f"UNIPILE_DSN is not usable: {decision.reason}")
    params = {
        k: (str(v).lower() if isinstance(v, bool) else v)
        for k, v in (query or {}).items()
        if v is not None
    }
    headers = {"X-API-KEY": key, "accept": "application/json"}
    try:
        if form is not None:
            # Messaging takes multipart fields (as Unipile's SDK sends them).
            files = [
                (k, (None, str(item)))
                for k, v in form.items()
                for item in (v if isinstance(v, list) else [v])
            ]
            resp = httpx.request(
                method, url, params=params, headers=headers, files=files, timeout=TIMEOUT_SECONDS
            )
        else:
            resp = httpx.request(
                method, url, params=params, headers=headers, json=body, timeout=TIMEOUT_SECONDS
            )
    except httpx.HTTPError as exc:
        raise LinkedinServiceError("network", f"could not reach Unipile: {exc}") from exc
    try:
        data = resp.json() if resp.content else None
    except ValueError:
        data = None
    if resp.status_code >= 400:
        err = data if isinstance(data, dict) else {}
        type_ = err.get("type") if isinstance(err.get("type"), str) else f"http_{resp.status_code}"
        detail = err.get("detail") or err.get("title") or resp.text[:300]
        raise UnipileError(resp.status_code, str(type_), str(detail))
    return data


def _s(v: Any) -> str | None:
    return v if isinstance(v, str) and v else None


def _sent_date(date: Any) -> dict[str, str | None]:
    """An invitation's ``date`` as ``sent_at`` or ``sent_label``, never both.

    LinkedIn shows a pending invitation's age only as relative wording ("Sent
    today"), and Unipile's ``parsed_datetime`` resolves that wording against
    the moment of the read: two reads minutes apart return the same invitation
    minutes apart. It is never used. ``date`` counts as a date only when it
    starts with one; anything else is passed on as the label it is. Mirrors
    the cloud's ``sentDate`` (apps/web/lib/services/unipile.ts).
    """
    value = _s(date)
    if value and re.match(r"\d{4}-\d{2}-\d{2}", value):
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return {"sent_at": value, "sent_label": None}
        except ValueError:
            pass
    return {"sent_at": None, "sent_label": value}


# ── state (bindings + pacing) ───────────────────────────────────────────────


def _state_path() -> Path:
    from olano.config.home import get_olano_paths

    return get_olano_paths().root / "linkedin" / "byok.json"


def _load() -> dict[str, Any]:
    try:
        data = json.loads(_state_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.chmod(0o600)
    tmp.replace(path)


def _workspace_key(origin: str) -> str:
    # Bindings are per workspace: an owner who swaps keys starts clean.
    return origin.lower()


def _bucket(state: dict[str, Any], origin: str) -> dict[str, Any]:
    ws: dict[str, Any] = state.setdefault("workspaces", {}).setdefault(_workspace_key(origin), {})
    ws.setdefault("bindings", {})
    ws.setdefault("pending", {})
    ws.setdefault("actions", {})
    return ws


def daily_cap(action: str) -> int:
    raw = os.environ.get(f"LINKEDIN_DAILY_CAP_{action.upper()}", "").strip()
    try:
        n = int(raw)
        return n if n >= 0 else DEFAULT_DAILY_CAPS[action]
    except ValueError:
        return DEFAULT_DAILY_CAPS[action]


def _used_today(ws: dict[str, Any], account_id: str, action: str, now: float) -> int:
    stamps = ws["actions"].get(account_id, {}).get(action, [])
    return sum(1 for t in stamps if now - t < 86_400)


def _log_action(origin: str, account_id: str, action: str) -> None:
    now = time.time()
    with _STATE_LOCK:
        state = _load()
        ws = _bucket(state, origin)
        per = ws["actions"].setdefault(account_id, {})
        per[action] = [t for t in per.get(action, []) if now - t < 86_400] + [now]
        _save(state)


# ── accounts ────────────────────────────────────────────────────────────────


def _account(account_id: str) -> dict[str, Any]:
    raw = _call("GET", f"/accounts/{quote(account_id, safe='')}") or {}
    sources = raw.get("sources") or []
    status = next(
        (
            s.get("status")
            for s in sources
            if isinstance(s, dict) and isinstance(s.get("status"), str)
        ),
        "",
    )
    im = ((raw.get("connection_params") or {}).get("im") or {}) if isinstance(raw, dict) else {}
    return {
        "id": _s(raw.get("id")) or account_id,
        "type": str(raw.get("type") or ""),
        "name": _s(raw.get("name")),
        "status": status or "UNKNOWN",
        "public_identifier": _s(im.get("publicIdentifier")),
        "created_at": _s(raw.get("created_at")),
    }


def _linkedin_accounts() -> list[dict[str, Any]]:
    raw = _call("GET", "/accounts", query={"limit": 250}) or {}
    out = []
    for item in raw.get("items") or []:
        if (
            isinstance(item, dict)
            and str(item.get("type") or "").upper() == "LINKEDIN"
            and _s(item.get("id"))
        ):
            out.append({"id": item["id"], "created_at": _s(item.get("created_at")) or ""})
    return out


def _resolve_binding(agent: str, origin: str) -> tuple[dict[str, Any] | None, str | None]:
    """The agent's bound account ``{id, via}`` and how a pending sign-in ended.

    Order: ``UNIPILE_ACCOUNT_ID`` pin → stored binding → a pending sign-in that
    has since added an account → the workspace's only LinkedIn account when no
    other agent holds it.
    """
    pinned = _pinned_account_id()
    if pinned:
        return {"id": pinned, "via": "pinned"}, None
    with _STATE_LOCK:
        state = _load()
        ws = _bucket(state, origin)
        bound = ws["bindings"].get(agent)
        pending = ws["pending"].get(agent)
    if bound and not pending:
        return bound, None

    accounts = _linkedin_accounts()
    ids = {a["id"] for a in accounts}
    intent_status: str | None = None
    if pending:
        known = set(pending.get("known_ids") or [])
        fresh = sorted((a for a in accounts if a["id"] not in known), key=lambda a: a["created_at"])
        if fresh:
            bound = {"id": fresh[-1]["id"], "via": "link"}
            intent_status = "connected"
        elif time.time() > float(pending.get("expires_at", 0)):
            intent_status = "expired"
        else:
            intent_status = "pending"
    if bound and bound["id"] not in ids and bound.get("via") != "pinned":
        bound = None
    if not bound and not pending:
        with _STATE_LOCK:
            taken = {
                b.get("id") for a, b in _bucket(_load(), origin)["bindings"].items() if a != agent
            }
        free = [a for a in accounts if a["id"] not in taken]
        if len(accounts) == 1 and len(free) == 1:
            bound = {"id": free[0]["id"], "via": "adopted"}

    with _STATE_LOCK:
        state = _load()
        ws = _bucket(state, origin)
        previous = ws["bindings"].get(agent)
        if intent_status in ("connected", "expired"):
            ws["pending"].pop(agent, None)
        if bound:
            ws["bindings"][agent] = bound
        else:
            ws["bindings"].pop(agent, None)
        _save(state)
    # A reconnect replaced an account this box's own link had added: delete it,
    # or it keeps costing the owner a Unipile account with nothing using it.
    if (
        previous
        and bound
        and previous.get("id") != bound.get("id")
        and previous.get("via") == "link"
    ):
        try:
            _call("DELETE", f"/accounts/{quote(previous['id'], safe='')}")
        except (UnipileError, LinkedinServiceError) as exc:
            logger.warning("unipile delete of the replaced account failed: %s", exc)
    return bound, intent_status


# ── profiles, posts, chats (shapes match the cloud route payloads) ─────────


def parse_profile_identifier(value: str) -> str | None:
    s = (value or "").strip()
    if not s:
        return None
    if re.search(r"linkedin\.com", s, re.I) or re.match(r"^https?://", s, re.I):
        url = urlparse(s if re.match(r"^https?://", s, re.I) else f"https://{s}")
        host = (url.hostname or "").lower()
        if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
            return None
        m = re.match(r"^/in/([^/]+)", url.path, re.I)
        if not m:
            return None
        slug = unquote(m.group(1))
        return slug if slug and len(slug) <= 120 and not re.search(r"[/?#\s]", slug) else None
    bare = s.lstrip("@")
    return bare if re.fullmatch(r"[A-Za-z0-9_\-%.]{2,120}", bare) else None


def parse_post_id(value: str) -> str | None:
    s = (value or "").strip()
    if not s or len(s) > 500:
        return None
    decoded = unquote(s)
    m = re.search(r"urn:li:(activity|ugcPost|share):(\d{6,25})", decoded)
    if m:
        return f"urn:li:{m.group(1)}:{m.group(2)}"
    if re.search(r"linkedin\.com", decoded, re.I):
        m = re.search(r"[-_]activity-(\d{6,25})(?:-|/|$|\?)", decoded, re.I)
        return f"urn:li:activity:{m.group(1)}" if m else None
    return f"urn:li:activity:{decoded}" if re.fullmatch(r"\d{6,25}", decoded) else None


def _bounded(text: str | None, limit: int, what: str) -> str:
    t = (text or "").strip()
    if not t:
        raise LinkedinServiceError("text_invalid", f"{what} is empty")
    if len(t) > limit:
        raise LinkedinServiceError(
            "text_invalid", f"{what} is {len(t)} characters; LinkedIn allows {limit}"
        )
    return t


def _profile(account_id: str, identifier: str, *, notify: bool) -> dict[str, Any]:
    raw = (
        _call(
            "GET",
            f"/users/{quote(identifier, safe='')}",
            query={"account_id": account_id, "notify": notify},
        )
        or {}
    )
    provider_id = _s(raw.get("provider_id"))
    if not provider_id:
        raise UnipileError(404, "errors/not_found", "profile not found")
    inv = raw.get("invitation") if isinstance(raw.get("invitation"), dict) else None
    name = " ".join(x for x in (_s(raw.get("first_name")), _s(raw.get("last_name"))) if x)
    return {
        "provider_id": provider_id,
        "view": {
            "name": name or _s(raw.get("public_identifier")) or provider_id,
            "headline": _s(raw.get("headline")),
            "location": _s(raw.get("location")),
            "profile_url": _s(raw.get("public_profile_url")),
            "public_identifier": _s(raw.get("public_identifier")),
            "network_distance": _s(raw.get("network_distance")),
            "connected": raw.get("is_relationship") is True
            or raw.get("network_distance") == "FIRST_DEGREE",
            "invitation": (
                {"type": inv["type"], "status": inv["status"]}
                if inv and isinstance(inv.get("type"), str) and isinstance(inv.get("status"), str)
                else None
            ),
            "follower_count": raw.get("follower_count")
            if isinstance(raw.get("follower_count"), int)
            else None,
        },
    }


def _post_view(p: dict[str, Any]) -> dict[str, Any]:
    author: dict[str, Any] = p["author"] if isinstance(p.get("author"), dict) else {}
    perms: dict[str, Any] = p["permissions"] if isinstance(p.get("permissions"), dict) else {}
    text: str = p["text"] if isinstance(p.get("text"), str) else ""
    return {
        "id": _s(p.get("social_id")) or _s(p.get("id")) or "",
        "url": _s(p.get("share_url")),
        "text": text if len(text) <= 3_000 else f"{text[:3_000]}…",
        "posted_at": _s(p.get("parsed_datetime")) or _s(p.get("date")),
        "author": _s(author.get("name")),
        "is_repost": p.get("is_repost") is True,
        "reactions": p.get("reaction_counter") if isinstance(p.get("reaction_counter"), int) else 0,
        "comments": p.get("comment_counter") if isinstance(p.get("comment_counter"), int) else 0,
        "can_comment": perms.get("can_post_comments") is not False,
    }


def _owned_chat(account_id: str, chat_id: str) -> dict[str, Any] | None:
    try:
        raw = _call("GET", f"/chats/{quote(chat_id, safe='')}") or {}
    except UnipileError as exc:
        if exc.status in (400, 404):
            return None
        raise
    return raw if _s(raw.get("account_id")) == account_id else None


# ── public API (mirrors linkedin_client) ────────────────────────────────────


def connect(agent: str) -> dict[str, Any]:
    creds = byok_credentials()
    assert creds is not None
    _key, origin = creds
    try:
        known = [a["id"] for a in _linkedin_accounts()]
        expires = datetime.now(UTC) + LINK_TTL
        nonce = secrets.token_urlsafe(12)
        raw = (
            _call(
                "POST",
                "/hosted/accounts/link",
                body={
                    "type": "create",
                    "providers": ["LINKEDIN"],
                    "api_url": origin,
                    "expiresOn": expires.isoformat().replace("+00:00", "Z"),
                    "name": f"olano:{agent}:{nonce}",
                },
            )
            or {}
        )
    except UnipileError as exc:
        raise _provider_failure(exc) from exc
    url = _s(raw.get("url"))
    if not url:
        raise LinkedinServiceError("provider_error", "Unipile returned no sign-in link")
    with _STATE_LOCK:
        state = _load()
        _bucket(state, origin)["pending"][agent] = {
            "intent_id": nonce,
            "known_ids": known,
            "expires_at": expires.timestamp(),
        }
        _save(state)
    return {"url": url, "intent_id": nonce, "expires_at": expires.isoformat(), "byok": True}


def status(agent: str, intent_id: str | None = None) -> dict[str, Any]:
    creds = byok_credentials()
    assert creds is not None
    _key, origin = creds
    bound: dict[str, Any] | None = None
    intent_status: str | None = None
    try:
        bound, intent_status = _resolve_binding(agent, origin)
        state, account = "not_connected", None
        if bound:
            acct = _account(bound["id"])
            if acct["type"] and acct["type"].upper() != "LINKEDIN":
                raise LinkedinServiceError(
                    "byok_config", f"{ACCOUNT_NAME} names a {acct['type']} account, not LinkedIn"
                )
            state = "needs_reconnect" if acct["status"].upper() in _NEEDS_RECONNECT else "connected"
            account = {"name": acct["name"], "public_identifier": acct["public_identifier"]}
    except UnipileError as exc:
        if exc.status == 404 and bound:
            state, account = "not_connected", None
        else:
            raise _provider_failure(exc) from exc
    return {
        "status": state,
        "account": account,
        "intent": {"status": intent_status, "error": None} if intent_status else None,
        "limits": {
            "note_max_chars": NOTE_MAX_CHARS,
            "daily": {a: daily_cap(a) for a in DEFAULT_DAILY_CAPS},
        },
        "prices": None,
        "byok": True,
        # No add-on applies: the owner's own workspace, billed by Unipile.
        "addon": {"active": True, "byok": True},
    }


def action(agent: str, name: str, **fields: Any) -> dict[str, Any]:
    creds = byok_credentials()
    assert creds is not None
    _key, origin = creds
    try:
        bound, _ = _resolve_binding(agent, origin)
    except UnipileError as exc:
        raise _provider_failure(exc) from exc
    if not bound:
        raise LinkedinServiceError(
            "not_connected", "LinkedIn is not connected for this agent", {"byok": True}
        )
    acct = bound["id"]
    try:
        if name == "disconnect":
            return _disconnect(agent, origin, bound)
        if name in (
            "list_invitations",
            "withdraw_invitation",
            "list_chats",
            "read_chat",
            "list_posts",
        ):
            return _free(acct, name, fields)
        return _billable(origin, acct, name, fields)
    except UnipileError as exc:
        raise _provider_failure(exc) from exc


def _disconnect(agent: str, origin: str, bound: dict[str, Any]) -> dict[str, Any]:
    if bound.get("via") == "link":
        try:
            _call("DELETE", f"/accounts/{quote(bound['id'], safe='')}")
        except UnipileError as exc:
            if exc.status != 404:
                logger.warning("unipile delete on disconnect failed: %s", exc)
    with _STATE_LOCK:
        state = _load()
        _bucket(state, origin)["bindings"].pop(agent, None)
        _save(state)
    return {"result": "disconnected", "credits_charged": 0, "byok": True}


def _free(acct: str, name: str, f: dict[str, Any]) -> dict[str, Any]:
    limit = int(f.get("limit") or 0) or None
    if name == "list_invitations":
        raw = (
            _call("GET", "/users/invite/sent", query={"account_id": acct, "limit": limit or 20})
            or {}
        )
        items = [
            {
                "id": _s(it.get("id")),
                "name": _s(it.get("invited_user")),
                "public_identifier": _s(it.get("invited_user_public_id")),
                **_sent_date(it.get("date")),
                "note": _s(it.get("invitation_text")),
            }
            for it in raw.get("items") or []
            if isinstance(it, dict) and _s(it.get("id"))
        ]
        return {"result": "ok", "invitations": items, "credits_charged": 0}
    if name == "withdraw_invitation":
        inv = str(f.get("invitation_id") or "").strip()
        if not inv:
            raise LinkedinServiceError("invalid_body", "invitation_id required")
        _call("DELETE", f"/users/invite/sent/{quote(inv, safe='')}", query={"account_id": acct})
        return {"result": "withdrawn", "credits_charged": 0}
    if name == "list_chats":
        raw = (
            _call(
                "GET",
                "/chats",
                query={
                    "account_id": acct,
                    "account_type": "LINKEDIN",
                    "limit": limit or 20,
                    "unread": True if f.get("unread_only") else None,
                },
            )
            or {}
        )
        chats = [
            {
                "id": c["id"],
                "name": _s(c.get("name")),
                "unread": c.get("unread_count") if isinstance(c.get("unread_count"), int) else 0,
                "last_activity": _s(c.get("timestamp")),
                "can_reply": c.get("read_only") not in (1, 2),
            }
            for c in raw.get("items") or []
            if isinstance(c, dict) and _s(c.get("id"))
        ]
        return {"result": "ok", "chats": chats, "credits_charged": 0}
    if name == "read_chat":
        chat_id = str(f.get("chat_id") or "").strip()
        if not chat_id:
            raise LinkedinServiceError("invalid_body", "chat_id required")
        chat = _owned_chat(acct, chat_id)
        if not chat:
            raise LinkedinServiceError("unknown_chat", "no such conversation on this account")
        cid = quote(chat["id"], safe="")
        msgs = (_call("GET", f"/chats/{cid}/messages", query={"limit": limit or 30}) or {}).get(
            "items"
        ) or []
        try:
            people = (_call("GET", f"/chats/{cid}/attendees") or {}).get("items") or []
        except UnipileError:
            people = []
        names = {p.get("provider_id"): _s(p.get("name")) for p in people if isinstance(p, dict)}
        other = next((p for p in people if isinstance(p, dict) and p.get("is_self") != 1), {})
        visible = [m for m in msgs if isinstance(m, dict) and _s(m.get("id"))]
        visible = [
            m
            for m in visible
            if m.get("is_event") != 1 and m.get("deleted") != 1 and m.get("hidden") != 1
        ]
        return {
            "result": "ok",
            "chat": {
                "id": chat["id"],
                "name": _s(chat.get("name")) or _s(other.get("name")),
                "with_profile_url": _s(other.get("profile_url")),
                "can_reply": chat.get("read_only") not in (1, 2),
            },
            "messages": [
                {
                    "id": m["id"],
                    "from": "me"
                    if m.get("is_sender") == 1
                    else (names.get(m.get("sender_id")) or "them"),
                    "from_me": m.get("is_sender") == 1,
                    "text": m.get("text") if isinstance(m.get("text"), str) else None,
                    "sent_at": _s(m.get("timestamp")),
                    "attachments": len(m.get("attachments") or [])
                    if isinstance(m.get("attachments"), list)
                    else 0,
                }
                for m in reversed(visible)
            ],
            "credits_charged": 0,
        }
    # list_posts
    ident = parse_profile_identifier(str(f.get("profile") or ""))
    if not ident:
        raise LinkedinServiceError("invalid_profile", "not a LinkedIn member profile")
    prof = _profile(acct, ident, notify=False)
    raw = (
        _call(
            "GET",
            f"/users/{quote(prof['provider_id'], safe='')}/posts",
            query={"account_id": acct, "limit": limit or 10},
        )
        or {}
    )
    posts = [_post_view(p) for p in raw.get("items") or [] if isinstance(p, dict)]
    return {
        "result": "ok",
        "profile": prof["view"],
        "posts": [p for p in posts if p["id"]],
        "credits_charged": 0,
    }


def _billable(origin: str, acct: str, name: str, f: dict[str, Any]) -> dict[str, Any]:
    if name not in DEFAULT_DAILY_CAPS:
        raise LinkedinServiceError("invalid_body", "unknown action")
    # Validate before spending the day's budget or touching the provider.
    ident = post_id = None
    note = text = None
    if name in ("visit", "follow", "invite") or (name == "message" and not f.get("chat_id")):
        ident = parse_profile_identifier(str(f.get("profile") or ""))
        if not ident:
            if name == "message":
                raise LinkedinServiceError(
                    "invalid_body", "message needs chat_id (a reply) or profile (a connection)"
                )
            raise LinkedinServiceError("invalid_profile", "not a LinkedIn member profile")
    if name == "invite":
        note = (str(f.get("note") or "")).strip() or None
        if note and len(note) > NOTE_MAX_CHARS:
            raise LinkedinServiceError(
                "note_too_long", f"note is {len(note)} characters; LinkedIn allows {NOTE_MAX_CHARS}"
            )
    if name == "message":
        text = _bounded(f.get("text"), MESSAGE_MAX_CHARS, "message")
    if name in ("comment", "react"):
        post_id = parse_post_id(str(f.get("post") or ""))
        if not post_id:
            raise LinkedinServiceError("invalid_post", "not a LinkedIn post")
    if name == "comment":
        text = _bounded(f.get("text"), COMMENT_MAX_CHARS, "comment")
    reaction = str(f.get("reaction") or "like")
    if name == "react" and reaction not in REACTIONS:
        raise LinkedinServiceError(
            "invalid_body", f"reaction must be one of {', '.join(REACTIONS)}"
        )

    cap = daily_cap(name)
    with _STATE_LOCK:
        used = _used_today(_bucket(_load(), origin), acct, name, time.time())
    if used >= cap:
        raise LinkedinServiceError(
            "daily_cap_reached", "daily limit reached", {"action": name, "cap": cap, "used": used}
        )

    done, payload = True, {}
    if name in ("visit", "follow", "invite"):
        prof = _profile(acct, ident or "", notify=name == "visit")
        view = prof["view"]
        if name == "visit":
            payload = {"result": "visited", "profile": view}
        elif name == "follow":
            urn = f"urn:li:fsd_followingState:urn:li:fsd_profile:{prof['provider_id']}"
            _call(
                "POST",
                "/linkedin",
                query={"account_id": acct},
                body={
                    "account_id": acct,
                    "method": "POST",
                    "request_url": f"https://www.linkedin.com/voyager/api/feed/dash/followingStates/{urn}",
                    "body": {"patch": {"$set": {"following": True}}},
                    "encoding": False,
                },
            )
            payload = {"result": "followed", "profile": view}
        elif view["connected"]:
            done, payload = False, {"result": "already_connected", "profile": view}
        elif (view["invitation"] or {}).get("status") == "PENDING":
            kind = (
                "invitation_received"
                if view["invitation"]["type"] == "RECEIVED"
                else "already_invited"
            )
            done, payload = False, {"result": kind, "profile": view}
        else:
            body: dict[str, Any] = {"account_id": acct, "provider_id": prof["provider_id"]}
            if note:
                body["message"] = note
            raw = _call("POST", "/users/invite", body=body) or {}
            payload = {"result": "invited", "profile": view}
            if _s(raw.get("invitation_id")):
                payload["invitation_id"] = raw["invitation_id"]
            if note:
                payload["note_sent"] = True
    elif name == "message":
        chat_id = str(f.get("chat_id") or "").strip()
        if chat_id:
            chat = _owned_chat(acct, chat_id)
            if not chat:
                raise LinkedinServiceError("unknown_chat", "no such conversation on this account")
            if chat.get("read_only") in (1, 2):
                raise LinkedinServiceError(
                    "chat_read_only", "LinkedIn does not allow replies in this conversation"
                )
            raw = (
                _call("POST", f"/chats/{quote(chat['id'], safe='')}/messages", form={"text": text})
                or {}
            )
            payload = {
                "result": "sent",
                "chat_id": chat["id"],
                "message_id": _s(raw.get("message_id")),
            }
        else:
            prof = _profile(acct, ident or "", notify=False)
            if not prof["view"]["connected"]:
                done, payload = (
                    False,
                    {"result": "not_connected_with_member", "profile": prof["view"]},
                )
            else:
                raw = (
                    _call(
                        "POST",
                        "/chats",
                        form={
                            "account_id": acct,
                            "attendees_ids": [prof["provider_id"]],
                            "text": text,
                        },
                    )
                    or {}
                )
                payload = {
                    "result": "sent",
                    "chat_id": _s(raw.get("chat_id")),
                    "message_id": _s(raw.get("message_id")),
                    "profile": prof["view"],
                }
    elif name == "comment":
        raw = (
            _call("GET", f"/posts/{quote(post_id or '', safe='')}", query={"account_id": acct})
            or {}
        )
        post = _post_view(raw)
        if not post["can_comment"]:
            done, payload = False, {"result": "comments_closed", "post": post}
        else:
            _call(
                "POST",
                f"/posts/{quote(post['id'] or post_id or '', safe='')}/comments",
                body={"account_id": acct, "text": text},
            )
            payload = {"result": "commented", "post": post}
    else:  # react
        _call(
            "POST",
            "/posts/reaction",
            body={"account_id": acct, "post_id": post_id, "reaction_type": reaction},
        )
        payload = {"result": "reacted", "reaction": reaction, "post_id": post_id}

    if done:
        _log_action(origin, acct, name)
    return {
        **payload,
        "credits_charged": 0,
        "byok": True,
        **({"used_today": used + 1, "daily_cap": cap} if done else {}),
    }


def _provider_failure(exc: UnipileError) -> LinkedinServiceError:
    """Same contract as the cloud route's providerFailure."""
    # Unlike the managed path, the key itself may be wrong here: a 401 that does
    # not name the LinkedIn session (disconnected / credentials / checkpoint) is
    # the workspace refusing UNIPILE_API_KEY, and "sign in again" would send the
    # user the wrong way.
    if re.search(r"disconnected|credentials|reconnect|checkpoint", exc.type, re.I):
        return LinkedinServiceError(
            "needs_reconnect", "LinkedIn signed this account out", {"byok": True}
        )
    if exc.status in (401, 403):
        return LinkedinServiceError(
            "byok_unauthorized", f"Unipile refused the request ({exc.detail[:200]})", {"byok": True}
        )
    if 400 <= exc.status < 500 and exc.status not in (401, 408, 429):
        return LinkedinServiceError(
            "provider_refused",
            exc.detail[:300],
            {"type": exc.type.removeprefix("errors/"), "detail": exc.detail[:300]},
        )
    if exc.status == 429:
        return LinkedinServiceError("rate_limited", "Unipile is rate limiting this workspace")
    return LinkedinServiceError("provider_error", f"Unipile error {exc.status}")
