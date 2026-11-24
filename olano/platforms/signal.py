"""Signal messenger platform adapter.

Connects to a signal-cli daemon running in HTTP mode.
Inbound messages arrive via SSE (Server-Sent Events) streaming.
Outbound messages and actions use JSON-RPC 2.0 over HTTP.

Based on PR #268 by ibhagwan, rebuilt with bug fixes.

Requires:
  - signal-cli installed and running: signal-cli daemon --http 127.0.0.1:8080
  - SIGNAL_HTTP_URL and SIGNAL_ACCOUNT environment variables set
"""

import asyncio
import base64
import json
import logging
import os
import platform as platform_mod
import random
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, unquote, urlparse

import httpx

from olano.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_audio_from_bytes,
    cache_document_from_bytes,
    cache_image_from_bytes,
    cache_image_from_url,
)
from olano.platforms.config import Platform, PlatformConfig
from olano.platforms.helpers import redact_phone

logger = logging.getLogger(__name__)

_IS_WINDOWS = platform_mod.system() == "Windows"


def _kill_port_process(port: int) -> None:
    """Kill any process listening on the given TCP port (clears a stale daemon
    orphaned on our port from a previous run). Mirrors the WhatsApp bridge."""
    try:
        if _IS_WINDOWS:
            result = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[3] == "LISTENING":
                    local_addr = parts[1]
                    if local_addr.endswith(f":{port}"):
                        try:
                            subprocess.run(
                                ["taskkill", "/PID", parts[4], "/F"],
                                capture_output=True,
                                timeout=5,
                            )
                        except subprocess.SubprocessError:
                            pass
        else:
            result = subprocess.run(
                ["fuser", f"{port}/tcp"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                subprocess.run(
                    ["fuser", "-k", f"{port}/tcp"],
                    capture_output=True,
                    timeout=5,
                )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SIGNAL_MAX_ATTACHMENT_SIZE = 100 * 1024 * 1024  # 100 MB
MAX_MESSAGE_LENGTH = 8000  # Signal message size limit
TYPING_INTERVAL = 8.0  # seconds between typing indicator refreshes
SSE_RETRY_DELAY_INITIAL = 2.0
SSE_RETRY_DELAY_MAX = 60.0
HEALTH_CHECK_INTERVAL = 30.0  # seconds between health checks
HEALTH_CHECK_STALE_THRESHOLD = 120.0  # seconds without SSE activity before concern
DAEMON_READY_TIMEOUT = 20.0  # seconds to poll /api/v1/check after spawning the daemon
DAEMON_READY_POLL_INTERVAL = 1.0  # seconds between readiness polls

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_comma_list(value: str) -> list[str]:
    """Split a comma-separated string into a list, stripping whitespace."""
    return [v.strip() for v in value.split(",") if v.strip()]


def _guess_extension(data: bytes) -> str:
    """Guess file extension from magic bytes."""
    if data[:4] == b"\x89PNG":
        return ".png"
    if data[:2] == b"\xff\xd8":
        return ".jpg"
    if data[:4] == b"GIF8":
        return ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:4] == b"%PDF":
        return ".pdf"
    if len(data) >= 8 and data[4:8] == b"ftyp":
        return ".mp4"
    if data[:4] == b"OggS":
        return ".ogg"
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return ".mp3"
    if data[:2] == b"PK":
        return ".zip"
    return ".bin"


def _is_image_ext(ext: str) -> bool:
    return ext.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp")


def _is_audio_ext(ext: str) -> bool:
    return ext.lower() in (".mp3", ".wav", ".ogg", ".m4a", ".aac")


_EXT_TO_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ogg": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".mp4": "video/mp4",
    ".pdf": "application/pdf",
    ".zip": "application/zip",
}


def _ext_to_mime(ext: str) -> str:
    """Map file extension to MIME type."""
    return _EXT_TO_MIME.get(ext.lower(), "application/octet-stream")


def _render_mentions(text: str, mentions: list) -> str:
    """Replace Signal mention placeholders (\\uFFFC) with readable @identifiers.

    Signal encodes @mentions as the Unicode object replacement character
    with out-of-band metadata containing the mentioned user's UUID/number.
    """
    if not mentions or "\ufffc" not in text:
        return text
    # Sort mentions by start position (reverse) to replace from end to start
    # so indices don't shift as we replace
    sorted_mentions = sorted(mentions, key=lambda m: m.get("start", 0), reverse=True)
    for mention in sorted_mentions:
        start = mention.get("start", 0)
        length = mention.get("length", 1)
        # Use the mention's number or UUID as the replacement
        identifier = mention.get("number") or mention.get("uuid") or "user"
        replacement = f"@{identifier}"
        text = text[:start] + replacement + text[start + length :]
    return text


