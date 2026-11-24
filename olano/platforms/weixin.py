"""
Weixin platform adapter.

Connects Olano Agent to WeChat personal accounts via Tencent's iLink Bot API.

Design notes:
- Long-poll ``getupdates`` drives inbound delivery.
- Every outbound reply must echo the latest ``context_token`` for the peer.
- Media files move through an AES-128-ECB encrypted CDN protocol.
- QR login is exposed as a helper for the gateway setup wizard.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import os
import re
import secrets
import struct
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, cast
from urllib.parse import quote, urlparse

logger = logging.getLogger(__name__)

try:
    import aiohttp

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - dependency gate
    aiohttp = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

try:
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    CRYPTO_AVAILABLE = True
except ImportError:  # pragma: no cover - dependency gate
    default_backend = None  # type: ignore[assignment]
    Cipher = None  # type: ignore[misc,assignment]
    algorithms = None  # type: ignore[assignment]
    modes = None  # type: ignore[assignment]
    CRYPTO_AVAILABLE = False

from olano.config.home import get_olano_home
from olano.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_audio_from_bytes,
    cache_document_from_bytes,
    cache_image_from_bytes,
)
from olano.platforms.config import Platform, PlatformConfig
from olano.platforms.helpers import MessageDeduplicator, atomic_json_write

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
WEIXIN_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0

EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_SEND_TYPING = "ilink/bot/sendtyping"
EP_GET_CONFIG = "ilink/bot/getconfig"
EP_GET_UPLOAD_URL = "ilink/bot/getuploadurl"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"

LONG_POLL_TIMEOUT_MS = 35_000
API_TIMEOUT_MS = 15_000
CONFIG_TIMEOUT_MS = 10_000
QR_TIMEOUT_MS = 35_000

MAX_CONSECUTIVE_FAILURES = 3
RETRY_DELAY_SECONDS = 2
BACKOFF_DELAY_SECONDS = 30
SESSION_EXPIRED_ERRCODE = -14
MESSAGE_DEDUP_TTL_SECONDS = 300

MEDIA_IMAGE = 1
MEDIA_VIDEO = 2
MEDIA_FILE = 3
MEDIA_VOICE = 4

_LIVE_ADAPTERS: dict[str, Any] = {}


def _make_ssl_connector() -> Optional[aiohttp.TCPConnector]:
    """Return a TCPConnector with a certifi CA bundle, or None if certifi is unavailable.

    Tencent's iLink server (``ilinkai.weixin.qq.com``) is not verifiable against
    some system CA stores (notably Homebrew's OpenSSL on macOS Apple Silicon).
    When ``certifi`` is installed, use its Mozilla CA bundle to guarantee
    verification. Otherwise fall back to aiohttp's default (which honors
    ``SSL_CERT_FILE`` env var via ``trust_env=True``).
    """
    try:
        import ssl

        import certifi
    except ImportError:
        return None
    if not AIOHTTP_AVAILABLE:
        return None
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    return aiohttp.TCPConnector(ssl=ssl_ctx)


ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

MSG_TYPE_USER = 1
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2

TYPING_START = 1
TYPING_STOP = 2

_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_TABLE_RULE_RE = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")
_FENCE_RE = re.compile(r"^```([^\n`]*)\s*$")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def check_weixin_requirements() -> bool:
    """Return True when runtime dependencies for Weixin are available."""
    return AIOHTTP_AVAILABLE and CRYPTO_AVAILABLE


def _safe_id(value: Optional[str], keep: int = 8) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "?"
    if len(raw) <= keep:
        return raw
    return raw[:keep]


# Gateway statuses that mean "nobody answered in time", as opposed to a real
# rejection. 524 is Cloudflare's own (it fronts ilinkai) and is what an idle
# long poll returns most often; the rest are the standard timeout family.
_POLL_TIMEOUT_STATUSES = (408, 502, 504, 522, 523, 524, 598, 599)
_HTTP_STATUS_RE = re.compile(r"\bHTTP (\d{3})\b")


def _is_poll_timeout(exc: BaseException) -> bool:
    """True when a ``getupdates`` failure is just the long poll expiring."""
    if isinstance(exc, TimeoutError):  # asyncio.TimeoutError aliases this on 3.11+
        return True
    if AIOHTTP_AVAILABLE and isinstance(
        exc, aiohttp.ServerTimeoutError | aiohttp.ServerDisconnectedError
    ):
        return True
    match = _HTTP_STATUS_RE.search(str(exc))
    return match is not None and int(match.group(1)) in _POLL_TIMEOUT_STATUSES


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)


def _aes128_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    encryptor = cipher.encryptor()
    return encryptor.update(_pkcs7_pad(plaintext)) + encryptor.finalize()


def _aes128_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    if not padded:
        return padded
    pad_len = padded[-1]
    if 1 <= pad_len <= 16 and padded.endswith(bytes([pad_len]) * pad_len):
        return padded[:-pad_len]
    return padded


def _aes_padded_size(size: int) -> int:
    return ((size + 1 + 15) // 16) * 16


def _random_wechat_uin() -> str:
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _base_info() -> dict[str, Any]:
    return {"channel_version": CHANNEL_VERSION}


def _headers(token: Optional[str], body: str) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body.encode("utf-8"))),
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _account_dir(hermes_home: str) -> Path:
    path = Path(hermes_home) / "weixin" / "accounts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _account_file(hermes_home: str, account_id: str) -> Path:
    return _account_dir(hermes_home) / f"{account_id}.json"


def save_weixin_account(
    hermes_home: str,
    *,
    account_id: str,
    token: str,
    base_url: str,
    user_id: str = "",
) -> None:
    """Persist account credentials for later reuse."""
    payload = {
        "token": token,
        "base_url": base_url,
        "user_id": user_id,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path = _account_file(hermes_home, account_id)
    atomic_json_write(path, payload)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def load_weixin_account(hermes_home: str, account_id: str) -> Optional[dict[str, Any]]:
    """Load persisted account credentials."""
    path = _account_file(hermes_home, account_id)
    if not path.exists():
        return None
    try:
        return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return None


def resolve_weixin_account_id(extra: dict[str, Any]) -> str:
    """Resolve the iLink account ID, honoring the dashboard's field names.

    The connector form and docs call this the "bot ID" (``bot_id`` /
    ``WEIXIN_BOT_ID``); the adapter historically read only ``account_id`` /
    ``WEIXIN_ACCOUNT_ID``. Both name the same iLink identifier, so accept
    either spelling — the canonical name wins when both are set.
    """
    return str(
        extra.get("account_id")
        or extra.get("bot_id")
        or os.getenv("WEIXIN_ACCOUNT_ID", "")
        or os.getenv("WEIXIN_BOT_ID", "")
    ).strip()


def resolve_weixin_token(extra: dict[str, Any], token: Optional[str] = None) -> str:
    """Resolve the iLink bot token (the dashboard's "app token" field)."""
    return str(
        token
        or extra.get("token")
        or extra.get("app_token")
        or os.getenv("WEIXIN_TOKEN", "")
        or os.getenv("WEIXIN_APP_TOKEN", "")
    ).strip()


def list_persisted_accounts(hermes_home: str) -> list[tuple[str, dict[str, Any]]]:
    """Every persisted QR login with a usable token, newest first.

    The accounts dir also holds ``*.sync.json`` poll buffers and
    ``*.context-tokens.json`` caches — those are not credentials. Ordering is
    by the payload's ``saved_at`` stamp (file mtime when absent), newest first,
    with mtime as the tie-break for logins saved within the same second.
    """
    try:
        candidates = [
            p
            for p in _account_dir(hermes_home).glob("*.json")
            if not p.name.endswith((".sync.json", ".context-tokens.json"))
        ]
    except OSError:
        return []
    accounts: list[tuple[str, float, str, dict[str, Any]]] = []
    for path in candidates:
        payload = load_weixin_account(hermes_home, path.stem)
        if not payload or not str(payload.get("token") or "").strip():
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        saved_at = str(payload.get("saved_at") or "")
        if not saved_at:
            saved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime))
        accounts.append((saved_at, mtime, path.stem, payload))
    accounts.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [(account_id, payload) for _, _, account_id, payload in accounts]


def distinct_login_owners(accounts: list[tuple[str, dict[str, Any]]]) -> set[str]:
    """Provably-different WeChat accounts among *accounts*.

    Only a **non-empty** ``ilink_user_id`` proves two logins belong to
    different WeChat accounts. iLink does not return a user id on QR confirm in
    practice, so most logins carry an empty one — those are *unknown*, never
    *distinct*: a person who re-scans (session expired, new device) mints a
    fresh iLink bot id each time with no user id to tie them together, and
    treating each as its own account is exactly what dead-ended a connector
    behind seven of that same person's logins.
    """
    return {uid for _, payload in accounts if (uid := str(payload.get("user_id") or "").strip())}


def pick_persisted_account(hermes_home: str) -> Optional[tuple[str, dict[str, Any]]]:
    """The QR login a blank connector config should ride, or ``None``.

    Lets a connector saved with empty credential fields use the account that
    ``olano connect weixin`` (or the dashboard QR modal) persisted. A
    re-scan after a session expires mints a NEW iLink bot id, so the accounts
    dir accumulates one file per login — the old "exactly one file" rule
    dead-ended the connector forever after the first re-link ("no bot token"
    while a perfectly good login sat on disk). The **newest** login wins, so a
    re-link just works. This refuses to guess (returns ``None``) only when two
    logins are *provably* different WeChat accounts — i.e. carry distinct
    non-empty ``ilink_user_id``s; then the caller must pin one via
    ``extra.bot_id``. Empty user ids never trigger that: the dashboard's QR
    flow already pins the account it just linked, so a genuine multi-account
    setup is pinned, and an un-pinned pile of one person's re-scans resolves to
    their latest login instead of blocking.
    """
    accounts = list_persisted_accounts(hermes_home)
    if not accounts:
        return None
    if len(distinct_login_owners(accounts)) > 1:
        return None
    return accounts[0]


