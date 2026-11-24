"""
DEPRECATED — duplicated connector, superseded by ``platform:telegram``.

This is the classic Telegram adapter (``type: "telegram"``). It duplicates
:mod:`olano.platforms.telegram` (``type: "platform:telegram"``), which is
the maintained implementation — richer media handling, DM topics, mention
gating, webhook mode, secretary/business mode, and bot-to-bot support all
live there, and the Stars payment handlers were ported there too.

The file is kept so existing ``agent.json5`` configs that say ``"telegram"``
keep booting, but it is hidden from the dashboard connector picker
(``platforms/catalog.py`` marks it deprecated) and the runtime logs a
deprecation warning on boot. Do not add features here — use the platform
adapter.

Features (frozen):
- Text messages, photos, documents, voice messages
- /start, /new (alias /clear), /status bot commands
- Per-chat thread_id mapping
- Allowed-users allowlist
- File downloads to workspace/downloads/
- UTF-16 aware truncation + markdown→plain fallback via ``send_with_retry``
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from pathlib import Path
from typing import Any

from olano.channels.base import (
    BaseChannel,
    ChannelMessage,
    SendResult,
    is_retryable_error,
)
from olano.vault.resolver import lookup_credential

logger = logging.getLogger(__name__)

# (agent_name, chat_id) → thread_id (str)
#
# Namespaced by agent so two agents that each own a Telegram bot don't
# share threads with the same chat_id (which can happen in private DMs
# where the chat_id is the user's Telegram ID — globally unique across
# bots, but the cached thread is per-bot conversation).
_CHAT_THREADS: dict[tuple[str, str], str] = {}

# Telegram bot API errors that indicate the text was rejected as invalid
# markdown/entities — retrying with parse_mode=None usually succeeds.
_MARKDOWN_ERROR_HINTS = ("can't parse", "parse_mode", "entity", "entities")

# Update types this channel actually handles. Telegram only delivers what
# getUpdates asks for, so requesting exactly these keeps unhandled updates —
# in particular the Telegram Business ("secretary mode") update types, which
# this deprecated adapter will never support — off the wire entirely.
_ALLOWED_UPDATE_TYPES = ["message", "callback_query", "pre_checkout_query"]

_APPROVAL_CB_PREFIX = "lcap"
_APPROVAL_TIMEOUT_SECONDS = 300
_CHOICE_LABELS = {
    "once": "✅ Approved once",
    "always": "✅ Always allowed",
    "deny": "❌ Denied",
}


def _escape_md(text: str) -> str:
    """Escape backticks so inline code blocks stay intact in Telegram Markdown."""
    return text.replace("`", "'")


def _format_args_brief(args: dict[str, Any]) -> str:
    """Render a short one-line summary of structured tool args."""
    try:
        import json

        return json.dumps(args, ensure_ascii=False)[:500]
    except Exception:
        return str(args)[:500]


def _dt_to_epoch(value: Any) -> float | None:
    """Normalize Telegram's ``subscription_expiration_date`` to Unix seconds.

    python-telegram-bot hands this back as a timezone-aware ``datetime``; older
    or raw payloads may carry a plain int. ``None`` stays ``None``.
    """
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(value.timestamp())
    except Exception:
        return None


def _thread_for_chat(chat_id: str, agent_name: str = "") -> str:
    key = (agent_name, chat_id)
    if key not in _CHAT_THREADS:
        import uuid

        _CHAT_THREADS[key] = f"tg_{uuid.uuid4().hex[:8]}"
    return _CHAT_THREADS[key]


def _downloads_dir() -> Path:
    from olano.config.workspace import get_workspace

    d = get_workspace().root / "downloads"
    d.mkdir(parents=True, exist_ok=True)
    return d


from olano.channels._health import HealthWatchdogMixin


class TelegramChannel(HealthWatchdogMixin, BaseChannel):
    """
    Full Telegram bot adapter.

    Config keys (agent.json5 or env):
        token (str): Bot token — TELEGRAM_BOT_TOKEN env fallback
        allowed_users (list[int | str]): optional allowlist — numeric Telegram
            user IDs and/or usernames (with or without leading '@')
        max_message_length (int): override the 4096 UTF-16 unit default
    """

    SUPPORTS_PROGRESS = True

    name = "telegram"
    # Telegram's 4096 limit is measured in UTF-16 code units (surrogate pairs count as 2).
    max_message_length: int = 4096
    use_utf16_length: bool = True

    # Watchdog cadence inherited from HealthWatchdogMixin (override here if
    # Telegram ever needs a different rate). _health_check + _health_rebuild
    # below provide the Telegram-specific probe and rebuild logic.

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._app: Any = None
        override = config.get("max_message_length")
        if override:
            self.max_message_length = int(override)

        # allowed_users accepts numeric Telegram user IDs and/or usernames
        # (with or without leading '@'). Usernames match case-insensitively.
        # Coerce a singleton (the most common dashboard mistake — entering
        # `1234` instead of `[1234]`) into a single-item list so the loop
        # below doesn't blow up with "'int' object is not iterable".
        raw_allowed = config.get("allowed_users", []) or []
        if isinstance(raw_allowed, int | str):
            raw_allowed = [raw_allowed]
        elif not isinstance(raw_allowed, list | tuple | set):
            logger.warning(
                "Telegram allowed_users has unexpected type %s; ignoring.",
                type(raw_allowed).__name__,
            )
            raw_allowed = []
        self._allowed_ids: set[int] = set()
        self._allowed_usernames: set[str] = set()
        for entry in raw_allowed:
            if isinstance(entry, int):
                self._allowed_ids.add(entry)
            elif isinstance(entry, str):
                s = entry.strip().lstrip("@")
                if s.isdigit():
                    self._allowed_ids.add(int(s))
                elif s:
                    self._allowed_usernames.add(s.lower())

        # approval_id → entry (future, chat_id, message_id, tool_name, agent_name)
        self._pending_approvals: dict[str, dict[str, Any]] = {}

    def _user_is_allowed(self, user: Any) -> bool:
        if not self._allowed_ids and not self._allowed_usernames:
            return True
        if not user:
            return False
        if getattr(user, "id", None) in self._allowed_ids:
            return True
        uname = (getattr(user, "username", None) or "").lower()
        return bool(uname) and uname in self._allowed_usernames

    async def start(self) -> None:
        try:
            from telegram import Update
            from telegram.ext import (
                Application,
                CallbackQueryHandler,
                CommandHandler,
                ContextTypes,
                PreCheckoutQueryHandler,
                filters,
            )
            from telegram.ext import (
                MessageHandler as TGHandler,
            )
        except ImportError:
            raise ImportError("python-telegram-bot not installed. Run: uv add python-telegram-bot")

        # The runtime injects the per-agent credential before we get here
        # (config/channel_secrets.hydrate_channel_credentials). This fallback is
        # for a channel constructed outside that path: vault common value, then
        # env — never agent-scoped, because a bare adapter has no agent.
        token = self.config.get("token") or lookup_credential("TELEGRAM_BOT_TOKEN")
        if not token:
            raise ValueError("Telegram token not configured (set TELEGRAM_BOT_TOKEN)")

        # concurrent_updates=True is required so that a callback_query
        # (approval button click) can be dispatched while the text handler
        # that posted the prompt is still awaiting the user's decision.
        # Without it, PTB serializes updates and the click waits forever
        # behind the handler that's waiting for the click.
        self._app = Application.builder().token(token).concurrent_updates(True).build()

        allowed_ids = self._allowed_ids
        allowed_usernames = self._allowed_usernames

        def _is_allowed(update: Update) -> bool:
            return self._user_is_allowed(update.effective_user)

        async def _reply(update: Update, text: str) -> None:
            """Send a reply using the base `send_with_retry` pipeline."""
            chat = update.effective_chat
            if not chat:
                return
            await self.send_with_retry(str(chat.id), text)

        async def _typing_heartbeat(chat_id: str) -> None:
            """Keep Telegram's 'typing…' indicator refreshed (expires every ~5s).

            Paused while the user is being prompted for an approval so the
            indicator does not fight with the inline-keyboard prompt.
            """
            while True:
                try:
                    if not self.is_typing_paused(chat_id):
                        await self._app.bot.send_chat_action(chat_id=int(chat_id), action="typing")
                except Exception:
                    return
                try:
                    await asyncio.sleep(4)
                except asyncio.CancelledError:
                    return

        # ── /start ──
        async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            uid = update.effective_user.id if update.effective_user else "?"
            logger.info("Telegram /start from user_id=%s", uid)
            if not _is_allowed(update):
                logger.warning("Telegram /start blocked — user_id=%s not in allowed_users", uid)
                return
            if not update.effective_chat:
                return
            chat_id = str(update.effective_chat.id)
            tid = _thread_for_chat(chat_id, self.agent_name)
            name = update.effective_user.first_name if update.effective_user else "there"

            # Monetized agent → greet with the trial + price + how-to-pay intro
            # instead of the generic hello. Starting the trial here (via the
            # gate) also means the user's first *text* message won't re-send the
            # welcome (it will see an existing record).
            mon = getattr(getattr(self, "agent_config", None), "monetization", None)
            if mon is not None and getattr(mon, "enabled", False):
                welcome = self._monetization_welcome(chat_id, uid)
                if welcome:
                    await _reply(update, welcome)
                    return

            await _reply(update, f"Hello {name}! I'm ready. Thread: `{tid}`")

        # ── /new (alias /clear) ──
        async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            uid = update.effective_user.id if update.effective_user else "?"
            logger.info("Telegram /new from user_id=%s", uid)
            if not _is_allowed(update):
                logger.warning("Telegram /new blocked — user_id=%s not in allowed_users", uid)
                return
            if not update.effective_chat:
                return
            chat_id = str(update.effective_chat.id)
            import uuid

            key = (self.agent_name, chat_id)
            _CHAT_THREADS[key] = f"tg_{uuid.uuid4().hex[:8]}"
            await _reply(update, f"New session started. Thread: `{_CHAT_THREADS[key]}`")

        # ── /status ──
        async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            uid = update.effective_user.id if update.effective_user else "?"
            logger.info("Telegram /status from user_id=%s", uid)
            if not _is_allowed(update):
                logger.warning("Telegram /status blocked — user_id=%s not in allowed_users", uid)
                return
            if not update.effective_chat:
                return
            from datetime import datetime, timezone

            chat_id = str(update.effective_chat.id)
            tid = _thread_for_chat(chat_id, self.agent_name)
            await _reply(
                update,
                f"Status\nThread: `{tid}`\nTime: {datetime.now(timezone.utc).strftime('%H:%M UTC')}",
            )

        # ── text messages ──
        async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            uid = update.effective_user.id if update.effective_user else "?"
            text_preview = (update.message.text or "")[:80] if update.message else ""
            logger.info("Telegram message from user_id=%s: %r", uid, text_preview)
            if not _is_allowed(update):
                logger.warning(
                    "Telegram message blocked — user_id=%s not in allowed_users (ids=%s usernames=%s)",
                    uid,
                    sorted(allowed_ids),
                    sorted(allowed_usernames),
                )
                return
            if not update.message or not update.effective_chat:
                return
            text = (update.message.text or "").strip()
            if not text:
                return
            chat_id = str(update.effective_chat.id)
            user_id = str(uid)
            thread_id = _thread_for_chat(chat_id, self.agent_name)

            msg = ChannelMessage(
                text=text,
                user_id=user_id,
                channel="telegram",
                thread_id=thread_id,
                raw=update,
                metadata={"chat_id": chat_id},
            )
            heartbeat = asyncio.create_task(_typing_heartbeat(chat_id))
            try:
                logger.debug("Telegram invoking agent for thread=%s", thread_id)
                response = await self.handle(msg)
                logger.info("Telegram response sent to user_id=%s (%d chars)", uid, len(response))
                await _reply(update, response)
            except Exception as e:
                from olano.core.actionable_errors import channel_text

                logger.error("Telegram text handler error: %s", e, exc_info=True)
                await _reply(update, channel_text(e))
            finally:
                heartbeat.cancel()

        # ── photo messages ──
        async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not _is_allowed(update):
                return
            if not update.effective_chat or not update.message:
                return
            chat_id = str(update.effective_chat.id)
            user_id = str(update.effective_user.id) if update.effective_user else chat_id
            thread_id = _thread_for_chat(chat_id, self.agent_name)

            caption = update.message.caption or "Describe this image."
            # Download the largest photo size
            photo = update.message.photo[-1]
            try:
                tg_file = await photo.get_file()
                dl_path = _downloads_dir() / f"tg_{photo.file_id[-10:]}.jpg"
                await tg_file.download_to_drive(str(dl_path))
                text = f"[Photo received, saved to {dl_path}]\n{caption}"
            except Exception as e:
                text = f"[Photo received, could not download: {e}]\n{caption}"

            msg = ChannelMessage(
                text=text,
                user_id=user_id,
                channel="telegram",
                thread_id=thread_id,
                raw=update,
                metadata={"chat_id": chat_id},
            )
            heartbeat = asyncio.create_task(_typing_heartbeat(chat_id))
            try:
                response = await self.handle(msg)
                await _reply(update, response)
            except Exception as e:
                logger.error("Telegram photo handler error: %s", e)
            finally:
                heartbeat.cancel()

        # ── document messages ──
        async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not _is_allowed(update):
                return
            if not update.effective_chat or not update.message:
                return
            chat_id = str(update.effective_chat.id)
            user_id = str(update.effective_user.id) if update.effective_user else chat_id
            thread_id = _thread_for_chat(chat_id, self.agent_name)

            doc = update.message.document
            if not doc:
                return
            caption = update.message.caption or f"Process this file: {doc.file_name}"
            try:
                tg_file = await doc.get_file()
                dl_path = _downloads_dir() / (doc.file_name or f"tg_{doc.file_id[-10:]}")
                await tg_file.download_to_drive(str(dl_path))
                text = f"[Document '{doc.file_name}' received, saved to {dl_path}]\n{caption}"
            except Exception as e:
                text = f"[Document received, could not download: {e}]\n{caption}"

            msg = ChannelMessage(
                text=text,
                user_id=user_id,
                channel="telegram",
                thread_id=thread_id,
                raw=update,
                metadata={"chat_id": chat_id},
            )
            heartbeat = asyncio.create_task(_typing_heartbeat(chat_id))
            try:
                response = await self.handle(msg)
                await _reply(update, response)
            except Exception as e:
                logger.error("Telegram document handler error: %s", e)
            finally:
                heartbeat.cancel()

        # ── voice messages ──
        async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not _is_allowed(update):
                return
            if not update.effective_chat or not update.message:
                return
            chat_id = str(update.effective_chat.id)
            user_id = str(update.effective_user.id) if update.effective_user else chat_id
            thread_id = _thread_for_chat(chat_id, self.agent_name)

            voice = update.message.voice
            if not voice:
                return
            try:
                tg_file = await voice.get_file()
                dl_path = _downloads_dir() / f"voice_{voice.file_id[-10:]}.ogg"
                await tg_file.download_to_drive(str(dl_path))
                text = f"[Voice message received, saved to {dl_path}]"
            except Exception as e:
                text = f"[Voice message, could not download: {e}]"

            msg = ChannelMessage(
                text=text,
                user_id=user_id,
                channel="telegram",
                thread_id=thread_id,
                raw=update,
                metadata={"chat_id": chat_id},
            )
            try:
                response = await self.handle(msg)
                await _reply(update, response)
            except Exception as e:
                logger.error("Telegram voice handler error: %s", e)

        # ── approval button clicks ──
        async def on_approval_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            query = update.callback_query
            if not query:
                return
            data = query.data or ""
            if not data.startswith(f"{_APPROVAL_CB_PREFIX}:"):
                return
            if not _is_allowed(update):
                await query.answer("Not authorized", show_alert=True)
                return
            parts = data.split(":", 2)
            if len(parts) != 3:
                await query.answer("Malformed approval", show_alert=False)
                return
            _, approval_id, choice = parts
            entry = self._pending_approvals.get(approval_id)
            if not entry:
                await query.answer("Approval no longer pending", show_alert=False)
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    pass
                return

            fut: asyncio.Future = entry["future"]
            if not fut.done():
                fut.set_result(choice)
            await query.answer(_CHOICE_LABELS.get(choice, "Resolved"))
            try:
                original_text = (
                    (getattr(query.message, "text", None) or "") if query.message else ""
                )
                await query.edit_message_text(
                    text=f"{original_text}\n\n*{_CHOICE_LABELS.get(choice, 'Resolved')}*",
                    parse_mode="Markdown",
                    reply_markup=None,
                )
            except Exception as e:
                logger.debug("Telegram: could not edit approval message: %s", e)

        # ── Telegram Stars payments (creator monetization) ──
        async def on_pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            """Approve a Stars checkout iff its payload is one of ours."""
            query = update.pre_checkout_query
            if query is None:
                return
            from olano.monetization.providers.stripe_provider import decode_reference

            ok = decode_reference(query.invoice_payload or "") is not None
            try:
                await query.answer(ok=ok, error_message=None if ok else "Invalid subscription.")
            except Exception as e:
                logger.warning("Telegram pre_checkout answer failed: %s", e)

        async def on_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            """Record a completed Stars payment as an active entitlement."""
            msg = update.message
            sp = getattr(msg, "successful_payment", None) if msg else None
            if sp is None:
                return
            from olano.monetization.providers.stars_provider import TelegramStarsProvider
            from olano.monetization.service import apply_entitlement_update

            payload = {
                "invoice_payload": getattr(sp, "invoice_payload", ""),
                "telegram_payment_charge_id": getattr(sp, "telegram_payment_charge_id", ""),
                "subscription_expiration_date": _dt_to_epoch(
                    getattr(sp, "subscription_expiration_date", None)
                ),
            }
            provider = TelegramStarsProvider(agent=self.agent_name or "", plan=None)
            payment_update = provider.parse_event(payload)
            if payment_update is None:
                logger.warning("Telegram successful_payment with unrecognized payload")
                return
            try:
                apply_entitlement_update(payment_update)
            except Exception as e:
                logger.error("Failed to record Stars payment: %s", e, exc_info=True)
            try:
                if msg is not None:
                    await self.send_with_retry(
                        str(msg.chat_id),
                        "✅ Subscription active — thanks! You now have full access.",
                    )
            except Exception as e:
                logger.debug("Telegram: could not confirm payment: %s", e)

        # Register handlers
        self._app.add_handler(CommandHandler("start", cmd_start))
        self._app.add_handler(CommandHandler("new", cmd_new))
        self._app.add_handler(CommandHandler("clear", cmd_new))
        self._app.add_handler(CommandHandler("status", cmd_status))
        self._app.add_handler(TGHandler(filters.TEXT & ~filters.COMMAND, on_text))
        self._app.add_handler(TGHandler(filters.PHOTO, on_photo))
        self._app.add_handler(TGHandler(filters.Document.ALL, on_document))
        self._app.add_handler(
            CallbackQueryHandler(on_approval_click, pattern=rf"^{_APPROVAL_CB_PREFIX}:")
        )
        self._app.add_handler(PreCheckoutQueryHandler(on_pre_checkout))
        self._app.add_handler(TGHandler(filters.SUCCESSFUL_PAYMENT, on_successful_payment))
        self._app.add_handler(TGHandler(filters.VOICE, on_voice))

        # Token fingerprint used in 409-conflict warnings so the operator can
        # immediately identify WHICH bot is being fought over without leaking
        # the full secret to the logs.
        token_fp = f"{token[:6]}…{token[-4:]}" if token and len(token) > 12 else "?"

        def _polling_error_callback(error: Exception) -> None:
            """Friendly one-line replacement for python-telegram-bot's raw traceback.

            We special-case ``Conflict`` (HTTP 409) because that's the single
            most common failure mode in multi-instance setups — and the
            stock traceback (130 lines, no agent identifier) makes triage
            painful. Other errors fall back to a compact WARNING line.
            """
            try:
                from telegram.error import Conflict
            except ImportError:  # pragma: no cover — defensive
                Conflict = ()  # type: ignore[assignment, misc]

            if isinstance(error, Conflict):
                logger.warning(
                    "[%s] Telegram 409 Conflict for bot %s — another process is polling "
                    "this token. Check for: (a) duplicate `olano serve`/`dashboard`, "
                    "(b) a sibling project (e.g. openclaw) using the same token, or "
                    "(c) a stray webhook (run `curl https://api.telegram.org/bot<TOKEN>/getWebhookInfo`).",
                    self.agent_name or "?",
                    token_fp,
                )
                return
            logger.warning(
                "[%s] Telegram polling error (bot %s): %s: %s",
                self.agent_name or "?",
                token_fp,
                type(error).__name__,
                error,
            )

        # Bring the bot up inside try/finally so the health watchdog ALWAYS
        # starts — even if initialize()/start_polling() raise on a transient
        # DNS failure (api.telegram.org briefly unresolvable, common right
        # after a single-agent restart). Without this the channel would be
        # attached with no poller AND no watchdog to retry it — "green but
        # dead" until a human restarts again. The watchdog detects the
        # half-started state (updater not running) and rebuilds it.
        try:
            await self._app.initialize()
            await self._app.start()

            # Defensive: clear any stale server-side webhook before polling.
            # If a webhook is registered, every `getUpdates` returns 409 Conflict.
            # `drop_pending_updates=False` keeps queued user messages intact.
            try:
                delete_webhook = getattr(self._app.bot, "delete_webhook", None)
                if callable(delete_webhook):
                    await delete_webhook(drop_pending_updates=False)
            except Exception as exc:
                logger.debug(
                    "[%s] Telegram delete_webhook pre-poll cleanup failed (non-fatal): %s",
                    self.agent_name or "?",
                    exc,
                )

            await self._app.updater.start_polling(
                allowed_updates=_ALLOWED_UPDATE_TYPES,
                error_callback=_polling_error_callback,
            )
            logger.info(
                "Telegram channel started (polling, agent=%s, bot=%s)",
                self.agent_name or "?",
                token_fp,
            )
        finally:
            # Watchdog: cheap get_me() probe + updater-running check; rebuilds
            # the SDK afresh on persistent failure so a stale httpx pool / DNS
            # state — or a bootstrap that just failed above — heals without a
            # daemon restart. See olano/channels/_health.py.
            self._start_health_watchdog()

    async def _health_check(self) -> None:
        app = self._app
        if app is None:
            return
        # A non-running updater means start_polling() never completed (e.g. the
        # bootstrap raised on a transient DNS failure): the bot is attached but
        # fetches no updates. Treat it as unhealthy so the watchdog rebuilds and
        # retries instead of reporting a false-healthy channel that get_me()
        # alone would pass once DNS recovers.
        updater = getattr(app, "updater", None)
        if updater is not None and not getattr(updater, "running", True):
            raise RuntimeError("Telegram updater is not running")
        await app.bot.get_me()

    async def _health_rebuild(self) -> None:
        old = self._app
        self._app = None
        if old is not None:
            for label, op in (
                ("updater.stop", lambda: old.updater.stop()),
                ("stop", lambda: old.stop()),
                ("shutdown", lambda: old.shutdown()),
            ):
                try:
                    await op()
                except Exception as e:
                    logger.debug("Telegram rebuild %s: %s", label, e)
        await self.start()

    async def stop(self) -> None:
        await self.cancel_background_tasks()
        self._watchdog_task = None
        if self._app:
            try:
                await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
            except Exception as e:
                logger.debug("Telegram stop error: %s", e)

    async def make_approval_handler(self, recipient: str, agent_name: str) -> Any | None:
        """Return a coroutine that prompts *recipient* with inline buttons.

        The handler posts one approval prompt per pending action (Once /
        Always / Deny), awaits the user's click, and maps the click back
        to a decision dict for the runner. Button clicks are routed via
        the Application's ``CallbackQueryHandler`` registered in ``start``.
        """
        if self._app is None:
            return None

        async def handle(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
            decisions: list[dict[str, Any]] = []
            for action in actions:
                decisions.append(await self._request_approval_button(recipient, agent_name, action))
            return decisions

        return handle

    async def _request_approval_button(
        self,
        recipient: str,
        agent_name: str,
        action: dict[str, Any],
    ) -> dict[str, Any]:
        """Post one approval prompt, wait for click, return a decision dict."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        tool_name = str(action.get("name") or "tool")
        args = action.get("args", {})
        if isinstance(args, dict):
            cmd = args.get("command") or args.get("cmd") or ""
            detail = cmd if cmd else _format_args_brief(args)
        else:
            detail = str(args)
        detail_preview = (detail or "").strip()[:500]

        prompt = (
            "🔒 *Approval needed*\n\n"
            f"Tool: `{_escape_md(tool_name)}`\n"
            f"Details: `{_escape_md(detail_preview or '(no args)')}`\n\n"
            "Choose:"
        )

        approval_id = secrets.token_hex(4)
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Once",
                        callback_data=f"{_APPROVAL_CB_PREFIX}:{approval_id}:once",
                    ),
                    InlineKeyboardButton(
                        "✅ Always",
                        callback_data=f"{_APPROVAL_CB_PREFIX}:{approval_id}:always",
                    ),
                    InlineKeyboardButton(
                        "❌ Deny",
                        callback_data=f"{_APPROVAL_CB_PREFIX}:{approval_id}:deny",
                    ),
                ]
            ]
        )

        try:
            sent = await self._app.bot.send_message(
                chat_id=int(recipient),
                text=prompt,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            message_id = getattr(sent, "message_id", None)
        except Exception as e:
            logger.warning("Telegram: could not post approval prompt: %s", e)
            return {
                "type": "reject",
                "message": "Channel could not display the approval prompt.",
            }

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending_approvals[approval_id] = {
            "future": future,
            "chat_id": int(recipient),
            "message_id": message_id,
            "tool_name": tool_name,
            "agent_name": agent_name,
        }

        self.pause_typing(recipient)
        try:
            choice = await asyncio.wait_for(future, timeout=_APPROVAL_TIMEOUT_SECONDS)
        except TimeoutError:
            choice = "timeout"
            try:
                if message_id:
                    await self._app.bot.edit_message_reply_markup(
                        chat_id=int(recipient),
                        message_id=message_id,
                        reply_markup=None,
                    )
                await self._app.bot.send_message(
                    chat_id=int(recipient),
                    text=f"⏱️ Approval timed out — denying `{_escape_md(tool_name)}`",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
        finally:
            self._pending_approvals.pop(approval_id, None)
            self.resume_typing(recipient)

        if choice == "once":
            return {"type": "approve"}
        if choice == "always":
            try:
                from olano.security.hitl import remember_tool_rule

                remember_tool_rule(agent_name, tool_name, "*")
                logger.info(
                    "Telegram: persisted always-allow rule [agent=%s tool=%s]",
                    agent_name,
                    tool_name,
                )
            except Exception as e:
                logger.warning("Telegram: could not persist always-allow rule: %s", e)
            return {"type": "approve"}
        # deny or timeout
        reason = "Denied via Telegram" if choice == "deny" else "Approval timed out"
        return {"type": "reject", "message": reason}

    def _monetization_welcome(self, chat_id: str, uid: Any) -> str:
        """Start the trial (idempotently) and build the /start welcome copy.

        Returns the trial + price + how-to-pay intro, or "" on any failure (the
        caller then falls back to the generic hello).
        """
        try:
            from olano.monetization.gate import evaluate_access
            from olano.monetization.service import welcome_text
            from olano.security.request_context import push_request_context

            config = getattr(self, "agent_config", None)
            agent = self.agent_name or ""
            with push_request_context(
                source="channel", channel="telegram", user_id=str(uid), is_owner=False
            ):
                # Starts the trial on first /start; a no-op if already started.
                evaluate_access(agent, "telegram", config)
                return welcome_text(agent, "telegram", config, user_id=str(uid))
        except Exception as e:
            logger.debug("Telegram: monetization welcome failed: %s", e)
            return ""

    async def send_subscription_offer(self, recipient: str, offer: Any) -> bool:
        """Present a subscription offer in-chat; return True if anything sent.

        Renders whatever rails the offer carries: a Stripe Checkout URL becomes
        an inline "Subscribe" button; a Telegram Stars invoice is sent natively
        via ``send_invoice`` (recurring, ``currency="XTR"``). Both can appear.
        """
        if self._app is None:
            return False
        sent = False
        text = getattr(offer, "text", "") or "Subscribe to continue."
        stripe_url = getattr(offer, "stripe_url", "") or ""
        stars_invoice = getattr(offer, "stars_invoice", None)

        if stripe_url:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup

            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("💳 Subscribe", url=stripe_url)]]
            )
            try:
                await self._app.bot.send_message(
                    chat_id=int(recipient), text=text, reply_markup=keyboard
                )
                sent = True
            except Exception as e:
                logger.warning("Telegram: could not send Stripe offer: %s", e)

        if stars_invoice:
            from telegram import LabeledPrice

            try:
                prices = [
                    LabeledPrice(str(p["label"]), int(p["amount"]))
                    for p in stars_invoice.get("prices", [])
                ]
                await self._app.bot.send_invoice(
                    chat_id=int(recipient),
                    title=stars_invoice.get("title", "Subscription"),
                    description=stars_invoice.get("description", "")
                    or "Subscribe for full access.",
                    payload=stars_invoice.get("payload", ""),
                    provider_token="",  # empty for Telegram Stars (XTR)
                    currency=stars_invoice.get("currency", "XTR"),
                    prices=prices,
                    subscription_period=stars_invoice.get("subscription_period"),
                )
                sent = True
            except Exception as e:
                logger.warning("Telegram: could not send Stars invoice: %s", e)

        return sent

    async def send_message(self, recipient: str, text: str) -> SendResult:
        """
        Send a single chunk and return a SendResult.

        Tries ``parse_mode="Markdown"`` first. If Telegram rejects the text
        because of invalid entities, retries once with ``parse_mode=None``.
        Connection-level failures are returned as retryable so
        ``send_with_retry`` can back off.
        """
        if self._app is None:
            return SendResult(success=False, error="telegram app not initialized")

        try:
            tg_msg = await self._app.bot.send_message(
                chat_id=int(recipient), text=text, parse_mode="Markdown"
            )
            return SendResult(success=True, message_id=str(getattr(tg_msg, "message_id", "") or ""))
        except Exception as e:
            err = str(e)
            if any(hint in err.lower() for hint in _MARKDOWN_ERROR_HINTS):
                try:
                    tg_msg = await self._app.bot.send_message(chat_id=int(recipient), text=text)
                    return SendResult(
                        success=True,
                        message_id=str(getattr(tg_msg, "message_id", "") or ""),
                    )
                except Exception as e2:
                    err = str(e2)
            return SendResult(
                success=False,
                error=err,
                retryable=is_retryable_error(err),
            )

    async def send(self, recipient: str, text: str) -> None:
        """Single-chunk send — raises on failure. Prefer ``send_with_retry``."""
        result = await self.send_message(recipient, text)
        if not result.success:
            raise RuntimeError(result.error or "telegram send failed")

    async def send_photo(self, recipient: str, photo_path: str, caption: str = "") -> None:
        if self._app:
            with open(photo_path, "rb") as f:
                await self._app.bot.send_photo(chat_id=int(recipient), photo=f, caption=caption)

    async def send_document(self, recipient: str, file_path: str, caption: str = "") -> None:
        if self._app:
            with open(file_path, "rb") as f:
                await self._app.bot.send_document(
                    chat_id=int(recipient), document=f, caption=caption
                )

    @staticmethod
    def _env(key: str) -> str | None:
        return os.environ.get(key)
