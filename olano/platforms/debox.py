"""DeBox platform adapter.

DeBox (https://debox.pro) is a Web3 social / crypto-messaging platform.
Bots are created and managed via DeBox's **BotMother** (their BotFather
equivalent; deep link ``https://m.debox.pro/user/chat?id=u7ooqdjt&start=``),
where the bot's credentials (**API Key**, plus **App Secret** when issued)
and optional webhook settings (App Domain + Webhook URL) live.

Per the official Bot Development Overview, DeBox supports two receive
modes that are **strictly mutually exclusive server-side**: once a Webhook
URL is configured for the bot, long polling receives no (or nearly no)
updates — to go back to polling, clear the webhook config in BotMother
first. This adapter supports both as a ``transport`` option and picks
webhook automatically when a Webhook Key is configured:

* **polling (default)** — the Bot API modelled on Telegram's, verified
  against the official ``boxbotapi`` Go SDK. Form-encoded POSTs to
  ``https://open.debox.pro/openapi/bot/<method>``:

  - ``bot/getMe`` → the bot's own ``User`` (user_id, name)
  - ``bot/getUpdates`` (``offset``/``limit``/``timeout``) → long-poll for
    ``Update`` objects: ``{"id": n, "message": {...}}`` where message has
    ``message_id``, ``text`` / ``text_raw``, ``mention_users: [User]``,
    ``from: {user_id, is_bot, name, ...}``, ``chat: {id, type, title}``
  - ``bot/sendMessage`` (``chat_id``, ``chat_type``, ``text``,
    ``parse_mode`` — HTML is supported) and ``bot/editMessageText``

  Every request carries ``X-API-KEY: <API Key>`` plus ``nonce``,
  ``timestamp`` and ``signature = sha1(app_secret + nonce + timestamp)``
  headers (the SDK notes SHA1 is for compatibility, not security; the
  secret may be empty). Responses use a Telegram-style envelope:
  ``{"ok": true, "result": ..., "error_code": n, "message": "..."}``.

  Polling needs **only the API Key** — no public URL — so it is the
  default whenever no Webhook Key is configured.

* **webhook** — the OpenPlatform chat-bot callbacks
  (https://docs.debox.pro/en/APIs/BotGuide): DeBox POSTs
  ``{"from_user_id", "to_user_id", "group_id", "message",
  "mention_users", "message_raw", "language"}`` to a registered Webhook
  URL, authenticated by the auto-generated Webhook Key in ``X-API-KEY``.
  Replies go to ``/openapi/send_robot_group_message`` (or the private
  variant) with the App Key in ``X-API-KEY`` and
  ``object_name: "RC:TxtMsg"``.

Configuration (``agent.json5 → channels``). ``token`` and ``api_key`` are
the same credential (the bot's API Key) and fall back to each other::

    // Polling (recommended): only the API Key is required, and the bot
    // must have NO Webhook URL configured in BotMother.
    {
      type: "platform:debox",
      token: "${DEBOX_BOT_TOKEN}",   // the bot's API Key
      extra: {
        api_secret: "${DEBOX_API_SECRET}",  // App Secret, if issued
        parse_mode: "HTML",      // HTML (default) | text (plain)
        require_mention: false,  // groups: only answer @bot messages
        allowed_users: ["uvg2p6ho"],
      }
    }

    // Webhook (set App Domain + Webhook URL in BotMother; needs a public
    // HTTPS URL routed to this adapter):
    {
      type: "platform:debox",
      api_key: "${DEBOX_API_KEY}",              // API Key (outbound)
      extra: {
        webhook_key: "${DEBOX_WEBHOOK_KEY}",    // inbound X-API-KEY
        host: "0.0.0.0", port: 8650, path: "/debox/webhook",
        mention_sender: false,
      }
    }
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html as _html
import json
import logging
import os
import re
import secrets as _secrets
import time
import uuid
from typing import Any, Optional

try:
    from aiohttp import web

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only when aiohttp is absent
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

from olano.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from olano.platforms.config import Platform, PlatformConfig
from olano.platforms.helpers import per_agent_port, strip_markdown

logger = logging.getLogger(__name__)

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8650
DEFAULT_PATH = "/debox/webhook"
DEFAULT_API_BASE = "https://open.debox.pro"
DEFAULT_SEND_GROUP_PATH = "/openapi/send_robot_group_message"
DEFAULT_SEND_PRIVATE_PATH = "/openapi/send_robot_message"

# Bot API method paths (appended to <api_base>/openapi/).
_BOT_GET_ME = "bot/getMe"
_BOT_GET_UPDATES = "bot/getUpdates"
_BOT_SEND_MESSAGE = "bot/sendMessage"
_BOT_EDIT_MESSAGE = "bot/editMessageText"

# Text message object type for the webhook-mode send API
# (RongCloud-style name reused by DeBox).
DEFAULT_OBJECT_NAME = "RC:TxtMsg"

# Long-poll duration passed to bot/getUpdates (server holds the request).
DEFAULT_POLL_TIMEOUT = 25

# DeBox's per-message ceiling is not published; 3000 chars is a safe, readable
# chunk size. Override with ``extra.max_message_length`` if DeBox rejects it.
DEFAULT_MAX_MESSAGE_LENGTH = 3000

# chat_id prefixes used to make the id self-describing for send() routing.
_DM_PREFIXES = ("user:", "dm:")
_GROUP_PREFIX = "group:"

# Poll-loop error backoff.
_BACKOFF_BASE = 2.0
_BACKOFF_MAX = 60.0


def check_debox_requirements() -> bool:
    """Return True if the DeBox adapter's dependencies are available."""
    return AIOHTTP_AVAILABLE