class ContextTokenStore:
    """Disk-backed ``context_token`` cache keyed by account + peer."""

    def __init__(self, hermes_home: str):
        self._root = _account_dir(hermes_home)
        self._cache: dict[str, str] = {}

    def _path(self, account_id: str) -> Path:
        return self._root / f"{account_id}.context-tokens.json"

    def _key(self, account_id: str, user_id: str) -> str:
        return f"{account_id}:{user_id}"

    def restore(self, account_id: str) -> None:
        path = self._path(account_id)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                "weixin: failed to restore context tokens for %s: %s", _safe_id(account_id), exc
            )
            return
        restored = 0
        for user_id, token in data.items():
            if isinstance(token, str) and token:
                self._cache[self._key(account_id, user_id)] = token
                restored += 1
        if restored:
            logger.info(
                "weixin: restored %d context token(s) for %s", restored, _safe_id(account_id)
            )

    def get(self, account_id: str, user_id: str) -> Optional[str]:
        return self._cache.get(self._key(account_id, user_id))

    def set(self, account_id: str, user_id: str, token: str) -> None:
        self._cache[self._key(account_id, user_id)] = token
        self._persist(account_id)

    def _persist(self, account_id: str) -> None:
        prefix = f"{account_id}:"
        payload = {
            key[len(prefix) :]: value
            for key, value in self._cache.items()
            if key.startswith(prefix)
        }
        try:
            atomic_json_write(self._path(account_id), payload)
        except Exception as exc:
            logger.warning(
                "weixin: failed to persist context tokens for %s: %s", _safe_id(account_id), exc
            )


class TypingTicketCache:
    """Short-lived typing ticket cache from ``getconfig``."""

    def __init__(self, ttl_seconds: float = 600.0):
        self._ttl_seconds = ttl_seconds
        self._cache: dict[str, tuple[str, float]] = {}

    def get(self, user_id: str) -> Optional[str]:
        entry = self._cache.get(user_id)
        if not entry:
            return None
        if time.time() - entry[1] >= self._ttl_seconds:
            self._cache.pop(user_id, None)
            return None
        return entry[0]

    def set(self, user_id: str, ticket: str) -> None:
        self._cache[user_id] = (ticket, time.time())


def _cdn_download_url(cdn_base_url: str, encrypted_query_param: str) -> str:
    return f"{cdn_base_url.rstrip('/')}/download?encrypted_query_param={quote(encrypted_query_param, safe='')}"


def _cdn_upload_url(cdn_base_url: str, upload_param: str, filekey: str) -> str:
    return (
        f"{cdn_base_url.rstrip('/')}/upload"
        f"?encrypted_query_param={quote(upload_param, safe='')}"
        f"&filekey={quote(filekey, safe='')}"
    )


def _parse_aes_key(aes_key_b64: str) -> bytes:
    decoded = base64.b64decode(aes_key_b64)
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32:
        text = decoded.decode("ascii", errors="ignore")
        if text and all(ch in "0123456789abcdefABCDEF" for ch in text):
            return bytes.fromhex(text)
    raise ValueError(f"unexpected aes_key format ({len(decoded)} decoded bytes)")


def _guess_chat_type(message: dict[str, Any], account_id: str) -> tuple[str, str]:
    room_id = str(message.get("room_id") or message.get("chat_room_id") or "").strip()
    to_user_id = str(message.get("to_user_id") or "").strip()
    is_group = bool(room_id) or (
        to_user_id and account_id and to_user_id != account_id and message.get("msg_type") == 1
    )
    if is_group:
        return "group", room_id or to_user_id or str(message.get("from_user_id") or "")
    return "dm", str(message.get("from_user_id") or "")


async def _api_post(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    endpoint: str,
    payload: dict[str, Any],
    token: Optional[str],
    timeout_ms: int,
) -> dict[str, Any]:
    body = _json_dumps({**payload, "base_info": _base_info()})
    url = f"{base_url.rstrip('/')}/{endpoint}"
    timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
    async with session.post(
        url, data=body, headers=_headers(token, body), timeout=timeout
    ) as response:
        raw = await response.text()
        if not response.ok:
            raise RuntimeError(f"iLink POST {endpoint} HTTP {response.status}: {raw[:200]}")
        return cast(dict[str, Any], json.loads(raw))


