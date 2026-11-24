"""Channel connectors — how a messenger gets wired to an agent, as data + code.

The registry behind the ``channel-connect`` handshake flow (and any future
setup surface): one :class:`ChannelConnector` per messenger describing how a
non-technical person connects it —

- **token** connectors (Telegram, Discord, Slack, …): which credential fields
  to show, how to *live-validate* them against the platform's API (so the page
  can say "✓ connected as @HerShopBot" before anything is saved), and how to
  build the ``agent.json5`` channel entry. Secrets placed in the entry are
  absorbed into the vault by the validated admin write path
  (``admin/agents.py:_vault_channel_secrets``) — they never persist in the file.
- **qr** connectors (WhatsApp, WeChat/Weixin): a background *pairing session*
  the page polls — it drives the existing pairing machinery (the Baileys bridge
  for WhatsApp, ``weixin.qr_login_events`` for WeChat) and exposes the current
  QR + status synchronously for the handshake flow's poll path.

Adding a messenger = subclassing :class:`ChannelConnector` (usually just
:class:`TokenChannelConnector` with a validate URL) and calling
:func:`register_connector`. The handshake flow, page, and mint surfaces pick it
up automatically. Signal, DingTalk, Matrix, Mattermost et al. slot in the same
way — nothing here is specific to the five launch connectors.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ── field + connector model ──────────────────────────────────────────────────


@dataclass(frozen=True)
class ConnectorField:
    """One credential input on a token connector's form."""

    name: str
    label: str
    secret: bool = True
    placeholder: str = ""
    help: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "secret": self.secret,
            "required": True,
            "placeholder": self.placeholder,
            "help": self.help,
            "options": [],
            "multiline": False,
            "value": "",
        }


class ChannelConnector:
    """Base class — one per messenger. Subclass and :func:`register_connector`."""

    #: Registry key and the page's identifier ("telegram", "whatsapp", …).
    platform: str = ""
    #: Human label ("Telegram").
    label: str = ""
    #: The ``agent.json5`` channel ``type`` this connector produces.
    channel_type: str = ""
    #: "token" (credential form) or "qr" (pairing session).
    mode: str = "token"
    #: Shown on the page — where the credential comes from, in plain words.
    instructions: str = ""
    #: Credential inputs (token mode only).
    fields: tuple[ConnectorField, ...] = ()

    async def validate(self, creds: dict[str, str]) -> str:
        """Live-check *creds* against the platform. Returns a human identity
        ("@HerShopBot") for the success message. Raises ``ValueError`` with a
        user-safe message on refusal. Token mode only."""
        raise ValueError(f"{self.label} does not take a credential form.")

    def build_entry(self, creds: dict[str, str], identity: str = "") -> dict[str, Any]:
        """The ``agent.json5`` channel entry to persist. Secret values may be
        included literally — the admin write path vaults them. *identity* is
        the paired account (QR mode) for connectors that must pin it into the
        entry; token connectors ignore it."""
        entry: dict[str, Any] = {"type": self.channel_type}
        for f in self.fields:
            value = str(creds.get(f.name) or "").strip()
            if value:
                entry[f.name] = value
        return entry

    async def start_pairing(self, key: str, agent: str, extra: dict[str, Any]) -> None:
        """Begin a pairing session writing into :func:`pairing_state` under
        *key*. QR mode only. Must be idempotent per key."""
        raise ValueError(f"{self.label} does not use QR pairing.")

    def public(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "label": self.label,
            "mode": self.mode,
            "channel_type": self.channel_type,
        }


_CONNECTORS: dict[str, ChannelConnector] = {}


def register_connector(connector: ChannelConnector) -> ChannelConnector:
    if not connector.platform:
        raise ValueError("channel connector must declare a platform key")
    _CONNECTORS[connector.platform] = connector
    return connector


def get_connector(platform: str) -> ChannelConnector | None:
    return _CONNECTORS.get(str(platform or "").strip().lower())


def all_connectors() -> list[ChannelConnector]:
    return [_CONNECTORS[k] for k in sorted(_CONNECTORS)]


# ── pairing sessions (qr mode) ───────────────────────────────────────────────


@dataclass
class PairingState:
    """Live state of one QR pairing session, readable synchronously."""

    status: str = "starting"  # starting | qr | waiting | done | error
    qr_data: str = ""
    qr_image: str = ""  # SVG data URI, "" when no encoder is available
    pair_code: str = ""  # phone-number pairing code (WhatsApp), when requested
    detail: str = ""
    identity: str = ""  # paired account (jid / account id) once done
    updated_at: float = field(default_factory=time.time)
    task: asyncio.Task | None = None
    cleanup: Any = None  # optional callable run on clear (kill spawned bridge)
    key: str = ""  # the _PAIRINGS key this state is stored under
    agent: str = ""  # the agent being paired
    extra_overrides: dict[str, Any] = field(default_factory=dict)  # channel entry overrides
    bridge_spawned: bool = False  # True once we've spawned a bridge for this session

    def touch(self) -> None:
        self.updated_at = time.time()

    def public(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "qr_data": self.qr_data,
            "qr_image": self.qr_image,
            "pair_code": self.pair_code,
            "detail": self.detail,
            "identity": self.identity,
        }


