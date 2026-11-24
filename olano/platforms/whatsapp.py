"""
WhatsApp platform adapter.

WhatsApp integration is more complex than Telegram/Discord because:
- No official bot API for personal accounts
- Business API requires Meta Business verification
- Most solutions use web-based automation

This adapter supports multiple backends:
1. WhatsApp Business API (requires Meta verification)
2. whatsapp-web.js (via Node.js subprocess) - for personal accounts
3. Baileys (via Node.js subprocess) - alternative for personal accounts

For simplicity, we'll implement a generic interface that can work
with different backends via a bridge pattern.
"""

import asyncio
import json
import logging
import os
import platform
import re
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, TextIO

if TYPE_CHECKING:
    import aiohttp

_IS_WINDOWS = platform.system() == "Windows"

logger = logging.getLogger(__name__)

#: WhatsApp routes every Status ("story") update through this one pseudo-chat.
#: It carries no group flag, so without an explicit rule it reads as a DM.
_STATUS_BROADCAST_JID = "status@broadcast"


def _kill_port_process(port: int) -> None:
    """Kill any process listening on the given TCP port."""
    try:
        if _IS_WINDOWS:
            # Use netstat to find the PID bound to this port, then taskkill
            result: subprocess.CompletedProcess[Any] = subprocess.run(
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


from olano.config.home import get_olano_home
from olano.platforms.base import (
    SUPPORTED_DOCUMENT_TYPES,
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_audio_from_url,
    cache_image_from_url,
)
from olano.platforms.config import Platform, PlatformConfig
from olano.security.shell_guard import strip_platform_secrets


def _as_int(value: Any) -> int:
    """``value`` as an int, or -1 when it isn't one. Never raises."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


# Extension → mime for the files the bridge decrypts into its media dir. The
# bridge names each file after the sender's real mimetype, so the extension is
# the trustworthy signal; `mediaType` on the event is coarse by design (it is
# what picks PHOTO/VOICE/VIDEO/DOCUMENT) and says `image/jpeg` for every image.
#
# This has to be truthful rather than merely plausible. The speech provider
# infers an audio container from the filename it is posted under, and
# ``uploads.sanitize_filename`` derives a saved image's extension from the mime
# it is given — so labelling an mp3 voice note `audio/ogg`, or a PNG
# `image/jpeg`, produces a rejected transcription and a `.jpg` holding PNG bytes.
_BRIDGE_MEDIA_MIMES = {
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".webm": "audio/webm",
    ".amr": "audio/amr",
    ".flac": "audio/flac",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".heic": "image/heic",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".3gp": "video/3gpp",
    ".mkv": "video/x-matroska",
}


# What to call an attachment the bridge could not hand over, in the note the
# agent relays to the sender.
_UNAVAILABLE_MEDIA_LABELS = {
    MessageType.VOICE: "voice message",
    MessageType.PHOTO: "photo",
    MessageType.VIDEO: "video",
    MessageType.DOCUMENT: "file",
}


def bridge_environment(reply_prefix: str | None = None) -> dict[str, str]:
    """The environment the Node bridge is spawned with: the runtime's own,
    minus the platform secrets (the vault key, the deployment and dashboard
    tokens, the gateway key, the account credits key; both spellings). The
    bridge reads PATH, HOME, OLANO_HOME and its WHATSAPP_* knobs and nothing
    else (``whatsapp-bridge/index.js``), so nothing it needs is removed, and a
    credential that spends the box's credits no longer rides into a child
    process that never asked for it. Shared by both spawn sites so they cannot
    disagree."""
    env = strip_platform_secrets(os.environ)
    if reply_prefix is not None:
        env["WHATSAPP_REPLY_PREFIX"] = reply_prefix
    return env


def _bridge_media_mime(path: str, declared_mime: Any, fallback: str) -> str:
    """Mime for a bridge-downloaded attachment: extension → declared → default.

    The extension leads because the bridge chose it from the sender's mimetype
    and the file on disk is what every downstream reader actually opens. An
    older bridge sends no declared mime and an unknown extension falls through
    to ``fallback``, so the pre-existing behaviour is the floor, never a
    regression.

    *declared_mime* is the sender's own mimetype as the bridge forwarded it —
    ``mediaMime`` for the message's attachment, ``quotedMediaMime`` for the
    attachment on the message it replies to.
    """
    mime = _BRIDGE_MEDIA_MIMES.get(Path(path).suffix.lower())
    if mime:
        return mime
    declared = str(declared_mime or "").split(";")[0].strip().lower()
    # Only honour a declared mime in the same family as the branch's default —
    # the message kind already decided which one we are in, and a sender-set
    # mimetype must not move an attachment between them.
    if declared and "/" in declared and declared.split("/")[0] == fallback.split("/")[0]:
        return declared
    return fallback


def _quoted_media(data: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Local paths + mimes for the attachment on the message being replied to.

    The bridge decrypts a quoted attachment exactly as it does the message's
    own (same size cap, same extension rule), so the only work left here is
    typing the files it handed over. Mime comes off the **extension** the
    bridge chose, with the sender's declared ``quotedMediaMime`` as the
    fallback and the coarse ``quotedMediaType`` last — the same ordering
    ``_bridge_media_mime`` applies to the message's own media, for the same
    reason: the file on disk is what a vision tool or the transcriber opens.

    Document extensions are resolved first, matching the message's own
    document branch: ``_BRIDGE_MEDIA_MIMES`` holds only image/audio/video, so
    without this a `.pdf` whose sender declared an image mimetype would be
    typed `image/*` and handed to the image path instead of the extractor.

    Returns two empty lists for a non-reply, a text-only quote, or a bridge
    too old to send the fields — in which case the renderer still says a reply
    happened, it just cannot say what was in it.
    """
    urls = [str(u) for u in (data.get("quotedMediaUrls") or []) if u]
    if not urls:
        return [], []
    declared = data.get("quotedMediaMime") or data.get("quotedMediaType") or ""
    fallback = str(declared or "").split(";")[0].strip().lower() or "application/octet-stream"
    mimes = [
        SUPPORTED_DOCUMENT_TYPES.get(Path(u).suffix.lower())
        or _bridge_media_mime(u, declared, fallback)
        for u in urls
    ]
    return urls, mimes


def check_whatsapp_requirements() -> bool:
    """
    Check if WhatsApp dependencies are available.

    WhatsApp requires a Node.js bridge for most implementations.
    """
    # Check for Node.js
    try:
        result = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


class WhatsAppAdapter(BasePlatformAdapter):
    """
    WhatsApp adapter.

    This implementation uses a simple HTTP bridge pattern where:
    1. A Node.js process runs the WhatsApp Web client
    2. Messages are forwarded via HTTP/IPC to this Python adapter
    3. Responses are sent back through the bridge

    The actual Node.js bridge implementation can vary:
    - whatsapp-web.js based
    - Baileys based
    - Business API based

    Configuration:
    - bridge_script: Path to the Node.js bridge script
    - bridge_port: Port for HTTP communication (default: 3000)
    - session_path: Path to store WhatsApp session data
    - require_mention: In groups, only handle @mentions/replies/wake words
      (default: false — the agent handles every group message)
    - group_respond: "all" | "mentions" | "none" — which group messages
      trigger an agent turn; wins over require_mention when set.
      A leading "/" is an ordinary trigger from anyone under "all"/"mentions";
      under "none" it works only for senders named on owner_users/
      allowed_users, so silent mode stays silent on an open bot.
    - group_listen: "off" | "mentions" | "all" — which non-responded group
      messages are recorded to the persistent observed-messages store
      (default: off)
    - free_response_chats: Chat IDs exempt from require_mention/group_respond
    - mention_patterns: Regex wake words that count as a mention
    - allowed_users / allowed_chats: Hard allowlists (see below)
    - listen_only: Monitoring mode — inbound messages are still processed
      (memory, transcripts, self-learning all see them) but the adapter never
      sends anything to WhatsApp: no replies, no media, no typing indicators
      (default: false)
    """

    # WhatsApp's edit window covers a turn's lifetime, so progress updates one
    # message rather than adding a new one per step. Listen-only mode reports
    # sends as suppressed-but-successful, which the reporter would otherwise
    # read as three failed pushes before retiring — declare it off instead, so
    # monitoring mode does no progress work at all.
    @property
    def PROGRESS_MODE(self) -> str:  # type: ignore[override]
        return "off" if self._whatsapp_listen_only() else "edit"

    # WhatsApp message limits — practical UX limit, not protocol max.
    # WhatsApp allows ~65K but long messages are unreadable on mobile.
    MAX_MESSAGE_LENGTH = 4096

    # Default bridge location.  The bundled Baileys bridge lives at the
    # project root (``whatsapp-bridge/index.js``); we also accept the legacy
    # ``scripts/whatsapp-bridge/bridge.js`` layout and an explicit env var
    # override (``LIONCLAW_WHATSAPP_BRIDGE``) for installs where the bridge
    # has been moved or vendored elsewhere.
    @staticmethod
    def _default_bridge_script() -> str:
        env_override = os.environ.get("LIONCLAW_WHATSAPP_BRIDGE")
        if env_override:
            return env_override
        repo_root = Path(__file__).resolve().parents[2]
        candidates = (
            repo_root / "whatsapp-bridge" / "index.js",  # canonical (bundled)
            repo_root / "scripts" / "whatsapp-bridge" / "bridge.js",  # legacy
            Path.cwd() / "whatsapp-bridge" / "index.js",  # cwd-relative install
        )
        for c in candidates:
            if c.exists():
                return str(c)
        # Fallback to the canonical path even if missing — clearer error than
        # an arbitrary stale path; the user can fix `bridge_script` in config.
        return str(candidates[0])

    # Default WhatsApp bridge port. Each agent gets its own deterministic
    # offset (``per_agent_port``) so two agents on ``platform:whatsapp``
    # don't fight over the same TCP port. Users can still pin a fixed
    # port via ``bridge_port`` in the connector config.
    _DEFAULT_BRIDGE_PORT = 3000

    # Ports below 1024 are privileged: binding one requires root
    # (CAP_NET_BIND_SERVICE) on Linux/macOS. The bridge always runs as the same
    # unprivileged user as the rest of olano, so a lower value guarantees an
    # EACCES bind error and a dead subprocess ~1s into every start.
    _MIN_BRIDGE_PORT = 1024
    _MAX_BRIDGE_PORT = 65535

    @classmethod
    def auto_bridge_port(cls, agent_name: str) -> int:
        """The port this agent gets when ``bridge_port`` is left unset.

        Deterministic per agent, so two agents on ``platform:whatsapp`` never
        land on the same port and nobody has to allocate one by hand.
        """
        from olano.platforms.helpers import per_agent_port

        if agent_name:
            return per_agent_port(cls._DEFAULT_BRIDGE_PORT, agent_name)
        return cls._DEFAULT_BRIDGE_PORT

    @classmethod
    def resolve_bridge_port(cls, agent_name: str, configured: Any) -> int:
        """Resolve ``configured`` to a port that can actually be bound.

        An unset (or blank) value means "auto" — the normal case, and what the
        connector form now stores by default. A configured value that could
        never work (non-numeric, privileged, out of range) does NOT take the
        connector down with it: a bridge that cannot bind is a dead connector
        either way, so we log loudly and fall back to the auto port, which
        leaves WhatsApp working while the operator fixes the config. Live
        incident: ``bridge_port: 6`` (a typo for 3006) killed the bridge on
        every restart and surfaced only as "connection refused on port 6".
        """
        auto = cls.auto_bridge_port(agent_name)
        if configured is None or (isinstance(configured, str) and not configured.strip()):
            return auto
        try:
            port = int(configured)
        except (TypeError, ValueError):
            port = -1
        if not (cls._MIN_BRIDGE_PORT <= port <= cls._MAX_BRIDGE_PORT):
            logger.warning(
                "[whatsapp:%s] Configured bridge_port %r is unusable "
                "(must be an integer %d-%d; ports below %d need root). "
                "Falling back to this agent's automatic port %d — clear "
                "bridge_port in the connector config to make that permanent.",
                agent_name or "(default)",
                configured,
                cls._MIN_BRIDGE_PORT,
                cls._MAX_BRIDGE_PORT,
                cls._MIN_BRIDGE_PORT,
                auto,
            )
            return auto
        return port

    @classmethod
    def resolve_runtime_layout(
        cls,
        agent_name: str,
        extra: dict[str, Any] | None = None,
    ) -> tuple[int, Path]:
        """Return ``(bridge_port, session_path)`` exactly as ``__init__`` would.

        Single source of truth for callers that need to talk to a specific
        agent's bridge WITHOUT instantiating the adapter (e.g. the
        ``olano connect whatsapp --agent <name>`` CLI and the
        dashboard's ``/api/connectors/whatsapp/pair?agent=<name>`` endpoint).
        Without this, those entry points used to default to port 3000 + the
        legacy global session path, silently routing the user's pairing to
        a location the live adapter never reads from.
        """
        from olano.platforms.helpers import per_agent_dir

        extra = extra or {}
        port = cls.resolve_bridge_port(agent_name, extra.get("bridge_port"))

        explicit_session = extra.get("session_path")
        if explicit_session is not None:
            session_path = Path(explicit_session)
        else:
            base = get_olano_home() / "platforms" / "whatsapp"
            if agent_name:
                session_path = per_agent_dir(base, agent_name) / "session"
            else:
                session_path = base / "session"
        return port, session_path

    @classmethod
    def spawn_pairing_bridge(
        cls, agent_name: str, extra: dict[str, Any] | None = None
    ) -> tuple[int, subprocess.Popen, Path]:
        """Spawn the bundled bridge for onboarding pairing, blocking until it is
        actually reachable. Returns ``(port, process, log_path)``.

        This is the shared spawn path for callers that need a fresh bridge
        WITHOUT running the full adapter (the channel-connect handshake, the
        dashboard pair endpoint): it does everything ``connect()`` does before
        the connect wait — installs npm deps on first run, kills an orphan on
        the port, and routes output to ``bridge.log`` — so a crash is diagnosable
        instead of "nothing is listening". Raises :class:`ValueError` with a
        user-safe message on any failure (no Node, npm install failed, the
        process died on boot). **Blocking**: run it in a thread from async code.
        """
        if not check_whatsapp_requirements():
            raise ValueError("Node.js is not installed on the server (run install.sh).")
        script = Path(cls._default_bridge_script())
        if not script.exists():
            raise ValueError(f"WhatsApp bridge script not found at {script}.")
        port, session_path = cls.resolve_runtime_layout(agent_name, extra or {})
        bridge_dir = script.parent
        if not (bridge_dir / "node_modules").exists():
            try:
                install = subprocess.run(
                    ["npm", "install", "--silent"],
                    cwd=str(bridge_dir),
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
            except Exception as exc:
                raise ValueError(
                    f"Could not install the WhatsApp bridge dependencies: {exc}"
                ) from exc
            if install.returncode != 0:
                raise ValueError(
                    "The WhatsApp bridge dependencies failed to install "
                    f"(npm exited {install.returncode}). Ask the operator to check the server."
                )

        session_path.mkdir(parents=True, exist_ok=True)
        _kill_port_process(port)
        log_path = session_path.parent / "bridge.log"
        log_fh = open(log_path, "a")
        try:
            proc = subprocess.Popen(
                ["node", str(script), "--port", str(port), "--session", str(session_path)],
                stdout=log_fh,
                stderr=log_fh,
                preexec_fn=None if _IS_WINDOWS else os.setsid,
                env=bridge_environment(),
            )
        finally:
            log_fh.close()
        # Fail fast if node exits immediately (bad script, missing dep, EADDRINUSE).
        time.sleep(1.5)
        if proc.poll() is not None:
            raise ValueError(
                f"The WhatsApp bridge exited on startup (code {proc.returncode}). Log: {log_path}"
            )
        return port, proc, log_path

    def __init__(self, config: PlatformConfig, agent_name: str = ""):
        super().__init__(config, Platform.WHATSAPP, agent_name=agent_name)

        self._bridge_process: Optional[subprocess.Popen] = None
        # Set for the whole of ``disconnect()``. The poll loop outlives the kill
        # (disconnect sleeps a second between SIGTERM and SIGKILL, and only
        # clears ``_running`` at the very end), so without this the bridge we
        # just asked to stop is discovered "exited" and reported as a crash.
        self._shutting_down = False
        # Single source of truth shared with the CLI + dashboard pair flows.
        self._bridge_port, self._session_path = self.resolve_runtime_layout(
            agent_name, config.extra
        )
        self._bridge_script: Optional[str] = config.extra.get(
            "bridge_script",
            self._default_bridge_script(),
        )
        self._reply_prefix: Optional[str] = config.extra.get("reply_prefix")
        self._mention_patterns = self._compile_mention_patterns()
        self._allowed_users: set[str] = self._compile_allowed_users()
        # Owner designation (extra.owner_users) — trust WITHOUT admission.
        # The runtime unions this list into sender trust; here it only
        # exempts owners from a configured allowed_users gate (designating an
        # owner must never lock that owner out). It never closes the bot.
        self._owner_users: set[str] = self._compile_user_entries(
            self.config.extra.get("owner_users")
        )
        self._allowed_chats: set[str] = self._compile_allowed_chats()
        # Populated from the bridge's /health response on connect; surfaced
        # by the dashboard's connector diagnostics ("bot_user" field).
        self._paired_jid: Optional[str] = None
        self._message_queue: asyncio.Queue = asyncio.Queue()
        # Inbound dedupe by message id, persisted beside the session so it
        # survives a runtime restart: WhatsApp replays offline messages when a
        # linked device reconnects, and the bridge outlives the runtime that
        # spawned it, so a restart is exactly when a redelivery arrives.
        from olano.platforms.helpers import PersistentMessageDeduplicator

        self._dedup = PersistentMessageDeduplicator(
            self._session_path.with_name(self._session_path.name + ".seen-ids")
        )
        self._bridge_log_fh: Optional[TextIO] = None
        self._bridge_log: Optional[Path] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._poll_interval_seconds = float(
            os.getenv("LIONCLAW_WHATSAPP_POLL_INTERVAL_SECONDS", "1")
        )
        self._http_session: Optional[aiohttp.ClientSession] = None

    def diagnostic_snapshot(self) -> dict[str, Any]:
        """Live connector health for the dashboard's diagnose panel.

        The default ``BasePlatformAdapter`` snapshot trusts the cached
        ``is_connected`` flag — but that flag is sticky: once the bridge
        ever reports ``status: connected``, it stays True even after
        WhatsApp logs the device out and the bridge falls back to
        emitting QR codes.  Without a live probe the user sees
        "Connected to platform." while messages silently vanish.

        Here we hit the bridge's ``/health`` endpoint with a 500ms
        timeout and report what it tells us right now.  Falls back to
        the cached state on connection refused (bridge process dead).
        """
        import json
        import urllib.error
        import urllib.request

        connected = bool(getattr(self, "is_connected", False))
        fatal = getattr(self, "fatal_error_message", None)

        bridge_status: str | None = None
        bridge_paired_jid: str | None = self._paired_jid
        bridge_error: str | None = None
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{self._bridge_port}/health")
            with urllib.request.urlopen(req, timeout=0.5) as resp:
                if resp.status == 200:
                    payload = json.loads(resp.read().decode("utf-8", errors="replace"))
                    if isinstance(payload, dict):
                        bridge_status = str(payload.get("status") or "")
                        jid = payload.get("jid")
                        if jid:
                            bridge_paired_jid = str(jid)
                            self._paired_jid = bridge_paired_jid
                else:
                    bridge_error = f"bridge /health returned HTTP {resp.status}"
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
            bridge_error = str(exc)

        is_paired = bool(bridge_paired_jid) and bridge_status == "connected"

        checks: list[dict[str, Any]] = [
            {
                "id": "runtime",
                "ok": True,
                "message": "Adapter is running.",
            },
        ]
        if self._whatsapp_listen_only():
            # ok=True — this is a configured mode, not a fault; without this row
            # a silent agent in listen-only mode looks like a delivery bug.
            checks.append(
                {
                    "id": "listen_only",
                    "ok": True,
                    "message": (
                        "Listen-only (monitoring) mode is ON — inbound messages are "
                        "processed but the agent never sends replies, media, or typing "
                        "indicators. Turn off 'Listen-only mode' in the connector "
                        "settings to let it respond."
                    ),
                }
            )
        # What the LIVE adapter resolved, not what the config file says. A
        # connector's settings are read once, at boot, so an edit saved without
        # a restart leaves the form showing one thing and the running adapter
        # doing another — and the only way anyone could tell was to post in the
        # group and watch. ok=True throughout: every value here is a configured
        # mode, not a fault.
        respond = self._whatsapp_group_respond()
        listen = self._whatsapp_group_listen()
        exempt = sorted(self._whatsapp_free_response_chats())
        respond_detail = {
            "all": "answers every group message",
            "mentions": "answers only @mentions, replies to itself, /commands and wake words",
            "none": "never answers in groups",
        }[respond]
        listen_detail = {
            "off": "unanswered group messages are discarded",
            "mentions": "records only unanswered messages that addressed it",
            "all": "records every group message it can see",
        }[listen]
        group_message = (
            f"Group replies: {respond} — {respond_detail}. "
            f"Group listening: {listen} — {listen_detail}. "
            "Direct messages are always answered. "
            "This is what the running adapter resolved; if it disagrees with the "
            "connector form, the agent has not been restarted since that change."
        )
        if respond == "none" and exempt:
            group_message += (
                " Exempt from silent mode (free-response chats): " + ", ".join(exempt) + "."
            )
        checks.append({"id": "group_modes", "ok": True, "message": group_message})

        # A stored port we had to ignore is worth saying out loud: the runtime
        # is healthy on the auto port, but the config still reads wrong and the
        # next person to look at it would be misled.
        configured_port = self.config.extra.get("bridge_port")
        if configured_port not in (None, "") and int(self._bridge_port) != _as_int(configured_port):
            checks.append(
                {
                    "id": "bridge_port_override",
                    "ok": False,
                    "message": (
                        f"Configured bridge port {configured_port!r} is unusable "
                        f"(must be {self._MIN_BRIDGE_PORT}-{self._MAX_BRIDGE_PORT}; "
                        "lower ports need root). Running on this agent's automatic "
                        f"port {self._bridge_port} instead — set the port to Auto in "
                        "the connector settings to clear the stale value."
                    ),
                }
            )

        if bridge_status is None and bridge_error:
            checks.append(
                {
                    "id": "bridge",
                    "ok": False,
                    "message": (
                        f"Bridge HTTP on port {self._bridge_port} is not responding "
                        f"({bridge_error}). Bridge subprocess likely crashed — "
                        "click Restart on the agent or tail the bridge log."
                    ),
                }
            )
        elif is_paired:
            checks.append(
                {
                    "id": "connection",
                    "ok": True,
                    "message": f"Bridge connected to WhatsApp as {bridge_paired_jid}.",
                }
            )
        elif bridge_status == "connected":
            checks.append(
                {
                    "id": "connection",
                    "ok": True,
                    "message": "Bridge reports connected (no paired JID yet — give it a few seconds).",
                }
            )
        else:
            label = bridge_status or "unknown"
            checks.append(
                {
                    "id": "pairing",
                    "ok": False,
                    "message": (
                        f"Bridge is up but not paired (status: {label}). Scan the QR code in "
                        f"`olano connect whatsapp` or in the dashboard's Pair button. "
                        f"Bridge log: {self._bridge_log or '~/.olano/platforms/whatsapp/<agent>/bridge.log'}"
                    ),
                }
            )

        if fatal:
            checks.append(
                {
                    "id": "fatal_error",
                    "ok": False,
                    "message": str(fatal),
                }
            )

        return {
            "adapter": "platform:whatsapp",
            "live": is_paired or connected,
            "bot_user": bridge_paired_jid,
            "checks": checks,
        }

    def _maybe_migrate_legacy_session(self) -> None:
        """Migrate a pre-multi-tenant WhatsApp session into the per-agent path.

        Before the per-agent dir layout was introduced, every
        ``platform:whatsapp`` adapter shared a single global session
        directory at ``<base>/session/``.  Upgrading silently moved every
        adapter onto an empty per-agent path, orphaning the original
        pairing — the bridge then loops on the QR screen forever while
        the dashboard's connection check still reports "Connected" because
        it last saw the bridge in a connected state.

        Strategy:
            • Skip if the per-agent path is explicitly configured (the
              user opted out of the default layout, don't surprise them).
            • Skip if the per-agent path already contains a ``creds.json``
              (already migrated or freshly paired).
            • Skip if the legacy path has no ``creds.json``.
            • Otherwise, ``os.rename`` the legacy directory onto the
              per-agent path.  Rename is atomic on POSIX — if two agents
              race for it, only one wins and the loser sees the legacy
              path vanish, falling back to a fresh QR pairing.
        """
        try:
            explicit = self.config.extra.get("session_path")
            if explicit is not None:
                return
            per_agent_creds = self._session_path / "creds.json"
            if per_agent_creds.exists():
                return
            legacy_session = self._session_path.parent.parent / "session"
            legacy_creds = legacy_session / "creds.json"
            if not legacy_creds.exists():
                return
            self._session_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self._session_path.rmdir()
            except (FileNotFoundError, OSError):
                pass
            try:
                legacy_session.rename(self._session_path)
            except FileNotFoundError:
                return
            logger.warning(
                "[%s] Migrated legacy WhatsApp session %s → %s (one-time per-agent migration). "
                "If multiple agents needed this pairing, only the first to boot inherits it; "
                "others will display a fresh QR.",
                self.name,
                legacy_session,
                self._session_path,
            )
        except Exception as exc:
            logger.warning(
                "[%s] Legacy session migration skipped due to error (non-fatal): %s",
                self.name,
                exc,
            )

    def _whatsapp_require_mention(self) -> bool:
        configured = self.config.extra.get("require_mention")
        if configured is not None:
            if isinstance(configured, str):
                return configured.lower() in ("true", "1", "yes", "on")
            return bool(configured)
        return os.getenv("WHATSAPP_REQUIRE_MENTION", "false").lower() in ("true", "1", "yes", "on")

    def _whatsapp_group_respond(self) -> str:
        """Effective ``group_respond`` mode ("all" | "mentions" | "none").

        ``group_respond`` wins when set; otherwise the legacy
        ``require_mention`` resolution decides (WhatsApp default: "all").
        """
        from olano.platforms.group_modes import resolve_group_respond

        return resolve_group_respond(
            self.config.extra, legacy_require_mention=self._whatsapp_require_mention()
        )

    def _whatsapp_group_listen(self) -> str:
        """Effective ``group_listen`` mode ("off" | "mentions" | "all").

        WhatsApp had no legacy passive-listen toggle, so an unset knob is
        simply "off". (``listen_only`` is an unrelated *outbound* suppression
        mode and never feeds this.)
        """
        from olano.platforms.group_modes import resolve_group_listen

        return resolve_group_listen(self.config.extra, legacy_passive_listen=False)

    def _whatsapp_listen_only(self) -> bool:
        """Monitoring mode: process inbound messages, never send anything.

        Inbound flow is untouched — messages still reach the agent (and its
        memory/transcripts), but every outbound surface (text, edits, media,
        typing indicators) is suppressed at this adapter. The agent's
        would-have-been replies remain visible in the session transcript.
        """
        configured = self.config.extra.get("listen_only")
        if configured is not None:
            if isinstance(configured, str):
                return configured.lower() in ("true", "1", "yes", "on")
            return bool(configured)
        return os.getenv("WHATSAPP_LISTEN_ONLY", "false").lower() in ("true", "1", "yes", "on")

    def _whatsapp_free_response_chats(self) -> set[str]:
        raw = self.config.extra.get("free_response_chats")
        if raw is None:
            raw = os.getenv("WHATSAPP_FREE_RESPONSE_CHATS", "")
        if isinstance(raw, list):
            return {str(part).strip() for part in raw if str(part).strip()}
        return {part.strip() for part in str(raw).split(",") if part.strip()}

    def _compile_mention_patterns(self):
        patterns = self.config.extra.get("mention_patterns")
        if patterns is None:
            raw = os.getenv("WHATSAPP_MENTION_PATTERNS", "").strip()
            if raw:
                try:
                    patterns = json.loads(raw)
                except Exception:
                    patterns = [part.strip() for part in raw.splitlines() if part.strip()]
                    if not patterns:
                        patterns = [part.strip() for part in raw.split(",") if part.strip()]
        if patterns is None:
            return []
        if isinstance(patterns, str):
            patterns = [patterns]
        if not isinstance(patterns, list):
            logger.warning(
                "[%s] whatsapp mention_patterns must be a list or string; got %s",
                self.name,
                type(patterns).__name__,
            )
            return []

        compiled = []
        for pattern in patterns:
            if not isinstance(pattern, str) or not pattern.strip():
                continue
            try:
                compiled.append(re.compile(pattern, re.IGNORECASE))
            except re.error as exc:
                logger.warning(
                    "[%s] Invalid WhatsApp mention pattern %r: %s", self.name, pattern, exc
                )
        if compiled:
            logger.info("[%s] Loaded %d WhatsApp mention pattern(s)", self.name, len(compiled))
        return compiled

    @staticmethod
    def _normalize_whatsapp_id(value: Optional[str]) -> str:
        if not value:
            return ""
        normalized = str(value).strip()
        if ":" in normalized and "@" in normalized:
            normalized = normalized.replace(":", "@", 1)
        return normalized

    # ── Per-user / per-chat allowlists ───────────────────────────────────────
    # ``allowed_users`` accepts any of:
    #   • E164 phone:           "+12025550188"
    #   • Raw digits:           "12025550188"
    #   • Full WhatsApp JID:    "12025550188@s.whatsapp.net"
    # All are normalized to the JID form so a single membership test works.
    # ``allowed_chats`` accepts the chat-id form WhatsApp uses internally:
    #   • Group:                "120363012345678901@g.us"
    #   • DM:                   "12025550188@s.whatsapp.net" or "…@c.us"

    @staticmethod
    def _phone_to_jid(value: str) -> str:
        s = str(value).strip()
        if not s:
            return ""
        if "@" in s:
            # Already a JID.  Strip a Baileys device suffix (":7@…") so that
            # ``12345:7@s.whatsapp.net`` matches an allowlist entry of
            # ``12345@s.whatsapp.net``.
            return re.sub(r":\d+@", "@", s)
        digits = re.sub(r"\D", "", s)
        if not digits:
            return ""
        return f"{digits}@s.whatsapp.net"

    @staticmethod
    def _normalize_chat_id(value: str) -> str:
        s = str(value).strip()
        if not s:
            return ""
        # Allow phone-style entries to stand in for DM chat ids.
        if "@" not in s:
            digits = re.sub(r"\D", "", s)
            if digits:
                return f"{digits}@s.whatsapp.net"
        return s

    @classmethod
    def _compile_user_entries(cls, raw: Any) -> set[str]:
        """Normalize a config user list (phones / JIDs) to JID-form entries."""
        if isinstance(raw, str):
            entries = [p.strip() for p in re.split(r"[\n\r,]+", raw) if p.strip()]
        elif isinstance(raw, list):
            entries = [str(p).strip() for p in raw if str(p).strip()]
        else:
            entries = []
        return {jid for jid in (cls._phone_to_jid(e) for e in entries) if jid}

    def _compile_allowed_users(self) -> set[str]:
        raw = self.config.extra.get("allowed_users")
        if raw is None:
            raw = os.getenv("WHATSAPP_ALLOWED_USERS", "")
        compiled = self._compile_user_entries(raw)
        if compiled:
            logger.info(
                "[%s] WhatsApp allowed_users: %d entry(ies) — others will be ignored",
                self.name,
                len(compiled),
            )
        return compiled

    def _compile_allowed_chats(self) -> set[str]:
        raw = self.config.extra.get("allowed_chats")
        if raw is None:
            raw = os.getenv("WHATSAPP_ALLOWED_CHATS", "")
        if isinstance(raw, str):
            entries = [p.strip() for p in re.split(r"[\n\r,]+", raw) if p.strip()]
        elif isinstance(raw, list):
            entries = [str(p).strip() for p in raw if str(p).strip()]
        else:
            entries = []
        compiled = {cid for cid in (self._normalize_chat_id(e) for e in entries) if cid}
        if compiled:
            logger.info(
                "[%s] WhatsApp allowed_chats: %d entry(ies) — other chats will be ignored",
                self.name,
                len(compiled),
            )
        return compiled

    def _sender_phone_jid(self, data: dict[str, Any]) -> str:
        """The sender's ``<phone>@s.whatsapp.net`` JID, or ``""`` when unknown."""
        for key in ("senderPn", "senderId"):
            jid = self._phone_to_jid(str(data.get(key) or ""))
            if jid.endswith(("@s.whatsapp.net", "@c.us")):
                return jid
        return ""

    def _sender_identities(self, data: dict[str, Any]) -> set[str]:
        """All identifying JIDs WhatsApp may have published for this sender.

        WhatsApp's LID privacy system means a single user can show up as:

        * ``<random>@lid``           — the anonymous Linked Identifier
        * ``<phone>@s.whatsapp.net`` — the real phone JID (a.k.a. ``senderPn``)
        * ``<phone>@c.us``           — legacy contact JID

        The bridge forwards every form it can resolve so the allowlist can
        match on whichever the user pasted in.
        """
        candidates = {
            self._normalize_whatsapp_id(str(data.get("senderId") or "")),
            self._normalize_whatsapp_id(str(data.get("senderPn") or "")),
            self._normalize_whatsapp_id(str(data.get("senderLid") or "")),
        }
        return {jid for jid in candidates if jid}

    def _is_allowed(self, data: dict[str, Any]) -> bool:
        """Hard allowlist: drop messages from senders/chats not on the list.

        Both lists are independent and AND-ed together when both are set:
        a message must satisfy *every* configured list.  An empty/missing list
        means "no restriction on that axis".
        """
        if self._allowed_users:
            identities = self._sender_identities(data)
            # Owners pass a configured allowlist even when it omits them —
            # owner_users alone (allowlist empty) never gates admission.
            if not (identities & (self._allowed_users | self._owner_users)):
                logger.info(
                    "[%s] Dropping message from non-allowlisted sender %s "
                    "(chat=%s).  To allow, add one of these to "
                    "allowed_users: %s",
                    self.name,
                    data.get("senderId") or "(unknown)",
                    data.get("chatId"),
                    ", ".join(sorted(identities)) or "(none)",
                )
                return False
        if self._allowed_chats:
            chat_id = str(data.get("chatId") or "")
            if chat_id not in self._allowed_chats:
                logger.info(
                    "[%s] Dropping message from non-allowlisted chat %s (sender=%s)",
                    self.name,
                    chat_id or "(unknown)",
                    data.get("senderId"),
                )
                return False
        return True

    def _bot_ids_from_message(self, data: dict[str, Any]) -> set[str]:
        bot_ids = set()
        for candidate in data.get("botIds") or []:
            normalized = self._normalize_whatsapp_id(candidate)
            if normalized:
                bot_ids.add(normalized)
        return bot_ids

    def _message_is_reply_to_bot(self, data: dict[str, Any]) -> bool:
        quoted_participant = self._normalize_whatsapp_id(data.get("quotedParticipant"))
        if not quoted_participant:
            return False
        return quoted_participant in self._bot_ids_from_message(data)

    def _message_mentions_bot(self, data: dict[str, Any]) -> bool:
        bot_ids = self._bot_ids_from_message(data)
        if not bot_ids:
            return False
        mentioned_ids = {
            nid
            for candidate in (data.get("mentionedIds") or [])
            if (nid := self._normalize_whatsapp_id(candidate))
        }
        if mentioned_ids & bot_ids:
            return True

        body = str(data.get("body") or "")
        lower_body = body.lower()
        for bot_id in bot_ids:
            bare_id = bot_id.split("@", 1)[0].lower()
            if bare_id and (f"@{bare_id}" in lower_body or bare_id in lower_body):
                return True
        return False

    def _message_matches_mention_patterns(self, data: dict[str, Any]) -> bool:
        if not self._mention_patterns:
            return False
        body = str(data.get("body") or "")
        return any(pattern.search(body) for pattern in self._mention_patterns)

    def _clean_bot_mention_text(self, text: str, data: dict[str, Any]) -> str:
        if not text:
            return text
        bot_ids = self._bot_ids_from_message(data)
        cleaned = text
        for bot_id in bot_ids:
            bare_id = bot_id.split("@", 1)[0]
            if bare_id:
                cleaned = re.sub(rf"@{re.escape(bare_id)}\b[,:\-]*\s*", "", cleaned)
        return cleaned.strip() or text

    @staticmethod
    def _is_group_chat(data: dict[str, Any]) -> bool:
        """Whether this inbound message came from a group.

        The bundled bridge publishes ``isGroup``, but the bridge is a
        replaceable component (``bridge_script`` in the connector config,
        ``LIONCLAW_WHATSAPP_BRIDGE``; whatsapp-web.js and Business-API bridges
        exist and the class docstring invites them), and an older or
        third-party one may not send that field at all. Reading a missing flag
        as False made every group message look like a DM, and a DM returns
        early from ``_should_process_message`` — so ``group_respond: "none"``
        was silently skipped and the agent answered a group it had been told
        to stay out of.

        WhatsApp's own addressing settles it without trusting the bridge: a
        group chat id always ends in ``@g.us`` and a DM's never does. Either
        signal alone marks a group, so the classification can only ever fail
        *closed* (toward the quieter behaviour).
        """
        if data.get("isGroup"):
            return True
        return str(data.get("chatId") or "").endswith("@g.us")

    def _sender_is_designated(self, data: dict[str, Any]) -> bool:
        """Whether the sender is named on ``owner_users``/``allowed_users``.

        Distinct from ``_is_allowed``, which answers "may this message in at
        all" and says yes to everyone when no list is configured. Here the
        question is "did the operator single this person out", so an empty
        pair of lists means *nobody* — used by the silent-mode escape hatch,
        where "everyone" would defeat the setting it exempts.
        """
        designated = self._allowed_users | self._owner_users
        if not designated:
            return False
        return bool(self._sender_identities(data) & designated)

    def _should_process_message(self, data: dict[str, Any]) -> bool:
        # Hard allowlists take precedence over every other rule (mention,
        # free-response, etc.).  If the sender or chat isn't allowed, drop.
        if not self._is_allowed(data):
            return False
        chat_id = str(data.get("chatId") or "")
        if chat_id == _STATUS_BROADCAST_JID:
            # A Status/"story" update is not addressed to anyone: it arrives
            # with no group flag, so it used to read as a DM and get a turn —
            # whose reply the adapter would then try to post back to
            # status@broadcast.
            return False
        if not self._is_group_chat(data):
            return True
        if chat_id in self._whatsapp_free_response_chats():
            # A per-chat exemption is more specific than the global mode, so
            # it wins even over group_respond="none".
            return True
        respond = self._whatsapp_group_respond()
        body = str(data.get("body") or "").strip()
        if body.startswith("/"):
            # Slash commands are the owner's escape hatch even in "never
            # reply" mode — but only the owner's. Any group member can type a
            # leading "/", and ``_is_allowed`` says yes to everyone while no
            # allowlist is configured, so honouring it unconditionally made
            # "Never reply (silent)" answer a stranger on an open bot. In the
            # other modes the hatch is documented as an ordinary trigger
            # (alongside @mentions and wake words) and stays open to all.
            if respond != "none" or self._sender_is_designated(data):
                return True
        if respond == "none":
            return False
        if respond == "all":
            return True
        if self._message_is_reply_to_bot(data):
            return True
        if self._message_mentions_bot(data):
            return True
        return self._message_matches_mention_patterns(data)

    def _maybe_record_observed(self, data: dict[str, Any]) -> None:
        """Record a group message the gate declined to answer.

        Feeds the persistent observed-messages store (``chat_directory``) per
        the effective ``group_listen`` mode — queryable later via
        ``chat_messages_recent`` / ``chat_messages_search`` ("mentions"
        records only messages that addressed the bot; "all" records every
        group message the access lists let through). Best-effort: recording
        must never break message handling.
        """
        try:
            if not self._is_group_chat(data):
                return
            listen = self._whatsapp_group_listen()
            if listen == "off":
                return
            # Re-check the access lists silently (``_is_allowed`` already
            # logged the drop once) — never record what the lists rejected.
            if self._allowed_users and not (
                self._sender_identities(data) & (self._allowed_users | self._owner_users)
            ):
                return
            if self._allowed_chats and str(data.get("chatId") or "") not in self._allowed_chats:
                return
            # The bridge folds media captions into ``body`` (pickText).
            text = str(data.get("body") or data.get("caption") or "").strip()
            if not text:
                return
            mentioned = (
                self._message_is_reply_to_bot(data)
                or self._message_mentions_bot(data)
                or self._message_matches_mention_patterns(data)
            )
            from olano.platforms.group_modes import should_record

            if not should_record(listen, mentioned):
                return
            from olano.platforms.chat_directory import record_observed_event

            source = self.build_source(
                chat_id=str(data.get("chatId") or ""),
                chat_name=data.get("chatName"),
                chat_type="group",
                user_id=data.get("senderId"),
                user_name=data.get("senderName"),
            )
            record_observed_event(self, source, text)
        except Exception as e:
            logger.debug("[%s] observed-store capture failed: %s", self.name, e)

    #: Capabilities a bridge must advertise to be worth adopting. Each one is
    #: something only the bridge can do, whose absence is silent rather than
    #: loud: ``inbound-media`` decrypts the message's own attachments (without
    #: it every voice note arrives as an empty message), ``quoted-media`` the
    #: attachment on a message a reply points at (without it replying to a
    #: photo arrives as a reply to nothing). Adding a name here means the next
    #: boot restarts every bridge older than it — which is the point.
    _REQUIRED_BRIDGE_CAPABILITIES = ("inbound-media", "quoted-media")

    async def _bridge_lacks_inbound_media(self) -> bool:
        """True when the bridge listening on our port is older than this code.

        Only the bridge can decrypt a WhatsApp attachment, so a bridge missing
        any of :attr:`_REQUIRED_BRIDGE_CAPABILITIES` silently drops media the
        runtime believes it is handling — and it survives a runtime restart, so
        ``connect()`` would otherwise adopt the pre-fix process indefinitely.

        Deliberately asymmetric: a *parsed* answer that omits the capability
        (including a ``404`` from a bridge too old to serve ``/`` at all — its
        ``/health`` just answered on this same port, so the server is alive and
        merely lacks the route) counts as stale, while a transport error counts
        as fine. Guessing "stale" on an unreachable endpoint would restart a
        healthy bridge on every single boot.
        """
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"http://127.0.0.1:{self._bridge_port}/",
                    timeout=aiohttp.ClientTimeout(total=3),
                ) as resp:
                    if resp.status == 404:
                        return True
                    if resp.status != 200:
                        return False
                    payload = await resp.json()
        except Exception as e:
            logger.debug("[%s] bridge capability probe failed: %s", self.name, e)
            return False
        if not isinstance(payload, dict):
            return False
        caps = payload.get("capabilities")
        if not isinstance(caps, list):
            return True
        return not set(self._REQUIRED_BRIDGE_CAPABILITIES).issubset(caps)

    async def connect(self) -> bool:
        """
        Start the WhatsApp bridge.

        This launches the Node.js bridge process and waits for it to be ready.
        """
        # Clear the teardown exemption from any previous disconnect — leaving it
        # set would swallow a genuine bridge crash for the rest of the process.
        self._shutting_down = False
        if not await asyncio.to_thread(check_whatsapp_requirements):
            logger.warning("[%s] Node.js not found. WhatsApp requires Node.js.", self.name)
            return False

        if not self._bridge_script:
            logger.warning("[%s] Bridge script not configured.", self.name)
            return False
        bridge_path = Path(self._bridge_script)
        if not bridge_path.exists():
            logger.warning("[%s] Bridge script not found: %s", self.name, bridge_path)
            return False

        logger.info("[%s] Bridge found at %s", self.name, bridge_path)

        # Acquire scoped lock to prevent duplicate sessions
        try:
            if not self._acquire_platform_lock(
                "whatsapp-session", str(self._session_path), "WhatsApp session"
            ):
                return False
        except Exception as e:
            logger.warning("[%s] Could not acquire session lock (non-fatal): %s", self.name, e)

        # Auto-install npm dependencies if node_modules doesn't exist
        bridge_dir = bridge_path.parent
        if not (bridge_dir / "node_modules").exists():
            print(f"[{self.name}] Installing WhatsApp bridge dependencies...")
            try:
                install_result = await asyncio.to_thread(
                    subprocess.run,
                    ["npm", "install", "--silent"],
                    cwd=str(bridge_dir),
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if install_result.returncode != 0:
                    print(f"[{self.name}] npm install failed: {install_result.stderr}")
                    return False
                print(f"[{self.name}] Dependencies installed")
            except Exception as e:
                print(f"[{self.name}] Failed to install dependencies: {e}")
                return False

        try:
            # Before creating an empty per-agent session dir, check if a
            # legacy pre-multi-tenant session exists at the parent path.
            # Auto-migrate it once, atomically, so users who paired before
            # the per-agent layout don't lose their device link.
            self._maybe_migrate_legacy_session()

            self._session_path.mkdir(parents=True, exist_ok=True)

            # Check if bridge is already running and connected
            import aiohttp

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"http://127.0.0.1:{self._bridge_port}/health",
                        timeout=aiohttp.ClientTimeout(total=2),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            bridge_status = data.get("status", "unknown")
                            stale = (
                                bridge_status == "connected"
                                and await self._bridge_lacks_inbound_media()
                            )
                            if stale:
                                # An upgrade ships new bridge code but never
                                # restarts the running process: it is spawned in
                                # its own process group (setsid), so it outlives
                                # the runtime that started it and this branch
                                # would adopt it forever. Reusing one that
                                # cannot decrypt inbound media means every voice
                                # note keeps arriving as an empty message after
                                # the fix is deployed — restart instead. The
                                # session dir is untouched, so no re-pairing.
                                print(
                                    f"[{self.name}] Existing bridge predates current media "
                                    "support — restarting it (pairing is preserved)"
                                )
                            elif bridge_status == "connected":
                                print(
                                    f"[{self.name}] Using existing bridge (status: {bridge_status})"
                                )
                                self._paired_jid = data.get("jid")
                                self._mark_connected()
                                self._bridge_process = None  # Not managed by us
                                self._http_session = aiohttp.ClientSession()
                                self._poll_task = asyncio.create_task(self._poll_messages())
                                self._log_listen_only_notice()
                                return True
                            else:
                                print(
                                    f"[{self.name}] Bridge found but not connected (status: {bridge_status}), restarting"
                                )
            except Exception:
                pass  # Bridge not running, start a new one

            # Kill any orphaned bridge from a previous gateway run
            await asyncio.to_thread(_kill_port_process, self._bridge_port)
            await asyncio.sleep(1)

            # Start the bridge process in its own process group.
            # Route output to a log file so QR codes, errors, and reconnection
            # messages are preserved for troubleshooting.
            whatsapp_mode = os.getenv("WHATSAPP_MODE", "self-chat")
            self._bridge_log = self._session_path.parent / "bridge.log"
            bridge_log_fh = open(self._bridge_log, "a")
            self._bridge_log_fh = bridge_log_fh

            # Build bridge subprocess environment.
            # Pass WHATSAPP_REPLY_PREFIX from config.yaml so the Node bridge
            # can use it without the user needing to set a separate env var.
            bridge_env = bridge_environment(self._reply_prefix)

            self._bridge_process = subprocess.Popen(
                [
                    "node",
                    str(bridge_path),
                    "--port",
                    str(self._bridge_port),
                    "--session",
                    str(self._session_path),
                    "--mode",
                    whatsapp_mode,
                ],
                stdout=bridge_log_fh,
                stderr=bridge_log_fh,
                preexec_fn=None if _IS_WINDOWS else os.setsid,
                env=bridge_env,
            )

            # Wait for the bridge to connect to WhatsApp.
            # Phase 1: wait for the HTTP server to come up (up to 15s).
            # Phase 2: wait for WhatsApp status: connected (up to 15s more).
            import aiohttp

            http_ready = False
            data = {}
            for attempt in range(15):
                await asyncio.sleep(1)
                if self._bridge_process.poll() is not None:
                    print(
                        f"[{self.name}] Bridge process died (exit code {self._bridge_process.returncode})"
                    )
                    print(f"[{self.name}] Check log: {self._bridge_log}")
                    self._close_bridge_log()
                    return False
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            f"http://127.0.0.1:{self._bridge_port}/health",
                            timeout=aiohttp.ClientTimeout(total=2),
                        ) as resp:
                            if resp.status == 200:
                                http_ready = True
                                data = await resp.json()
                                if data.get("status") == "connected":
                                    self._paired_jid = data.get("jid")
                                    print(f"[{self.name}] Bridge ready (status: connected)")
                                    break
                except Exception:
                    continue

            if not http_ready:
                print(f"[{self.name}] Bridge HTTP server did not start in 15s")
                print(f"[{self.name}] Check log: {self._bridge_log}")
                self._close_bridge_log()
                return False

            # Phase 2: HTTP is up but WhatsApp may still be connecting.
            # Give it more time to authenticate with saved credentials.
            if data.get("status") != "connected":
                print(f"[{self.name}] Bridge HTTP ready, waiting for WhatsApp connection...")
                for attempt in range(15):
                    await asyncio.sleep(1)
                    if self._bridge_process.poll() is not None:
                        print(f"[{self.name}] Bridge process died during connection")
                        print(f"[{self.name}] Check log: {self._bridge_log}")
                        self._close_bridge_log()
                        return False
                    try:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(
                                f"http://127.0.0.1:{self._bridge_port}/health",
                                timeout=aiohttp.ClientTimeout(total=2),
                            ) as resp:
                                if resp.status == 200:
                                    data = await resp.json()
                                    if data.get("status") == "connected":
                                        self._paired_jid = data.get("jid")
                                        print(f"[{self.name}] Bridge ready (status: connected)")
                                        break
                    except Exception:
                        continue
                else:
                    # Still not connected — warn but proceed (bridge may
                    # auto-reconnect later, e.g. after a code 515 restart).
                    print(f"[{self.name}] ⚠ WhatsApp not connected after 30s")
                    print(f"[{self.name}]   Bridge log: {self._bridge_log}")
                    print(
                        f"[{self.name}]   First run? Pair a device: "
                        f"olano connect whatsapp --exit-after-pair"
                    )
                    print(
                        f"[{self.name}]   Already paired but stuck? Delete "
                        f"{self._session_path} and re-pair."
                    )

            # Create a persistent HTTP session for all bridge communication
            self._http_session = aiohttp.ClientSession()

            # Start message polling task
            self._poll_task = asyncio.create_task(self._poll_messages())

            self._mark_connected()
            print(f"[{self.name}] Bridge started on port {self._bridge_port}")
            self._log_listen_only_notice()
            return True

        except Exception as e:
            self._release_platform_lock()
            logger.error("[%s] Failed to start bridge: %s", self.name, e, exc_info=True)
            self._close_bridge_log()
            return False

    def _log_listen_only_notice(self) -> None:
        """One loud line at connect so a silent agent is never a mystery.

        The group modes are logged beside it for the mirror-image mystery — a
        connector that answers a group it was told to stay out of. They are
        the only way to tell, from the log alone, whether the running adapter
        picked up an edited config: the dashboard writes ``agent.json5`` but
        the adapter reads it once, at boot.
        """
        if self._whatsapp_listen_only():
            print(
                f"[{self.name}] Listen-only mode: messages will be received and "
                f"processed, but NO replies, media, or typing indicators will be sent."
            )
        respond = self._whatsapp_group_respond()
        listen = self._whatsapp_group_listen()
        logger.info(
            "[%s] WhatsApp group modes: group_respond=%s, group_listen=%s (DMs are always handled)",
            self.name,
            respond,
            listen,
        )
        if respond == "none":
            print(
                f"[{self.name}] Group replies: never (group_respond=none) — "
                f"group messages will not trigger a turn."
            )

    def _close_bridge_log(self) -> None:
        """Close the bridge log file handle if open."""
        if self._bridge_log_fh:
            try:
                self._bridge_log_fh.close()
            except Exception:
                pass
            self._bridge_log_fh = None

    async def _check_managed_bridge_exit(self) -> Optional[str]:
        """Return a fatal error message if the managed bridge child exited.

        A bridge that stops while we are tearing it down is not a failure, so
        ``disconnect()`` is exempt — otherwise every ordinary agent restart
        raised a fatal error and pushed the operator a crash notification.
        """
        if self._bridge_process is None or self._shutting_down:
            return None

        returncode = self._bridge_process.poll()
        if returncode is None:
            return None

        # Distinguish a crash from a clean stop we didn't ask for. Both end the
        # session and both are worth surfacing, but only one is an error: a
        # bridge logged out of WhatsApp exits 0 on purpose, and calling that
        # "exited unexpectedly (code 0)" at ERROR reads as a fault that isn't.
        clean = returncode == 0
        message = (
            "WhatsApp bridge process stopped (code 0) — most likely logged out "
            "on the phone. Re-pair to reconnect."
            if clean
            else f"WhatsApp bridge process exited unexpectedly (code {returncode})."
        )
        if not self.has_fatal_error:
            logger.log(logging.WARNING if clean else logging.ERROR, "[%s] %s", self.name, message)
            self._set_fatal_error("whatsapp_bridge_exited", message, retryable=True)
            self._close_bridge_log()
            await self._notify_fatal_error()
        return self.fatal_error_message or message

    async def disconnect(self) -> None:
        """Stop the WhatsApp bridge and clean up any orphaned processes."""
        # Before the first kill: the poll loop runs until we cancel it below,
        # and must not read the bridge's exit as a crash.
        self._shutting_down = True
        if self._bridge_process:
            try:
                # Kill the entire process group so child node processes die too
                import signal

                try:
                    if _IS_WINDOWS:
                        self._bridge_process.terminate()
                    else:
                        os.killpg(os.getpgid(self._bridge_process.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    self._bridge_process.terminate()
                # Give SIGTERM up to a second, but stop waiting the moment the
                # child is actually gone — node usually exits in ~50ms. The
                # runtime tears every adapter down under ONE 5s budget
                # (`server._stop_agent`), so a full second spent sleeping here
                # was a fifth of it burned on an already-dead process, and with
                # several connectors attached that is what tips an ordinary
                # restart into "Stop timed out for agent '<name>'".
                for _ in range(20):
                    if self._bridge_process.poll() is not None:
                        break
                    await asyncio.sleep(0.05)
                if self._bridge_process.poll() is None:
                    try:
                        if _IS_WINDOWS:
                            self._bridge_process.kill()
                        else:
                            os.killpg(os.getpgid(self._bridge_process.pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        self._bridge_process.kill()
            except Exception as e:
                print(f"[{self.name}] Error stopping bridge: {e}")
        else:
            # Bridge was not started by us, don't kill it
            print(f"[{self.name}] Disconnecting (external bridge left running)")

        # Cancel the poll task explicitly
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
        self._poll_task = None

        # Close the persistent HTTP session
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
        self._http_session = None

        self._release_platform_lock()

        self._mark_disconnected()
        self._bridge_process = None
        self._close_bridge_log()
        print(f"[{self.name}] Disconnected")

    def format_message(self, content: str) -> str:
        """Convert standard markdown to WhatsApp-compatible formatting.

        WhatsApp supports: *bold*, _italic_, ~strikethrough~, ```code```,
        and monospaced `inline`. Standard markdown uses different syntax
        for bold/italic/strikethrough, so we convert here.

        Code blocks (``` fenced) and inline code (`) are protected from
        conversion via placeholder substitution.

        The conversion lives in ``helpers.markdown_to_whatsapp`` and is
        shared with the Twilio Conversations adapter: same client on the
        customer's phone, so one copy of the rules.
        """
        from olano.platforms.helpers import markdown_to_whatsapp

        return markdown_to_whatsapp(content)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Send a message via the WhatsApp bridge.

        Formats markdown for WhatsApp, splits long messages into chunks
        that preserve code block boundaries, and sends each chunk sequentially.
        """
        if self._whatsapp_listen_only():
            # Report success so the gateway records the reply in the session
            # transcript instead of retrying/erroring — the suppression is the
            # feature, not a delivery failure.
            logger.info(
                "[%s] listen-only: suppressed reply to %s (%d chars)",
                self.name,
                chat_id,
                len(content or ""),
            )
            return SendResult(success=True, message_id=None)
        if not self._running or not self._http_session:
            return SendResult(success=False, error="Not connected")
        bridge_exit = await self._check_managed_bridge_exit()
        if bridge_exit:
            return SendResult(success=False, error=bridge_exit)

        if not content or not content.strip():
            return SendResult(success=True, message_id=None)

        try:
            import aiohttp

            # Format and chunk the message
            formatted = self.format_message(content)
            chunks = self.truncate_message(formatted, self.MAX_MESSAGE_LENGTH)

            last_message_id = None
            for chunk in chunks:
                payload: dict[str, Any] = {
                    "chatId": chat_id,
                    "message": chunk,
                }
                if reply_to and last_message_id is None:
                    # Only reply-to on the first chunk
                    payload["replyTo"] = reply_to

                async with self._http_session.post(
                    f"http://127.0.0.1:{self._bridge_port}/send",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        last_message_id = data.get("messageId")
                    else:
                        error = await resp.text()
                        return SendResult(success=False, error=error)

                # Small delay between chunks to avoid rate limiting
                if len(chunks) > 1:
                    await asyncio.sleep(0.3)

            return SendResult(
                success=True,
                message_id=last_message_id,
            )
        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        """Edit a previously sent message via the WhatsApp bridge."""
        if self._whatsapp_listen_only():
            return SendResult(success=True, message_id=message_id)
        if not self._running or not self._http_session:
            return SendResult(success=False, error="Not connected")
        bridge_exit = await self._check_managed_bridge_exit()
        if bridge_exit:
            return SendResult(success=False, error=bridge_exit)
        try:
            import aiohttp

            async with self._http_session.post(
                f"http://127.0.0.1:{self._bridge_port}/edit",
                json={
                    "chatId": chat_id,
                    "messageId": message_id,
                    "message": content,
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    return SendResult(success=True, message_id=message_id)
                else:
                    error = await resp.text()
                    return SendResult(success=False, error=error)
        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def _send_media_to_bridge(
        self,
        chat_id: str,
        file_path: str,
        media_type: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Send any media file via bridge /send-media endpoint.

        ``extra`` merges additional keys into the bridge payload (``ptt`` for
        voice notes). Anything the running bridge does not understand is simply
        ignored by it — which is deliberate: a bridge older than this code
        still delivers a playable audio message instead of erroring out.
        """
        if self._whatsapp_listen_only():
            logger.info(
                "[%s] listen-only: suppressed %s to %s (%s)",
                self.name,
                media_type,
                chat_id,
                file_path,
            )
            return SendResult(success=True, message_id=None)
        if not self._running or not self._http_session:
            return SendResult(success=False, error="Not connected")
        bridge_exit = await self._check_managed_bridge_exit()
        if bridge_exit:
            return SendResult(success=False, error=bridge_exit)
        try:
            import aiohttp

            if not os.path.exists(file_path):
                return SendResult(success=False, error=f"File not found: {file_path}")

            payload: dict[str, Any] = {
                "chatId": chat_id,
                "filePath": file_path,
                "mediaType": media_type,
            }
            if caption:
                payload["caption"] = caption
            if file_name:
                payload["fileName"] = file_name
            if extra:
                payload.update(extra)

            async with self._http_session.post(
                f"http://127.0.0.1:{self._bridge_port}/send-media",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return SendResult(
                        success=True,
                        message_id=data.get("messageId"),
                        raw_response=data,
                    )
                else:
                    error = await resp.text()
                    return SendResult(success=False, error=error)

        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Download image URL to cache, send natively via bridge."""
        try:
            local_path = await cache_image_from_url(image_url)
            return await self._send_media_to_bridge(chat_id, local_path, "image", caption)
        except Exception:
            return await super().send_image(chat_id, image_url, caption, reply_to)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a local image file natively via bridge."""
        return await self._send_media_to_bridge(chat_id, image_path, "image", caption)

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a video natively via bridge — plays inline in WhatsApp."""
        return await self._send_media_to_bridge(chat_id, video_path, "video", caption)

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        as_voice: bool = True,
        **kwargs,
    ) -> SendResult:
        """Send audio natively via the bridge — a PTT bubble when possible.

        WhatsApp push-to-talk (what Baileys calls ``ptt``) only renders for
        Ogg/Opus, so the container decides: an Ogg gets the voice bubble, an
        mp3/wav gets a normal audio message (still playable in-chat). Without
        this override the base adapter fell back to posting the *server-side
        file path as text* — the reason WhatsApp appeared to have no TTS at all.

        WhatsApp audio messages carry no caption, so a caption is delivered as
        a separate text message rather than silently dropped.
        """
        from olano.speech import VOICE_NOTE_EXTENSIONS

        ptt = as_voice and Path(audio_path).suffix.lower() in VOICE_NOTE_EXTENSIONS
        result = await self._send_media_to_bridge(
            chat_id,
            audio_path,
            "audio",
            extra={"ptt": ptt},
        )
        if result.success and caption:
            await self.send(chat_id=chat_id, content=caption, reply_to=reply_to)
        return result

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a document/file as a downloadable attachment via bridge."""
        return await self._send_media_to_bridge(
            chat_id,
            file_path,
            "document",
            caption,
            file_name or os.path.basename(file_path),
        )

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Send typing indicator via bridge."""
        if self._whatsapp_listen_only():
            return  # monitoring mode must not reveal presence
        if not self._running or not self._http_session:
            return
        if await self._check_managed_bridge_exit():
            return

        try:
            import aiohttp

            await self._http_session.post(
                f"http://127.0.0.1:{self._bridge_port}/typing",
                json={"chatId": chat_id},
                timeout=aiohttp.ClientTimeout(total=5),
            )
        except Exception:
            pass  # Ignore typing indicator failures

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        """Get information about a WhatsApp chat."""
        if not self._running or not self._http_session:
            return {"name": "Unknown", "type": "dm"}
        if await self._check_managed_bridge_exit():
            return {"name": chat_id, "type": "dm"}

        try:
            import aiohttp

            async with self._http_session.get(
                f"http://127.0.0.1:{self._bridge_port}/chat/{chat_id}",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return {
                        "name": data.get("name", chat_id),
                        "type": "group" if data.get("isGroup") else "dm",
                        "participants": data.get("participants", []),
                    }
        except Exception as e:
            logger.debug("Could not get WhatsApp chat info for %s: %s", chat_id, e)

        return {"name": chat_id, "type": "dm"}

    async def list_chats(self) -> list[dict[str, Any]] | None:
        """Enumerate every WhatsApp group this account participates in.

        Baileys can list participating groups natively (bridge ``GET
        /groups``); individual DM contacts are not enumerable this way and
        come from the passive chat directory instead.
        """
        if not self._running or not self._http_session:
            return None
        if await self._check_managed_bridge_exit():
            return None
        try:
            import aiohttp

            async with self._http_session.get(
                f"http://127.0.0.1:{self._bridge_port}/groups",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        except Exception as e:
            logger.debug("[%s] WhatsApp group enumeration failed: %s", self.name, e)
            return None
        return [
            {
                "chat_id": str(entry.get("chatId") or ""),
                "name": str(entry.get("name") or entry.get("chatId") or ""),
                "type": "group",
            }
            for entry in (data or [])
            if entry.get("chatId")
        ]

    async def _poll_messages(self) -> None:
        """Poll the bridge for incoming messages."""
        import aiohttp

        while self._running:
            if not self._http_session:
                break
            bridge_exit = await self._check_managed_bridge_exit()
            if bridge_exit:
                print(f"[{self.name}] {bridge_exit}")
                break
            try:
                async with self._http_session.get(
                    f"http://127.0.0.1:{self._bridge_port}/messages",
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 200:
                        messages = await resp.json()
                        for msg_data in messages:
                            if self._is_redelivery(msg_data):
                                continue
                            event = await self._build_message_event(msg_data)
                            if event:
                                await self.handle_message(event)
            except asyncio.CancelledError:
                break
            except Exception as e:
                bridge_exit = await self._check_managed_bridge_exit()
                if bridge_exit:
                    print(f"[{self.name}] {bridge_exit}")
                    break
                print(f"[{self.name}] Poll error: {e}")
                await asyncio.sleep(5)

            await asyncio.sleep(self._poll_interval_seconds)

    def _is_redelivery(self, data: dict[str, Any]) -> bool:
        """True when this bridge message was already processed (same chat + id).

        Keyed on chat and id together: a WhatsApp message id is unique per
        sender, not globally. A message with no id is never treated as a
        duplicate, so an old bridge that omits it keeps working.
        """
        msg_id = str(data.get("messageId") or "")
        if not msg_id:
            return False
        if self._dedup.is_duplicate(f"{data.get('chatId') or ''}:{msg_id}"):
            logger.info("[%s] dropped redelivered WhatsApp message %s", self.name, msg_id)
            return True
        return False

    async def _build_message_event(self, data: dict[str, Any]) -> Optional[MessageEvent]:
        """Build a MessageEvent from bridge message data, downloading images to cache."""
        try:
            if not self._should_process_message(data):
                # Group listening: an unaddressed group message may still be
                # persisted to the observed-messages store instead of vanishing.
                self._maybe_record_observed(data)
                return None

            # Determine message type
            msg_type = MessageType.TEXT
            if data.get("hasMedia"):
                media_type = data.get("mediaType", "")
                if "image" in media_type:
                    msg_type = MessageType.PHOTO
                elif "video" in media_type:
                    msg_type = MessageType.VIDEO
                elif "audio" in media_type or "ptt" in media_type:  # ptt = voice note
                    msg_type = MessageType.VOICE
                else:
                    msg_type = MessageType.DOCUMENT

            # Determine chat type
            is_group = self._is_group_chat(data)
            chat_type = "group" if is_group else "dm"

            # Build source
            source = self.build_source(
                chat_id=data.get("chatId", ""),
                chat_name=data.get("chatName"),
                chat_type=chat_type,
                user_id=data.get("senderId"),
                user_name=data.get("senderName"),
                # The phone JID, when WhatsApp published one. ``senderId`` is
                # often a privacy ``@lid`` id, and an owner listed by phone
                # number can only be recognised through this.
                user_id_alt=self._sender_phone_jid(data) or None,
            )

            # Download media URLs to the local cache so agent tools
            # can access them reliably regardless of URL expiration.
            raw_urls = data.get("mediaUrls", [])
            cached_urls = []
            media_types = []
            for url in raw_urls:
                if msg_type == MessageType.PHOTO and url.startswith(("http://", "https://")):
                    try:
                        cached_path = await cache_image_from_url(url, ext=".jpg")
                        cached_urls.append(cached_path)
                        media_types.append("image/jpeg")
                        print(f"[{self.name}] Cached user image: {cached_path}", flush=True)
                    except Exception as e:
                        print(f"[{self.name}] Failed to cache image: {e}", flush=True)
                        cached_urls.append(url)
                        media_types.append("image/jpeg")
                elif msg_type == MessageType.PHOTO and os.path.isabs(url):
                    # Local file path — bridge already downloaded the image
                    cached_urls.append(url)
                    media_types.append(_bridge_media_mime(url, data.get("mediaMime"), "image/jpeg"))
                    print(f"[{self.name}] Using bridge-cached image: {url}", flush=True)
                elif msg_type == MessageType.VOICE and url.startswith(("http://", "https://")):
                    try:
                        cached_path = await cache_audio_from_url(url, ext=".ogg")
                        cached_urls.append(cached_path)
                        media_types.append("audio/ogg")
                        print(f"[{self.name}] Cached user voice: {cached_path}", flush=True)
                    except Exception as e:
                        print(f"[{self.name}] Failed to cache voice: {e}", flush=True)
                        cached_urls.append(url)
                        media_types.append("audio/ogg")
                elif msg_type == MessageType.VOICE and os.path.isabs(url):
                    # Local file path — bridge already downloaded the audio
                    cached_urls.append(url)
                    media_types.append(_bridge_media_mime(url, data.get("mediaMime"), "audio/ogg"))
                    print(f"[{self.name}] Using bridge-cached audio: {url}", flush=True)
                elif msg_type == MessageType.DOCUMENT and os.path.isabs(url):
                    # Local file path — bridge already downloaded the document
                    cached_urls.append(url)
                    ext = Path(url).suffix.lower()
                    mime = SUPPORTED_DOCUMENT_TYPES.get(ext, "application/octet-stream")
                    media_types.append(mime)
                    print(f"[{self.name}] Using bridge-cached document: {url}", flush=True)
                elif msg_type == MessageType.VIDEO and os.path.isabs(url):
                    cached_urls.append(url)
                    media_types.append(_bridge_media_mime(url, data.get("mediaMime"), "video/mp4"))
                    print(f"[{self.name}] Using bridge-cached video: {url}", flush=True)
                else:
                    cached_urls.append(url)
                    media_types.append("unknown")

            # For text-readable documents, inject file content directly into
            # the message text so the agent can read it inline.
            # Cap at 100KB to match Telegram/Discord/Slack behaviour.
            body = data.get("body", "")
            if is_group:
                body = self._clean_bot_mention_text(body, data)
            MAX_TEXT_INJECT_BYTES = 100 * 1024
            if msg_type == MessageType.DOCUMENT and cached_urls:
                for doc_path in cached_urls:
                    ext = Path(doc_path).suffix.lower()
                    if ext in (
                        ".txt",
                        ".md",
                        ".csv",
                        ".json",
                        ".xml",
                        ".yaml",
                        ".yml",
                        ".log",
                        ".py",
                        ".js",
                        ".ts",
                        ".html",
                        ".css",
                    ):
                        try:
                            file_size = Path(doc_path).stat().st_size
                            if file_size > MAX_TEXT_INJECT_BYTES:
                                print(
                                    f"[{self.name}] Skipping text injection for {doc_path} ({file_size} bytes > {MAX_TEXT_INJECT_BYTES})",
                                    flush=True,
                                )
                                continue
                            content = Path(doc_path).read_text(errors="replace")
                            fname = Path(doc_path).name
                            # Remove the doc_<hex>_ prefix for display
                            display_name = fname
                            if "_" in fname:
                                parts = fname.split("_", 2)
                                if len(parts) >= 3:
                                    display_name = parts[2]
                            injection = f"[Content of {display_name}]:\n{content}"
                            body = f"{injection}\n\n{body}" if body else injection
                            print(
                                f"[{self.name}] Injected text content from: {doc_path}", flush=True
                            )
                        except Exception as e:
                            print(f"[{self.name}] Failed to read document text: {e}", flush=True)

            # An attachment the bridge could not hand over must never be
            # silent. A voice note carries no text body, so without this the
            # agent is streamed a 0-char message and answers nothing coherent —
            # which is exactly how a failed voice-note download looked from the
            # chat. Covers an oversized file, a failed/expired download, and a
            # bridge older than the one that writes `mediaUrls`.
            if data.get("hasMedia") and not cached_urls:
                kind = _UNAVAILABLE_MEDIA_LABELS.get(msg_type, "attachment")
                note = (
                    f"[A {kind} was received but its file could not be retrieved from "
                    f"WhatsApp. Tell the sender: \"I couldn't open that {kind} — please "
                    'resend it, or send the text instead". (Operator: see the WhatsApp '
                    "bridge log for the download error.)]"
                )
                body = f"{body}\n\n{note}" if body.strip() else note
                print(
                    f"[{self.name}] Inbound {kind} had no retrievable file "
                    f"(mediaType={data.get('mediaType', '')!r})",
                    flush=True,
                )

            # Reply context: the bridge forwards the quoted message's id and
            # its text inline (Baileys ships both in contextInfo), so a
            # long-press reply reaches the agent with its referent attached —
            # plus the quoted attachment itself, which only the bridge can
            # decrypt and which a caption-less photo has instead of text.
            quoted_urls, quoted_types = _quoted_media(data)
            if quoted_urls:
                print(
                    f"[{self.name}] Using bridge-cached quoted media: {', '.join(quoted_urls)}",
                    flush=True,
                )
            return MessageEvent(
                text=body,
                message_type=msg_type,
                source=source,
                raw_message=data,
                message_id=data.get("messageId"),
                media_urls=cached_urls,
                media_types=media_types,
                reply_to_message_id=data.get("quotedId") or None,
                reply_to_text=data.get("quotedText") or None,
                reply_to_media_urls=quoted_urls,
                reply_to_media_types=quoted_types,
            )
        except Exception as e:
            print(f"[{self.name}] Error building event: {e}")
            return None