async def _api_get(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    endpoint: str,
    timeout_ms: int,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/{endpoint}"
    headers = {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
    async with session.get(url, headers=headers, timeout=timeout) as response:
        raw = await response.text()
        if not response.ok:
            raise RuntimeError(f"iLink GET {endpoint} HTTP {response.status}: {raw[:200]}")
        return cast(dict[str, Any], json.loads(raw))


async def _get_updates(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    token: str,
    sync_buf: str,
    timeout_ms: int,
) -> dict[str, Any]:
    try:
        return await _api_post(
            session,
            base_url=base_url,
            endpoint=EP_GET_UPDATES,
            payload={"get_updates_buf": sync_buf},
            token=token,
            timeout_ms=timeout_ms,
        )
    except TimeoutError:
        return {"ret": 0, "msgs": [], "get_updates_buf": sync_buf}


async def _send_message(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    token: str,
    to: str,
    text: str,
    context_token: Optional[str],
    client_id: str,
) -> dict[str, Any]:
    """Send a text message via iLink sendmessage API.

    Returns the raw API response dict (may contain error codes like
    ``errcode: -14`` for session expiry that the caller can inspect).
    """
    if not text or not text.strip():
        raise ValueError("_send_message: text must not be empty")
    message: dict[str, Any] = {
        "from_user_id": "",
        "to_user_id": to,
        "client_id": client_id,
        "message_type": MSG_TYPE_BOT,
        "message_state": MSG_STATE_FINISH,
        "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
    }
    if context_token:
        message["context_token"] = context_token
    return await _api_post(
        session,
        base_url=base_url,
        endpoint=EP_SEND_MESSAGE,
        payload={"msg": message},
        token=token,
        timeout_ms=API_TIMEOUT_MS,
    )


async def _send_typing(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    token: str,
    to_user_id: str,
    typing_ticket: str,
    status: int,
) -> None:
    await _api_post(
        session,
        base_url=base_url,
        endpoint=EP_SEND_TYPING,
        payload={
            "ilink_user_id": to_user_id,
            "typing_ticket": typing_ticket,
            "status": status,
        },
        token=token,
        timeout_ms=CONFIG_TIMEOUT_MS,
    )


async def _get_config(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    token: str,
    user_id: str,
    context_token: Optional[str],
) -> dict[str, Any]:
    payload: dict[str, Any] = {"ilink_user_id": user_id}
    if context_token:
        payload["context_token"] = context_token
    return await _api_post(
        session,
        base_url=base_url,
        endpoint=EP_GET_CONFIG,
        payload=payload,
        token=token,
        timeout_ms=CONFIG_TIMEOUT_MS,
    )


async def _get_upload_url(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    token: str,
    to_user_id: str,
    media_type: int,
    filekey: str,
    rawsize: int,
    rawfilemd5: str,
    filesize: int,
    aeskey_hex: str,
) -> dict[str, Any]:
    return await _api_post(
        session,
        base_url=base_url,
        endpoint=EP_GET_UPLOAD_URL,
        payload={
            "filekey": filekey,
            "media_type": media_type,
            "to_user_id": to_user_id,
            "rawsize": rawsize,
            "rawfilemd5": rawfilemd5,
            "filesize": filesize,
            "no_need_thumb": True,
            "aeskey": aeskey_hex,
        },
        token=token,
        timeout_ms=API_TIMEOUT_MS,
    )


async def _upload_ciphertext(
    session: aiohttp.ClientSession,
    *,
    ciphertext: bytes,
    upload_url: str,
) -> str:
    """Upload encrypted media to the CDN.

    Accepts either a constructed CDN URL (from upload_param) or a direct
    upload_full_url — both use POST with the raw ciphertext as the body.
    """
    timeout = aiohttp.ClientTimeout(total=120)
    async with session.post(
        upload_url,
        data=ciphertext,
        headers={"Content-Type": "application/octet-stream"},
        timeout=timeout,
    ) as response:
        if response.status == 200:
            encrypted_param = response.headers.get("x-encrypted-param")
            if encrypted_param:
                await response.read()
                return encrypted_param
            raw = await response.text()
            raise RuntimeError(f"CDN upload missing x-encrypted-param header: {raw[:200]}")
        raw = await response.text()
        raise RuntimeError(f"CDN upload HTTP {response.status}: {raw[:200]}")


async def _download_bytes(
    session: aiohttp.ClientSession,
    *,
    url: str,
    timeout_seconds: float = 60.0,
) -> bytes:
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with session.get(url, timeout=timeout) as response:
        response.raise_for_status()
        return await response.read()


_WEIXIN_CDN_ALLOWLIST: frozenset[str] = frozenset(
    {
        "novac2c.cdn.weixin.qq.com",
        "ilinkai.weixin.qq.com",
        "wx.qlogo.cn",
        "thirdwx.qlogo.cn",
        "res.wx.qq.com",
        "mmbiz.qpic.cn",
        "mmbiz.qlogo.cn",
    }
)


def _assert_weixin_cdn_url(url: str) -> None:
    """Raise ValueError if *url* does not point at a known WeChat CDN host."""
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname or ""
    except Exception as exc:
        raise ValueError(f"Unparseable media URL: {url!r}") from exc

    if scheme not in ("http", "https"):
        raise ValueError(
            f"Media URL has disallowed scheme {scheme!r}; only http/https are permitted."
        )
    if host not in _WEIXIN_CDN_ALLOWLIST:
        raise ValueError(
            f"Media URL host {host!r} is not in the WeChat CDN allowlist. "
            "Refusing to fetch to prevent SSRF."
        )


def _media_reference(item: dict[str, Any], key: str) -> dict[str, Any]:
    return (item.get(key) or {}).get("media") or {}


async def _download_and_decrypt_media(
    session: aiohttp.ClientSession,
    *,
    cdn_base_url: str,
    encrypted_query_param: Optional[str],
    aes_key_b64: Optional[str],
    full_url: Optional[str],
    timeout_seconds: float,
) -> bytes:
    if encrypted_query_param:
        raw = await _download_bytes(
            session,
            url=_cdn_download_url(cdn_base_url, encrypted_query_param),
            timeout_seconds=timeout_seconds,
        )
    elif full_url:
        _assert_weixin_cdn_url(full_url)
        raw = await _download_bytes(session, url=full_url, timeout_seconds=timeout_seconds)
    else:
        raise RuntimeError("media item had neither encrypt_query_param nor full_url")
    if aes_key_b64:
        raw = _aes128_ecb_decrypt(raw, _parse_aes_key(aes_key_b64))
    return raw


def _mime_from_filename(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _split_table_row(line: str) -> list[str]:
    row = line.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [cell.strip() for cell in row.split("|")]


def _rewrite_headers_for_weixin(line: str) -> str:
    match = _HEADER_RE.match(line)
    if not match:
        return line.rstrip()
    level = len(match.group(1))
    title = match.group(2).strip()
    if level == 1:
        return f"【{title}】"
    return f"**{title}**"


def _rewrite_table_block_for_weixin(lines: list[str]) -> str:
    if len(lines) < 2:
        return "\n".join(lines)
    headers = _split_table_row(lines[0])
    body_rows = [_split_table_row(line) for line in lines[2:] if line.strip()]
    if not headers or not body_rows:
        return "\n".join(lines)

    formatted_rows: list[str] = []
    for row in body_rows:
        pairs = []
        for idx, header in enumerate(headers):
            if idx >= len(row):
                break
            label = header or f"Column {idx + 1}"
            value = row[idx].strip()
            if value:
                pairs.append((label, value))
        if not pairs:
            continue
        if len(pairs) == 1:
            label, value = pairs[0]
            formatted_rows.append(f"- {label}: {value}")
            continue
        if len(pairs) == 2:
            label, value = pairs[0]
            other_label, other_value = pairs[1]
            formatted_rows.append(f"- {label}: {value}")
            formatted_rows.append(f"  {other_label}: {other_value}")
            continue
        summary = " | ".join(f"{label}: {value}" for label, value in pairs)
        formatted_rows.append(f"- {summary}")
    return "\n".join(formatted_rows) if formatted_rows else "\n".join(lines)


def _normalize_markdown_blocks(content: str) -> str:
    lines = content.splitlines()
    result: list[str] = []
    in_code_block = False
    blank_run = 0

    for raw_line in lines:
        line = raw_line.rstrip()
        if _FENCE_RE.match(line.strip()):
            in_code_block = not in_code_block
            result.append(line)
            blank_run = 0
            continue

        if in_code_block:
            result.append(line)
            continue

        if not line.strip():
            blank_run += 1
            if blank_run <= 1:
                result.append("")
            continue

        blank_run = 0
        result.append(line)

    return "\n".join(result).strip()


def _split_markdown_blocks(content: str) -> list[str]:
    if not content:
        return []

    blocks: list[str] = []
    lines = content.splitlines()
    current: list[str] = []
    in_code_block = False

    for raw_line in lines:
        line = raw_line.rstrip()
        if _FENCE_RE.match(line.strip()):
            if not in_code_block and current:
                blocks.append("\n".join(current).strip())
                current = []
            current.append(line)
            in_code_block = not in_code_block
            if not in_code_block:
                blocks.append("\n".join(current).strip())
                current = []
            continue

        if in_code_block:
            current.append(line)
            continue

        if not line.strip():
            if current:
                blocks.append("\n".join(current).strip())
                current = []
            continue
        current.append(line)

    if current:
        blocks.append("\n".join(current).strip())
    return [block for block in blocks if block]


def _split_delivery_units_for_weixin(content: str) -> list[str]:
    """Split formatted content into chat-friendly delivery units.

    Weixin can render Markdown, but chat readability is better when top-level
    line breaks become separate messages. Keep fenced code blocks intact and
    attach indented continuation lines to the previous top-level line so nested
    list items do not get torn apart.
    """
    units: list[str] = []

    for block in _split_markdown_blocks(content):
        if _FENCE_RE.match(block.splitlines()[0].strip()):
            units.append(block)
            continue

        current: list[str] = []
        for raw_line in block.splitlines():
            line = raw_line.rstrip()
            if not line.strip():
                if current:
                    units.append("\n".join(current).strip())
                    current = []
                continue

            is_continuation = bool(current) and raw_line.startswith((" ", "\t"))
            if is_continuation:
                current.append(line)
                continue

            if current:
                units.append("\n".join(current).strip())
            current = [line]

        if current:
            units.append("\n".join(current).strip())

    return [unit for unit in units if unit]


def _looks_like_chatty_line_for_weixin(line: str) -> bool:
    """Return True when a line looks like a standalone chat utterance."""
    stripped = line.strip()
    if not stripped:
        return False
    if len(stripped) > 48:
        return False
    if line.startswith((" ", "\t")):
        return False
    if stripped.startswith((">", "-", "*", "【", "#", "|")):
        return False
    if _TABLE_RULE_RE.match(stripped):
        return False
    if re.match(r"^\*\*[^*]+\*\*$", stripped):
        return False
    return not re.match(r"^\d+\.\s", stripped)


def _looks_like_heading_line_for_weixin(line: str) -> bool:
    """Return True when a short line behaves like a heading."""
    stripped = line.strip()
    if not stripped:
        return False
    if _HEADER_RE.match(stripped):
        return True
    return len(stripped) <= 24 and stripped.endswith((":", "："))


def _should_split_short_chat_block_for_weixin(block: str) -> bool:
    """Split only chat-like multiline blocks into separate bubbles."""
    lines = [line for line in block.splitlines() if line.strip()]
    if not 2 <= len(lines) <= 6:
        return False
    if _looks_like_heading_line_for_weixin(lines[0]):
        return False
    return all(_looks_like_chatty_line_for_weixin(line) for line in lines)


def _pack_markdown_blocks_for_weixin(content: str, max_length: int) -> list[str]:
    if len(content) <= max_length:
        return [content]

    packed: list[str] = []
    current = ""
    for block in _split_markdown_blocks(content):
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= max_length:
            current = candidate
            continue
        if current:
            packed.append(current)
            current = ""
        if len(block) <= max_length:
            current = block
            continue
        packed.extend(BasePlatformAdapter.truncate_message(block, max_length))
    if current:
        packed.append(current)
    return packed


def _split_text_for_weixin_delivery(
    content: str,
    max_length: int,
    split_per_line: bool = False,
) -> list[str]:
    """Split content into sequential Weixin messages.

    *compact* (default): Keep everything in a single message whenever it fits
    within the platform limit, even when the author used explicit line breaks.
    Only fall back to block-aware packing when the payload exceeds
    ``max_length``.

    *per_line* (``split_per_line=True``): Legacy behavior — top-level line
    breaks become separate chat messages; oversized units still use
    block-aware packing.

    The active mode is controlled via ``config.yaml`` ->
    ``platforms.weixin.extra.split_multiline_messages`` (``true`` / ``false``)
    or the env var ``WEIXIN_SPLIT_MULTILINE_MESSAGES``.
    """
    if not content:
        return []
    if split_per_line:
        # Legacy: one message per top-level delivery unit.
        if len(content) <= max_length and "\n" not in content:
            return [content]
        chunks: list[str] = []
        for unit in _split_delivery_units_for_weixin(content):
            if len(unit) <= max_length:
                chunks.append(unit)
                continue
            chunks.extend(_pack_markdown_blocks_for_weixin(unit, max_length))
        return [c for c in chunks if c] or [content]

    # Compact (default): single message when under the limit — unless the
    # content looks like a short chatty exchange, in which case split into
    # separate bubbles for a more natural chat feel.
    if len(content) <= max_length:
        return (
            [u for u in _split_delivery_units_for_weixin(content) if u]
            if _should_split_short_chat_block_for_weixin(content)
            else [content]
        )
    return _pack_markdown_blocks_for_weixin(content, max_length) or [content]


def _coerce_bool(value: Any, default: bool = True) -> bool:
    """Coerce a config value to bool, tolerating strings like ``"true"``."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _extract_text(item_list: list[dict[str, Any]]) -> str:
    for item in item_list:
        if item.get("type") == ITEM_TEXT:
            text = str((item.get("text_item") or {}).get("text") or "")
            ref = item.get("ref_msg") or {}
            ref_item = ref.get("message_item") or {}
            ref_type = ref_item.get("type")
            if ref_type in (ITEM_IMAGE, ITEM_VIDEO, ITEM_FILE, ITEM_VOICE):
                title = ref.get("title") or ""
                prefix = f"[引用媒体: {title}]\n" if title else "[引用媒体]\n"
                return f"{prefix}{text}".strip()
            if ref_item:
                parts: list[str] = []
                if ref.get("title"):
                    parts.append(str(ref["title"]))
                ref_text = _extract_text([ref_item])
                if ref_text:
                    parts.append(ref_text)
                if parts:
                    return f"[引用: {' | '.join(parts)}]\n{text}".strip()
            return text
    for item in item_list:
        if item.get("type") == ITEM_VOICE:
            voice_text = str((item.get("voice_item") or {}).get("text") or "")
            if voice_text:
                return voice_text
    return ""


def _message_type_from_media(
    media_types: list[str], text: str, saw_voice: bool = False
) -> MessageType:
    """Classify an inbound message from what it carried.

    ``saw_voice`` is tracked separately from ``media_types`` on purpose: when
    WeChat has already transcribed a voice note it hands us the text and we
    skip the audio download entirely (:meth:`WeixinAdapter._download_voice`),
    so there is no ``audio/*`` entry to classify from. Without this flag such a
    message looked like plain TEXT, and every downstream consumer of
    voice-ness — above all the voice-in/voice-out auto-TTS reply in
    ``platforms/base.py`` — silently never fired.
    """
    if any(m.startswith("image/") for m in media_types):
        return MessageType.PHOTO
    if any(m.startswith("video/") for m in media_types):
        return MessageType.VIDEO
    if any(m.startswith("audio/") for m in media_types) or saw_voice:
        return MessageType.VOICE
    if media_types:
        return MessageType.DOCUMENT
    if text.startswith("/"):
        return MessageType.COMMAND
    return MessageType.TEXT


def _contains_voice_item(item_list: list[dict[str, Any]]) -> bool:
    """True when the message (or the message it quotes) carries a voice note."""
    for item in item_list:
        if item.get("type") == ITEM_VOICE:
            return True
        ref_item = (item.get("ref_msg") or {}).get("message_item")
        if isinstance(ref_item, dict) and ref_item.get("type") == ITEM_VOICE:
            return True
    return False


def _sync_buf_path(hermes_home: str, account_id: str) -> Path:
    return _account_dir(hermes_home) / f"{account_id}.sync.json"


def _load_sync_buf(hermes_home: str, account_id: str) -> str:
    path = _sync_buf_path(hermes_home, account_id)
    if not path.exists():
        return ""
    try:
        return cast(str, json.loads(path.read_text(encoding="utf-8")).get("get_updates_buf", ""))
    except Exception:
        return ""


def _save_sync_buf(hermes_home: str, account_id: str, sync_buf: str) -> None:
    path = _sync_buf_path(hermes_home, account_id)
    atomic_json_write(path, {"get_updates_buf": sync_buf})


async def qr_login_events(
    hermes_home: str,
    *,
    bot_type: str = "3",
    timeout_seconds: int = 480,
):
    """Drive the iLink QR login flow, yielding structured events.

    The shared core behind the ``olano connect weixin`` CLI and the
    dashboard's ``/api/connectors/weixin/pair`` SSE endpoint. Events:

    - ``{"event": "qr", "qr": <scan data>, "refresh": n}`` — a (re)issued QR;
      ``qr`` is the full liteapp URL WeChat must scan (falls back to the hex
      token when the server omits the URL).
    - ``{"event": "status", "status": "wait" | "scanned"}`` — poll updates,
      roughly once per second (``wait`` doubles as a keepalive).
    - ``{"event": "confirmed", "account_id", "token", "base_url", "user_id"}``
      — credentials minted and already persisted via ``save_weixin_account``.
      Consumers streaming to a browser must NOT forward ``token``.
    - ``{"event": "error", "reason": ...}`` — terminal failure or timeout.

    The generator always ends after a ``confirmed`` or ``error`` event.
    """
    if not AIOHTTP_AVAILABLE:
        raise RuntimeError("aiohttp is required for Weixin QR login")

    async with aiohttp.ClientSession(trust_env=True, connector=_make_ssl_connector()) as session:
        try:
            qr_resp = await _api_get(
                session,
                base_url=ILINK_BASE_URL,
                endpoint=f"{EP_GET_BOT_QR}?bot_type={bot_type}",
                timeout_ms=QR_TIMEOUT_MS,
            )
        except Exception as exc:
            logger.error("weixin: failed to fetch QR code: %s", exc)
            yield {"event": "error", "reason": f"Failed to fetch the QR code: {exc}"}
            return

        qrcode_value = str(qr_resp.get("qrcode") or "")
        qrcode_url = str(qr_resp.get("qrcode_img_content") or "")
        if not qrcode_value:
            logger.error("weixin: QR response missing qrcode")
            yield {"event": "error", "reason": "QR response did not include a code."}
            return

        # qrcode_url is the full scannable liteapp URL; qrcode_value is just the
        # hex token. WeChat needs to scan the full URL, not the raw hex string.
        yield {"event": "qr", "qr": qrcode_url or qrcode_value, "refresh": 0}

        deadline = time.time() + timeout_seconds
        current_base_url = ILINK_BASE_URL
        refresh_count = 0

        while time.time() < deadline:
            try:
                status_resp = await _api_get(
                    session,
                    base_url=current_base_url,
                    endpoint=f"{EP_GET_QR_STATUS}?qrcode={qrcode_value}",
                    timeout_ms=QR_TIMEOUT_MS,
                )
            except TimeoutError:
                await asyncio.sleep(1)
                continue
            except Exception as exc:
                logger.warning("weixin: QR poll error: %s", exc)
                await asyncio.sleep(1)
                continue

            status = str(status_resp.get("status") or "wait")
            if status == "wait":
                yield {"event": "status", "status": "wait"}
            elif status == "scaned":
                yield {"event": "status", "status": "scanned"}
            elif status == "scaned_but_redirect":
                redirect_host = str(status_resp.get("redirect_host") or "")
                if redirect_host:
                    current_base_url = f"https://{redirect_host}"
            elif status == "expired":
                refresh_count += 1
                if refresh_count > 3:
                    yield {
                        "event": "error",
                        "reason": "The QR code expired repeatedly. Start the login again.",
                    }
                    return
                try:
                    qr_resp = await _api_get(
                        session,
                        base_url=ILINK_BASE_URL,
                        endpoint=f"{EP_GET_BOT_QR}?bot_type={bot_type}",
                        timeout_ms=QR_TIMEOUT_MS,
                    )
                    qrcode_value = str(qr_resp.get("qrcode") or "")
                    qrcode_url = str(qr_resp.get("qrcode_img_content") or "")
                    yield {
                        "event": "qr",
                        "qr": qrcode_url or qrcode_value,
                        "refresh": refresh_count,
                    }
                except Exception as exc:
                    logger.error("weixin: QR refresh failed: %s", exc)
                    yield {"event": "error", "reason": f"Refreshing the QR code failed: {exc}"}
                    return
            elif status == "confirmed":
                account_id = str(status_resp.get("ilink_bot_id") or "")
                token = str(status_resp.get("bot_token") or "")
                base_url = str(status_resp.get("baseurl") or ILINK_BASE_URL)
                user_id = str(status_resp.get("ilink_user_id") or "")
                if not account_id or not token:
                    logger.error("weixin: QR confirmed but credential payload was incomplete")
                    yield {
                        "event": "error",
                        "reason": "Login confirmed but the credential payload was incomplete.",
                    }
                    return
                save_weixin_account(
                    hermes_home,
                    account_id=account_id,
                    token=token,
                    base_url=base_url,
                    user_id=user_id,
                )
                yield {
                    "event": "confirmed",
                    "account_id": account_id,
                    "token": token,
                    "base_url": base_url,
                    "user_id": user_id,
                }
                return
            await asyncio.sleep(1)

        yield {"event": "error", "reason": f"Timed out after {timeout_seconds}s without a scan."}


def _print_terminal_qr(scan_data: str) -> None:
    print(scan_data)
    try:
        import qrcode

        qr = qrcode.QRCode()
        qr.add_data(scan_data)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception as _qr_exc:
        print(f"（终端二维码渲染失败: {_qr_exc}，请直接打开上面的二维码链接）")


async def qr_login(
    hermes_home: str,
    *,
    bot_type: str = "3",
    timeout_seconds: int = 480,
) -> Optional[dict[str, str]]:
    """
    Run the interactive (terminal) iLink QR login flow.

    Thin presentation layer over ``qr_login_events``. Returns a credential
    dict on success, or ``None`` if login fails or times out.
    """
    async for event in qr_login_events(
        hermes_home, bot_type=bot_type, timeout_seconds=timeout_seconds
    ):
        kind = event.get("event")
        if kind == "qr":
            if event.get("refresh"):
                print(f"\n二维码已过期，正在刷新... ({event['refresh']}/3)")
            else:
                print("\n请使用微信扫描以下二维码：")
            _print_terminal_qr(str(event.get("qr") or ""))
        elif kind == "status":
            if event.get("status") == "scanned":
                print("\n已扫码，请在微信里确认...")
            else:
                print(".", end="", flush=True)
        elif kind == "confirmed":
            print(f"\n微信连接成功，account_id={event['account_id']}")
            return {
                "account_id": str(event["account_id"]),
                "token": str(event["token"]),
                "base_url": str(event["base_url"]),
                "user_id": str(event.get("user_id") or ""),
            }
        elif kind == "error":
            print(f"\n微信登录失败：{event.get('reason')}")
            return None
    return None


class WeixinAdapter(BasePlatformAdapter):
    """Native Olano adapter for Weixin personal accounts."""

    MAX_MESSAGE_LENGTH = 4000

    # WeChat does not support editing sent messages — streaming must use the
    # fallback "send-final-only" path so the cursor (▉) is never left visible.
    SUPPORTS_MESSAGE_EDITING = False

    # No edits — progress is appended as occasional messages instead.
    PROGRESS_MODE = "append"

    def __init__(self, config: PlatformConfig, agent_name: str = ""):
        super().__init__(config, Platform.WEIXIN, agent_name=agent_name)
        extra = config.extra or {}
        hermes_home = str(get_olano_home())
        self._hermes_home = hermes_home
        self._token_store = ContextTokenStore(hermes_home)
        self._typing_cache = TypingTicketCache()
        self._poll_session: Optional[aiohttp.ClientSession] = None
        self._send_session: Optional[aiohttp.ClientSession] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._dedup = MessageDeduplicator(ttl_seconds=MESSAGE_DEDUP_TTL_SECONDS)

        self._account_id = resolve_weixin_account_id(extra)
        self._token = resolve_weixin_token(extra, config.token)
        self._base_url = (
            str(extra.get("base_url") or os.getenv("WEIXIN_BASE_URL", ILINK_BASE_URL))
            .strip()
            .rstrip("/")
        )
        self._cdn_base_url = (
            str(extra.get("cdn_base_url") or os.getenv("WEIXIN_CDN_BASE_URL", WEIXIN_CDN_BASE_URL))
            .strip()
            .rstrip("/")
        )
        self._send_chunk_delay_seconds = float(
            str(
                extra.get("send_chunk_delay_seconds")
                or os.getenv("WEIXIN_SEND_CHUNK_DELAY_SECONDS", "0.35")
            )
        )
        self._send_chunk_retries = int(
            str(extra.get("send_chunk_retries") or os.getenv("WEIXIN_SEND_CHUNK_RETRIES", "2"))
        )
        self._send_chunk_retry_delay_seconds = float(
            str(
                extra.get("send_chunk_retry_delay_seconds")
                or os.getenv("WEIXIN_SEND_CHUNK_RETRY_DELAY_SECONDS", "1.0")
            )
        )
        self._dm_policy = (
            str(extra.get("dm_policy") or os.getenv("WEIXIN_DM_POLICY", "open")).strip().lower()
        )
        self._group_policy = (
            str(extra.get("group_policy") or os.getenv("WEIXIN_GROUP_POLICY", "disabled"))
            .strip()
            .lower()
        )
        allow_from = extra.get("allow_from")
        if allow_from is None:
            allow_from = os.getenv("WEIXIN_ALLOWED_USERS", "")
        group_allow_from = extra.get("group_allow_from")
        if group_allow_from is None:
            group_allow_from = os.getenv("WEIXIN_GROUP_ALLOWED_USERS", "")
        self._allow_from = self._coerce_list(allow_from)
        self._group_allow_from = self._coerce_list(group_allow_from)
        self._split_multiline_messages = _coerce_bool(
            extra.get("split_multiline_messages") or os.getenv("WEIXIN_SPLIT_MULTILINE_MESSAGES"),
            default=False,
        )

        # When credential resolution dead-ends below, this carries the exact
        # (code, message) `connect()` should fail with — the generic "no bot
        # token, scan the QR" advice is wrong (and self-defeating: each scan
        # mints another login) once saved logins exist on this server.
        self._startup_problem: Optional[tuple[str, str]] = None

        if self._account_id and not self._token:
            persisted = load_weixin_account(hermes_home, self._account_id)
            if persisted:
                self._token = str(persisted.get("token") or "").strip()
                self._base_url = (
                    str(persisted.get("base_url") or self._base_url).strip().rstrip("/")
                )
            else:
                linked = [aid for aid, _ in list_persisted_accounts(hermes_home)]
                if linked:
                    self._startup_problem = (
                        "weixin_unknown_account",
                        f"Weixin startup failed: no saved login for bot ID "
                        f"'{self._account_id}'. Linked account(s) on this server: "
                        f"{', '.join(linked)}. Fix extra.bot_id / WEIXIN_BOT_ID (or clear "
                        "it to use the linked account), or re-link with "
                        "`olano connect weixin`.",
                    )
        elif not self._account_id and not self._token:
            # Nothing configured at all — fall back to the account the
            # `olano connect weixin` QR flow persisted, so the dashboard
            # connector works with its credential fields left blank.
            fallback = pick_persisted_account(hermes_home)
            if fallback:
                self._account_id, persisted = fallback
                self._token = str(persisted.get("token") or "").strip()
                self._base_url = (
                    str(persisted.get("base_url") or self._base_url).strip().rstrip("/")
                )
                logger.info(
                    "[%s] weixin: using persisted QR-login account %s",
                    self.name,
                    _safe_id(self._account_id),
                )
            else:
                # pick_persisted_account only returns None here when two logins
                # are provably different WeChat accounts (distinct non-empty
                # user ids) — a pile of one person's re-scans resolves to the
                # newest instead. So this branch means a genuine multi-account
                # server with nothing pinned.
                persisted_accounts = list_persisted_accounts(hermes_home)
                linked = [aid for aid, _ in persisted_accounts]
                if len(distinct_login_owners(persisted_accounts)) > 1:
                    self._startup_problem = (
                        "weixin_ambiguous_account",
                        f"Weixin startup failed: {len(linked)} different WeChat logins "
                        f"are linked to this server ({', '.join(linked)}) and none is "
                        "pinned. Set extra.bot_id / WEIXIN_BOT_ID to the account this "
                        "agent should use — re-scanning the QR only adds another login.",
                    )

    @staticmethod
    def _coerce_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, list | tuple | set):
            return [str(item).strip() for item in value if str(item).strip()]
        return [str(value).strip()] if str(value).strip() else []

    async def connect(self) -> bool:
        if not check_weixin_requirements():
            message = "Weixin startup failed: aiohttp and cryptography are required"
            self._set_fatal_error("weixin_missing_dependency", message, retryable=False)
            logger.warning("[%s] %s", self.name, message)
            return False
        if not self._token:
            if self._startup_problem:
                code, message = self._startup_problem
            else:
                code = "weixin_missing_token"
                message = (
                    "Weixin startup failed: no bot token. Run `olano connect weixin` "
                    "and scan the QR with WeChat, or set WEIXIN_TOKEN / extra.app_token."
                )
            self._set_fatal_error(code, message, retryable=False)
            logger.warning("[%s] %s", self.name, message)
            return False
        if not self._account_id:
            message = (
                "Weixin startup failed: no account ID. Run `olano connect weixin` "
                "and scan the QR with WeChat, or set WEIXIN_ACCOUNT_ID / extra.bot_id."
            )
            self._set_fatal_error("weixin_missing_account", message, retryable=False)
            logger.warning("[%s] %s", self.name, message)
            return False

        # One WeChat login per agent. The machine-local platform lock below only
        # guards across PROCESSES (a same-pid re-acquire is deliberately granted
        # so an agent reboot never deadlocks on its own record), so two agents
        # in one runtime resolving to the same token would both come up — and
        # each getupdates poll consumes from the same queue, so messages split
        # between them at random. Refuse loudly instead. The same agent
        # re-connecting (hot swap, reload) replaces its own registration.
        prior = _LIVE_ADAPTERS.get(self._token)
        if (
            prior is not None
            and prior is not self
            and getattr(prior, "_running", False)
            and getattr(prior, "agent_name", "") != (self.agent_name or "")
        ):
            prior_agent = getattr(prior, "agent_name", "") or "another agent"
            message = (
                f"Weixin startup failed: WeChat login '{self._account_id}' is already "
                f"connected by agent '{prior_agent}'. Two agents cannot share one login "
                "(each would receive only part of the messages). Link a separate WeChat "
                "account for this agent from its connector form, or pin extra.bot_id / "
                "WEIXIN_BOT_ID to a different linked login."
            )
            self._set_fatal_error("weixin_login_in_use", message, retryable=False)
            logger.warning("[%s] %s", self.name, message)
            return False

        try:
            if not self._acquire_platform_lock("weixin-bot-token", self._token, "Weixin bot token"):
                return False
        except Exception as exc:
            logger.debug("[%s] Token lock unavailable (non-fatal): %s", self.name, exc)

        self._poll_session = aiohttp.ClientSession(trust_env=True, connector=_make_ssl_connector())
        self._send_session = aiohttp.ClientSession(trust_env=True, connector=_make_ssl_connector())
        self._token_store.restore(self._account_id)
        self._poll_task = asyncio.create_task(self._poll_loop(), name="weixin-poll")
        self._mark_connected()
        _LIVE_ADAPTERS[self._token] = self
        logger.info(
            "[%s] Connected account=%s base=%s",
            self.name,
            _safe_id(self._account_id),
            self._base_url,
        )
        return True

    async def disconnect(self) -> None:
        _LIVE_ADAPTERS.pop(self._token, None)
        self._running = False
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        self._poll_task = None
        if self._poll_session and not self._poll_session.closed:
            await self._poll_session.close()
        self._poll_session = None
        if self._send_session and not self._send_session.closed:
            await self._send_session.close()
        self._send_session = None
        self._release_platform_lock()
        self._mark_disconnected()
        logger.info("[%s] Disconnected", self.name)

    async def _poll_loop(self) -> None:
        assert self._poll_session is not None
        sync_buf = _load_sync_buf(self._hermes_home, self._account_id)
        timeout_ms = LONG_POLL_TIMEOUT_MS
        consecutive_failures = 0

        while self._running:
            try:
                response = await _get_updates(
                    self._poll_session,
                    base_url=self._base_url,
                    token=self._token,
                    sync_buf=sync_buf,
                    timeout_ms=timeout_ms,
                )
                suggested_timeout = response.get("longpolling_timeout_ms")
                if isinstance(suggested_timeout, int) and suggested_timeout > 0:
                    timeout_ms = suggested_timeout

                ret = response.get("ret", 0)
                errcode = response.get("errcode", 0)
                if ret not in (0, None) or errcode not in (0, None):
                    if ret == SESSION_EXPIRED_ERRCODE or errcode == SESSION_EXPIRED_ERRCODE:
                        logger.error("[%s] Session expired; pausing for 10 minutes", self.name)
                        await asyncio.sleep(600)
                        consecutive_failures = 0
                        continue
                    consecutive_failures += 1
                    logger.warning(
                        "[%s] getUpdates failed ret=%s errcode=%s errmsg=%s (%d/%d)",
                        self.name,
                        ret,
                        errcode,
                        response.get("errmsg", ""),
                        consecutive_failures,
                        MAX_CONSECUTIVE_FAILURES,
                    )
                    await asyncio.sleep(
                        BACKOFF_DELAY_SECONDS
                        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES
                        else RETRY_DELAY_SECONDS
                    )
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        consecutive_failures = 0
                    continue

                consecutive_failures = 0
                new_sync_buf = str(response.get("get_updates_buf") or "")
                if new_sync_buf:
                    sync_buf = new_sync_buf
                    _save_sync_buf(self._hermes_home, self._account_id, sync_buf)

                for message in response.get("msgs") or []:
                    asyncio.create_task(self._process_message_safe(message))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                consecutive_failures += 1
                # A long poll that times out is how a *quiet* chat looks, not a
                # fault: nothing arrived within the window and the edge closed
                # the connection (Cloudflare 524 in front of iLink, or our own
                # client deadline). It still counts toward the backoff, so a
                # genuinely dead endpoint is throttled — but logging it at ERROR
                # painted normal idle traffic red in the dashboard log.
                logger.log(
                    logging.INFO if _is_poll_timeout(exc) else logging.ERROR,
                    "[%s] poll %s (%d/%d): %s",
                    self.name,
                    "timed out" if _is_poll_timeout(exc) else "error",
                    consecutive_failures,
                    MAX_CONSECUTIVE_FAILURES,
                    exc,
                )
                await asyncio.sleep(
                    BACKOFF_DELAY_SECONDS
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES
                    else RETRY_DELAY_SECONDS
                )
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    consecutive_failures = 0

    async def _process_message_safe(self, message: dict[str, Any]) -> None:
        try:
            await self._process_message(message)
        except Exception as exc:
            logger.error(
                "[%s] unhandled inbound error from=%s: %s",
                self.name,
                _safe_id(message.get("from_user_id")),
                exc,
                exc_info=True,
            )

    async def _process_message(self, message: dict[str, Any]) -> None:
        assert self._poll_session is not None
        sender_id = str(message.get("from_user_id") or "").strip()
        if not sender_id:
            return
        if sender_id == self._account_id:
            return

        message_id = str(message.get("message_id") or "").strip()
        if message_id and self._dedup.is_duplicate(message_id):
            return

        chat_type, effective_chat_id = _guess_chat_type(message, self._account_id)
        if chat_type == "group":
            if self._group_policy == "disabled":
                return
            if (
                self._group_policy == "allowlist"
                and effective_chat_id not in self._group_allow_from
            ):
                return
        elif not self._is_dm_allowed(sender_id):
            return

        context_token = str(message.get("context_token") or "").strip()
        if context_token:
            self._token_store.set(self._account_id, sender_id, context_token)
        asyncio.create_task(self._maybe_fetch_typing_ticket(sender_id, context_token or None))

        item_list = message.get("item_list") or []
        text = _extract_text(item_list)
        media_paths: list[str] = []
        media_types: list[str] = []

        for item in item_list:
            await self._collect_media(item, media_paths, media_types)
            ref_message = item.get("ref_msg") or {}
            ref_item = ref_message.get("message_item")
            if isinstance(ref_item, dict):
                await self._collect_media(ref_item, media_paths, media_types)

        if not text and not media_paths:
            return

        source = self.build_source(
            chat_id=effective_chat_id,
            chat_type=chat_type,
            user_id=sender_id,
            user_name=sender_id,
        )
        event = MessageEvent(
            text=text,
            message_type=_message_type_from_media(
                media_types, text, saw_voice=_contains_voice_item(item_list)
            ),
            source=source,
            raw_message=message,
            message_id=message_id or None,
            media_urls=media_paths,
            media_types=media_types,
            timestamp=datetime.now(),
        )
        logger.info(
            "[%s] inbound from=%s type=%s media=%d",
            self.name,
            _safe_id(sender_id),
            source.chat_type,
            len(media_paths),
        )
        await self.handle_message(event)

    def _is_dm_allowed(self, sender_id: str) -> bool:
        if self._dm_policy == "disabled":
            return False
        if self._dm_policy == "allowlist":
            return sender_id in self._allow_from
        return True

    async def _collect_media(
        self, item: dict[str, Any], media_paths: list[str], media_types: list[str]
    ) -> None:
        item_type = item.get("type")
        if item_type == ITEM_IMAGE:
            path = await self._download_image(item)
            if path:
                media_paths.append(path)
                media_types.append("image/jpeg")
        elif item_type == ITEM_VIDEO:
            path = await self._download_video(item)
            if path:
                media_paths.append(path)
                media_types.append("video/mp4")
        elif item_type == ITEM_FILE:
            path, mime = await self._download_file(item)
            if path:
                media_paths.append(path)
                media_types.append(mime)
        elif item_type == ITEM_VOICE:
            voice_path = await self._download_voice(item)
            if voice_path:
                media_paths.append(voice_path)
                media_types.append("audio/silk")

    async def _download_image(self, item: dict[str, Any]) -> Optional[str]:
        media = _media_reference(item, "image_item")
        try:
            assert self._poll_session is not None
            data = await _download_and_decrypt_media(
                self._poll_session,
                cdn_base_url=self._cdn_base_url,
                encrypted_query_param=media.get("encrypt_query_param"),
                aes_key_b64=(
                    (item.get("image_item") or {}).get("aeskey")
                    and base64.b64encode(
                        bytes.fromhex(str((item.get("image_item") or {}).get("aeskey")))
                    ).decode("ascii")
                )
                or media.get("aes_key"),
                full_url=media.get("full_url"),
                timeout_seconds=30.0,
            )
            return cache_image_from_bytes(data, ".jpg")
        except Exception as exc:
            logger.warning("[%s] image download failed: %s", self.name, exc)
            return None

    async def _download_video(self, item: dict[str, Any]) -> Optional[str]:
        media = _media_reference(item, "video_item")
        try:
            assert self._poll_session is not None
            data = await _download_and_decrypt_media(
                self._poll_session,
                cdn_base_url=self._cdn_base_url,
                encrypted_query_param=media.get("encrypt_query_param"),
                aes_key_b64=media.get("aes_key"),
                full_url=media.get("full_url"),
                timeout_seconds=120.0,
            )
            return cache_document_from_bytes(data, "video.mp4")
        except Exception as exc:
            logger.warning("[%s] video download failed: %s", self.name, exc)
            return None

    async def _download_file(self, item: dict[str, Any]) -> tuple[Optional[str], str]:
        file_item = item.get("file_item") or {}
        media = file_item.get("media") or {}
        filename = str(file_item.get("file_name") or "document.bin")
        mime = _mime_from_filename(filename)
        try:
            assert self._poll_session is not None
            data = await _download_and_decrypt_media(
                self._poll_session,
                cdn_base_url=self._cdn_base_url,
                encrypted_query_param=media.get("encrypt_query_param"),
                aes_key_b64=media.get("aes_key"),
                full_url=media.get("full_url"),
                timeout_seconds=60.0,
            )
            return cache_document_from_bytes(data, filename), mime
        except Exception as exc:
            logger.warning("[%s] file download failed: %s", self.name, exc)
            return None, mime

    async def _download_voice(self, item: dict[str, Any]) -> Optional[str]:
        voice_item = item.get("voice_item") or {}
        media = voice_item.get("media") or {}
        if voice_item.get("text"):
            return None
        try:
            assert self._poll_session is not None
            data = await _download_and_decrypt_media(
                self._poll_session,
                cdn_base_url=self._cdn_base_url,
                encrypted_query_param=media.get("encrypt_query_param"),
                aes_key_b64=media.get("aes_key"),
                full_url=media.get("full_url"),
                timeout_seconds=60.0,
            )
            return cache_audio_from_bytes(data, ".silk")
        except Exception as exc:
            logger.warning("[%s] voice download failed: %s", self.name, exc)
            return None

    async def _maybe_fetch_typing_ticket(self, user_id: str, context_token: Optional[str]) -> None:
        if not self._poll_session or not self._token:
            return
        if self._typing_cache.get(user_id):
            return
        try:
            response = await _get_config(
                self._poll_session,
                base_url=self._base_url,
                token=self._token,
                user_id=user_id,
                context_token=context_token,
            )
            typing_ticket = str(response.get("typing_ticket") or "")
            if typing_ticket:
                self._typing_cache.set(user_id, typing_ticket)
        except Exception as exc:
            logger.debug("[%s] getConfig failed for %s: %s", self.name, _safe_id(user_id), exc)

    def _split_text(self, content: str) -> list[str]:
        return _split_text_for_weixin_delivery(
            content,
            self.MAX_MESSAGE_LENGTH,
            self._split_multiline_messages,
        )

    async def _send_text_chunk(
        self,
        *,
        chat_id: str,
        chunk: str,
        context_token: Optional[str],
        client_id: str,
    ) -> None:
        """Send a single text chunk with per-chunk retry and backoff.

        On session-expired errors (errcode -14), automatically retries
        *without* ``context_token`` — iLink accepts tokenless sends as a
        degraded fallback, which keeps cron-initiated push messages working
        even when no user message has refreshed the session recently.
        """
        last_error: Optional[Exception] = None
        retried_without_token = False
        assert self._send_session is not None
        for attempt in range(self._send_chunk_retries + 1):
            try:
                resp = await _send_message(
                    self._send_session,
                    base_url=self._base_url,
                    token=self._token,
                    to=chat_id,
                    text=chunk,
                    context_token=context_token,
                    client_id=client_id,
                )
                # Check iLink response for session-expired error
                if resp and isinstance(resp, dict):
                    ret = resp.get("ret")
                    errcode = resp.get("errcode")
                    if (ret is not None and ret not in (0,)) or (
                        errcode is not None and errcode not in (0,)
                    ):
                        is_session_expired = (
                            ret == SESSION_EXPIRED_ERRCODE or errcode == SESSION_EXPIRED_ERRCODE
                        )
                        # Session expired — strip token and retry once
                        if is_session_expired and not retried_without_token and context_token:
                            retried_without_token = True
                            context_token = None
                            self._token_store._cache.pop(
                                self._token_store._key(self._account_id, chat_id), None
                            )
                            logger.warning(
                                "[%s] session expired for %s; retrying without context_token",
                                self.name,
                                _safe_id(chat_id),
                            )
                            continue
                        errmsg = resp.get("errmsg") or resp.get("msg") or "unknown error"
                        raise RuntimeError(
                            f"iLink sendmessage error: ret={ret} errcode={errcode} errmsg={errmsg}"
                        )
                return
            except Exception as exc:
                last_error = exc
                if attempt >= self._send_chunk_retries:
                    break
                wait = self._send_chunk_retry_delay_seconds * (attempt + 1)
                logger.warning(
                    "[%s] send chunk failed to=%s attempt=%d/%d, retrying in %.2fs: %s",
                    self.name,
                    _safe_id(chat_id),
                    attempt + 1,
                    self._send_chunk_retries + 1,
                    wait,
                    exc,
                )
                if wait > 0:
                    await asyncio.sleep(wait)
        assert last_error is not None
        raise last_error

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        if not self._send_session or not self._token:
            return SendResult(success=False, error="Not connected")
        context_token = self._token_store.get(self._account_id, chat_id)
        last_message_id: Optional[str] = None

        # Extract MEDIA: tags and bare local file paths before text delivery.
        media_files, cleaned_content = self.extract_media(content)
        _, image_cleaned = self.extract_images(cleaned_content)
        local_files, final_content = self.extract_local_files(image_cleaned)

        _AUDIO_EXTS = {".ogg", ".opus", ".mp3", ".wav", ".m4a"}
        _VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}
        _IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

        async def _deliver_media(path: str, is_voice: bool = False) -> None:
            ext = Path(path).suffix.lower()
            if is_voice or ext in _AUDIO_EXTS:
                # Forward the [[audio_as_voice]] preference rather than dropping
                # it — WeChat can only ever attach a non-SILK clip, but the flag
                # is the same contract every other adapter reads.
                await self.send_voice(
                    chat_id=chat_id, audio_path=path, as_voice=is_voice, metadata=metadata
                )
            elif ext in _VIDEO_EXTS:
                await self.send_video(chat_id=chat_id, video_path=path, metadata=metadata)
            elif ext in _IMAGE_EXTS:
                await self.send_image_file(chat_id=chat_id, image_path=path, metadata=metadata)
            else:
                await self.send_document(chat_id=chat_id, file_path=path, metadata=metadata)

        try:
            # Deliver extracted MEDIA: attachments first.
            for media_path, is_voice in media_files:
                try:
                    await _deliver_media(media_path, is_voice)
                except Exception as exc:
                    logger.warning(
                        "[%s] media delivery failed for %s: %s", self.name, media_path, exc
                    )

            # Deliver bare local file paths.
            for file_path in local_files:
                try:
                    await _deliver_media(file_path, is_voice=False)
                except Exception as exc:
                    logger.warning(
                        "[%s] local file delivery failed for %s: %s", self.name, file_path, exc
                    )

            # Deliver text content.
            chunks = [
                c for c in self._split_text(self.format_message(final_content)) if c and c.strip()
            ]
            for idx, chunk in enumerate(chunks):
                client_id = f"olano-weixin-{uuid.uuid4().hex}"
                await self._send_text_chunk(
                    chat_id=chat_id,
                    chunk=chunk,
                    context_token=context_token,
                    client_id=client_id,
                )
                last_message_id = client_id
                if idx < len(chunks) - 1 and self._send_chunk_delay_seconds > 0:
                    await asyncio.sleep(self._send_chunk_delay_seconds)
            return SendResult(success=True, message_id=last_message_id)
        except Exception as exc:
            logger.error("[%s] send failed to=%s: %s", self.name, _safe_id(chat_id), exc)
            return SendResult(success=False, error=str(exc))

    async def send_typing(self, chat_id: str, metadata: Optional[dict[str, Any]] = None) -> None:
        if not self._send_session or not self._token:
            return
        typing_ticket = self._typing_cache.get(chat_id)
        if not typing_ticket:
            return
        try:
            await _send_typing(
                self._send_session,
                base_url=self._base_url,
                token=self._token,
                to_user_id=chat_id,
                typing_ticket=typing_ticket,
                status=TYPING_START,
            )
        except Exception as exc:
            logger.debug("[%s] typing start failed for %s: %s", self.name, _safe_id(chat_id), exc)

    async def stop_typing(self, chat_id: str) -> None:
        if not self._send_session or not self._token:
            return
        typing_ticket = self._typing_cache.get(chat_id)
        if not typing_ticket:
            return
        try:
            await _send_typing(
                self._send_session,
                base_url=self._base_url,
                token=self._token,
                to_user_id=chat_id,
                typing_ticket=typing_ticket,
                status=TYPING_STOP,
            )
        except Exception as exc:
            logger.debug("[%s] typing stop failed for %s: %s", self.name, _safe_id(chat_id), exc)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        if image_url.startswith(("http://", "https://")):
            file_path = await self._download_remote_media(image_url)
            cleanup = True
        else:
            file_path = image_url.replace("file://", "")
            if not os.path.isabs(file_path):
                file_path = os.path.abspath(file_path)
            cleanup = False
        try:
            return await self.send_document(chat_id, file_path, caption=caption, metadata=metadata)
        finally:
            if cleanup and file_path and os.path.exists(file_path):
                try:
                    os.unlink(file_path)
                except OSError:
                    pass

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        del reply_to, kwargs
        return await self.send_document(
            chat_id=chat_id,
            file_path=image_path,
            caption=caption,
            metadata=metadata,
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        del file_name, reply_to, metadata, kwargs
        if not self._send_session or not self._token:
            return SendResult(success=False, error="Not connected")
        try:
            message_id = await self._send_file(chat_id, file_path, caption or "")
            return SendResult(success=True, message_id=message_id)
        except Exception as exc:
            logger.error("[%s] send_document failed to=%s: %s", self.name, _safe_id(chat_id), exc)
            return SendResult(success=False, error=str(exc))

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        if not self._send_session or not self._token:
            return SendResult(success=False, error="Not connected")
        try:
            message_id = await self._send_file(chat_id, video_path, caption or "")
            return SendResult(success=True, message_id=message_id)
        except Exception as exc:
            logger.error("[%s] send_video failed to=%s: %s", self.name, _safe_id(chat_id), exc)
            return SendResult(success=False, error=str(exc))

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        if not self._send_session or not self._token:
            return SendResult(success=False, error="Not connected")

        # WeChat voice bubbles are SILK-encoded, and nothing in olano encodes
        # SILK — a TTS reply arrives here as Ogg/mp3/wav. So the split is by
        # container, and the attachment path is a *conscious* fallback rather
        # than the accident it used to be (this method forced an attachment
        # even for a .silk file it could have sent natively):
        #   * .silk  → native voice item, the real bubble
        #   * others → file attachment, playable in-chat, clearly labelled
        native_voice = audio_path.endswith(".silk")
        fallback_caption = caption or (None if native_voice else "[voice message as attachment]")
        try:
            message_id = await self._send_file(
                chat_id,
                audio_path,
                fallback_caption or "",
                force_file_attachment=not native_voice,
            )
            return SendResult(success=True, message_id=message_id)
        except Exception as exc:
            logger.error("[%s] send_voice failed to=%s: %s", self.name, _safe_id(chat_id), exc)
            return SendResult(success=False, error=str(exc))

    async def _download_remote_media(self, url: str) -> str:
        from olano.platforms.url_safety import is_safe_url

        if not is_safe_url(url):
            raise ValueError(f"Blocked unsafe URL (SSRF protection): {url}")

        assert self._send_session is not None
        async with self._send_session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
            response.raise_for_status()
            data = await response.read()
            suffix = Path(url.split("?", 1)[0]).suffix or ".bin"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
            handle.write(data)
            return handle.name

    async def _send_file(
        self,
        chat_id: str,
        path: str,
        caption: str,
        force_file_attachment: bool = False,
    ) -> str:
        assert self._send_session is not None and self._token is not None
        plaintext = Path(path).read_bytes()
        media_type, item_builder = self._outbound_media_builder(
            path, force_file_attachment=force_file_attachment
        )
        filekey = secrets.token_hex(16)
        aes_key = secrets.token_bytes(16)
        rawsize = len(plaintext)
        rawfilemd5 = hashlib.md5(plaintext).hexdigest()
        upload_response = await _get_upload_url(
            self._send_session,
            base_url=self._base_url,
            token=self._token,
            to_user_id=chat_id,
            media_type=media_type,
            filekey=filekey,
            rawsize=rawsize,
            rawfilemd5=rawfilemd5,
            filesize=_aes_padded_size(rawsize),
            aeskey_hex=aes_key.hex(),
        )
        upload_param = str(upload_response.get("upload_param") or "")
        upload_full_url = str(upload_response.get("upload_full_url") or "")
        ciphertext = _aes128_ecb_encrypt(plaintext, aes_key)

        # Prefer upload_full_url (direct CDN), fall back to constructed CDN URL
        # from upload_param.  Both paths use POST — the old PUT for
        # upload_full_url caused 404s on the WeChat CDN.
        if upload_full_url:
            upload_url = upload_full_url
        elif upload_param:
            upload_url = _cdn_upload_url(self._cdn_base_url, upload_param, filekey)
        else:
            raise RuntimeError(
                f"getUploadUrl returned neither upload_param nor upload_full_url: {upload_response}"
            )

        encrypted_query_param = await _upload_ciphertext(
            self._send_session,
            ciphertext=ciphertext,
            upload_url=upload_url,
        )
        context_token = self._token_store.get(self._account_id, chat_id)
        # The iLink API expects aes_key as base64(hex_string), not base64(raw_bytes).
        # Sending base64(raw_bytes) causes images to show as grey boxes on the
        # receiver side because the decryption key doesn't match.
        aes_key_for_api = base64.b64encode(aes_key.hex().encode("ascii")).decode("ascii")
        item_kwargs = {
            "encrypt_query_param": encrypted_query_param,
            "aes_key_for_api": aes_key_for_api,
            "ciphertext_size": len(ciphertext),
            "plaintext_size": rawsize,
            "filename": Path(path).name,
            "rawfilemd5": rawfilemd5,
        }
        if media_type == MEDIA_VOICE and path.endswith(".silk"):
            item_kwargs["encode_type"] = 6
            item_kwargs["sample_rate"] = 24000
            item_kwargs["bits_per_sample"] = 16
        media_item = item_builder(**item_kwargs)

        last_message_id = None
        if caption:
            last_message_id = f"olano-weixin-{uuid.uuid4().hex}"
            await _send_message(
                self._send_session,
                base_url=self._base_url,
                token=self._token,
                to=chat_id,
                text=self.format_message(caption),
                context_token=context_token,
                client_id=last_message_id,
            )

        last_message_id = f"olano-weixin-{uuid.uuid4().hex}"
        await _api_post(
            self._send_session,
            base_url=self._base_url,
            endpoint=EP_SEND_MESSAGE,
            payload={
                "msg": {
                    "from_user_id": "",
                    "to_user_id": chat_id,
                    "client_id": last_message_id,
                    "message_type": MSG_TYPE_BOT,
                    "message_state": MSG_STATE_FINISH,
                    "item_list": [media_item],
                    **({"context_token": context_token} if context_token else {}),
                }
            },
            token=self._token,
            timeout_ms=API_TIMEOUT_MS,
        )
        return last_message_id

    def _outbound_media_builder(self, path: str, force_file_attachment: bool = False):
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if mime.startswith("image/"):
            return MEDIA_IMAGE, lambda **kw: {
                "type": ITEM_IMAGE,
                "image_item": {
                    "media": {
                        "encrypt_query_param": kw["encrypt_query_param"],
                        "aes_key": kw["aes_key_for_api"],
                        "encrypt_type": 1,
                    },
                    "mid_size": kw["ciphertext_size"],
                },
            }
        if mime.startswith("video/"):
            return MEDIA_VIDEO, lambda **kw: {
                "type": ITEM_VIDEO,
                "video_item": {
                    "media": {
                        "encrypt_query_param": kw["encrypt_query_param"],
                        "aes_key": kw["aes_key_for_api"],
                        "encrypt_type": 1,
                    },
                    "video_size": kw["ciphertext_size"],
                    "play_length": kw.get("play_length", 0),
                    "video_md5": kw.get("rawfilemd5", ""),
                },
            }
        if path.endswith(".silk") and not force_file_attachment:
            return MEDIA_VOICE, lambda **kw: {
                "type": ITEM_VOICE,
                "voice_item": {
                    "media": {
                        "encrypt_query_param": kw["encrypt_query_param"],
                        "aes_key": kw["aes_key_for_api"],
                        "encrypt_type": 1,
                    },
                    "encode_type": kw.get("encode_type"),
                    "bits_per_sample": kw.get("bits_per_sample"),
                    "sample_rate": kw.get("sample_rate"),
                    "playtime": kw.get("playtime", 0),
                },
            }
        if mime.startswith("audio/"):
            return MEDIA_FILE, lambda **kw: {
                "type": ITEM_FILE,
                "file_item": {
                    "media": {
                        "encrypt_query_param": kw["encrypt_query_param"],
                        "aes_key": kw["aes_key_for_api"],
                        "encrypt_type": 1,
                    },
                    "file_name": kw["filename"],
                    "len": str(kw["plaintext_size"]),
                },
            }
        return MEDIA_FILE, lambda **kw: {
            "type": ITEM_FILE,
            "file_item": {
                "media": {
                    "encrypt_query_param": kw["encrypt_query_param"],
                    "aes_key": kw["aes_key_for_api"],
                    "encrypt_type": 1,
                },
                "file_name": kw["filename"],
                "len": str(kw["plaintext_size"]),
            },
        }

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        chat_type = "group" if chat_id.endswith("@chatroom") else "dm"
        return {"name": chat_id, "type": chat_type, "chat_id": chat_id}

    def format_message(self, content: Optional[str]) -> str:
        if content is None:
            return ""
        return _normalize_markdown_blocks(content)


async def send_weixin_direct(
    *,
    extra: dict[str, Any],
    token: Optional[str],
    chat_id: str,
    message: str,
    media_files: Optional[list[tuple[str, bool]]] = None,
) -> dict[str, Any]:
    """
    One-shot send helper for ``send_message`` and cron delivery.

    This bypasses the long-poll adapter lifecycle and uses the raw API directly.
    """
    hermes_home = str(get_olano_home())
    account_id = resolve_weixin_account_id(extra)
    base_url = (
        str(extra.get("base_url") or os.getenv("WEIXIN_BASE_URL", ILINK_BASE_URL))
        .strip()
        .rstrip("/")
    )
    cdn_base_url = (
        str(extra.get("cdn_base_url") or os.getenv("WEIXIN_CDN_BASE_URL", WEIXIN_CDN_BASE_URL))
        .strip()
        .rstrip("/")
    )
    resolved_token = resolve_weixin_token(extra, token)
    # Mirror the adapter's fallbacks so cron/tool delivery works on a
    # QR-login-only setup (credentials persisted, nothing in the config).
    if account_id and not resolved_token:
        persisted = load_weixin_account(hermes_home, account_id)
        if persisted:
            resolved_token = str(persisted.get("token") or "").strip()
            base_url = str(persisted.get("base_url") or base_url).strip().rstrip("/")
    elif not account_id and not resolved_token:
        fallback = pick_persisted_account(hermes_home)
        if fallback:
            account_id, persisted = fallback
            resolved_token = str(persisted.get("token") or "").strip()
            base_url = str(persisted.get("base_url") or base_url).strip().rstrip("/")
    if not resolved_token:
        persisted_accounts = list_persisted_accounts(hermes_home)
        linked = [aid for aid, _ in persisted_accounts]
        if account_id and linked:
            return {
                "error": f"Weixin: no saved login for bot ID '{account_id}'. Linked "
                f"account(s): {', '.join(linked)}. Fix platforms.weixin.extra.bot_id / "
                "WEIXIN_BOT_ID, or clear it to use the linked account."
            }
        if len(distinct_login_owners(persisted_accounts)) > 1:
            return {
                "error": f"Weixin login ambiguous: {len(linked)} different WeChat logins "
                f"are linked ({', '.join(linked)}) and none is pinned. Set "
                "platforms.weixin.extra.bot_id / WEIXIN_BOT_ID to the account to use."
            }
        return {
            "error": "Weixin token missing. Run `olano connect weixin`, or configure "
            "WEIXIN_TOKEN / platforms.weixin.token."
        }
    if not account_id:
        return {
            "error": "Weixin account ID missing. Run `olano connect weixin`, or configure "
            "WEIXIN_ACCOUNT_ID / platforms.weixin.extra.account_id."
        }

    token_store = ContextTokenStore(hermes_home)
    token_store.restore(account_id)
    context_token = token_store.get(account_id, chat_id)

    live_adapter = _LIVE_ADAPTERS.get(resolved_token)
    send_session = getattr(live_adapter, "_send_session", None)
    if live_adapter is not None and send_session is not None and not send_session.closed:
        last_result: Optional[SendResult] = None
        cleaned = live_adapter.format_message(message)
        if cleaned:
            last_result = await live_adapter.send(chat_id, cleaned)
            if not last_result.success:
                return {"error": f"Weixin send failed: {last_result.error}"}

        for media_path, _is_voice in media_files or []:
            ext = Path(media_path).suffix.lower()
            if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
                last_result = await live_adapter.send_image_file(chat_id, media_path)
            else:
                last_result = await live_adapter.send_document(chat_id, media_path)
            if not last_result.success:
                return {"error": f"Weixin media send failed: {last_result.error}"}

        return {
            "success": True,
            "platform": "weixin",
            "chat_id": chat_id,
            "message_id": last_result.message_id if last_result else None,
            "context_token_used": bool(context_token),
        }

    async with aiohttp.ClientSession(trust_env=True, connector=_make_ssl_connector()) as session:
        adapter = WeixinAdapter(
            PlatformConfig(
                enabled=True,
                token=resolved_token,
                extra={
                    **dict(extra or {}),
                    "account_id": account_id,
                    "base_url": base_url,
                    "cdn_base_url": cdn_base_url,
                },
            )
        )
        adapter._send_session = session
        adapter._session = session  # type: ignore[attr-defined]  # dynamic attr, not read elsewhere
        adapter._token = resolved_token
        adapter._account_id = account_id
        adapter._base_url = base_url
        adapter._cdn_base_url = cdn_base_url
        adapter._token_store = token_store

        last_result = None
        cleaned = adapter.format_message(message)
        if cleaned:
            last_result = await adapter.send(chat_id, cleaned)
            if not last_result.success:
                return {"error": f"Weixin send failed: {last_result.error}"}

        for media_path, _is_voice in media_files or []:
            ext = Path(media_path).suffix.lower()
            if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
                last_result = await adapter.send_image_file(chat_id, media_path)
            else:
                last_result = await adapter.send_document(chat_id, media_path)
            if not last_result.success:
                return {"error": f"Weixin media send failed: {last_result.error}"}

        return {
            "success": True,
            "platform": "weixin",
            "chat_id": chat_id,
            "message_id": last_result.message_id if last_result else None,
            "context_token_used": bool(context_token),
        }