_PAIRINGS: dict[str, PairingState] = {}

#: A finished/errored session older than this is restartable.
_PAIRING_STALE_SECONDS = 15 * 60


def pairing_state(key: str) -> PairingState | None:
    return _PAIRINGS.get(key)


def clear_pairing(key: str) -> None:
    state = _PAIRINGS.pop(key, None)
    if state is None:
        return
    if state.task is not None and not state.task.done():
        state.task.cancel()
    if callable(state.cleanup):
        with contextlib.suppress(Exception):
            state.cleanup()


def _begin(key: str) -> PairingState | None:
    """Claim *key* for a new session, or return None when one is live."""
    existing = _PAIRINGS.get(key)
    if existing is not None:
        alive = existing.task is not None and not existing.task.done()
        fresh = (time.time() - existing.updated_at) < _PAIRING_STALE_SECONDS
        if alive or (existing.status in ("done",) and fresh):
            return None
        clear_pairing(key)
    state = PairingState(key=key)
    _PAIRINGS[key] = state
    return state


def qr_svg_data_uri(data: str) -> str:
    """Encode *data* as an SVG QR data URI. Returns "" when segno is missing —
    the page then shows the copyable text form instead."""
    if not data:
        return ""
    try:
        import segno

        return str(segno.make(data, error="m").svg_data_uri(scale=4, border=1))
    except Exception:  # pragma: no cover - encoder optional by design
        return ""


# ── token connectors ─────────────────────────────────────────────────────────