def check_signal_requirements() -> bool:
    """Check if Signal is configured (has URL and account)."""
    return bool(os.getenv("SIGNAL_HTTP_URL") and os.getenv("SIGNAL_ACCOUNT"))


# ---------------------------------------------------------------------------
# Signal Adapter
# ---------------------------------------------------------------------------


class SignalAdapter(BasePlatformAdapter):
    """Signal messenger adapter using signal-cli HTTP daemon."""

    # Signal has no edit API, so progress is appended as occasional messages
    # (throttled hard by the reporter — each one is a notification).
    PROGRESS_MODE = "append"

    platform = Platform.SIGNAL

    def __init__(self, config: PlatformConfig, agent_name: str = ""):
        super().__init__(config, Platform.SIGNAL, agent_name=agent_name)

        extra = config.extra or {}
        self.http_url = extra.get("http_url", "http://127.0.0.1:8080").rstrip("/")
        self.account = extra.get("account", "")
        self.ignore_stories = extra.get("ignore_stories", True)

        # Managed-daemon options. When manage_daemon is set, olano spawns and
        # supervises `signal-cli daemon --http` itself (like the WhatsApp bridge)
        # instead of requiring the user to run it manually. Default false keeps
        # the original "attach to an externally-run daemon" behavior.
        self.manage_daemon = bool(extra.get("manage_daemon", False))
        self.signal_cli_path = extra.get("signal_cli_path") or "signal-cli"
        self.data_dir = extra.get("data_dir")  # optional signal-cli --config dir
        _parsed = urlparse(self.http_url)
        self._daemon_host = _parsed.hostname or "127.0.0.1"
        self._daemon_port = _parsed.port or 8080

        # Managed-daemon process state (mirrors WhatsApp's _bridge_* fields).
        self._daemon_process: Optional[subprocess.Popen] = None
        self._daemon_log_fh: Optional[Any] = None
        self._daemon_log: Optional[Path] = None
        # True only when WE spawned the daemon — disconnect() must leave an
        # externally-run daemon alone.
        self._daemon_managed_by_us = False

        # Parse allowlists — group policy is derived from presence of group allowlist
        group_allowed_str = os.getenv("SIGNAL_GROUP_ALLOWED_USERS", "")
        self.group_allow_from = set(_parse_comma_list(group_allowed_str))

        # HTTP client
        self.client: Optional[httpx.AsyncClient] = None

        # Background tasks
        self._sse_task: Optional[asyncio.Task] = None
        self._health_monitor_task: Optional[asyncio.Task] = None
        self._typing_tasks: dict[str, asyncio.Task] = {}
        self._running = False
        self._last_sse_activity = 0.0
        self._sse_response: Optional[httpx.Response] = None

        # Normalize account for self-message filtering
        self._account_normalized = self.account.strip()

        # Track recently sent message timestamps to prevent echo-back loops
        # in Note to Self / self-chat mode (mirrors WhatsApp recentlySentIds)
        self._recent_sent_timestamps: set = set()
        self._max_recent_timestamps = 50

        logger.info(
            "Signal adapter initialized: url=%s account=%s groups=%s",
            self.http_url,
            redact_phone(self.account),
            "enabled" if self.group_allow_from else "disabled",
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Connect to signal-cli daemon and start SSE listener.

        Order matters: acquire the account-scoped lock FIRST (so two agents
        sharing one number can't both spawn a daemon — signal-cli locks the
        account data dir), then probe the daemon. If it's already reachable we
        attach to it (never spawning a duplicate). If it's unreachable and
        ``manage_daemon`` is set, we spawn and supervise it ourselves.
        """
        if not self.http_url or not self.account:
            logger.error("Signal: SIGNAL_HTTP_URL and SIGNAL_ACCOUNT are required")
            return False

        # Acquire scoped lock to prevent duplicate Signal listeners for the same phone
        try:
            if not self._acquire_platform_lock("signal-phone", self.account, "Signal account"):
                return False
        except Exception as e:
            logger.warning("Signal: Could not acquire phone lock (non-fatal): %s", e)

        self.client = httpx.AsyncClient(timeout=30.0)

        # Attach-if-running: probe BEFORE any spawn. If reachable, attach to an
        # already-running daemon (external or from a prior run) and never spawn.
        reachable = await self._health_check_once(timeout=10.0)

        if reachable:
            self._daemon_managed_by_us = False
            logger.info(
                "[%s] attached to existing signal-cli daemon at %s", self.name, self.http_url
            )
        elif self.manage_daemon:
            logger.info(
                "[%s] daemon unreachable at %s — spawning managed signal-cli daemon",
                self.name,
                self.http_url,
            )
            if not await self._spawn_daemon():
                # _spawn_daemon already set a fatal error; tear down and bail.
                await self.client.aclose()
                self.client = None
                self._release_platform_lock()
                return False
        else:
            logger.error(
                "Signal: cannot reach signal-cli at %s (manage_daemon=false — run "
                "`signal-cli -a %s daemon --http %s:%d` or enable manage_daemon)",
                self.http_url,
                redact_phone(self.account),
                self._daemon_host,
                self._daemon_port,
            )
            await self.client.aclose()
            self.client = None
            self._release_platform_lock()
            return False

        self._running = True
        self._last_sse_activity = time.time()
        self._mark_connected()
        self._sse_task = asyncio.create_task(self._sse_listener())
        self._health_monitor_task = asyncio.create_task(self._health_monitor())

        logger.info("Signal: connected to %s", self.http_url)
        return True

    # ------------------------------------------------------------------
    # Managed signal-cli daemon (mirrors the WhatsApp bridge subprocess)
    # ------------------------------------------------------------------

    def _daemon_log_path(self) -> Path:
        """Per-agent log file for the supervised signal-cli daemon."""
        from olano.config.home import get_olano_home
        from olano.platforms.helpers import per_agent_dir

        base = get_olano_home() / "platforms" / "signal"
        d = per_agent_dir(base, self.agent_name) if self.agent_name else base
        d.mkdir(parents=True, exist_ok=True)
        return d / "daemon.log"

    def _close_daemon_log(self) -> None:
        """Close the daemon log file handle if open."""
        if self._daemon_log_fh:
            try:
                self._daemon_log_fh.close()
            except Exception:
                pass
            self._daemon_log_fh = None

    async def _health_check_once(self, timeout: float = 10.0) -> bool:
        """Return True if the signal-cli daemon answers GET /api/v1/check."""
        if not self.client:
            return False
        try:
            resp = await self.client.get(f"{self.http_url}/api/v1/check", timeout=timeout)
            return resp.status_code == 200
        except Exception:
            return False

    async def _spawn_daemon(self) -> bool:
        """Spawn `signal-cli daemon --http` and poll until it's ready.

        Returns True once /api/v1/check answers; False (with a surfaced fatal
        error) if signal-cli is missing, exits during startup, or never becomes
        ready. Mirrors WhatsAppAdapter.connect()'s bridge spawn.
        """
        exe = shutil.which(self.signal_cli_path) or self.signal_cli_path
        if shutil.which(self.signal_cli_path) is None and not Path(self.signal_cli_path).exists():
            message = (
                f"signal-cli not found ({self.signal_cli_path}). Install it "
                "(brew install signal-cli) or set signal_cli_path."
            )
            logger.error("[%s] %s", self.name, message)
            self._set_fatal_error("signal_cli_missing", message, retryable=False)
            return False

        # Free a stale daemon orphaned on our port from a prior run.
        await asyncio.to_thread(_kill_port_process, self._daemon_port)
        await asyncio.sleep(1)

        self._daemon_log = self._daemon_log_path()
        # Long-lived handle: stays open for the daemon's lifetime, closed in
        # disconnect()/_close_daemon_log() — a context manager doesn't apply.
        self._daemon_log_fh = open(self._daemon_log, "a")

        # Account-scoped: bind the daemon to this single account so signal-cli's
        # per-account data-dir lock can't collide with another account's daemon.
        cmd = [exe]
        if self.data_dir:
            cmd += ["--config", str(self.data_dir)]
        cmd += ["-a", self.account, "daemon", "--http", f"{self._daemon_host}:{self._daemon_port}"]

        self._daemon_process = subprocess.Popen(
            cmd,
            stdout=self._daemon_log_fh,
            stderr=self._daemon_log_fh,
            preexec_fn=None if _IS_WINDOWS else os.setsid,
            env=os.environ.copy(),
        )
        self._daemon_managed_by_us = True

        deadline = time.time() + DAEMON_READY_TIMEOUT
        while time.time() < deadline:
            await asyncio.sleep(DAEMON_READY_POLL_INTERVAL)
            rc = self._daemon_process.poll()
            if rc is not None:
                message = (
                    f"signal-cli daemon exited during startup (code {rc}). "
                    f"Check {self._daemon_log} — often an unregistered or locked account."
                )
                logger.error("[%s] %s", self.name, message)
                self._set_fatal_error("signal_daemon_exited", message, retryable=True)
                self._close_daemon_log()
                return False
            if await self._health_check_once(timeout=3.0):
                logger.info(
                    "[%s] signal-cli daemon ready on %s:%d",
                    self.name,
                    self._daemon_host,
                    self._daemon_port,
                )
                return True

        message = (
            f"signal-cli daemon did not become ready in {DAEMON_READY_TIMEOUT:.0f}s. "
            f"Check {self._daemon_log}."
        )
        logger.error("[%s] %s", self.name, message)
        self._set_fatal_error("signal_daemon_timeout", message, retryable=True)
        return False

    async def _check_managed_daemon_exit(self) -> Optional[str]:
        """Return a fatal error message if our supervised daemon child exited."""
        if self._daemon_process is None or not self._daemon_managed_by_us:
            return None

        returncode = self._daemon_process.poll()
        if returncode is None:
            return None

        message = (
            f"signal-cli daemon exited unexpectedly (code {returncode}). Check {self._daemon_log}."
        )
        if not self.has_fatal_error:
            logger.error("[%s] %s", self.name, message)
            self._set_fatal_error("signal_daemon_exited", message, retryable=True)
            self._close_daemon_log()
            await self._notify_fatal_error()
        return self.fatal_error_message or message

    async def disconnect(self) -> None:
        """Stop SSE listener and clean up."""
        self._running = False

        if self._sse_task:
            self._sse_task.cancel()
            try:
                await self._sse_task
            except asyncio.CancelledError:
                pass

        if self._health_monitor_task:
            self._health_monitor_task.cancel()
            try:
                await self._health_monitor_task
            except asyncio.CancelledError:
                pass

        # Cancel all typing tasks
        for task in self._typing_tasks.values():
            task.cancel()
        self._typing_tasks.clear()

        if self.client:
            await self.client.aclose()
            self.client = None

        # Tear down the signal-cli daemon ONLY if we spawned it. An externally
        # run daemon is left alone. Kill the whole process group (SIGTERM, then
        # SIGKILL) so signal-cli children die too — mirrors the WhatsApp bridge.
        if self._daemon_process is not None and self._daemon_managed_by_us:
            try:
                import signal as _sig

                try:
                    if _IS_WINDOWS:
                        self._daemon_process.terminate()
                    else:
                        os.killpg(os.getpgid(self._daemon_process.pid), _sig.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    self._daemon_process.terminate()
                await asyncio.sleep(1)
                if self._daemon_process.poll() is None:
                    try:
                        if _IS_WINDOWS:
                            self._daemon_process.kill()
                        else:
                            os.killpg(os.getpgid(self._daemon_process.pid), _sig.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        self._daemon_process.kill()
            except Exception as e:
                logger.warning("[%s] error stopping signal-cli daemon: %s", self.name, e)
            finally:
                self._daemon_process = None
                self._close_daemon_log()
        elif self._daemon_process is None and not self._daemon_managed_by_us:
            logger.info("[%s] disconnecting (external signal-cli daemon left running)", self.name)

        self._release_platform_lock()
        self._mark_disconnected()

        logger.info("Signal: disconnected")

    # ------------------------------------------------------------------
    # SSE Streaming (inbound messages)
    # ------------------------------------------------------------------

    async def _sse_listener(self) -> None:
        """Listen for SSE events from signal-cli daemon."""
        url = f"{self.http_url}/api/v1/events?account={quote(self.account, safe='')}"
        backoff = SSE_RETRY_DELAY_INITIAL

        while self._running:
            try:
                if self.client is None:
                    break
                logger.debug("Signal SSE: connecting to %s", url)
                async with self.client.stream(
                    "GET",
                    url,
                    headers={"Accept": "text/event-stream"},
                    timeout=None,
                ) as response:
                    self._sse_response = response
                    backoff = SSE_RETRY_DELAY_INITIAL  # Reset on successful connection
                    self._last_sse_activity = time.time()
                    logger.info("Signal SSE: connected")

                    buffer = ""
                    async for chunk in response.aiter_text():
                        if not self._running:
                            break
                        buffer += chunk
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.strip()
                            if not line:
                                continue
                            # SSE keepalive comments (":") prove the connection
                            # is alive — update activity so the health monitor
                            # doesn't report false idle warnings.
                            if line.startswith(":"):
                                self._last_sse_activity = time.time()
                                continue
                            # Parse SSE data lines
                            if line.startswith("data:"):
                                data_str = line[5:].strip()
                                if not data_str:
                                    continue
                                self._last_sse_activity = time.time()
                                try:
                                    data = json.loads(data_str)
                                    await self._handle_envelope(data)
                                except json.JSONDecodeError:
                                    logger.debug("Signal SSE: invalid JSON: %s", data_str[:100])
                                except Exception:
                                    logger.exception("Signal SSE: error handling event")

            except asyncio.CancelledError:
                break
            except httpx.HTTPError as e:
                if self._running:
                    logger.warning("Signal SSE: HTTP error: %s (reconnecting in %.0fs)", e, backoff)
            except Exception as e:
                if self._running:
                    logger.warning("Signal SSE: error: %s (reconnecting in %.0fs)", e, backoff)

            if self._running:
                # Add 20% jitter to prevent thundering herd on reconnection
                jitter = backoff * 0.2 * random.random()
                await asyncio.sleep(backoff + jitter)
                backoff = min(backoff * 2, SSE_RETRY_DELAY_MAX)

        self._sse_response = None

    # ------------------------------------------------------------------
    # Health Monitor
    # ------------------------------------------------------------------

    async def _health_monitor(self) -> None:
        """Monitor SSE connection health and force reconnect if stale.

        Also watches a supervised daemon child: if WE spawned signal-cli and it
        died, surface a fatal error and stop (the dashboard's connector diagnose
        panel shows it; the user clicks Restart).
        """
        while self._running:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
            if not self._running:
                break

            # Managed-daemon crash detection (mirrors WhatsApp's bridge check).
            exit_msg = await self._check_managed_daemon_exit()
            if exit_msg:
                logger.error("[%s] %s — stopping health monitor", self.name, exit_msg)
                self._running = False
                break

            elapsed = time.time() - self._last_sse_activity
            if elapsed > HEALTH_CHECK_STALE_THRESHOLD and self.client is not None:
                logger.warning("Signal: SSE idle for %.0fs, checking daemon health", elapsed)
                try:
                    resp = await self.client.get(f"{self.http_url}/api/v1/check", timeout=10.0)
                    if resp.status_code == 200:
                        # Daemon is alive but SSE is idle — update activity to
                        # avoid repeated warnings (connection may just be quiet)
                        self._last_sse_activity = time.time()
                        logger.debug("Signal: daemon healthy, SSE idle")
                    else:
                        logger.warning(
                            "Signal: health check failed (%d), forcing reconnect", resp.status_code
                        )
                        self._force_reconnect()
                except Exception as e:
                    logger.warning("Signal: health check error: %s, forcing reconnect", e)
                    self._force_reconnect()

    def _force_reconnect(self) -> None:
        """Force SSE reconnection by closing the current response."""
        if self._sse_response and not self._sse_response.is_stream_consumed:
            try:
                task = asyncio.create_task(self._sse_response.aclose())
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            except Exception:
                pass
            self._sse_response = None

    # ------------------------------------------------------------------
    # Message Handling
    # ------------------------------------------------------------------

    async def _handle_envelope(self, envelope: dict) -> None:
        """Process an incoming signal-cli envelope."""
        # Unwrap nested envelope if present
        envelope_data = envelope.get("envelope", envelope)

        # Handle syncMessage: extract "Note to Self" messages (sent to own account)
        # while still filtering other sync events (read receipts, typing, etc.)
        is_note_to_self = False
        if "syncMessage" in envelope_data:
            sync_msg = envelope_data.get("syncMessage")
            if sync_msg and isinstance(sync_msg, dict):
                sent_msg = sync_msg.get("sentMessage")
                if sent_msg and isinstance(sent_msg, dict):
                    dest = sent_msg.get("destinationNumber") or sent_msg.get("destination")
                    sent_ts = sent_msg.get("timestamp")
                    if dest == self._account_normalized:
                        # Check if this is an echo of our own outbound reply
                        if sent_ts and sent_ts in self._recent_sent_timestamps:
                            self._recent_sent_timestamps.discard(sent_ts)
                            return
                        # Genuine user Note to Self — promote to dataMessage
                        is_note_to_self = True
                        envelope_data = {**envelope_data, "dataMessage": sent_msg}
            if not is_note_to_self:
                return

        # Extract sender info
        sender = (
            envelope_data.get("sourceNumber")
            or envelope_data.get("sourceUuid")
            or envelope_data.get("source")
        )
        sender_name = envelope_data.get("sourceName", "")
        sender_uuid = envelope_data.get("sourceUuid", "")

        if not sender:
            logger.debug("Signal: ignoring envelope with no sender")
            return

        # Self-message filtering — prevent reply loops (but allow Note to Self)
        if self._account_normalized and sender == self._account_normalized and not is_note_to_self:
            return

        # Filter stories
        if self.ignore_stories and envelope_data.get("storyMessage"):
            return

        # Get data message — also check editMessage (edited messages contain
        # their updated dataMessage inside editMessage.dataMessage)
        data_message = envelope_data.get("dataMessage") or (
            envelope_data.get("editMessage") or {}
        ).get("dataMessage")
        if not data_message:
            return

        # Check for group message
        group_info = data_message.get("groupInfo")
        group_id = group_info.get("groupId") if group_info else None
        is_group = bool(group_id)

        # Group message filtering — derived from SIGNAL_GROUP_ALLOWED_USERS:
        # - No env var set → groups disabled (default safe behavior)
        # - Env var set with group IDs → only those groups allowed
        # - Env var set with "*" → all groups allowed
        # DM auth is fully handled by run.py (_is_user_authorized)
        if is_group:
            if not self.group_allow_from:
                logger.debug("Signal: ignoring group message (no SIGNAL_GROUP_ALLOWED_USERS)")
                return
            if "*" not in self.group_allow_from and group_id not in self.group_allow_from:
                logger.debug("Signal: group %s not in allowlist", group_id[:8] if group_id else "?")
                return

        # Build chat info
        chat_id = sender if not is_group else f"group:{group_id}"
        chat_type = "group" if is_group else "dm"

        # Extract text and render mentions
        text = data_message.get("message", "")
        mentions = data_message.get("mentions", [])
        if text and mentions:
            text = _render_mentions(text, mentions)

        # Process attachments
        attachments_data = data_message.get("attachments", [])
        media_urls = []
        media_types = []

        if attachments_data and not getattr(self, "ignore_attachments", False):
            for att in attachments_data:
                att_id = att.get("id")
                att_size = att.get("size", 0)
                if not att_id:
                    continue
                if att_size > SIGNAL_MAX_ATTACHMENT_SIZE:
                    logger.warning("Signal: attachment too large (%d bytes), skipping", att_size)
                    continue
                try:
                    cached_path, ext = await self._fetch_attachment(att_id)
                    if cached_path:
                        # Use contentType from Signal if available, else map from extension
                        content_type = att.get("contentType") or _ext_to_mime(ext)
                        media_urls.append(cached_path)
                        media_types.append(content_type)
                except Exception:
                    logger.exception("Signal: failed to fetch attachment %s", att_id)

        # Build session source
        source = self.build_source(
            chat_id=chat_id,
            chat_name=group_info.get("groupName") if group_info else sender_name,
            chat_type=chat_type,
            user_id=sender,
            user_name=sender_name or sender,
            user_id_alt=sender_uuid if sender_uuid else None,
            chat_id_alt=group_id if is_group else None,
        )

        # Determine message type from media
        msg_type = MessageType.TEXT
        if media_types:
            if any(mt.startswith("audio/") for mt in media_types):
                msg_type = MessageType.VOICE
            elif any(mt.startswith("image/") for mt in media_types):
                msg_type = MessageType.PHOTO

        # Parse timestamp from envelope data (milliseconds since epoch)
        ts_ms = envelope_data.get("timestamp", 0)
        if ts_ms:
            try:
                timestamp = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
            except (ValueError, OSError):
                timestamp = datetime.now(tz=timezone.utc)
        else:
            timestamp = datetime.now(tz=timezone.utc)

        # Build and dispatch event
        event = MessageEvent(
            source=source,
            text=text or "",
            message_type=msg_type,
            media_urls=media_urls,
            media_types=media_types,
            timestamp=timestamp,
        )

        logger.debug(
            "Signal: message from %s in %s: %s",
            redact_phone(sender),
            chat_id[:20],
            (text or "")[:50],
        )

        await self.handle_message(event)

    # ------------------------------------------------------------------
    # Attachment Handling
    # ------------------------------------------------------------------

    async def _fetch_attachment(self, attachment_id: str) -> tuple:
        """Fetch an attachment via JSON-RPC and cache it. Returns (path, ext)."""
        result = await self._rpc(
            "getAttachment",
            {
                "account": self.account,
                "id": attachment_id,
            },
        )

        if not result:
            return None, ""

        # Handle dict response (signal-cli returns {"data": "base64..."})
        if isinstance(result, dict):
            result = result.get("data")
            if not result:
                logger.warning("Signal: attachment response missing 'data' key")
                return None, ""

        # Result is base64-encoded file content
        raw_data = base64.b64decode(result)
        ext = _guess_extension(raw_data)

        if _is_image_ext(ext):
            path = cache_image_from_bytes(raw_data, ext)
        elif _is_audio_ext(ext):
            path = cache_audio_from_bytes(raw_data, ext)
        else:
            path = cache_document_from_bytes(raw_data, ext)

        return path, ext

    # ------------------------------------------------------------------
    # JSON-RPC Communication
    # ------------------------------------------------------------------

    async def _rpc(self, method: str, params: dict, rpc_id: str | None = None) -> Any:
        """Send a JSON-RPC 2.0 request to signal-cli daemon."""
        if not self.client:
            logger.warning("Signal: RPC called but client not connected")
            return None

        if rpc_id is None:
            rpc_id = f"{method}_{int(time.time() * 1000)}"

        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": rpc_id,
        }

        try:
            resp = await self.client.post(
                f"{self.http_url}/api/v1/rpc",
                json=payload,
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()

            if "error" in data:
                logger.warning("Signal RPC error (%s): %s", method, data["error"])
                return None

            return data.get("result")

        except Exception as e:
            logger.warning("Signal RPC %s failed: %s", method, e)
            return None

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Send a text message."""
        await self._stop_typing_indicator(chat_id)

        params: dict[str, Any] = {
            "account": self.account,
            "message": content,
        }

        if chat_id.startswith("group:"):
            params["groupId"] = chat_id[6:]
        else:
            params["recipient"] = [chat_id]

        result = await self._rpc("send", params)

        if result is not None:
            self._track_sent_timestamp(result)
            # Use the timestamp from the RPC result as a pseudo message_id.
            # Signal doesn't have real message IDs, but the stream consumer
            # needs a truthy value to follow its edit→fallback path correctly.
            _msg_id = str(result.get("timestamp", "")) if isinstance(result, dict) else None
            return SendResult(success=True, message_id=_msg_id or None)
        return SendResult(success=False, error="RPC send failed")

    def _track_sent_timestamp(self, rpc_result) -> None:
        """Record outbound message timestamp for echo-back filtering."""
        ts = rpc_result.get("timestamp") if isinstance(rpc_result, dict) else None
        if ts:
            self._recent_sent_timestamps.add(ts)
            if len(self._recent_sent_timestamps) > self._max_recent_timestamps:
                self._recent_sent_timestamps.pop()

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Send a typing indicator."""
        params: dict[str, Any] = {
            "account": self.account,
        }

        if chat_id.startswith("group:"):
            params["groupId"] = chat_id[6:]
        else:
            params["recipient"] = [chat_id]

        await self._rpc("sendTyping", params, rpc_id="typing")

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send an image. Supports http(s):// and file:// URLs."""
        await self._stop_typing_indicator(chat_id)

        # Resolve image to local path
        if image_url.startswith("file://"):
            file_path = unquote(image_url[7:])
        else:
            # Download remote image to cache
            try:
                file_path = await cache_image_from_url(image_url)
            except Exception as e:
                logger.warning("Signal: failed to download image: %s", e)
                return SendResult(success=False, error=str(e))

        if not file_path or not Path(file_path).exists():
            return SendResult(success=False, error="Image file not found")

        # Validate size
        file_size = Path(file_path).stat().st_size
        if file_size > SIGNAL_MAX_ATTACHMENT_SIZE:
            return SendResult(success=False, error=f"Image too large ({file_size} bytes)")

        params: dict[str, Any] = {
            "account": self.account,
            "message": caption or "",
            "attachments": [file_path],
        }

        if chat_id.startswith("group:"):
            params["groupId"] = chat_id[6:]
        else:
            params["recipient"] = [chat_id]

        result = await self._rpc("send", params)
        if result is not None:
            self._track_sent_timestamp(result)
            return SendResult(success=True)
        return SendResult(success=False, error="RPC send with attachment failed")

    async def _send_attachment(
        self,
        chat_id: str,
        file_path: str,
        media_label: str,
        caption: Optional[str] = None,
    ) -> SendResult:
        """Send any file as a Signal attachment via RPC.

        Shared implementation for send_document, send_image_file, send_voice,
        and send_video — avoids duplicating the validation/routing/RPC logic.
        """
        await self._stop_typing_indicator(chat_id)

        try:
            file_size = Path(file_path).stat().st_size
        except FileNotFoundError:
            return SendResult(success=False, error=f"{media_label} file not found: {file_path}")

        if file_size > SIGNAL_MAX_ATTACHMENT_SIZE:
            return SendResult(success=False, error=f"{media_label} too large ({file_size} bytes)")

        params: dict[str, Any] = {
            "account": self.account,
            "message": caption or "",
            "attachments": [file_path],
        }

        if chat_id.startswith("group:"):
            params["groupId"] = chat_id[6:]
        else:
            params["recipient"] = [chat_id]

        result = await self._rpc("send", params)
        if result is not None:
            self._track_sent_timestamp(result)
            return SendResult(success=True)
        return SendResult(success=False, error=f"RPC send {media_label.lower()} failed")

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a document/file attachment."""
        return await self._send_attachment(chat_id, file_path, "File", caption)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a local image file as a native Signal attachment.

        Called by the gateway media delivery flow when MEDIA: tags containing
        image paths are extracted from agent responses.
        """
        return await self._send_attachment(chat_id, image_path, "Image", caption)

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send an audio file as a Signal attachment.

        Signal does not distinguish voice messages from file attachments at
        the API level, so this routes through the same RPC send path.
        """
        return await self._send_attachment(chat_id, audio_path, "Audio", caption)

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a video file as a Signal attachment."""
        return await self._send_attachment(chat_id, video_path, "Video", caption)

    # ------------------------------------------------------------------
    # Typing Indicators
    # ------------------------------------------------------------------

    async def _stop_typing_indicator(self, chat_id: str) -> None:
        """Stop a typing indicator loop for a chat."""
        task = self._typing_tasks.pop(chat_id, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def stop_typing(self, chat_id: str) -> None:
        """Public interface for stopping typing — called by base adapter's
        _keep_typing finally block to clean up platform-level typing tasks."""
        await self._stop_typing_indicator(chat_id)

    # ------------------------------------------------------------------
    # Chat Info
    # ------------------------------------------------------------------

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        """Get information about a chat/contact."""
        if chat_id.startswith("group:"):
            return {
                "name": chat_id,
                "type": "group",
                "chat_id": chat_id,
            }

        # Try to resolve contact name
        result = await self._rpc(
            "getContact",
            {
                "account": self.account,
                "contactAddress": chat_id,
            },
        )

        name = chat_id
        if result and isinstance(result, dict):
            name = result.get("name") or result.get("profileName") or chat_id

        return {
            "name": name,
            "type": "dm",
            "chat_id": chat_id,
        }

    async def list_chats(self) -> list[dict[str, Any]] | None:
        """Enumerate Signal groups via signal-cli's ``listGroups``."""
        try:
            result = await self._rpc("listGroups", {"account": self.account})
        except Exception as e:
            logger.debug("[%s] Signal group enumeration failed: %s", self.name, e)
            return None
        if not isinstance(result, list):
            return None
        chats: list[dict[str, Any]] = []
        for group in result:
            if not isinstance(group, dict):
                continue
            group_id = str(group.get("id") or "")
            if not group_id:
                continue
            chats.append(
                {
                    # Match the adapter's inbound chat-id shape ("group:<id>").
                    "chat_id": f"group:{group_id}",
                    "name": str(group.get("name") or group_id),
                    "type": "group",
                }
            )
        return chats