class DeBoxAdapter(BasePlatformAdapter):
    """DeBox chat-bot adapter: Bot-API long-polling or OpenPlatform webhooks."""

    # DeBox supports message edits, so progress updates one message in place.
    PROGRESS_MODE = "edit"

    def __init__(self, config: PlatformConfig, agent_name: str = ""):
        super().__init__(config, Platform.DEBOX, agent_name=agent_name)

        extra = config.extra or {}

        # ── Credentials ───────────────────────────────────────────────
        # BotMother issues one API Key per bot (used by both the Bot API and
        # the OpenPlatform send endpoints), so token and api_key are the same
        # credential and fall back to each other. App Secret feeds the Bot
        # API request signature; empty is valid (the official demo runs so).
        _cred = (
            (config.token or "")
            or (config.api_key or "")
            or os.getenv("DEBOX_BOT_TOKEN", "")
            or os.getenv("DEBOX_API_KEY", "")
        ).strip()
        self._token: str = _cred
        self._api_key: str = _cred
        self._api_secret: str = (
            str(extra.get("api_secret", "") or extra.get("app_secret", "") or "")
            or os.getenv("DEBOX_API_SECRET", "")
            or os.getenv("DEBOX_APP_SECRET", "")
        ).strip()
        # Webhook Key — the X-API-KEY DeBox sends with each callback.
        self._webhook_key: str = (
            str(extra.get("webhook_key", "")) or os.getenv("DEBOX_WEBHOOK_KEY", "")
        ).strip()

        # ── Transport selection ───────────────────────────────────────
        # Explicit extra.transport wins. Otherwise key on the Webhook Key:
        # DeBox makes the receive modes mutually exclusive server-side (a
        # bot with a Webhook URL configured gets no getUpdates results), and
        # a Webhook Key only exists once a Webhook URL has been set.
        transport = str(extra.get("transport", "")).strip().lower()
        if transport not in ("polling", "webhook"):
            transport = "webhook" if self._webhook_key else "polling"
        self._transport: str = transport

        # ── Polling settings ──────────────────────────────────────────
        self._poll_timeout: int = int(extra.get("poll_timeout", DEFAULT_POLL_TIMEOUT))
        # HTML (default) renders markdown converted by _markdown_to_html;
        # "text" strips markdown and sends plain; any other value is passed
        # through as-is with the content untouched.
        self._parse_mode: str = str(extra.get("parse_mode", "HTML")).strip()
        self._poll_task: Optional[asyncio.Task] = None
        self._self_id: str = ""
        self._self_name: str = ""

        # ── Webhook server binding ────────────────────────────────────
        self._host: str = str(extra.get("host", DEFAULT_HOST))
        self._port: int = int(extra.get("port", per_agent_port(DEFAULT_PORT, agent_name)))
        self._path: str = "/" + str(extra.get("path", DEFAULT_PATH)).lstrip("/")

        # ── Outbound API endpoints (overridable for API evolution) ────
        self._api_base: str = str(extra.get("api_base", DEFAULT_API_BASE)).rstrip("/")
        self._send_group_path: str = str(extra.get("send_group_path", DEFAULT_SEND_GROUP_PATH))
        self._send_private_path: str = str(
            extra.get("send_private_path", DEFAULT_SEND_PRIVATE_PATH)
        )
        self._object_name: str = str(extra.get("object_name", DEFAULT_OBJECT_NAME))

        self._max_len: int = int(extra.get("max_message_length", DEFAULT_MAX_MESSAGE_LENGTH))

        # ── Behaviour toggles ─────────────────────────────────────────
        # mention_sender (webhook mode): echo the asker's id into to_user_id
        # so a group reply @-mentions them.
        self._mention_sender: bool = bool(extra.get("mention_sender", False))
        # require_mention: only answer group messages that @-mention the bot.
        self._require_mention: bool = bool(extra.get("require_mention", False))

        # Inbound sender allowlist (DeBox user ids). Empty = allow anyone —
        # matching the Telegram/DingTalk/WhatsApp adapter convention of
        # enforcing allowed_users in the adapter itself.
        raw_allow = extra.get("allowed_users") or []
        if isinstance(raw_allow, str):
            raw_allow = [raw_allow]
        self._allowed_users: set[str] = {
            str(u).strip().lower() for u in raw_allow if str(u).strip()
        }
        # Owner designation (extra.owner_users) — trust WITHOUT admission.
        # The runtime unions this list into sender trust; here it only
        # exempts owners from a configured allowed_users gate. It never
        # closes the bot on its own.
        raw_owner = extra.get("owner_users") or []
        if isinstance(raw_owner, str):
            raw_owner = [raw_owner]
        self._owner_users: set[str] = {str(u).strip().lower() for u in raw_owner if str(u).strip()}

        # Webhook retries carry no message id → dedupe on a raw-body hash
        # within a short window. Set to 0 to disable. (Polling mode needs no
        # dedupe — the getUpdates offset already advances past seen updates.)
        self._dedup_ttl: float = float(extra.get("dedup_ttl_seconds", 10))
        self._seen: dict[str, float] = {}

        # Body size guard (auth-before-body).
        self._max_body_bytes: int = int(extra.get("max_body_bytes", 262_144))  # 256 KiB

        # Per-chat state, bounded. _reply_targets: last sender per chat for
        # webhook-mode @mentions. _chat_types: exact Bot-API chat.type per
        # chat_id so polling replies echo what the platform sent us.
        self._reply_targets: dict[str, str] = {}
        self._chat_types: dict[str, str] = {}
        self._per_chat_max = 1000

        self._runner: Optional[Any] = None  # web.AppRunner
        self._site: Optional[Any] = None  # web.TCPSite
        self._session: Optional[Any] = None  # aiohttp.ClientSession

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        if not AIOHTTP_AVAILABLE:
            logger.error("[debox] aiohttp is not installed — cannot start")
            return False
        if self._transport == "polling":
            return await self._connect_polling()
        return await self._connect_webhook()

    async def _connect_polling(self) -> bool:
        if not self._token:
            logger.error(
                "[debox] Polling transport needs the bot's API Key — set token (or "
                "DEBOX_BOT_TOKEN) to the API Key from BotMother's bot management page."
            )
            return False

        ok, result, err = await self._bot_request(_BOT_GET_ME, {})
        if not ok:
            logger.error("[debox] bot/getMe failed — check the bot's API Key: %s", err)
            return False
        if isinstance(result, dict):
            self._self_id = str(result.get("user_id", "") or "")
            self._self_name = str(result.get("name", "") or "")

        self._mark_connected()
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(
            "[debox] Connected via long-polling as '%s' (id=%s). Note: DeBox's receive "
            "modes are mutually exclusive — if this bot has a Webhook URL configured in "
            "BotMother, polling receives no updates until that webhook config is cleared.",
            self._self_name or "?",
            self._self_id or "?",
        )
        return True

    async def _connect_webhook(self) -> bool:
        if not self._webhook_key:
            logger.warning(
                "[debox] No webhook_key configured — inbound callbacks will be accepted "
                "without X-API-KEY verification. Set extra.webhook_key (or DEBOX_WEBHOOK_KEY) "
                "to the bot's auto-generated Webhook Key to authenticate DeBox."
            )
        if not self._api_key:
            logger.warning(
                "[debox] No api_key configured — the bot can receive messages but cannot "
                "reply. Set api_key (or DEBOX_API_KEY) to the App Key from developer.debox.pro."
            )

        # Fail fast on a port clash rather than silently swallowing inbound.
        import socket as _socket

        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as _s:
                _s.settimeout(1)
                _s.connect(("127.0.0.1", self._port))
            logger.error(
                "[debox] Port %d already in use. Set a different port in the connector "
                "config (extra.port).",
                self._port,
            )
            return False
        except (ConnectionRefusedError, OSError):
            pass  # port is free

        app = web.Application()
        app.router.add_post(self._path, self._handle_webhook)
        app.router.add_get("/health", self._handle_health)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        self._mark_connected()

        logger.info(
            "[debox] Listening on %s:%d%s — configure this as the bot's Webhook URL in "
            "the DeBox developer portal.",
            self._host,
            self._port,
            self._path,
        )
        return True

    async def disconnect(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._poll_task = None
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
            self._runner = None
            self._site = None
        self._mark_disconnected()
        logger.info("[debox] Disconnected")

    # ------------------------------------------------------------------
    # Bot API client (polling transport)
    # ------------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        """Bot API auth headers, mirroring the official Go SDK.

        ``signature = sha1(api_secret + nonce + timestamp)`` — the SDK
        comments this is for third-party API compatibility, not security,
        and the demo runs with an empty secret.
        """
        nonce = str(_secrets.randbelow(1_000_000_000))
        timestamp = str(int(time.time()))
        signature = hashlib.sha1(f"{self._api_secret}{nonce}{timestamp}".encode()).hexdigest()
        return {
            "X-API-KEY": self._token,
            "nonce": nonce,
            "timestamp": timestamp,
            "signature": signature,
            "X-Request-Id": str(uuid.uuid4()),
        }

    async def _bot_request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout_s: float = 30.0,
    ) -> tuple[bool, Any, str]:
        """POST a form-encoded Bot API request; return ``(ok, result, error)``."""
        import aiohttp

        url = f"{self._api_base}/openapi/{method}"
        data = {k: str(v) for k, v in params.items() if v is not None and str(v) != ""}
        try:
            session = await self._get_session()
            async with session.post(
                url,
                data=data,
                headers=self._auth_headers(),
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    return False, None, f"HTTP {resp.status}: {body[:200]}"
                try:
                    envelope = json.loads(body)
                except json.JSONDecodeError:
                    return False, None, f"non-JSON response: {body[:200]}"
                if not isinstance(envelope, dict) or not envelope.get("ok"):
                    code = envelope.get("error_code") if isinstance(envelope, dict) else None
                    msg = envelope.get("message") if isinstance(envelope, dict) else None
                    return False, None, f"error_code={code}: {msg or body[:200]}"
                return True, envelope.get("result"), ""
        except aiohttp.ClientError as exc:
            return False, None, f"network error: {exc}"
        except TimeoutError:
            return False, None, "request timed out"

    async def _poll_loop(self) -> None:
        """Long-poll ``bot/getUpdates`` and dispatch inbound messages."""
        offset = 0
        backoff = _BACKOFF_BASE
        while self._running:
            try:
                ok, result, err = await self._bot_request(
                    _BOT_GET_UPDATES,
                    {"offset": offset, "timeout": self._poll_timeout},
                    timeout_s=self._poll_timeout + 15.0,
                )
                if not ok:
                    logger.warning(
                        "[debox] getUpdates failed: %s — retrying in %.0fs", err, backoff
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, _BACKOFF_MAX)
                    continue
                backoff = _BACKOFF_BASE
                for update in result if isinstance(result, list) else []:
                    if not isinstance(update, dict):
                        continue
                    update_id = int(update.get("id", 0) or 0)
                    if update_id >= offset:
                        offset = update_id + 1
                    try:
                        await self._process_update(update)
                    except Exception:
                        logger.exception("[debox] Error processing update %s", update_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[debox] Poll loop error: %s — retrying in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)

    async def _process_update(self, update: dict[str, Any]) -> None:
        """Normalise one Bot API ``Update`` and dispatch it."""
        message = update.get("message")
        if not isinstance(message, dict):
            # callback_query (button clicks) and other update kinds are not
            # mapped to agent turns yet.
            return

        from_user = message.get("from") or {}
        user_id = str(from_user.get("user_id", "") or "").strip()
        # Never react to bots (loop protection) or to our own messages.
        if from_user.get("is_bot") or (self._self_id and user_id == self._self_id):
            return

        chat = message.get("chat") or {}
        raw_chat_id = str(chat.get("id", "") or "").strip()
        chat_type_raw = str(chat.get("type", "") or "").strip().lower()
        is_dm = chat_type_raw == "private"
        text = str(message.get("text", "") or message.get("text_raw", "") or "").strip()

        if not text or not raw_chat_id:
            return

        if self._allowed_users and user_id.lower() not in (self._allowed_users | self._owner_users):
            logger.info(
                "[debox] Dropping inbound message: sender %s not in allowed_users",
                user_id or "?",
            )
            return

        if is_dm:
            chat_id = _DM_PREFIXES[0] + raw_chat_id  # "user:<id>"
            chat_type = "dm"
            chat_name = str(chat.get("title", "") or "") or f"DeBox DM {user_id or raw_chat_id}"
        else:
            needs_prefix = raw_chat_id.startswith((_GROUP_PREFIX, *_DM_PREFIXES))
            chat_id = _GROUP_PREFIX + raw_chat_id if needs_prefix else raw_chat_id
            chat_type = "group"
            chat_name = str(chat.get("title", "") or "") or f"DeBox group {raw_chat_id}"

            # Group gating: ``extra.group_respond`` wins when set; the legacy
            # ``require_mention`` toggle maps to "mentions"/"all". DMs are
            # never gated. A dropped message may still be persisted to the
            # observed-messages store per ``group_listen``.
            from olano.platforms.group_modes import should_respond

            mentioned = self._update_mentions_bot(message)
            if not should_respond(self._debox_group_respond(), mentioned):
                self._maybe_record_observed(
                    chat_id=chat_id,
                    chat_name=chat_name,
                    user_id=user_id,
                    user_name=str(from_user.get("name", "") or ""),
                    text=text,
                    mentioned=mentioned,
                )
                return

        self._remember(
            self._chat_types, chat_id, chat_type_raw or ("private" if is_dm else "group")
        )
        self._remember(self._reply_targets, chat_id, user_id)

        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=user_id or None,
            user_name=str(from_user.get("name", "") or "") or None,
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=update,
            message_id=str(message.get("message_id", "") or "") or None,
        )

        logger.info(
            "[debox] inbound %s from=%s chat=%s len=%d",
            chat_type,
            user_id or "?",
            raw_chat_id,
            len(text),
        )
        await self.handle_message(event)

    # ------------------------------------------------------------------
    # Group gating (group_respond / group_listen)
    # ------------------------------------------------------------------

    def _debox_group_respond(self) -> str:
        """Effective ``group_respond`` mode ("all" | "mentions" | "none").

        ``extra.group_respond`` wins when set; otherwise the legacy
        ``require_mention`` toggle decides (DeBox default: "all").
        """
        from olano.platforms.group_modes import resolve_group_respond

        return resolve_group_respond(
            self.config.extra, legacy_require_mention=self._require_mention
        )

    def _debox_group_listen(self) -> str:
        """Effective ``group_listen`` mode ("off" | "mentions" | "all").

        DeBox never had a legacy passive-listen toggle, so unset is "off".
        """
        from olano.platforms.group_modes import resolve_group_listen

        return resolve_group_listen(self.config.extra, legacy_passive_listen=False)

    def _update_mentions_bot(self, message: dict[str, Any]) -> bool:
        """Whether a Bot-API group message @-mentions this bot.

        Exact legacy ``require_mention`` semantics: with a known self id the
        bot itself must appear in ``mention_users``; without one, any mention
        counts.
        """
        # Shape-defensive like _bot_mentioned: this now runs on EVERY group
        # message (the respond gate needs it), so a malformed payload must
        # read as "no mention", never raise into the poll loop.
        raw_mentions = message.get("mention_users")
        mentioned_ids = {
            str(u.get("user_id", "") or "")
            for u in (raw_mentions if isinstance(raw_mentions, list) else [])
            if isinstance(u, dict)
        }
        mentioned_ids.discard("")
        if self._self_id:
            return self._self_id in mentioned_ids
        return bool(mentioned_ids)

    def _maybe_record_observed(
        self,
        *,
        chat_id: str,
        chat_name: str,
        user_id: str,
        user_name: str,
        text: str,
        mentioned: bool,
    ) -> None:
        """Record a group message the ``group_respond`` gate declined to answer.

        Feeds the persistent observed-messages store (``chat_directory``) per
        the effective ``group_listen`` mode: "mentions" records only messages
        that addressed the bot, "all" records every group message the bot
        received. Called only from drop branches — recording must never break
        message handling, so everything here is best-effort.
        """
        try:
            from olano.platforms.group_modes import should_record

            if not should_record(self._debox_group_listen(), mentioned):
                return
            if not str(text or "").strip():
                return
            from olano.platforms.chat_directory import record_observed_event

            source = self.build_source(
                chat_id=chat_id,
                chat_name=chat_name or None,
                chat_type="group",
                user_id=user_id or None,
                user_name=user_name or None,
            )
            record_observed_event(self, source, text)
        except Exception as exc:
            logger.debug("[debox] observed-store capture failed: %s", exc)

    # ------------------------------------------------------------------
    # Inbound webhook handling (webhook transport)
    # ------------------------------------------------------------------

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "platform": "debox"})

    def _verify(self, request: web.Request) -> bool:
        """Verify the inbound X-API-KEY against the configured webhook key."""
        if not self._webhook_key:
            return True  # verification disabled (warned at connect time)
        provided = request.headers.get("X-API-KEY", "") or request.headers.get("x-api-key", "")
        return hmac.compare_digest(str(provided), self._webhook_key)

    def _is_duplicate(self, digest: str) -> bool:
        """TTL dedupe on the raw-body hash to absorb webhook retries."""
        if self._dedup_ttl <= 0:
            return False
        now = time.time()
        # prune
        if len(self._seen) > 2000:
            self._seen = {k: v for k, v in self._seen.items() if now - v < self._dedup_ttl}
        seen_at = self._seen.get(digest)
        if seen_at is not None and now - seen_at < self._dedup_ttl:
            return True
        self._seen[digest] = now
        return False

    def _remember(self, store: dict[str, str], chat_id: str, value: str) -> None:
        if not value:
            return
        if len(store) > self._per_chat_max:
            store.pop(next(iter(store)), None)
        store[chat_id] = value

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        """POST handler for DeBox bot callbacks."""
        # Size guard before reading the body.
        if (request.content_length or 0) > self._max_body_bytes:
            return web.json_response({"code": 1, "msg": "payload too large"}, status=413)

        if not self._verify(request):
            logger.warning("[debox] Rejected callback with invalid X-API-KEY")
            return web.json_response({"code": 1, "msg": "invalid signature"}, status=401)

        try:
            raw = await request.read()
        except Exception as exc:
            logger.error("[debox] Failed to read callback body: %s", exc)
            return web.json_response({"code": 1, "msg": "bad request"}, status=400)

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("[debox] Callback body was not valid JSON")
            return web.json_response({"code": 1, "msg": "bad json"}, status=400)

        if not isinstance(payload, dict):
            return web.json_response({"code": 1, "msg": "bad payload"}, status=400)

        from_user_id = str(payload.get("from_user_id", "") or "").strip()
        group_id = str(payload.get("group_id", "") or "").strip()
        # Prefer the @-stripped text; fall back to the raw text.
        text = str(payload.get("message", "") or payload.get("message_raw", "") or "").strip()

        # Ignore empty callbacks and DMs with no identifiable sender (a DM
        # chat_id is derived from from_user_id, so we couldn't reply anyway).
        if not text:
            return web.json_response({"code": 0, "msg": "ignored (empty)"})
        is_group = bool(group_id)
        if not is_group and not from_user_id:
            return web.json_response({"code": 0, "msg": "ignored (no sender)"})

        # Sender allowlist — enforced here, in the adapter, like the other
        # platform adapters (the runtime only uses allowed_users for trust).
        if self._allowed_users and from_user_id.lower() not in (
            self._allowed_users | self._owner_users
        ):
            logger.info(
                "[debox] Dropping inbound message: sender %s not in allowed_users",
                from_user_id or "?",
            )
            return web.json_response({"code": 0, "msg": "ignored (not allowed)"})

        # Group gating: ``extra.group_respond`` wins when set; the legacy
        # ``require_mention`` toggle ("callback all messages" mode: only
        # answer @-mentions) maps to "mentions"/"all". A dropped message may
        # still be persisted to the observed store per ``group_listen``.
        # Digest computed up-front so the drop branch below can skip
        # re-recording webhook retries; the processed path keeps its dedupe
        # check exactly where it was.
        digest = hashlib.sha1(raw).hexdigest()

        if is_group:
            from olano.platforms.group_modes import should_respond

            mentioned = _bot_mentioned(payload)
            respond = self._debox_group_respond()
            if not should_respond(respond, mentioned):
                # Record once per unique body — DeBox retries webhooks, and
                # without this a retried gated message lands in the observed
                # store once per retry. The response body stays the legacy
                # "ignored" shape either way.
                if not self._is_duplicate(digest):
                    needs_prefix = group_id.startswith((_GROUP_PREFIX, *_DM_PREFIXES))
                    self._maybe_record_observed(
                        chat_id=_GROUP_PREFIX + group_id if needs_prefix else group_id,
                        chat_name=f"DeBox group {group_id}",
                        user_id=from_user_id,
                        user_name=from_user_id,
                        text=text,
                        mentioned=mentioned,
                    )
                msg = (
                    "ignored (no mention)"
                    if respond == "mentions"
                    else "ignored (group_respond=none)"
                )
                return web.json_response({"code": 0, "msg": msg})

        # Dedupe on the raw body (DeBox provides no message id).
        if self._is_duplicate(digest):
            return web.json_response({"code": 0, "msg": "duplicate"})

        if is_group:
            # Prefix defensively if the group id would collide with the
            # prefixes send() strips, so the chat_id round-trips exactly.
            needs_prefix = group_id.startswith((_GROUP_PREFIX, *_DM_PREFIXES))
            chat_id = _GROUP_PREFIX + group_id if needs_prefix else group_id
            chat_type = "group"
            chat_name = f"DeBox group {group_id}"
        else:
            chat_id = _DM_PREFIXES[0] + from_user_id  # "user:<id>"
            chat_type = "dm"
            chat_name = f"DeBox DM {from_user_id}"

        self._remember(self._reply_targets, chat_id, from_user_id)

        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=from_user_id or None,
            user_name=from_user_id or None,
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=payload,
            message_id=digest,
        )

        logger.info(
            "[debox] inbound %s from=%s group=%s len=%d",
            chat_type,
            from_user_id or "?",
            group_id or "-",
            len(text),
        )

        # Dispatch without blocking DeBox's HTTP call; the base spawns the
        # background task and routes the reply back through send().
        await self.handle_message(event)
        return web.json_response({"code": 0, "msg": "success"})

    # ------------------------------------------------------------------
    # Outbound sending
    # ------------------------------------------------------------------

    def _parse_chat_id(self, chat_id: str) -> tuple[bool, str]:
        """Return ``(is_dm, target_id)`` from a self-describing chat_id.

        ``user:<id>`` / ``dm:<id>`` → DM to that user; ``group:<id>`` or a bare
        id → group message to that group.
        """
        cid = str(chat_id)
        for prefix in _DM_PREFIXES:
            if cid.startswith(prefix):
                return True, cid[len(prefix) :]
        if cid.startswith(_GROUP_PREFIX):
            return False, cid[len(_GROUP_PREFIX) :]
        return False, cid

    async def _get_session(self) -> Any:
        import aiohttp

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _post(self, path: str, payload: dict[str, Any]) -> SendResult:
        """POST a message payload to the OpenPlatform send API (webhook mode)."""
        import aiohttp

        url = f"{self._api_base}{path}"
        headers = {"X-API-KEY": self._api_key, "Content-Type": "application/json"}
        try:
            session = await self._get_session()
            async with session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    logger.error("[debox] send %s → HTTP %s: %s", path, resp.status, body[:300])
                    return SendResult(
                        success=False,
                        error=f"HTTP {resp.status}: {body[:200]}",
                        raw_response=body,
                        retryable=resp.status >= 500,
                    )
                data: Any = {}
                try:
                    data = json.loads(body)
                except Exception:
                    data = {}
                # DeBox returns {"code": 0, ...} on success; treat a non-zero
                # code as a failure so callers see the real outcome.
                code = data.get("code") if isinstance(data, dict) else None
                if code not in (None, 0, "0", 200, "200"):
                    logger.error("[debox] send %s → code=%s body=%s", path, code, body[:300])
                    return SendResult(
                        success=False,
                        error=f"DeBox error code {code}: {body[:200]}",
                        raw_response=data,
                    )
                msg_id = None
                if isinstance(data, dict):
                    inner = data.get("data")
                    if isinstance(inner, dict):
                        msg_id = inner.get("message_id") or inner.get("msg_id") or inner.get("id")
                return SendResult(success=True, message_id=msg_id, raw_response=data)
        except aiohttp.ClientError as exc:
            logger.error("[debox] send %s network error: %s", path, exc)
            return SendResult(success=False, error=str(exc), retryable=True)
        except TimeoutError:
            logger.error("[debox] send %s timed out", path)
            return SendResult(success=False, error="DeBox send timed out")

    def _outbound_chat(self, chat_id: str) -> tuple[str, str]:
        """Resolve ``(target_id, bot_api_chat_type)`` for the polling send path.

        Prefers the exact ``chat.type`` observed on inbound; falls back to the
        chat_id prefix scheme for owner-configured outbound-only targets.
        """
        is_dm, target = self._parse_chat_id(chat_id)
        chat_type = self._chat_types.get(chat_id) or ("private" if is_dm else "group")
        return target, chat_type

    def _render_outbound(self, content: str) -> tuple[str, str]:
        """Return ``(text, parse_mode)`` for a Bot API send/edit."""
        mode = self._parse_mode
        if mode.upper() == "HTML":
            return _markdown_to_html(content), "HTML"
        if mode.lower() in ("", "text", "plain"):
            return strip_markdown(content), ""
        return content, mode

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Send *content* to a DeBox chat via the configured transport.

        ``reply_to`` is accepted for interface compatibility but unused —
        DeBox's send APIs have no reply-to-message concept.
        """
        if not content or not content.strip():
            return SendResult(success=False, error="empty content")
        if self._transport == "polling":
            return await self._send_polling(chat_id, content)
        return await self._send_webhook(chat_id, content, metadata)

    async def _send_polling(self, chat_id: str, content: str) -> SendResult:
        if not self._token:
            return SendResult(
                success=False,
                error="DeBox API Key not configured (set token or DEBOX_BOT_TOKEN).",
            )
        target, chat_type = self._outbound_chat(chat_id)
        if not target:
            return SendResult(
                success=False, error=f"could not resolve target from chat_id {chat_id!r}"
            )

        result = SendResult(success=False, error="nothing sent")
        # Chunk on the raw markdown, then render each chunk — rendering first
        # would replace the newlines the chunker prefers to split on.
        for chunk in self.truncate_message(content, self._max_len):
            text, parse_mode = self._render_outbound(chunk)
            ok, res, err = await self._bot_request(
                _BOT_SEND_MESSAGE,
                {
                    "chat_id": target,
                    "chat_type": chat_type,
                    "text": text,
                    "parse_mode": parse_mode,
                },
            )
            if not ok:
                logger.error("[debox] sendMessage failed: %s", err)
                return SendResult(success=False, error=err, retryable="network" in err)
            msg_id = None
            if isinstance(res, dict):
                msg_id = str(res.get("message_id", "") or "") or None
            result = SendResult(success=True, message_id=msg_id, raw_response=res)
        return result

    async def _send_webhook(
        self,
        chat_id: str,
        content: str,
        metadata: Optional[dict[str, Any]],
    ) -> SendResult:
        if not self._api_key:
            return SendResult(
                success=False,
                error="DeBox API key not configured (set api_key or DEBOX_API_KEY).",
            )

        metadata = metadata or {}
        is_dm, target = self._parse_chat_id(chat_id)
        if not target:
            return SendResult(
                success=False, error=f"could not resolve target from chat_id {chat_id!r}"
            )

        object_name = str(metadata.get("object_name") or self._object_name)
        # RC:TxtMsg renders as plain text — markdown artifacts would show
        # literally (same convention as the SMS/QQ/BlueBubbles adapters).
        # Rich object types are passed through untouched.
        if object_name == DEFAULT_OBJECT_NAME:
            content = strip_markdown(content)
        chunks = self.truncate_message(content, self._max_len)

        result = SendResult(success=False, error="nothing sent")
        for chunk in chunks:
            if is_dm:
                payload: dict[str, Any] = {
                    "to_user_id": target,
                    "object_name": object_name,
                    "message": chunk,
                }
                path = self._send_private_path
            else:
                payload = {
                    "group_id": target,
                    "object_name": object_name,
                    "message": chunk,
                }
                to_user = metadata.get("to_user_id")
                if not to_user and self._mention_sender:
                    to_user = self._reply_targets.get(chat_id)
                if to_user:
                    payload["to_user_id"] = to_user
                path = self._send_group_path

            result = await self._post(path, payload)
            if not result.success:
                return result
        return result

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        """Edit a previously sent message (polling transport only)."""
        if self._transport != "polling" or not message_id:
            return SendResult(success=False, error="Not supported")
        target, chat_type = self._outbound_chat(chat_id)
        text, parse_mode = self._render_outbound(content)
        ok, res, err = await self._bot_request(
            _BOT_EDIT_MESSAGE,
            {
                "chat_id": target,
                "chat_type": chat_type,
                "message_id": message_id,
                "text": text,
                "parse_mode": parse_mode,
            },
        )
        if not ok:
            return SendResult(success=False, error=err)
        return SendResult(success=True, message_id=message_id, raw_response=res)

    def format_message(self, content: str) -> str:
        """Strip markdown for DeBox's plain-text (RC:TxtMsg) messages."""
        return strip_markdown(content)

    async def send_typing(self, chat_id: str, metadata: Optional[dict] = None) -> None:
        """DeBox has no typing-indicator API — no-op."""
        return

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        is_dm, target = self._parse_chat_id(chat_id)
        return {
            "name": chat_id,
            "type": "dm" if is_dm else "group",
            "id": target,
        }


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_BOLD_UNDER = re.compile(r"__(.+?)__", re.DOTALL)
_MD_ITALIC = re.compile(r"(?<!\w)[*_]([^*_\n]+)[*_](?!\w)")
_MD_HEADING = re.compile(r"^#{1,6}\s+(.*)$", re.MULTILINE)
_MD_CODE_FENCE = re.compile(r"```[a-zA-Z0-9_+-]*\n?")
_MD_INLINE_CODE = re.compile(r"`([^`\n]+)`")


def _markdown_to_html(text: str) -> str:
    """Convert agent markdown to the HTML subset DeBox renders.

    The official demo bot uses ``<b>``, ``<a href>``, ``<br/>``, ``<font>``,
    ``<img>`` — we stick to the safe core (``b``/``a``/``br``) and degrade
    everything else to plain text. Content is HTML-escaped first so user or
    model text can't inject markup.
    """
    out = _html.escape(text, quote=False)
    out = _MD_CODE_FENCE.sub("", out)
    out = _MD_INLINE_CODE.sub(r"\1", out)
    out = _MD_LINK.sub(r'<a href="\2">\1</a>', out)
    out = _MD_BOLD.sub(r"<b>\1</b>", out)
    out = _MD_BOLD_UNDER.sub(r"<b>\1</b>", out)
    out = _MD_HEADING.sub(r"<b>\1</b>", out)
    out = _MD_ITALIC.sub(r"\1", out)
    return out.replace("\n", "<br/>")


def _has_mention(mention_users: Any) -> bool:
    """Return True when a DeBox ``mention_users`` field indicates an @mention."""
    if not mention_users:
        return False
    if isinstance(mention_users, str):
        return bool(mention_users.strip())
    if isinstance(mention_users, list | tuple | set | dict):
        return len(mention_users) > 0
    return bool(mention_users)


def _bot_mentioned(payload: dict[str, Any]) -> bool:
    """Return True when a webhook callback @-mentions the bot itself.

    The callback's ``to_user_id`` is the bot's own DeBox id, so when it is
    present we require it to appear in ``mention_users`` — a mention of some
    *other* user shouldn't trigger a reply. ``mention_users``' exact shape is
    not pinned down by the docs (string vs list), so membership is checked by
    substring against a JSON rendering, which covers strings, lists of ids,
    and lists of objects alike. Without a usable ``to_user_id`` we fall back
    to "any mention counts".
    """
    mention_users = payload.get("mention_users")
    if not _has_mention(mention_users):
        return False
    bot_id = str(payload.get("to_user_id", "") or "").strip()
    if not bot_id:
        return True
    if isinstance(mention_users, str):
        haystack = mention_users
    else:
        try:
            haystack = json.dumps(mention_users, ensure_ascii=False)
        except (TypeError, ValueError):
            return True
    return bot_id in haystack