async def _http_json(
    method: str, url: str, *, headers: dict[str, str] | None = None, timeout: float = 10.0
) -> tuple[int, dict[str, Any]]:
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.request(
            method, url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            try:
                body = await resp.json(content_type=None)
            except Exception:
                body = {}
            return resp.status, body if isinstance(body, dict) else {}


class TelegramConnector(ChannelConnector):
    platform = "telegram"
    label = "Telegram"
    channel_type = "platform:telegram"
    mode = "token"
    instructions = (
        "In Telegram, message @BotFather, send /newbot, and copy the token it "
        "gives you (it looks like 123456789:AA…). Paste it below."
    )
    fields = (
        ConnectorField(
            name="token",
            label="Bot token",
            placeholder="123456789:AA…",
            help="From @BotFather. Stored encrypted in the vault.",
        ),
    )

    async def validate(self, creds: dict[str, str]) -> str:
        token = str(creds.get("token") or "").strip()
        if not token or ":" not in token:
            raise ValueError("That doesn't look like a Telegram bot token (123456789:AA…).")
        status, body = await _http_json("GET", f"https://api.telegram.org/bot{token}/getMe")
        if status == 401 or not body.get("ok"):
            raise ValueError("Telegram rejected that token. Copy it again from @BotFather.")
        username = str((body.get("result") or {}).get("username") or "your bot")
        return f"@{username}"


class DiscordConnector(ChannelConnector):
    platform = "discord"
    label = "Discord"
    channel_type = "platform:discord"
    mode = "token"
    instructions = (
        "In the Discord Developer Portal, open your application → Bot → Reset "
        "Token, and paste the token below."
    )
    fields = (
        ConnectorField(
            name="token",
            label="Bot token",
            placeholder="Paste the bot token",
            help="From the Discord Developer Portal. Stored encrypted in the vault.",
        ),
    )

    async def validate(self, creds: dict[str, str]) -> str:
        token = str(creds.get("token") or "").strip()
        if not token:
            raise ValueError("Paste the Discord bot token.")
        status, body = await _http_json(
            "GET",
            "https://discord.com/api/v10/users/@me",
            headers={"Authorization": f"Bot {token}"},
        )
        if status == 401:
            raise ValueError("Discord rejected that token. Reset and copy it again.")
        return str(body.get("username") or "your bot")


class SlackConnector(ChannelConnector):
    platform = "slack"
    label = "Slack"
    channel_type = "platform:slack"
    mode = "token"
    instructions = (
        "From your Slack app (api.slack.com/apps): the Bot User OAuth Token "
        "(xoxb-…) under OAuth & Permissions, and the App-Level Token (xapp-…) "
        "with connections:write under Basic Information → App-Level Tokens."
    )
    fields = (
        ConnectorField(
            name="bot_token",
            label="Bot token (xoxb-…)",
            placeholder="xoxb-…",
            help="OAuth & Permissions → Bot User OAuth Token.",
        ),
        ConnectorField(
            name="app_token",
            label="App-level token (xapp-…)",
            placeholder="xapp-…",
            help="Basic Information → App-Level Tokens (scope connections:write).",
        ),
    )

    async def validate(self, creds: dict[str, str]) -> str:
        bot_token = str(creds.get("bot_token") or "").strip()
        app_token = str(creds.get("app_token") or "").strip()
        if not bot_token.startswith("xoxb-"):
            raise ValueError("The bot token should start with xoxb-…")
        if not app_token.startswith("xapp-"):
            raise ValueError("The app-level token should start with xapp-…")
        status, body = await _http_json(
            "POST",
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {bot_token}"},
        )
        if status != 200 or not body.get("ok"):
            raise ValueError("Slack rejected the bot token (auth.test failed).")
        who = str(body.get("user") or "your app")
        team = str(body.get("team") or "")
        return f"{who} in {team}" if team else who


# ── qr connectors ────────────────────────────────────────────────────────────


class WhatsAppConnector(ChannelConnector):
    platform = "whatsapp"
    label = "WhatsApp"
    channel_type = "platform:whatsapp"
    mode = "qr"
    instructions = (
        "On your phone: WhatsApp → Settings → Linked devices → Link a device, "
        "then scan the code. Or tap 'Link with phone number instead' and use "
        "the pairing code option below."
    )

    #: How long a pairing session keeps running without success.
    PAIR_TIMEOUT_SECONDS = 600

    async def start_pairing(self, key: str, agent: str, extra: dict[str, Any]) -> None:
        state = _begin(key)
        if state is None:
            return
        state.agent = agent
        state.extra_overrides = dict(extra or {})
        state.task = asyncio.create_task(self._run(state))

    async def request_pair_code(self, key: str, agent: str, number: str) -> str:
        """Ask the bridge for a phone-number pairing code (digits only)."""
        from olano.platforms.whatsapp import WhatsAppAdapter

        digits = "".join(ch for ch in str(number or "") if ch.isdigit())
        if len(digits) < 8:
            raise ValueError(
                "Enter the full phone number including the country code — digits only, "
                "e.g. 15551234567 (the + is not needed)."
            )
        state = pairing_state(key)
        extra = dict(state.extra_overrides) if state else {}
        # The user may tap "get pairing code" before the QR poll has a bridge up;
        # ensure one is running (idempotent — a live bridge is reused).
        await self._ensure_bridge(key, agent, extra)
        port, _ = WhatsAppAdapter.resolve_runtime_layout(agent, extra)
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"http://127.0.0.1:{port}/pair-code",
                    json={"number": digits},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    body = await resp.json(content_type=None)
                    if resp.status != 200:
                        raise ValueError(
                            str((body or {}).get("error") or "Pairing code unavailable right now.")
                        )
                    code = str((body or {}).get("code") or "")
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Could not reach the WhatsApp bridge: {exc}") from exc
        if not code:
            raise ValueError("The bridge returned no pairing code — try the QR instead.")
        state = pairing_state(key)
        if state is not None:
            state.pair_code = code
            state.touch()
        return code

    async def _run(self, state: PairingState) -> None:
        try:
            await self._pair(state)
        except asyncio.CancelledError:  # pragma: no cover - teardown
            raise
        except Exception as exc:
            logger.warning("whatsapp pairing (%s) failed: %s", state.key, exc)
            state.status = "error"
            state.detail = f"Pairing failed: {exc}"
            state.touch()

    @staticmethod
    async def _bridge_health(port: int) -> dict[str, Any] | None:
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"http://127.0.0.1:{port}/health",
                    timeout=aiohttp.ClientTimeout(total=2),
                ) as resp:
                    if resp.status == 200:
                        body = await resp.json(content_type=None)
                        return body if isinstance(body, dict) else None
        except Exception:
            return None
        return None

    async def _ensure_bridge(self, key: str, agent: str, extra: dict[str, Any]) -> int:
        """Make sure a reachable bridge exists for this agent; return its port.

        Reuses a live bridge (the runtime adapter, or one we spawned earlier in
        this pairing session); otherwise spawns the bundled bridge via the
        adapter's shared spawn path (npm install + logged output + fail-fast),
        which is the fix for a silent crash showing as "cannot connect".
        """
        from olano.platforms.whatsapp import WhatsAppAdapter

        port, _ = WhatsAppAdapter.resolve_runtime_layout(agent, extra)
        current = await self._bridge_health(port)
        if current is not None:
            if str(current.get("status")) == "logged_out":
                raise ValueError(
                    "WhatsApp logged this session out. Re-pair from the dashboard "
                    "(Connections → WhatsApp), which can clear the stale session."
                )
            return port
        state = pairing_state(key)
        if state is not None and state.bridge_spawned:
            return port  # our spawn is still coming up; don't double-spawn
        # Blocking spawn (npm install, port cleanup) off the event loop.
        _, proc, _log = await asyncio.to_thread(WhatsAppAdapter.spawn_pairing_bridge, agent, extra)
        if state is not None:
            state.bridge_spawned = True

            def _kill() -> None:
                with contextlib.suppress(Exception):
                    proc.terminate()

            state.cleanup = _kill
        return port

    async def _pair(self, state: PairingState) -> None:
        from olano.platforms.whatsapp import WhatsAppAdapter

        agent, extra = state.agent, dict(state.extra_overrides)
        port, _ = WhatsAppAdapter.resolve_runtime_layout(agent, extra)

        async def health() -> dict[str, Any] | None:
            return await self._bridge_health(port)

        # Spawn (or reuse) the bridge through the shared, npm-aware path.
        await self._ensure_bridge(state.key, agent, extra)

        deadline = time.time() + self.PAIR_TIMEOUT_SECONDS
        while time.time() < deadline:
            body = await health()
            if body is not None:
                status = str(body.get("status") or "")
                if status == "connected":
                    state.status = "done"
                    state.identity = str(body.get("jid") or "")
                    state.detail = "WhatsApp linked."
                    state.touch()
                    return
                if status == "qr":
                    qr = str(body.get("qr") or "")
                    if qr and qr != state.qr_data:
                        state.qr_data = qr
                        state.qr_image = qr_svg_data_uri(qr)
                    state.status = "qr"
                    state.detail = "Scan with WhatsApp → Linked devices."
                    state.touch()
                elif status == "logged_out":
                    raise ValueError("WhatsApp logged this session out — ask for a fresh link.")
                else:
                    state.status = "waiting"
                    state.detail = "Starting WhatsApp…"
                    state.touch()
            await asyncio.sleep(2)
        raise ValueError("Pairing timed out — open a fresh link to try again.")


