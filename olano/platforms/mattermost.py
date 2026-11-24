"""Mattermost gateway adapter.

Connects to a self-hosted (or cloud) Mattermost instance via its REST API
(v4) and WebSocket for real-time events.  No external Mattermost library
required — uses aiohttp which is already a Olano dependency.

Environment variables:
    MATTERMOST_URL              Server URL (e.g. https://mm.example.com)
    MATTERMOST_TOKEN            Bot token or personal-access token
    MATTERMOST_ALLOWED_USERS    Comma-separated user IDs
    MATTERMOST_HOME_CHANNEL     Channel ID for cron/notification delivery
    MATTERMOST_REQUIRE_MENTION  Legacy channel mention gate (default: true)
    MATTERMOST_FREE_RESPONSE_CHANNELS  Channel IDs that reply without mention

Config extras (``platforms.mattermost.extra``):
    group_respond   "all" | "mentions" | "none" — which channel messages
                    trigger a reply; wins over MATTERMOST_REQUIRE_MENTION
    group_listen    "off" | "mentions" | "all" — which non-responded channel
                    messages are recorded to the persistent observed-messages
                    store (default: off)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional, cast

from olano.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from olano.platforms.config import Platform, PlatformConfig
from olano.platforms.helpers import MessageDeduplicator

logger = logging.getLogger(__name__)

# Mattermost post size limit (server default is 16383, but 4000 is the
# practical limit for readable messages — matching OpenClaw's choice).
MAX_POST_LENGTH = 4000

# Channel type codes returned by the Mattermost API.
_CHANNEL_TYPE_MAP = {
    "D": "dm",
    "G": "group",
    "P": "group",  # private channel → treat as group
    "O": "channel",
}

# Reconnect parameters (exponential backoff).
_RECONNECT_BASE_DELAY = 2.0
_RECONNECT_MAX_DELAY = 60.0
_RECONNECT_JITTER = 0.2


def check_mattermost_requirements() -> bool:
    """Return True if the Mattermost adapter can be used."""
    token = os.getenv("MATTERMOST_TOKEN", "")
    url = os.getenv("MATTERMOST_URL", "")
    if not token:
        logger.debug("Mattermost: MATTERMOST_TOKEN not set")
        return False
    if not url:
        logger.warning("Mattermost: MATTERMOST_URL not set")
        return False
    try:
        import aiohttp  # noqa: F401

        return True
    except ImportError:
        logger.warning("Mattermost: aiohttp not installed")
        return False


class MattermostAdapter(BasePlatformAdapter):
    """Gateway adapter for Mattermost (self-hosted or cloud)."""

    # Post updates let a long turn's progress live in one post.
    PROGRESS_MODE = "edit"

    def __init__(self, config: PlatformConfig, agent_name: str = ""):
        super().__init__(config, Platform.MATTERMOST, agent_name=agent_name)

        self._base_url: str = (
            config.extra.get("url", "") or os.getenv("MATTERMOST_URL", "")
        ).rstrip("/")
        self._token: str = (config.token or "") or os.getenv("MATTERMOST_TOKEN", "")

        self._bot_user_id: str = ""
        self._bot_username: str = ""

        # aiohttp session + websocket handle
        self._session: Any = None  # aiohttp.ClientSession
        self._ws: Any = None  # aiohttp.ClientWebSocketResponse
        self._ws_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._closing = False

        # Reply mode: "thread" to nest replies, "off" for flat messages.
        self._reply_mode: str = (
            config.extra.get("reply_mode", "") or os.getenv("MATTERMOST_REPLY_MODE", "off")
        ).lower()

        # Dedup cache (prevent reprocessing)
        self._dedup = MessageDeduplicator()

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def _api_get(self, path: str) -> dict[str, Any]:
        """GET /api/v4/{path}."""
        import aiohttp

        url = f"{self._base_url}/api/v4/{path.lstrip('/')}"
        try:
            async with self._session.get(
                url, headers=self._headers(), timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.error("MM API GET %s → %s: %s", path, resp.status, body[:200])
                    return {}
                return cast(dict[str, Any], await resp.json())
        except aiohttp.ClientError as exc:
            logger.error("MM API GET %s network error: %s", path, exc)
            return {}

    async def _api_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /api/v4/{path} with JSON body."""
        import aiohttp

        url = f"{self._base_url}/api/v4/{path.lstrip('/')}"
        try:
            async with self._session.post(
                url, headers=self._headers(), json=payload, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.error("MM API POST %s → %s: %s", path, resp.status, body[:200])
                    return {}
                return cast(dict[str, Any], await resp.json())
        except aiohttp.ClientError as exc:
            logger.error("MM API POST %s network error: %s", path, exc)
            return {}

    async def _api_put(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """PUT /api/v4/{path} with JSON body."""
        import aiohttp

        url = f"{self._base_url}/api/v4/{path.lstrip('/')}"
        try:
            async with self._session.put(url, headers=self._headers(), json=payload) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.error("MM API PUT %s → %s: %s", path, resp.status, body[:200])
                    return {}
                return cast(dict[str, Any], await resp.json())
        except aiohttp.ClientError as exc:
            logger.error("MM API PUT %s network error: %s", path, exc)
            return {}

    async def _upload_file(
        self,
        channel_id: str,
        file_data: bytes,
        filename: str,
        content_type: str = "application/octet-stream",
    ) -> Optional[str]:
        """Upload a file and return its file ID, or None on failure."""
        import aiohttp

        url = f"{self._base_url}/api/v4/files"
        form = aiohttp.FormData()
        form.add_field("channel_id", channel_id)
        form.add_field(
            "files",
            file_data,
            filename=filename,
            content_type=content_type,
        )
        headers = {"Authorization": f"Bearer {self._token}"}
        async with self._session.post(
            url, headers=headers, data=form, timeout=aiohttp.ClientTimeout(total=60)
        ) as resp:
            if resp.status >= 400:
                body = await resp.text()
                logger.error("MM file upload → %s: %s", resp.status, body[:200])
                return None
            data = await resp.json()
            infos = data.get("file_infos", [])
            return infos[0]["id"] if infos else None

    # ------------------------------------------------------------------
    # Required overrides
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Connect to Mattermost and start the WebSocket listener."""
        import aiohttp

        if not self._base_url or not self._token:
            logger.error("Mattermost: URL or token not configured")
            return False

        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        self._closing = False

        # Verify credentials and fetch bot identity.
        me = await self._api_get("users/me")
        if not me or "id" not in me:
            logger.error(
                "Mattermost: failed to authenticate — check MATTERMOST_TOKEN and MATTERMOST_URL"
            )
            await self._session.close()
            return False

        self._bot_user_id = me["id"]
        self._bot_username = me.get("username", "")
        logger.info(
            "Mattermost: authenticated as @%s (%s) on %s",
            self._bot_username,
            self._bot_user_id,
            self._base_url,
        )

        # Start WebSocket in background.
        self._ws_task = asyncio.create_task(self._ws_loop())
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        """Disconnect from Mattermost."""
        self._closing = True

        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):
                pass

        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()

        if self._ws:
            await self._ws.close()
            self._ws = None

        if self._session and not self._session.closed:
            await self._session.close()

        logger.info("Mattermost: disconnected")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Send a message (or multiple chunks) to a channel."""
        if not content:
            return SendResult(success=True)

        formatted = self.format_message(content)
        chunks = self.truncate_message(formatted, MAX_POST_LENGTH)

        last_id = None
        for chunk in chunks:
            payload: dict[str, Any] = {
                "channel_id": chat_id,
                "message": chunk,
            }
            # Thread support: reply_to is the root post ID.
            if reply_to and self._reply_mode == "thread":
                payload["root_id"] = reply_to

            data = await self._api_post("posts", payload)
            if not data or "id" not in data:
                return SendResult(success=False, error="Failed to create post")
            last_id = data["id"]

        return SendResult(success=True, message_id=last_id)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        """Return channel name and type."""
        data = await self._api_get(f"channels/{chat_id}")
        if not data:
            return {"name": chat_id, "type": "channel"}

        ch_type = _CHANNEL_TYPE_MAP.get(data.get("type", "O"), "channel")
        display_name = data.get("display_name") or data.get("name") or chat_id
        return {"name": display_name, "type": ch_type}

    # ------------------------------------------------------------------
    # Optional overrides
    # ------------------------------------------------------------------

    async def send_typing(self, chat_id: str, metadata: Optional[dict[str, Any]] = None) -> None:
        """Send a typing indicator."""
        await self._api_post(
            f"users/{self._bot_user_id}/typing",
            {"channel_id": chat_id},
        )

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        """Edit an existing post."""
        formatted = self.format_message(content)
        data = await self._api_put(
            f"posts/{message_id}/patch",
            {"message": formatted},
        )
        if not data or "id" not in data:
            return SendResult(success=False, error="Failed to edit post")
        return SendResult(success=True, message_id=data["id"])

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Download an image and upload it as a file attachment."""
        return await self._send_url_as_file(chat_id, image_url, caption, reply_to, "image")

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        """Upload a local image file."""
        return await self._send_local_file(chat_id, image_path, caption, reply_to)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        """Upload a local file as a document."""
        return await self._send_local_file(chat_id, file_path, caption, reply_to, file_name)

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        """Upload an audio file."""
        return await self._send_local_file(chat_id, audio_path, caption, reply_to)

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        """Upload a video file."""
        return await self._send_local_file(chat_id, video_path, caption, reply_to)

    def format_message(self, content: str) -> str:
        """Mattermost uses standard Markdown — mostly pass through.

        Strip image markdown into plain links (files are uploaded separately).
        """
        # Convert ![alt](url) to just the URL — Mattermost renders
        # image URLs as inline previews automatically.
        content = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\2", content)
        return content

    # ------------------------------------------------------------------
    # File helpers
    # ------------------------------------------------------------------

    async def _send_url_as_file(
        self,
        chat_id: str,
        url: str,
        caption: Optional[str],
        reply_to: Optional[str],
        kind: str = "file",
    ) -> SendResult:
        """Download a URL and upload it as a file attachment."""
        from olano.platforms.url_safety import is_safe_url

        if not is_safe_url(url):
            logger.warning("Mattermost: blocked unsafe URL (SSRF protection)")
            return await self.send(chat_id, f"{caption or ''}\n{url}".strip(), reply_to)

        import asyncio

        import aiohttp

        file_data = None
        ct = "application/octet-stream"
        fname = url.rsplit("/", 1)[-1].split("?")[0] or f"{kind}.png"

        for attempt in range(3):
            try:
                async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status >= 500 or resp.status == 429:
                        if attempt < 2:
                            logger.debug(
                                "Mattermost download retry %d/2 for %s (status %d)",
                                attempt + 1,
                                url[:80],
                                resp.status,
                            )
                            await asyncio.sleep(1.5 * (attempt + 1))
                            continue
                    if resp.status >= 400:
                        return await self.send(chat_id, f"{caption or ''}\n{url}".strip(), reply_to)
                    file_data = await resp.read()
                    ct = resp.content_type or "application/octet-stream"
                    break
            except (TimeoutError, aiohttp.ClientError) as exc:
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                logger.warning(
                    "Mattermost: failed to download %s after %d attempts: %s", url, attempt + 1, exc
                )
                return await self.send(chat_id, f"{caption or ''}\n{url}".strip(), reply_to)

        if file_data is None:
            logger.warning("Mattermost: download returned no data for %s", url)
            return await self.send(chat_id, f"{caption or ''}\n{url}".strip(), reply_to)

        file_id = await self._upload_file(chat_id, file_data, fname, ct)
        if not file_id:
            return await self.send(chat_id, f"{caption or ''}\n{url}".strip(), reply_to)

        payload: dict[str, Any] = {
            "channel_id": chat_id,
            "message": caption or "",
            "file_ids": [file_id],
        }
        if reply_to and self._reply_mode == "thread":
            payload["root_id"] = reply_to

        data = await self._api_post("posts", payload)
        if not data or "id" not in data:
            return SendResult(success=False, error="Failed to post with file")
        return SendResult(success=True, message_id=data["id"])

    async def _send_local_file(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str],
        reply_to: Optional[str],
        file_name: Optional[str] = None,
    ) -> SendResult:
        """Upload a local file and attach it to a post."""
        import mimetypes

        p = Path(file_path)
        if not p.exists():
            return await self.send(
                chat_id, f"{caption or ''}\n(file not found: {file_path})", reply_to
            )

        fname = file_name or p.name
        ct = mimetypes.guess_type(fname)[0] or "application/octet-stream"
        file_data = p.read_bytes()

        file_id = await self._upload_file(chat_id, file_data, fname, ct)
        if not file_id:
            return SendResult(success=False, error="File upload failed")

        payload: dict[str, Any] = {
            "channel_id": chat_id,
            "message": caption or "",
            "file_ids": [file_id],
        }
        if reply_to and self._reply_mode == "thread":
            payload["root_id"] = reply_to

        data = await self._api_post("posts", payload)
        if not data or "id" not in data:
            return SendResult(success=False, error="Failed to post with file")
        return SendResult(success=True, message_id=data["id"])

    # ------------------------------------------------------------------
    # Group gating (group_respond / group_listen)
    # ------------------------------------------------------------------

    def _mattermost_require_mention(self) -> bool:
        """Legacy channel mention gate — ``MATTERMOST_REQUIRE_MENTION`` env
        (default: true), kept exactly as the original inline resolution."""
        return os.getenv("MATTERMOST_REQUIRE_MENTION", "true").lower() not in (
            "false",
            "0",
            "no",
        )

    def _mattermost_group_respond(self) -> str:
        """Effective ``group_respond`` mode ("all" | "mentions" | "none").

        ``extra.group_respond`` wins when set; otherwise the legacy
        ``MATTERMOST_REQUIRE_MENTION`` resolution decides (default: "mentions").
        """
        from olano.platforms.group_modes import resolve_group_respond

        return resolve_group_respond(
            self.config.extra, legacy_require_mention=self._mattermost_require_mention()
        )

    def _mattermost_group_listen(self) -> str:
        """Effective ``group_listen`` mode ("off" | "mentions" | "all").

        Mattermost never had a legacy passive-listen toggle, so unset is "off".
        """
        from olano.platforms.group_modes import resolve_group_listen

        return resolve_group_listen(self.config.extra, legacy_passive_listen=False)

    def _maybe_record_observed(
        self,
        post: dict[str, Any],
        data: dict[str, Any],
        *,
        chat_type: str,
        mentioned: bool,
    ) -> None:
        """Record a channel message the mention gate declined to answer.

        Feeds the persistent observed-messages store (``chat_directory``) per
        the effective ``group_listen`` mode: "mentions" records only messages
        that addressed the bot, "all" records every channel message the bot
        received. Called only from the drop branch — recording must never
        break message handling, so everything here is best-effort.
        """
        try:
            from olano.platforms.group_modes import should_record

            if not should_record(self._mattermost_group_listen(), mentioned):
                return
            text = str(post.get("message", "") or "")
            if not text.strip():
                return
            from olano.platforms.chat_directory import record_observed_event

            # ``sender_name`` on the WS event is the @-handle — pass it as
            # ``username``, same as the responded path's build_source call.
            sender_name = str(data.get("sender_name", "") or "").lstrip("@")
            source = self.build_source(
                chat_id=str(post.get("channel_id", "") or ""),
                chat_name=data.get("channel_display_name") or data.get("channel_name") or None,
                chat_type=chat_type,
                user_id=str(post.get("user_id", "") or "") or None,
                username=sender_name or None,
                thread_id=post.get("root_id") or None,
            )
            record_observed_event(self, source, text)
        except Exception as exc:
            logger.debug("Mattermost: observed-store capture failed: %s", exc)

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------

    async def _ws_loop(self) -> None:
        """Connect to the WebSocket and listen for events, reconnecting on failure."""
        delay = _RECONNECT_BASE_DELAY
        while not self._closing:
            try:
                await self._ws_connect_and_listen()
                # Clean disconnect — reset delay.
                delay = _RECONNECT_BASE_DELAY
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if self._closing:
                    return
                # Detect permanent auth/permission failures that will never
                # succeed on retry — stop reconnecting instead of looping forever.
                import aiohttp

                err_str = str(exc).lower()
                if isinstance(exc, aiohttp.WSServerHandshakeError) and exc.status in (401, 403):
                    logger.error(
                        "Mattermost WS auth failed (HTTP %d) — stopping reconnect", exc.status
                    )
                    return
                if "401" in err_str or "403" in err_str or "unauthorized" in err_str:
                    logger.error("Mattermost WS permanent error: %s — stopping reconnect", exc)
                    return
                logger.warning("Mattermost WS error: %s — reconnecting in %.0fs", exc, delay)

            if self._closing:
                return

            # Exponential backoff with jitter.
            import random

            jitter = delay * _RECONNECT_JITTER * random.random()
            await asyncio.sleep(delay + jitter)
            delay = min(delay * 2, _RECONNECT_MAX_DELAY)

    async def _ws_connect_and_listen(self) -> None:
        """Single WebSocket session: connect, authenticate, process events."""
        # Build WS URL: https:// → wss://, http:// → ws://
        ws_url = re.sub(r"^http", "ws", self._base_url) + "/api/v4/websocket"
        logger.info("Mattermost: connecting to %s", ws_url)

        self._ws = await self._session.ws_connect(ws_url, heartbeat=30.0)

        # Authenticate via the WebSocket.
        auth_msg = {
            "seq": 1,
            "action": "authentication_challenge",
            "data": {"token": self._token},
        }
        await self._ws.send_json(auth_msg)
        logger.info("Mattermost: WebSocket connected and authenticated")

        async for raw_msg in self._ws:
            if self._closing:
                return

            if raw_msg.type in (
                raw_msg.type.TEXT,
                raw_msg.type.BINARY,
            ):
                try:
                    event = json.loads(raw_msg.data)
                except (json.JSONDecodeError, TypeError):
                    continue
                await self._handle_ws_event(event)
            elif raw_msg.type in (
                raw_msg.type.ERROR,
                raw_msg.type.CLOSE,
                raw_msg.type.CLOSING,
                raw_msg.type.CLOSED,
            ):
                logger.info("Mattermost: WebSocket closed (%s)", raw_msg.type)
                break

    async def _handle_ws_event(self, event: dict[str, Any]) -> None:
        """Process a single WebSocket event."""
        event_type = event.get("event")
        if event_type != "posted":
            return

        data = event.get("data", {})
        raw_post_str = data.get("post")
        if not raw_post_str:
            return

        try:
            post = json.loads(raw_post_str)
        except (json.JSONDecodeError, TypeError):
            return

        # Ignore own messages.
        if post.get("user_id") == self._bot_user_id:
            return

        # Ignore system posts.
        if post.get("type"):
            return

        post_id = post.get("id", "")

        # Dedup.
        if self._dedup.is_duplicate(post_id):
            return

        # Build message event.
        channel_id = post.get("channel_id", "")
        channel_type_raw = data.get("channel_type", "O")
        chat_type = _CHANNEL_TYPE_MAP.get(channel_type_raw, "channel")

        # For DMs, user_id is sufficient.  For channels, check for @mention.
        message_text = post.get("message", "")

        # Group gating for non-DM channels.
        # Config:
        #   extra.group_respond: "all" | "mentions" | "none" (wins when set)
        #   MATTERMOST_REQUIRE_MENTION: legacy mention gate (default: true)
        #   MATTERMOST_FREE_RESPONSE_CHANNELS: Channel IDs where bot responds without mention
        if channel_type_raw != "D":
            free_channels_raw = os.getenv("MATTERMOST_FREE_RESPONSE_CHANNELS", "")
            free_channels = {ch.strip() for ch in free_channels_raw.split(",") if ch.strip()}
            is_free_channel = channel_id in free_channels

            mention_patterns = [
                f"@{self._bot_username}",
                f"@{self._bot_user_id}",
            ]
            has_mention = any(
                pattern.lower() in message_text.lower() for pattern in mention_patterns
            )

            from olano.platforms.group_modes import should_respond

            respond = self._mattermost_group_respond()
            # A free-response channel is more specific than the global mode,
            # so it keeps answering even under group_respond="none".
            if not is_free_channel and not should_respond(respond, has_mention):
                logger.debug(
                    "Mattermost: skipping non-DM message (group_respond=%s, channel=%s)",
                    respond,
                    channel_id,
                )
                # Group listening: the unanswered channel message may still be
                # persisted to the observed-messages store instead of vanishing.
                self._maybe_record_observed(post, data, chat_type=chat_type, mentioned=has_mention)
                return

            # Strip @mention from the message text so the agent sees clean input.
            if has_mention:
                for pattern in mention_patterns:
                    message_text = re.sub(
                        re.escape(pattern), "", message_text, flags=re.IGNORECASE
                    ).strip()

        # Resolve sender info.
        sender_id = post.get("user_id", "")
        sender_name = data.get("sender_name", "").lstrip("@") or sender_id

        # Thread support: if the post is in a thread, use root_id.
        thread_id = post.get("root_id") or None

        # Determine message type.
        file_ids = post.get("file_ids") or []
        msg_type = MessageType.TEXT
        if message_text.startswith("/"):
            msg_type = MessageType.COMMAND

        # Download file attachments immediately (URLs require auth headers
        # that downstream tools won't have).
        media_urls: list[str] = []
        media_types: list[str] = []
        for fid in file_ids:
            try:
                file_info = await self._api_get(f"files/{fid}/info")
                fname = file_info.get("name", f"file_{fid}")
                ext = Path(fname).suffix or ""
                mime = file_info.get("mime_type", "application/octet-stream")

                import aiohttp

                dl_url = f"{self._base_url}/api/v4/files/{fid}"
                async with self._session.get(
                    dl_url,
                    headers={"Authorization": f"Bearer {self._token}"},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status < 400:
                        file_data = await resp.read()
                        from olano.platforms.base import (
                            cache_document_from_bytes,
                            cache_image_from_bytes,
                        )

                        if mime.startswith("image/"):
                            local_path = cache_image_from_bytes(file_data, ext or ".png")
                            media_urls.append(local_path)
                            media_types.append(mime)
                        elif mime.startswith("audio/"):
                            from olano.platforms.base import cache_audio_from_bytes

                            local_path = cache_audio_from_bytes(file_data, ext or ".ogg")
                            media_urls.append(local_path)
                            media_types.append(mime)
                        else:
                            local_path = cache_document_from_bytes(file_data, fname)
                            media_urls.append(local_path)
                            media_types.append(mime)
                    else:
                        logger.warning(
                            "Mattermost: failed to download file %s: HTTP %s", fid, resp.status
                        )
            except Exception as exc:
                logger.warning("Mattermost: error downloading file %s: %s", fid, exc)

        # Set message type based on downloaded media types.
        if media_types and msg_type == MessageType.TEXT:
            if any(m.startswith("image/") for m in media_types):
                msg_type = MessageType.PHOTO
            elif any(m.startswith("audio/") for m in media_types):
                msg_type = MessageType.VOICE
            elif media_types:
                msg_type = MessageType.DOCUMENT

        source = self.build_source(
            chat_id=channel_id,
            chat_type=chat_type,
            user_id=sender_id,
            # Mattermost's ``sender_name`` is the @-handle, not a display name.
            # Pass it as ``username`` so format_sender_prefix renders it as @handle.
            username=sender_name,
            thread_id=thread_id,
        )

        # Per-channel ephemeral prompt
        from olano.platforms.base import resolve_channel_prompt

        _channel_prompt = resolve_channel_prompt(
            self.config.extra,
            channel_id,
            None,
        )

        msg_event = MessageEvent(
            text=message_text,
            message_type=msg_type,
            source=source,
            raw_message=post,
            message_id=post_id,
            media_urls=cast(list[str], media_urls if media_urls else None),
            media_types=cast(list[str], media_types if media_types else None),
            channel_prompt=_channel_prompt,
        )

        await self.handle_message(msg_event)
