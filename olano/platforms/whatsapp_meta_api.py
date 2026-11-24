"""WhatsApp through Meta's own Cloud API: the official Business Platform, no provider.

The third WhatsApp connector, next to ``whatsapp.py`` (the bundled Baileys
bridge, a paired phone) and ``whatsapp_twilio.py`` (the Business Platform
through Twilio). This one talks to Meta's Graph API directly with a WhatsApp
Business Account's access token, so there is no provider account, no provider
fee, and Meta's own template, status and billing data comes back unfiltered.
Every Graph call goes through :class:`~olano.platforms.whatsapp_meta_graph.
MetaGraphClient`; this module is the connector's behaviour.

## What the operator enters

An access token (a system user token: it does not expire), the WhatsApp
Business Account ID, the phone number ID, and the app secret. The token and
the secret are vaulted (``config/channel_secrets``); the two IDs are plain
settings. Nothing is pasted into Meta.

## Inbound: verified, and routed to this agent

Meta pushes every customer message and every delivery status to a webhook. The
connector points its own number's webhooks at this agent's route on the
deployment's public address, ``/api/meta/whatsapp/<agent>``
(``runtime/meta_webhook_routes.py``), with Meta's **phone-number override**
(``POST /{phone-number-id}`` ``webhook_configuration``). A number's override
wins over the Business Account's and the app's, so several agents can share
one Business Account and one app, each with its own number, and each hears
only its own. Meta checks the address with a GET that must echo its
challenge for this agent's verify token (:meth:`verify_subscription`); the
token is derived from the app secret, so it is stable across restarts and
never stored. The app is subscribed to the Business Account first, only when
it is not already: subscribing again *without* an override clears a Business
Account-level override somebody else may rely on.

Every POST is checked against ``X-Hub-Signature-256`` (HMAC-SHA256 of the raw
body, keyed by the app secret) before it is parsed. Without the app secret
nothing inbound is accepted, and the diagnostics say so; sending still works.

The Business Account-level override is **never** set automatically: it would
move every other number on the account (and whatever consumes them through
the app's own callback) to this agent. When the number-level override is
refused, the diagnostics give the address and the verify token to set by hand.

## Three rules this connector keeps

**The 24-hour window.** Outside 24 hours from the person's last message
WhatsApp delivers only an approved template. The Cloud API refuses an ordinary
message there with 131047 rather than dropping it, but the connector checks
first anyway, so the agent gets the reason and the way forward (a template)
before a call is spent. The window is measured from the connector's own record
of inbound messages, persisted under ``<home>/run/whatsapp-meta-api/`` so a
restart does not close every window: the Cloud API has no message history to
recover it from, unlike Twilio's conversation log.

**Delivery is reported only by webhook.** There is no "status of message X"
endpoint, so every status Meta posts (sent, delivered, read, played, failed,
with the billing category and type) is recorded, persisted with the window,
and read back by the WhatsApp tools.

**A person may have no phone number.** Since 2026 a WhatsApp user may hide
their number behind a username; Meta then identifies them by a Business-Scoped
User ID (``US.1349…``). Such a person's chat is keyed by that id and answered
through ``recipient``, never ``to`` (Meta accepts a BSUID in ``to`` and drops
the message).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

from olano.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from olano.platforms.config import Platform, PlatformConfig
from olano.platforms.helpers import redact_phone
from olano.platforms.whatsapp_meta_graph import (
    DEFAULT_GRAPH_VERSION,
    MEDIA_LIMITS,
    NUMBER_STATUS_HELP,
    PERMISSION_MANAGEMENT,
    PERMISSION_MESSAGING,
    PRICING_TYPE_HELP,
    MetaGraphClient,
    MetaGraphError,
    describe_meta_error,
    normalize_recipient,
    recipient_fields,
    template_send_components,
    verify_signature,
)
from olano.platforms.whatsapp_templates import (
    MAX_TEMPLATE_BODY,
    STATUS_HELP,
    Template,
    clean_variable,
    render,
)

logger = logging.getLogger(__name__)

#: Where Meta delivers this agent's webhooks, on the runtime's own public
#: origin. ``runtime/meta_webhook_routes.WHATSAPP_ROUTE`` serves the same path,
#: and a test pins the two together.
WEBHOOK_PATH_TEMPLATE = "/api/meta/whatsapp/{agent}"

# A text message's limit on the Cloud API.
MAX_BODY_LENGTH = 4096

# The free-form messaging window. Meta's rule, not ours.
SESSION_WINDOW_SECONDS = 24 * 60 * 60

# Background inbound setup: first attempt at connect, then retries with backoff.
ROUTING_RETRY_INITIAL_SECONDS = 60.0
ROUTING_RETRY_MAX_SECONDS = 15 * 60.0

# The checks at connect are bounded: platforms connect one after another, and
# a slow Meta must not hold up every other connector's boot.
CONNECT_CHECK_TIMEOUT_SECONDS = 10.0

# How many sent messages' statuses, and inbound message ids, are remembered.
MAX_STATUS_RECORDS = 2000
MAX_SEEN_MESSAGES = 5000

# A token that expires within this long is reported as a problem to fix now.
TOKEN_EXPIRY_WARNING_SECONDS = 7 * 24 * 60 * 60

#: Inbound message kinds that carry a file.
_MEDIA_KINDS = ("image", "audio", "voice", "video", "document", "sticker")
#: Inbound kinds with nothing for the agent to answer.
_SKIPPED_KINDS = ("reaction", "system", "unsupported", "unknown", "ephemeral", "request_welcome")

#: Status order: a later webhook never moves a message backwards (Meta may
#: deliver "delivered" after "read").
_STATUS_RANK = {"accepted": 0, "sent": 1, "delivered": 2, "read": 3, "played": 4, "failed": 5}

_NO_APP_SECRET = (
    "Incoming messages are off: the app secret is not set, so a delivery from Meta cannot be "
    "told apart from a forged one and none is accepted. Enter the app secret (Meta App "
    "Dashboard, App settings, Basic) in this connector and restart the agent. Sending works."
)


def check_whatsapp_meta_api_requirements() -> bool:
    """True when the adapter's dependencies and credentials are present."""
    try:
        import httpx  # noqa: F401
    except ImportError:
        return False
    return bool(
        os.getenv("WHATSAPP_META_ACCESS_TOKEN") and os.getenv("WHATSAPP_META_PHONE_NUMBER_ID")
    )


