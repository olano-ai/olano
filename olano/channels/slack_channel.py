"""Slack + WhatsApp channel adapters."""

from __future__ import annotations

import logging
from typing import Any

from olano.channels.base import (
    BaseChannel,
    ChannelMessage,
    SendResult,
    is_retryable_error,
    truncate_message,
)
from olano.vault.resolver import lookup_credential

logger = logging.getLogger(__name__)


from olano.channels._health import HealthWatchdogMixin


class SlackChannel(HealthWatchdogMixin, BaseChannel):
    """
    Slack bot adapter using Socket Mode.

    Config keys:
        bot_token (str): Slack Bot OAuth token (xoxb-...)
        app_token (str): Slack App-level token (xapp-...) for Socket Mode
        max_message_length (int): per-block char limit (default 3000)
    """

    SUPPORTS_PROGRESS = True

    name = "slack"
    max_message_length: int = 3000

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._handler_obj: Any = None
        self._app: Any = None
        override = config.get("max_message_length")
        if override:
            self.max_message_length = int(override)

    async def start(self) -> None:
        try:
            from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
            from slack_bolt.async_app import AsyncApp

            # Injected per-agent by the runtime; this resolves the common value
            # for a channel constructed outside the boot path.
            bot_token = self.config.get("bot_token") or lookup_credential("SLACK_BOT_TOKEN")
            app_token = self.config.get("app_token") or lookup_credential("SLACK_APP_TOKEN")

            if not bot_token or not app_token:
                raise ValueError(
                    "Slack tokens not configured. Set SLACK_BOT_TOKEN and SLACK_APP_TOKEN."
                )

            app = AsyncApp(token=bot_token)
            self._app = app

            @app.event("message")
            async def on_message(event: dict, say: Any) -> None:
                if event.get("bot_id"):
                    return
                text = event.get("text", "").strip()
                if not text:
                    return

                user_id = event.get("user", "unknown")
                channel_id = event.get("channel", "unknown")
                thread_ts = event.get("thread_ts") or event.get("ts", "")

                msg = ChannelMessage(
                    text=text,
                    user_id=user_id,
                    channel="slack",
                    thread_id=f"slack_{channel_id}_{thread_ts}",
                    raw=event,
                )
                try:
                    response = await self.handle(msg)
                    for chunk in truncate_message(response, self.max_message_length):
                        await say(chunk, thread_ts=event.get("ts"))
                except Exception as e:
                    from olano.core.actionable_errors import channel_text

                    logger.error("Slack handler error: %s", e)
                    await say(channel_text(e))

            self._handler_obj = AsyncSocketModeHandler(app, app_token)
            import asyncio

            task = asyncio.create_task(self._handler_obj.start_async())
            self.track_task(task)
            logger.info("Slack channel starting (Socket Mode)")
        except ImportError:
            raise ImportError("Install slack-bolt: pip install slack-bolt")

        # Watchdog: cheap auth_test() probe; rebuild on persistent
        # failure so a wedged Socket-Mode websocket heals without a
        # daemon restart. See olano/channels/_health.py.
        self._start_health_watchdog()

    async def _health_check(self) -> None:
        # auth_test is Slack's standard ping — confirms both token
        # validity and reachability of slack.com in one call.
        if self._app is None or getattr(self._app, "client", None) is None:
            raise RuntimeError("Slack app not initialised")
        await self._app.client.auth_test()

    async def _health_rebuild(self) -> None:
        if self._handler_obj is not None:
            try:
                await self._handler_obj.close_async()
            except Exception as e:
                logger.debug("Slack rebuild close: %s", e)
        self._handler_obj = None
        self._app = None
        await self.start()

    async def stop(self) -> None:
        await self.cancel_background_tasks()
        self._watchdog_task = None
        if self._handler_obj:
            await self._handler_obj.close_async()

    async def send_message(self, recipient: str, text: str) -> SendResult:
        """Send a chunk via the Slack Web API. Returns SendResult with message ts."""
        if self._app is None or not hasattr(self._app, "client"):
            return SendResult(success=False, error="Slack app not initialized", retryable=False)
        try:
            resp = await self._app.client.chat_postMessage(channel=recipient, text=text)
            ts = (
                resp.get("ts")
                if isinstance(resp, dict)
                else getattr(resp, "get", lambda _: None)("ts")
            )
            return SendResult(success=True, message_id=str(ts or ""))
        except Exception as e:
            err = str(e)
            return SendResult(success=False, error=err, retryable=is_retryable_error(err))

    async def send(self, recipient: str, text: str) -> None:
        """Proactive send via Web API. Raises on failure."""
        result = await self.send_message(recipient, text)
        if not result.success:
            raise RuntimeError(result.error or "slack send failed")


class WhatsAppChannel(BaseChannel):
    """
    WhatsApp channel — bridge to a Node.js Baileys process via subprocess IPC.

    Requires the whatsapp-bridge/ directory with a Node.js Baileys script.
    The bridge sends JSON events to stdout and reads commands from stdin.

    Config keys:
        bridge_script (str): path to the Node.js bridge script
        max_message_length (int): per-message char limit (default 4096)
    """

    name = "whatsapp"
    max_message_length: int = 4096

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._proc: Any = None
        override = config.get("max_message_length")
        if override:
            self.max_message_length = int(override)

    async def start(self) -> None:
        import asyncio
        import shutil

        node = shutil.which("node")
        if not node:
            raise RuntimeError("Node.js not found — required for WhatsApp bridge")

        script = self.config.get("bridge_script", "whatsapp-bridge/index.js")
        self._proc = await asyncio.create_subprocess_exec(
            node,
            script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )

        task = asyncio.create_task(self._read_loop())
        self.track_task(task)
        logger.info("WhatsApp bridge started (pid=%s)", self._proc.pid)

    async def _read_loop(self) -> None:
        import json

        async for raw_line in self._proc.stdout:
            try:
                event = json.loads(raw_line.decode())
                if event.get("type") == "message":
                    msg = ChannelMessage(
                        text=event["text"],
                        user_id=event["from"],
                        channel="whatsapp",
                        thread_id=f"wa_{event['from']}",
                        raw=event,
                    )
                    response = await self.handle(msg)
                    await self.send_with_retry(event["from"], response)
            except Exception as e:
                logger.warning("WhatsApp bridge event error: %s", e)

    async def stop(self) -> None:
        await self.cancel_background_tasks()
        if self._proc:
            self._proc.terminate()

    async def send_message(self, recipient: str, text: str) -> SendResult:
        """Write a single framed message to the bridge's stdin."""
        import json

        if not (self._proc and self._proc.stdin):
            return SendResult(success=False, error="WhatsApp bridge not running", retryable=False)
        try:
            payload = json.dumps({"to": recipient, "text": text}) + "\n"
            self._proc.stdin.write(payload.encode())
            await self._proc.stdin.drain()
            return SendResult(success=True)
        except Exception as e:
            err = str(e)
            return SendResult(success=False, error=err, retryable=is_retryable_error(err))

    async def send(self, recipient: str, text: str) -> None:
        result = await self.send_message(recipient, text)
        if not result.success:
            raise RuntimeError(result.error or "whatsapp send failed")