class WeixinConnector(ChannelConnector):
    platform = "wechat"
    label = "WeChat"
    channel_type = "platform:weixin"
    mode = "qr"
    instructions = "Open WeChat on your phone, tap Scan (扫一扫), and scan the code."

    PAIR_TIMEOUT_SECONDS = 480

    def build_entry(self, creds: dict[str, str], identity: str = "") -> dict[str, Any]:
        """Pin the paired login into the entry. The QR credential store is
        server-global, so a blank entry would ride whatever login resolution
        picks — with several agents each linking their own WeChat account, the
        entry must say which one is THIS agent's (identity = the minted
        iLink account_id, set by the pairing session on confirm)."""
        entry = super().build_entry(creds, identity)
        if identity:
            entry["extra"] = {**(entry.get("extra") or {}), "bot_id": identity}
        return entry

    async def start_pairing(self, key: str, agent: str, extra: dict[str, Any]) -> None:
        state = _begin(key)
        if state is None:
            return
        state.agent = agent
        state.task = asyncio.create_task(self._run(state))

    async def _run(self, state: PairingState) -> None:
        try:
            from olano.config.home import get_olano_home
            from olano.platforms.weixin import check_weixin_requirements, qr_login_events

            if not check_weixin_requirements():
                raise ValueError("WeChat support needs the aiohttp and cryptography packages.")
            async for event in qr_login_events(
                str(get_olano_home()), timeout_seconds=self.PAIR_TIMEOUT_SECONDS
            ):
                kind = event.get("event")
                if kind == "qr":
                    qr = str(event.get("qr") or "")
                    state.qr_data = qr
                    state.qr_image = qr_svg_data_uri(qr)
                    state.status = "qr"
                    state.detail = "Scan with WeChat (扫一扫)."
                elif kind == "status":
                    if str(event.get("status")) == "scanned":
                        state.status = "waiting"
                        state.detail = "Scanned — confirm on your phone."
                elif kind == "confirmed":
                    # Credentials are persisted server-side by qr_login_events;
                    # the token never enters this state object.
                    state.status = "done"
                    state.identity = str(event.get("account_id") or "")
                    state.detail = "WeChat linked."
                elif kind == "error":
                    raise ValueError(str(event.get("reason") or "QR login failed."))
                state.touch()
        except asyncio.CancelledError:  # pragma: no cover - teardown
            raise
        except Exception as exc:
            logger.warning("weixin pairing (%s) failed: %s", state.key, exc)
            state.status = "error"
            state.detail = f"Pairing failed: {exc}"
            state.touch()


register_connector(TelegramConnector())
register_connector(DiscordConnector())
register_connector(SlackConnector())
register_connector(WhatsAppConnector())
register_connector(WeixinConnector())