def _truthy(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _state_dir() -> Path:
    from olano.config.home import get_olano_home

    return get_olano_home() / "run" / "whatsapp-meta-api"


def _iso(stamp: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat(timespec="seconds")


def _media_type_for(kind: str) -> MessageType:
    return {
        "image": MessageType.PHOTO,
        "sticker": MessageType.PHOTO,
        "audio": MessageType.VOICE,
        "voice": MessageType.VOICE,
        "video": MessageType.VIDEO,
    }.get(kind, MessageType.DOCUMENT)


class WhatsAppMetaAPIAdapter(BasePlatformAdapter):
    """WhatsApp <-> Olano over Meta's WhatsApp Cloud API."""

    MAX_MESSAGE_LENGTH = MAX_BODY_LENGTH

    # Live progress is off for the same reason as on the Twilio connector:
    # every push is a separate chat bubble a real customer reads.
    PROGRESS_MODE: str = "off"

    def __init__(self, config: PlatformConfig, agent_name: str = ""):
        super().__init__(config, Platform.WHATSAPP_META_API, agent_name=agent_name)
        extra = config.extra or {}
        env = os.environ.get

        # The token and the secret are vaulted (config/channel_secrets) and put
        # back into the config at boot; the environment is the fallback.
        self._access_token: str = str(
            extra.get("access_token") or env("WHATSAPP_META_ACCESS_TOKEN", "")
        ).strip()
        self._app_secret: str = str(
            extra.get("app_secret") or env("WHATSAPP_META_APP_SECRET", "")
        ).strip()
        self._waba_id: str = str(extra.get("waba_id") or env("WHATSAPP_META_WABA_ID", "")).strip()
        self._phone_number_id: str = str(
            extra.get("phone_number_id") or env("WHATSAPP_META_PHONE_NUMBER_ID", "")
        ).strip()
        self._graph_version: str = (
            str(extra.get("graph_version") or env("WHATSAPP_META_GRAPH_VERSION", "")).strip()
            or DEFAULT_GRAPH_VERSION
        )

        self._allowed_users: set[str] = self._compile_allowed(
            extra.get("allowed_users", env("WHATSAPP_META_ALLOWED_USERS", ""))
        )
        # Owners (extra.owner_users): trusted by the runtime, never a gate;
        # admitted even when allowed_users omits them.
        self._owner_users: set[str] = self._compile_allowed(extra.get("owner_users", ""))
        self._allow_all: bool = _truthy(
            extra.get("allow_all_users", env("WHATSAPP_META_ALLOW_ALL_USERS", "")), True
        )
        self._mark_read: bool = _truthy(extra.get("mark_read"), True)
        self._quote_replies: bool = _truthy(extra.get("quote_replies"), False)

        self._graph: Optional[MetaGraphClient] = None
        self._credentials_verified: Optional[bool] = None
        self._number: dict[str, Any] = {}
        self._sender_address: str = ""
        self._sender_problem: str = ""
        self._sender_caveat: str = ""
        self._app_id: str = ""
        self._token_info: dict[str, Any] = {}
        self._permissions_ok: Optional[bool] = None
        self._permissions_message: str = "Not checked yet."

        # Inbound
        self._webhook_url: str = ""
        self._routing_task: Optional[asyncio.Task] = None
        self._inbound_state: str = "pending"
        self._inbound_message: str = "Setting up incoming messages with Meta."
        self._last_delivery_at: float = 0.0
        self._seen_messages: OrderedDict[str, None] = OrderedDict()

        # Persisted: the 24-hour window and delivery statuses.
        self._last_inbound: dict[str, float] = {}
        self._last_inbound_wamid: dict[str, str] = {}
        self._aliases: dict[str, str] = {}
        self._statuses: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._state_saved_at: float = 0.0
        self._state_save_pending: bool = False
        self._load_state()

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compile_allowed(raw: Any) -> set[str]:
        """Normalize an allow-list (phone numbers or BSUIDs) from a list or text."""
        if not raw:
            return set()
        if isinstance(raw, str):
            parts = [p for chunk in raw.split("\n") for p in chunk.split(",")]
        else:
            parts = [str(p) for p in raw]
        return {r for p in parts if (r := normalize_recipient(p))}

    def _is_allowed(self, chat_id: str) -> bool:
        if self._allowed_users:
            # An explicit list always wins over allow_all's default.
            alias = self._aliases.get(chat_id, "")
            admitted = self._allowed_users | self._owner_users
            return chat_id in admitted or bool(alias and alias in admitted)
        return self._allow_all

    def webhook_path(self) -> str:
        from urllib.parse import quote

        return WEBHOOK_PATH_TEMPLATE.format(agent=quote(self.agent_name or "", safe=""))

    @property
    def phone_number_id(self) -> str:
        return self._phone_number_id

    @property
    def app_fingerprint(self) -> str:
        """A stable, non-secret name for the app secret (same app, same value)."""
        if not self._app_secret:
            return ""
        return hashlib.sha256(self._app_secret.encode()).hexdigest()[:16]

    @property
    def verify_token(self) -> str:
        """The token Meta must present when it checks this agent's webhook address.

        Derived, never stored: the same app secret, agent and number give the
        same token after every restart, which is what an address typed into
        the Meta App Dashboard by hand needs.
        """
        key = (self._app_secret or self._access_token).encode()
        msg = f"olano-whatsapp-meta-api:{self.agent_name}:{self._phone_number_id}".encode()
        return hmac.new(key, msg, hashlib.sha256).hexdigest()[:40]

    @property
    def is_sandbox(self) -> bool:
        return False

    @property
    def sender_address(self) -> str:
        """The business number this connector sends from, ``+E164`` (or its id)."""
        return self._sender_address

    @property
    def sender_problem(self) -> str:
        """Why nothing can be sent from the configured number, or ``""``."""
        return self._sender_problem

    @property
    def sender_caveat(self) -> str:
        return self._sender_caveat

    @property
    def waba_id(self) -> str:
        return self._waba_id

    def graph_client(self) -> MetaGraphClient:
        """The connector's Graph client (open while connected), for the WhatsApp tools."""
        if self._graph is not None:
            return self._graph
        return self._new_graph()

    def _new_graph(self) -> MetaGraphClient:
        return MetaGraphClient(
            self._access_token,
            waba_id=self._waba_id,
            phone_number_id=self._phone_number_id,
            app_secret=self._app_secret,
            version=self._graph_version,
        )

    # ------------------------------------------------------------------
    # Persisted state
    # ------------------------------------------------------------------

    def _state_path(self) -> Optional[Path]:
        if not self.agent_name or not self._phone_number_id:
            return None
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in self.agent_name)
        return _state_dir() / f"{safe}-{self._phone_number_id}.json"

    def _load_state(self) -> None:
        path = self._state_path()
        if path is None or not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("[whatsapp-meta-api] could not read %s: %s", path, exc)
            return
        if not isinstance(data, dict):
            return
        cutoff = time.time() - SESSION_WINDOW_SECONDS
        for chat_id, stamp in (data.get("last_inbound") or {}).items():
            if isinstance(stamp, int | float) and stamp > cutoff:
                self._last_inbound[str(chat_id)] = float(stamp)
        for chat_id, wamid in (data.get("last_inbound_wamid") or {}).items():
            self._last_inbound_wamid[str(chat_id)] = str(wamid)
        for alias, chat_id in (data.get("aliases") or {}).items():
            self._aliases[str(alias)] = str(chat_id)
        for wamid, record in list((data.get("statuses") or {}).items())[-MAX_STATUS_RECORDS:]:
            if isinstance(record, dict):
                self._statuses[str(wamid)] = record

    def _save_state(self) -> None:
        path = self._state_path()
        if path is None:
            return
        cutoff = time.time() - SESSION_WINDOW_SECONDS
        data = {
            "last_inbound": {k: v for k, v in self._last_inbound.items() if v > cutoff},
            "last_inbound_wamid": dict(list(self._last_inbound_wamid.items())[-MAX_SEEN_MESSAGES:]),
            "aliases": dict(list(self._aliases.items())[-MAX_SEEN_MESSAGES:]),
            "statuses": dict(self._statuses),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, path)
            self._state_saved_at = time.monotonic()
        except OSError as exc:
            logger.warning("[whatsapp-meta-api] could not save %s: %s", path, exc)

    def _state_changed(self) -> None:
        """Save now, or within two seconds when a save just happened."""
        if time.monotonic() - self._state_saved_at >= 2.0:
            self._save_state()
            return
        if self._state_save_pending:
            return
        self._state_save_pending = True

        async def _later() -> None:
            await asyncio.sleep(2.0)
            self._state_save_pending = False
            self._save_state()

        try:
            task = asyncio.get_running_loop().create_task(_later())
        except RuntimeError:
            self._state_save_pending = False
            self._save_state()
            return
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        missing = [
            label
            for label, value in (
                ("access token", self._access_token),
                ("WhatsApp Business Account ID", self._waba_id),
                ("phone number ID", self._phone_number_id),
            )
            if not value
        ]
        if missing:
            message = (
                "The WhatsApp (Meta Cloud API) connector needs its "
                + ", ".join(missing)
                + ". All three are in the Meta App Dashboard under WhatsApp, API Setup; use a "
                "system user token (Business settings, System users) so it does not expire."
            )
            logger.error("[whatsapp-meta-api] %s", message)
            self._set_fatal_error("whatsapp_meta_api_missing_config", message, retryable=False)
            return False

        await self._stop_routing()
        if self._graph is not None:
            await self._graph.aclose()
        self._graph = self._new_graph()
        await self._graph.open()

        verdict, detail = await self._check_number()
        self._credentials_verified = verdict
        if verdict is False:
            await self._graph.aclose()
            self._graph = None
            self._set_fatal_error("whatsapp_meta_api_refused", detail, retryable=False)
            return False
        if verdict:
            await self._check_waba()
            await self._check_token()

        self._mark_connected()
        self._routing_task = asyncio.create_task(
            self._inbound_routing_loop(), name=f"whatsapp-meta-api-routing-{self.agent_name}"
        )
        logger.info(
            "[whatsapp-meta-api] connected: number %s (id %s), Business Account %s",
            redact_phone(self._sender_address) if self._sender_address else "?",
            self._phone_number_id,
            self._waba_id,
        )
        return True

    async def disconnect(self) -> None:
        await self._stop_routing()
        if self._graph is not None:
            await self._graph.aclose()
            self._graph = None
        self._save_state()
        self._mark_disconnected()
        logger.info("[whatsapp-meta-api] Disconnected")

    async def _stop_routing(self) -> None:
        task, self._routing_task = self._routing_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _bounded(self, coro: Any) -> Any:
        return await asyncio.wait_for(coro, timeout=CONNECT_CHECK_TIMEOUT_SECONDS)

    async def _check_number(self) -> tuple[Optional[bool], str]:
        """Read the number with the token: ``(True|False|None, why not)``.

        ``None`` (Meta unreachable, a 5xx) does not stop the connector: an
        outage at boot must not become a dead channel until the next restart.
        A refused token, or a phone number ID the token cannot see, does.
        """
        assert self._graph is not None
        try:
            number = await self._bounded(self._graph.phone_number())
        except MetaGraphError as exc:
            if exc.auth_refused:
                return False, (
                    f"Meta refused the access token ({exc}). It expired, was revoked, or was "
                    "copied incompletely: create a system user token in Business settings, "
                    "System users, with whatsapp_business_messaging and "
                    "whatsapp_business_management, and store it in this connector."
                )
            if exc.status in (400, 403, 404):
                return False, (
                    f"Meta did not let this token read phone number ID {self._phone_number_id} "
                    f"({exc}). Check the phone number ID (Meta App Dashboard, WhatsApp, API "
                    "Setup; it is the number's ID, not the phone number itself) and that the "
                    "token's system user has access to its WhatsApp Business Account."
                )
            logger.warning("[whatsapp-meta-api] number check inconclusive: %s", exc)
            return None, str(exc)
        except Exception as exc:
            logger.warning("[whatsapp-meta-api] number check could not reach Meta: %s", exc)
            return None, str(exc) or type(exc).__name__
        self._number = number if isinstance(number, dict) else {}
        self._sender_address = normalize_recipient(self._number.get("display_phone_number"))
        status = str(self._number.get("status") or "").upper()
        self._sender_problem = ""
        if status and status not in ("CONNECTED", "FLAGGED", "RATE_LIMITED"):
            self._sender_problem = (
                f"The number {self._sender_address or self._phone_number_id} is {status} on "
                f"WhatsApp ({NUMBER_STATUS_HELP.get(status, 'not usable')}), so nothing can be "
                "sent from it. Finish or repair it in WhatsApp Manager (Phone numbers)."
            )
            logger.error("[whatsapp-meta-api] %s", self._sender_problem)
        name_status = str(self._number.get("name_status") or "").upper()
        if name_status in ("DECLINED", "EXPIRED", "NONE", "PENDING_REVIEW"):
            self._sender_caveat = (
                f"The number's display name is {name_status.lower().replace('_', ' ')}; until "
                "WhatsApp approves one, the number's daily messaging limit stays low and "
                "some sends are refused (error 131037). Set it in WhatsApp Manager (Phone "
                "numbers, the number, Profile)."
            )
        return True, ""

    async def _check_waba(self) -> None:
        """Confirm the number belongs to the configured WhatsApp Business Account."""
        assert self._graph is not None
        try:
            rows = await self._bounded(self._graph.waba_numbers())
        except Exception as exc:
            logger.warning("[whatsapp-meta-api] Business Account check skipped: %s", exc)
            return
        ids = {str(r.get("id") or "") for r in rows}
        if self._phone_number_id not in ids:
            numbers = [
                f"{normalize_recipient(r.get('display_phone_number')) or '?'} (id {r.get('id')})"
                for r in rows
            ]
            problem = (
                f"Phone number ID {self._phone_number_id} is not on WhatsApp Business Account "
                f"{self._waba_id}, so its templates would not be this number's. "
                + (
                    "The Business Account's numbers: " + ", ".join(numbers) + "."
                    if numbers
                    else "The Business Account has no numbers the token can see."
                )
                + " Check both IDs in the Meta App Dashboard (WhatsApp, API Setup)."
            )
            self._sender_problem = self._sender_problem or problem
            logger.error("[whatsapp-meta-api] %s", problem)

    async def _check_token(self) -> None:
        """Read the token's app, expiry and permissions for the diagnostics.

        Never blocks the connector: a check that cannot be made is reported
        as unknown, and a missing permission surfaces on the call that needs
        it with Meta's own error as well.
        """
        assert self._graph is not None
        try:
            app = await self._bounded(self._graph.app())
            self._app_id = str(app.get("id") or "")
        except Exception as exc:
            logger.debug("[whatsapp-meta-api] app lookup failed: %s", exc)
        scopes: set[str] = set()
        targets: dict[str, list[str]] = {}
        if self._app_id and self._app_secret:
            try:
                info = await self._bounded(self._graph.debug_token(self._app_id))
                self._token_info = {
                    "type": info.get("type"),
                    "expires_at": info.get("expires_at"),
                    "is_valid": info.get("is_valid"),
                }
                scopes = {str(s) for s in info.get("scopes") or []}
                for grant in info.get("granular_scopes") or []:
                    if isinstance(grant, dict) and grant.get("target_ids"):
                        targets[str(grant.get("scope"))] = [str(t) for t in grant["target_ids"]]
            except Exception as exc:
                logger.debug("[whatsapp-meta-api] token debug failed: %s", exc)
        if not scopes:
            try:
                rows = await self._bounded(self._graph.permissions())
                scopes = {
                    str(r.get("permission"))
                    for r in rows
                    if str(r.get("status") or "").lower() == "granted"
                }
            except Exception as exc:
                self._permissions_ok = None
                self._permissions_message = f"The token's permissions could not be read ({exc})."
                return
        needed = (PERMISSION_MESSAGING, PERMISSION_MANAGEMENT)
        lacking = [p for p in needed if p not in scopes]
        outside = [
            p for p in needed if p in targets and targets[p] and self._waba_id not in targets[p]
        ]
        if lacking or outside:
            self._permissions_ok = False
            parts = []
            if lacking:
                parts.append("the token lacks " + " and ".join(lacking))
            if outside:
                parts.append(
                    " and ".join(outside)
                    + f" is granted for other Business Accounts, not {self._waba_id}"
                )
            self._permissions_message = (
                "; ".join(parts).capitalize()
                + ". whatsapp_business_messaging sends and receives messages; "
                "whatsapp_business_management reads templates and sets up the webhook. Grant "
                "them to the system user's token in Business settings, System users, and store "
                "the new token."
            )
            if PERMISSION_MESSAGING in lacking:
                self._sender_problem = self._sender_problem or self._permissions_message
        else:
            self._permissions_ok = True
            self._permissions_message = (
                "The token has whatsapp_business_messaging and whatsapp_business_management"
                + (f" for Business Account {self._waba_id}." if targets else ".")
            )
        expires = self._token_info.get("expires_at")
        if isinstance(expires, int | float) and expires:
            left = expires - time.time()
            if left < TOKEN_EXPIRY_WARNING_SECONDS:
                warning = (
                    f"The access token expires {_iso(expires)} (it is a temporary token). "
                    "Replace it with a system user token (Business settings, System users), "
                    "which does not expire."
                )
                self._sender_caveat = self._sender_caveat or warning

    # ------------------------------------------------------------------
    # Inbound setup: Meta is told where to deliver, by us
    # ------------------------------------------------------------------

    def _current_webhook_url(self) -> tuple[str, str]:
        from olano.core.mcp_setup import public_origin
        from olano.platforms.whatsapp_twilio import public_webhook_base

        if not self.agent_name:
            return "", "this connector is not attached to a named agent"
        base, problem = public_webhook_base(public_origin())
        if not base:
            return "", problem
        return f"{base}{self.webhook_path()}", ""

    def _set_inbound(self, state: str, message: str) -> None:
        changed = (state, message) != (self._inbound_state, self._inbound_message)
        self._inbound_state = state
        self._inbound_message = message
        if changed:
            (logger.info if state == "ready" else logger.warning)("[whatsapp-meta-api] %s", message)

    def _manual_setup(self, url: str) -> str:
        return (
            "To set it up by hand: Meta App Dashboard, WhatsApp, Configuration, Callback URL "
            f"{url}, Verify token {self.verify_token}, then subscribe the messages field."
        )

    async def _inbound_routing_loop(self) -> None:
        delay = ROUTING_RETRY_INITIAL_SECONDS
        while self._running:
            try:
                done = await self._ensure_inbound_routing()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("[whatsapp-meta-api] inbound setup failed")
                self._set_inbound(
                    "error", f"Setting up incoming messages failed ({exc}). Retrying automatically."
                )
                done = False
            if done:
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, ROUTING_RETRY_MAX_SECONDS)

    async def _ensure_inbound_routing(self) -> bool:
        """Point this number's webhooks at this agent. True when nothing is left to retry."""
        if not self._app_secret:
            self._set_inbound("error", _NO_APP_SECRET)
            return True  # nothing a retry can change before a restart
        url, problem = self._current_webhook_url()
        if not url:
            self._set_inbound(
                "error", f"Incoming messages cannot be set up: {problem}. Sending still works."
            )
            return False
        self._webhook_url = url
        graph = self.graph_client()
        try:
            current = await graph.phone_number(fields="webhook_configuration")
        except MetaGraphError as exc:
            self._set_inbound(
                "error",
                f"Could not read where Meta delivers this number's messages ({exc}). "
                "Retrying automatically.",
            )
            return False
        config = current.get("webhook_configuration") if isinstance(current, dict) else None
        config = config if isinstance(config, dict) else {}
        previous = str(config.get("phone_number") or "")

        if not await self._ensure_app_subscribed(graph, url):
            return False
        if previous == url:
            self._set_inbound("ready", self._ready_message())
            return True
        try:
            await graph.set_number_callback(url, self.verify_token)
        except MetaGraphError as exc:
            detail = (
                "the token lacks whatsapp_business_management or its system user has no full "
                "access to this number"
                if exc.permission_refused
                else "Meta could not reach this address or it did not answer the check"
                if exc.status in (400, 502, 503)
                else str(exc)
            )
            self._set_inbound(
                "error",
                f"Meta would not send this number's messages to this agent ({detail}; {exc}). "
                f"{self._manual_setup(url)} Retrying automatically.",
            )
            return False
        if previous and previous != url:
            logger.warning(
                "[whatsapp-meta-api] webhooks for %s went to %s; they now go to agent %r",
                redact_phone(self._sender_address),
                previous,
                self.agent_name,
            )
        self._set_inbound("ready", self._ready_message(previous if previous != url else ""))
        return True

    async def _ensure_app_subscribed(self, graph: MetaGraphClient, url: str) -> bool:
        """Subscribe the app to the Business Account's webhooks, only when it is not.

        Subscribing again with no body would clear a Business Account-level
        override somebody else relies on, so an existing subscription is left
        alone. With the app unknown, any subscription counts as ours: the
        conservative reading.
        """
        try:
            apps = await graph.subscribed_apps()
        except MetaGraphError as exc:
            self._set_inbound(
                "error",
                f"Could not read the Business Account's webhook subscriptions ({exc}). "
                f"{self._manual_setup(url)} Retrying automatically.",
            )
            return False
        ids = {
            str((row.get("whatsapp_business_api_data") or {}).get("id") or "")
            for row in apps
            if isinstance(row.get("whatsapp_business_api_data"), dict)
        }
        if (self._app_id and self._app_id in ids) or (not self._app_id and apps):
            return True
        try:
            await graph.subscribe_app()
        except MetaGraphError as exc:
            self._set_inbound(
                "error",
                f"Could not subscribe the app to WhatsApp Business Account {self._waba_id}'s "
                f"webhooks ({exc}). {self._manual_setup(url)} Retrying automatically.",
            )
            return False
        return True

    def _ready_message(self, previous: str = "") -> str:
        message = (
            "Incoming messages: set up automatically. Meta delivers messages to "
            f"{self._sender_address or self._phone_number_id} to {self._webhook_url}, "
            "each checked against the app secret."
        )
        if previous:
            message += f" This replaced the number's previous webhook address ({previous})."
        return message

    # ------------------------------------------------------------------
    # Webhook entry points (runtime/meta_webhook_routes.py)
    # ------------------------------------------------------------------

    def verify_subscription(self, mode: str, token: str, challenge: str) -> Optional[str]:
        """Meta's GET check of the webhook address: the challenge, or ``None``."""
        if mode != "subscribe" or not challenge:
            return None
        if not hmac.compare_digest(token.encode(), self.verify_token.encode()):
            logger.warning(
                "[whatsapp-meta-api] refused a webhook check with the wrong verify token"
            )
            return None
        logger.info("[whatsapp-meta-api] Meta checked the webhook address")
        return challenge

    def signature_valid(self, raw: bytes, header: str) -> bool:
        return verify_signature(self._app_secret, raw, header)

    async def handle_webhook_value(self, value: dict[str, Any]) -> None:
        """Process one ``changes[].value`` for this number: statuses, messages, errors."""
        self._last_delivery_at = time.time()
        if self._inbound_state != "ready" and self._app_secret:
            # A delivery proves the route works whatever the setup reported.
            self._set_inbound(
                "ready",
                "Incoming messages are arriving from Meta, each checked against the app secret.",
            )
        contacts = [c for c in value.get("contacts") or [] if isinstance(c, dict)]
        for status in value.get("statuses") or []:
            if isinstance(status, dict):
                self._record_status(status)
        for message in value.get("messages") or []:
            if isinstance(message, dict):
                try:
                    await self._ingest(message, contacts)
                except Exception:
                    logger.exception("[whatsapp-meta-api] could not process an inbound message")
        for error in value.get("errors") or []:
            if isinstance(error, dict):
                logger.warning(
                    "[whatsapp-meta-api] Meta reported %s",
                    describe_meta_error(error.get("code"), str(error.get("title") or "")),
                )

    def _record_status(self, status: dict[str, Any]) -> None:
        wamid = str(status.get("id") or "")
        state = str(status.get("status") or "").lower()
        if not wamid or state not in _STATUS_RANK:
            return
        record = self._statuses.get(wamid) or {}
        if _STATUS_RANK.get(record.get("status", ""), -1) > _STATUS_RANK[state]:
            return
        record = dict(record)
        record["status"] = state
        try:
            record["at"] = float(status.get("timestamp") or time.time())
        except (TypeError, ValueError):
            record["at"] = time.time()
        recipient = normalize_recipient(status.get("recipient_id"))
        if recipient:
            record.setdefault("to", recipient)
        pricing = status.get("pricing")
        if isinstance(pricing, dict):
            record["billing"] = {
                "billable": pricing.get("billable"),
                "category": pricing.get("category"),
                "type": pricing.get("type"),
            }
        errors = status.get("errors")
        if (
            state == "failed"
            and isinstance(errors, list)
            and errors
            and isinstance(errors[0], dict)
        ):
            err = errors[0]
            data = err.get("error_data") if isinstance(err.get("error_data"), dict) else {}
            record["error_code"] = err.get("code")
            record["error"] = describe_meta_error(
                err.get("code"), str((data or {}).get("details") or err.get("title") or "")
            )
        self._statuses[wamid] = record
        self._statuses.move_to_end(wamid)
        while len(self._statuses) > MAX_STATUS_RECORDS:
            self._statuses.popitem(last=False)
        self._state_changed()

    def _remember_sent(self, wamid: str, to: str, **fields: Any) -> None:
        if not wamid:
            return
        self._statuses[wamid] = {"status": "accepted", "at": time.time(), "to": to, **fields}
        while len(self._statuses) > MAX_STATUS_RECORDS:
            self._statuses.popitem(last=False)
        self._state_changed()

    async def _ingest(self, message: dict[str, Any], contacts: list[dict[str, Any]]) -> None:
        wamid = str(message.get("id") or "")
        if wamid:
            if wamid in self._seen_messages:
                return
            self._seen_messages[wamid] = None
            while len(self._seen_messages) > MAX_SEEN_MESSAGES:
                self._seen_messages.popitem(last=False)
        kind = str(message.get("type") or "text").lower()
        if kind in _SKIPPED_KINDS:
            return
        if message.get("group_id") or message.get("chat"):
            logger.info("[whatsapp-meta-api] ignoring a group message: groups are not supported")
            return

        phone = normalize_recipient(message.get("from"))
        contact = contacts[0] if contacts else {}
        bsuid = str(message.get("from_user_id") or contact.get("user_id") or "").strip()
        chat_id = phone or normalize_recipient(bsuid)
        if not chat_id:
            return
        if phone and bsuid:
            self._aliases[bsuid] = phone
        if not self._is_allowed(chat_id):
            logger.info(
                "[whatsapp-meta-api] ignoring a message from %s (not allowed)",
                redact_phone(chat_id),
            )
            return

        raw_profile = contact.get("profile")
        profile: dict[str, Any] = raw_profile if isinstance(raw_profile, dict) else {}
        name = str(profile.get("name") or profile.get("username") or "").strip()
        try:
            stamp = float(message.get("timestamp") or 0) or time.time()
        except (TypeError, ValueError):
            stamp = time.time()
        # Opens the 24-hour window, recorded before the agent runs so a reply
        # from a long turn is still measured from the customer's message.
        self._last_inbound[chat_id] = max(stamp, self._last_inbound.get(chat_id, 0.0))
        if wamid:
            self._last_inbound_wamid[chat_id] = wamid
        self._state_changed()
        if self._mark_read and wamid:
            self._spawn(self._read_receipt(wamid))

        event = await self._build_event(message, kind, chat_id, name, wamid)
        if event is not None:
            await self.handle_message(event)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _read_receipt(self, wamid: str, *, typing: bool = False) -> None:
        try:
            await self.graph_client().mark_read(wamid, typing=typing)
        except Exception as exc:
            logger.debug("[whatsapp-meta-api] read receipt failed: %s", exc)

    async def _build_event(
        self, message: dict[str, Any], kind: str, chat_id: str, name: str, wamid: str
    ) -> Optional[MessageEvent]:
        text = ""
        media_urls: list[str] = []
        media_types: list[str] = []
        msg_type = MessageType.TEXT
        raw_block = message.get(kind)
        block: dict[str, Any] = raw_block if isinstance(raw_block, dict) else {}
        if kind == "text":
            text = str(block.get("body") or "")
        elif kind in _MEDIA_KINDS:
            text = str(block.get("caption") or "")
            saved, mime, failure = await self._fetch_media(block, kind)
            if saved:
                media_urls.append(saved)
                media_types.append(mime)
                msg_type = _media_type_for(kind)
            else:
                # Never silence a missing attachment: a voice note has no text,
                # so dropping it quietly starts a turn with nothing in it.
                text = (text + "\n" + failure).strip() if text else failure
        elif kind == "button":
            # A tap on a template's quick-reply button.
            text = str(block.get("text") or block.get("payload") or "")
        elif kind == "interactive":
            reply = block.get("button_reply") or block.get("list_reply") or {}
            if isinstance(reply, dict):
                text = str(reply.get("title") or reply.get("id") or "")
                if reply.get("description"):
                    text += f" ({reply['description']})"
            if not text and isinstance(block.get("nfm_reply"), dict):
                text = (
                    "[a form was submitted: "
                    + str(block["nfm_reply"].get("response_json") or "")
                    + "]"
                )
        elif kind == "location":
            parts = [str(block.get(k)) for k in ("name", "address") if block.get(k)]
            text = (
                f"[location: {block.get('latitude')}, {block.get('longitude')}"
                + (f" ({', '.join(parts)})" if parts else "")
                + "]"
            )
        elif kind == "contacts":
            shared = []
            for card in message.get("contacts") or []:
                if not isinstance(card, dict):
                    continue
                cname = (
                    (card.get("name") or {}).get("formatted_name")
                    if isinstance(card.get("name"), dict)
                    else ""
                )
                phones = [
                    str(p.get("phone"))
                    for p in card.get("phones") or []
                    if isinstance(p, dict) and p.get("phone")
                ]
                shared.append(f"{cname or 'a contact'} {', '.join(phones)}".strip())
            text = (
                "[shared contact: " + "; ".join(shared) + "]"
                if shared
                else "[a contact was shared]"
            )
        elif kind == "order":
            items = block.get("product_items") or []
            text = f"[an order with {len(items)} item(s) from the catalog]"
        else:
            text = f"[a {kind} message arrived that this connector does not read]"
        if not text and not media_urls:
            return None
        source = self.build_source(
            chat_id=chat_id,
            chat_name=name or chat_id,
            chat_type="dm",
            user_id=chat_id,
            user_name=name or chat_id,
        )
        event = MessageEvent(
            text=text,
            message_type=msg_type,
            source=source,
            raw_message=message,
            message_id=wamid,
            media_urls=media_urls,
            media_types=media_types,
        )
        context = message.get("context")
        if isinstance(context, dict) and context.get("id"):
            event.reply_to_message_id = str(context["id"])
        return event

    async def _fetch_media(
        self, block: dict[str, Any], kind: str
    ) -> tuple[Optional[str], str, str]:
        """Download one attachment: ``(local_path, mime, "")`` or ``(None, "", note)``."""
        media_id = str(block.get("id") or "")
        if not media_id:
            return None, "", f"[a {kind} arrived without an identifier and could not be fetched]"
        limit = MEDIA_LIMITS["document"]
        try:
            data, mime = await self.graph_client().download_media(media_id, limit)
        except MetaGraphError as exc:
            logger.warning("[whatsapp-meta-api] media download failed: %s", exc)
            return None, "", f"[a {kind} arrived but could not be downloaded from WhatsApp]"
        except Exception as exc:
            logger.warning("[whatsapp-meta-api] media download error: %s", exc)
            return None, "", f"[a {kind} arrived but could not be downloaded]"
        mime = (mime or str(block.get("mime_type") or "") or "application/octet-stream").split(";")[
            0
        ]
        return self._cache_inbound(data, mime, str(block.get("filename") or ""), media_id, kind)

    @staticmethod
    def _cache_inbound(
        data: bytes, mime: str, filename: str, media_id: str, kind: str
    ) -> tuple[Optional[str], str, str]:
        """Write downloaded bytes into the adapter media cache (absolute path out).

        The extension comes from the MIME type Meta reports: transcription
        reads the audio container off it, so an ``audio/mpeg`` note written
        ``.ogg`` would be a format error rather than a transcript.
        """
        from olano.platforms.base import (
            cache_audio_from_bytes,
            cache_document_from_bytes,
            cache_image_from_bytes,
        )
        from olano.uploads import sanitize_filename

        label = (
            "voice"
            if kind in ("audio", "voice")
            else ("image" if kind in ("image", "sticker") else kind)
        )
        safe_name = sanitize_filename(filename or f"{label}-{media_id}", mime, label)
        ext = Path(safe_name).suffix
        try:
            if mime.startswith("image/"):
                path = cache_image_from_bytes(data, ext=ext or ".jpg")
            elif mime.startswith("audio/"):
                path = cache_audio_from_bytes(data, ext=ext or ".ogg")
            else:
                path = cache_document_from_bytes(data, safe_name)
        except Exception as exc:
            logger.warning("[whatsapp-meta-api] could not cache an attachment: %s", exc)
            return None, "", f"[a {kind} arrived but could not be saved]"
        return path, mime, ""

    # ------------------------------------------------------------------
    # The 24-hour window
    # ------------------------------------------------------------------

    def _window_stamp(self, chat_id: str) -> float:
        stamp = self._last_inbound.get(chat_id, 0.0)
        for alias, target in self._aliases.items():
            if target == chat_id:
                stamp = max(stamp, self._last_inbound.get(alias, 0.0))
            elif alias == chat_id:
                stamp = max(stamp, self._last_inbound.get(target, 0.0))
        return stamp

    def _window_is_open(self, chat_id: str) -> bool:
        last = self._window_stamp(chat_id)
        return bool(last and (time.time() - last) < SESSION_WINDOW_SECONDS)

    def open_windows(self) -> int:
        cutoff = time.time() - SESSION_WINDOW_SECONDS
        return sum(1 for stamp in self._last_inbound.values() if stamp > cutoff)

    def _closed_window_error(self, chat_id: str) -> str:
        return (
            f"WhatsApp's 24-hour messaging window is closed for {redact_phone(chat_id)} (no "
            "message from them in the last 24 hours). WhatsApp only delivers an approved "
            "template outside that window. Send an approved template instead: "
            "whatsapp_template_list shows them and whatsapp_template_send sends one, when this "
            "agent has those tools. Otherwise wait for the person to write first."
        )

    async def window_status(self, chat_id: str) -> dict[str, Any]:
        to = normalize_recipient(chat_id)
        if not to:
            return {"to": chat_id, "error": f"{chat_id!r} is not a WhatsApp number"}
        last = self._window_stamp(to)
        out: dict[str, Any] = {
            "to": to,
            "window_open": self._window_is_open(to),
            "last_message_from_them": _iso(last) if last else None,
        }
        if last:
            out["window_closes_at"] = _iso(last + SESSION_WINDOW_SECONDS)
        return out

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def _send_error(self, exc: Exception, to: str) -> SendResult:
        if isinstance(exc, MetaGraphError):
            if exc.code == 131047:
                return SendResult(success=False, error=self._closed_window_error(to))
            error = describe_meta_error(exc.code, str(exc)) if exc.code else str(exc)
            retryable = exc.status >= 500 or exc.code in (130429, 131000, 131016, 131056, 131057)
            logger.error("[whatsapp-meta-api] send failed: %s", exc)
            return SendResult(success=False, error=error, retryable=retryable)
        logger.error("[whatsapp-meta-api] send error: %s", exc)
        return SendResult(success=False, error=str(exc) or type(exc).__name__, retryable=True)

    async def _post(self, to: str, payload: dict[str, Any], **record: Any) -> SendResult:
        if self._sender_problem:
            return SendResult(success=False, error=self._sender_problem, retryable=False)
        try:
            wamid = await self.graph_client().send_message({**recipient_fields(to), **payload})
        except Exception as exc:
            return self._send_error(exc, to)
        self._remember_sent(wamid, to, **record)
        return SendResult(success=True, message_id=wamid)

    def _context(self, reply_to: Optional[str]) -> dict[str, Any]:
        if self._quote_replies and reply_to and str(reply_to).startswith("wamid."):
            return {"context": {"message_id": str(reply_to)}}
        return {}

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Send text, in chunks of 4096 characters, inside the 24-hour window."""
        to = normalize_recipient(chat_id)
        if not to:
            return SendResult(success=False, error=f"{self.name}: invalid recipient {chat_id!r}")
        if not (content or "").strip():
            return SendResult(success=True)
        if not self._window_is_open(to):
            return SendResult(success=False, error=self._closed_window_error(to))
        last = SendResult(success=True)
        for index, chunk in enumerate(
            self.truncate_message(self.format_message(content), MAX_BODY_LENGTH)
        ):
            payload: dict[str, Any] = {"type": "text", "text": {"body": chunk, "preview_url": True}}
            if index == 0:
                payload.update(self._context(reply_to))
            last = await self._post(to, payload)
            if not last.success:
                return last
        return last

    async def send_typing(self, chat_id: str, metadata: Any = None) -> None:
        """Mark the person's last message read and show "typing…" (up to 25 s).

        Meta couples the two in one request, so with read receipts switched
        off (``mark_read: false``) there is no typing indicator either.
        """
        if not self._mark_read:
            return
        wamid = self._last_inbound_wamid.get(normalize_recipient(chat_id))
        if wamid and self._graph is not None:
            await self._read_receipt(wamid, typing=True)

    async def send_template(
        self,
        chat_id: str,
        template: Template,
        variables: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Send an approved template, inside or outside the window.

        Every placeholder must have a value (a missing one would otherwise
        make Meta refuse the send, or show the template's sample text).
        Values are cleaned of line breaks. On success ``raw_response`` carries
        the rendered text.
        """
        to = normalize_recipient(chat_id)
        if not to:
            return SendResult(success=False, error=f"{self.name}: invalid recipient {chat_id!r}")
        if not template.approved:
            return SendResult(
                success=False,
                error=(
                    f"Template {template.name} is {template.status} "
                    f"({STATUS_HELP.get(template.status, 'not approved')}); only approved "
                    "templates can be sent."
                ),
            )
        if template.header_format == "LOCATION":
            return SendResult(
                success=False,
                error=f"Template {template.name} has a location header, which this connector cannot fill.",
            )
        values = {str(k): clean_variable(v) for k, v in (variables or {}).items()}
        missing = [k for k in template.placeholders if not values.get(k)]
        if missing:
            return SendResult(
                success=False,
                error=(
                    f"Template {template.name} needs a value for "
                    + ", ".join("{{" + k + "}}" for k in missing)
                    + f". It reads: {template.rendered({})}"
                ),
            )
        if template.header_format in ("IMAGE", "VIDEO", "DOCUMENT") and not values[
            "header"
        ].startswith("https://"):
            return SendResult(
                success=False,
                error=(
                    f"Template {template.name} has a {template.header_format.lower()} header: "
                    "give {{header}} an https link to the file."
                ),
            )
        body = render(template.body, values)
        if len(body) > MAX_TEMPLATE_BODY:
            return SendResult(
                success=False,
                error=(
                    f"With these values the message is {len(body)} characters; WhatsApp "
                    f"allows {MAX_TEMPLATE_BODY}. Shorten the values."
                ),
            )
        block: dict[str, Any] = {"name": template.name, "language": {"code": template.language}}
        components = template_send_components(template, values)
        if components:
            block["components"] = components
        result = await self._post(
            to,
            {"type": "template", "template": block},
            template=template.name,
            category=template.category,
        )
        if result.success:
            result.raw_response = {"rendered": template.rendered(values)}
        return result

    def message_record(self, message_id: str) -> dict[str, Any]:
        """What Meta has reported about a sent message, or ``{}``."""
        return dict(self._statuses.get(str(message_id or "").strip()) or {})

    async def message_status(self, chat_id: str, message_id: str) -> tuple[str, str]:
        """``(status, detail)`` of a sent message, from the statuses Meta posted.

        ``("", reason)`` when nothing is known: no record of the id, or no
        status from WhatsApp yet.
        """
        record = self.message_record(message_id)
        if not record:
            return "", (
                "there is no record of this message: the connector keeps the last "
                f"{MAX_STATUS_RECORDS} messages it sent, and only statuses Meta delivered to it"
            )
        status = str(record.get("status") or "")
        if status == "failed":
            return "failed", str(record.get("error") or "")
        if status == "accepted":
            return "", "WhatsApp has not reported on it yet"
        return status, ""

    # ── media ────────────────────────────────────────────────────────────

    async def _send_file(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str],
        kind: str,
        *,
        filename: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> SendResult:
        to = normalize_recipient(chat_id)
        if not to:
            return SendResult(success=False, error=f"{self.name}: invalid recipient {chat_id!r}")
        path = Path(file_path)
        if not path.is_file():
            return SendResult(success=False, error=f"{self.name}: no such file: {file_path}")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if kind == "image" and mime not in ("image/jpeg", "image/png"):
            kind = "document"  # WhatsApp shows only JPEG and PNG as images
        limit = MEDIA_LIMITS.get(kind, MEDIA_LIMITS["document"])
        size = path.stat().st_size
        if size > limit:
            from olano.uploads import human_size

            return SendResult(
                success=False,
                error=f"{self.name}: {path.name} is {human_size(size)}, over WhatsApp's {human_size(limit)} limit for {kind} files.",
            )
        if not self._window_is_open(to):
            return SendResult(success=False, error=self._closed_window_error(to))
        if self._sender_problem:
            return SendResult(success=False, error=self._sender_problem, retryable=False)
        try:
            media_id = await self.graph_client().upload_media(path.name, path.read_bytes(), mime)
        except Exception as exc:
            return self._send_error(exc, to)
        if not media_id:
            return SendResult(success=False, error=f"{self.name}: media upload failed")
        block: dict[str, Any] = {"id": media_id}
        if caption and kind in ("image", "video", "document"):
            block["caption"] = self.format_message(caption)[:1024]
        if kind == "document":
            block["filename"] = filename or path.name
        if kind == "audio" and mime in ("audio/ogg", "audio/opus"):
            block["voice"] = True
        payload: dict[str, Any] = {"type": kind, kind: block, **self._context(reply_to)}
        return await self._post(to, payload)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs: Any,
    ) -> SendResult:
        return await self._send_file(chat_id, image_path, caption, "image", reply_to=reply_to)

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs: Any,
    ) -> SendResult:
        return await self._send_file(chat_id, video_path, caption, "video", reply_to=reply_to)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs: Any,
    ) -> SendResult:
        return await self._send_file(
            chat_id, file_path, caption, "document", filename=file_name, reply_to=reply_to
        )

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs: Any,
    ) -> SendResult:
        """Audio: an Ogg/Opus file shows as a voice note, anything else as an audio file."""
        return await self._send_file(chat_id, audio_path, None, "audio", reply_to=reply_to)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """A remote image by link: Meta fetches it itself."""
        to = normalize_recipient(chat_id)
        if not to:
            return SendResult(success=False, error=f"{self.name}: invalid recipient {chat_id!r}")
        if not str(image_url).startswith("https://"):
            return SendResult(
                success=False, error=f"{self.name}: only https image links can be sent"
            )
        if not self._window_is_open(to):
            return SendResult(success=False, error=self._closed_window_error(to))
        block: dict[str, Any] = {"link": image_url}
        if caption:
            block["caption"] = self.format_message(caption)[:1024]
        return await self._post(to, {"type": "image", "image": block, **self._context(reply_to)})

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    def format_message(self, content: str) -> str:
        """Markdown to WhatsApp's own markup (shared with the other WhatsApp connectors)."""
        from olano.platforms.helpers import markdown_to_whatsapp

        return markdown_to_whatsapp(content or "").strip()

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": normalize_recipient(chat_id) or chat_id, "type": "dm"}

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def diagnostic_snapshot(self) -> dict[str, Any]:
        """Connector health: credentials, permissions, number, webhook, window."""
        connected = bool(self.is_connected)
        number = self._number
        token_note = ""
        expires = self._token_info.get("expires_at")
        if expires == 0:
            token_note = " The token does not expire."
        elif isinstance(expires, int | float) and expires:
            token_note = f" The token expires {_iso(expires)}."
        name = str(number.get("verified_name") or "")
        checks: list[dict[str, Any]] = [
            {"id": "runtime", "ok": True, "message": "Adapter is running."},
            {
                "id": "connection",
                "ok": connected,
                "message": (
                    f"Meta accepted the access token.{token_note}"
                    if connected and self._credentials_verified
                    else (
                        "Running, but Meta could not be reached at startup to check the "
                        "token; it is checked again on every request."
                        if connected
                        else "Not connected to Meta."
                    )
                ),
            },
            {
                "id": "permissions",
                "ok": self._permissions_ok,
                "message": self._permissions_message,
            },
        ]
        if self._sender_problem or self._sender_caveat:
            sender_message = self._sender_problem or self._sender_caveat
        else:
            status = str(number.get("status") or "")
            details = [
                d
                for d in (
                    name,
                    status and f"{status.lower()}",
                    number.get("quality_rating") and f"quality {number.get('quality_rating')}",
                    (lambda m: m and f"limit {m}")(
                        (number.get("whatsapp_business_manager_messaging_limit") or {}).get(
                            "current_limit"
                        )
                        if isinstance(number.get("whatsapp_business_manager_messaging_limit"), dict)
                        else number.get("whatsapp_business_manager_messaging_limit")
                    ),
                )
                if d
            ]
            sender_message = (
                f"Sending as {self._sender_address or 'phone number ID ' + self._phone_number_id}"
                + (f" ({', '.join(str(d) for d in details)})" if details else "")
                + f", WhatsApp Business Account {self._waba_id}."
            )
        checks.append(
            {
                "id": "sender",
                "ok": not (self._sender_problem or self._sender_caveat),
                "message": sender_message,
            }
        )
        inbound_message = self._inbound_message
        if self._last_delivery_at:
            inbound_message += f" Last delivery from Meta: {_iso(self._last_delivery_at)}."
        if self._inbound_state not in ("ready", "pending") and self._webhook_url:
            inbound_message += f" Verify token: {self.verify_token}."
        checks.append(
            {
                "id": "inbound",
                "ok": None if self._inbound_state == "pending" else self._inbound_state == "ready",
                "message": inbound_message,
            }
        )
        open_now = self.open_windows()
        checks.append(
            {
                "id": "window",
                "ok": True,
                "message": (
                    f"{open_now} {'person has' if open_now == 1 else 'people have'} written in "
                    "the last 24 hours, so an ordinary message reaches them. Everyone else gets "
                    "only an approved template: the agent lists and sends one with its WhatsApp "
                    "template tools, and an ordinary message to them is refused with the reason."
                ),
            }
        )
        fatal = self.fatal_error_message
        if fatal:
            checks.append({"id": "fatal_error", "ok": False, "message": str(fatal)})
        return {
            "adapter": f"platform:{self.platform.value}",
            "live": connected,
            "bot_user": self._sender_address or None,
            "checks": checks,
        }


def billing_summary(record: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The billing half of a status record, in words, or ``None``."""
    billing = record.get("billing")
    if not isinstance(billing, dict):
        return None
    kind = str(billing.get("type") or "")
    return {
        "category": billing.get("category"),
        "type": kind,
        "billable": billing.get("billable"),
        "means": PRICING_TYPE_HELP.get(kind, ""),
    }
