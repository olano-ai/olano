"""WhatsApp via Twilio Conversations: official Business Platform, no bridge.

The sibling of ``whatsapp.py`` (bundled Baileys bridge, unofficial, QR pairing)
and of ``sms.py`` (Twilio Programmable Messaging, text only). This one speaks
the **Conversations** API against a registered WhatsApp sender, so there is no
browser session to pair, nothing to keep logged in, and no account-ban risk.

## Why Conversations and not Programmable Messaging

Programmable Messaging (what ``sms.py`` uses) accepts media **only** as
``MediaUrl``: Twilio fetches the bytes from a public HTTPS URL and validates
the ``Content-Type`` header before accepting them. An agent produces local
files, so that route needs a public, signed, expiring media endpoint on the
box, plus whatever reverse proxy reaches it. Conversations accepts a
``MediaSid`` instead, so the runtime **uploads** bytes and attaches them. The
whole public-hosting problem disappears, which is the only reason this adapter
is not a thirty-line subclass of ``SmsAdapter``.

The flow is two calls and they are deliberately adjacent in
:meth:`WhatsAppTwilioAdapter._upload_and_attach`::

    POST https://mcs.us1.twilio.com/v1/Services/<chat_service_sid>/Media   (bytes)
      -> {"sid": "ME…"}
    POST https://conversations.twilio.com/v1/Conversations/<CH…>/Messages
      MediaSid=ME…

**Uploaded media is garbage-collected after five minutes if nothing attaches
it.** That is why the upload is never a separate public step that a caller
could interleave work with: one method owns both halves, and a failure between
them leaves nothing to clean up.

## Inbound: nobody pastes a webhook URL

Twilio can only *push* a customer's message; there is no long-poll to fall back
on the way Telegram has one. The push lands on the runtime's own public origin,
``/api/twilio/whatsapp/<agent>`` (``runtime/twilio_webhook_routes.py``), which
every deployment already routes, and the adapter tells Twilio about it itself.
An operator types credentials and a number; nothing else.

**An owned number** gets an **Address Configuration** with autocreation: the
first message from any customer makes Twilio create a conversation and attach a
conversation-scoped webhook pointing here (:meth:`_configure_address`). Twilio
documents that such an address takes over from the number's own "a message
comes in" handler, so no Console setting has to be cleared.

**The shared sandbox number** cannot have one (an Address Configuration must
name a number the account owns, and Twilio documents that the sandbox does not
support autocreation). So the adapter opens a conversation for each number in
``allowed_users`` ahead of time, the way Twilio's own sandbox tutorial does by
hand, and puts the webhook on it (:meth:`_wire_allowed_numbers`). A tester who
has not sent ``join <code>`` yet is retried in the background until they have.

**Every conversation the adapter opens or adopts** carries the webhook
(:meth:`_ensure_conversation_webhook`): a number pair can be bound to only one
active conversation, so a conversation without it would swallow that
customer's messages forever. The webhook goes on **before** the participant.

All of this runs in the background after connect and retries with backoff, so
a slow Twilio or an unknown public address never blocks the boot; what it
achieved is on the connector's diagnostics (:meth:`diagnostic_snapshot`).

## Three WhatsApp rules this adapter cannot change

**The 24-hour window.** A business may send free-form messages only within 24
hours of the customer's last inbound message. Outside it, only a Meta-approved
template (``ContentSid``) is delivered. This is Meta policy and applies
identically to the Cloud API; it is not a Twilio limitation to route around.

**Outside the window, a non-template send is swallowed silently.** Twilio's own
wording: "Without approved WhatsApp templates, these outbound messages will be
swallowed by the system." No error, no failed status, nothing delivered. An
adapter that returned ``success=True`` there would reproduce the exact defect
shape the Baileys voice-note bug had, where the runtime logged a successful
turn against a message that carried nothing. So every send checks
:meth:`WhatsAppTwilioAdapter._window_is_open` first and refuses with a reason
that points the agent at an approved template (:meth:`send_template`, which the
``whatsapp_templates`` tools drive). Never relax this into an optimistic send,
and never into wrapping the text in a catch-all template: which template fits,
and what goes in each placeholder, is the agent's decision.

**Quoted replies do not exist outbound.** The Conversations message-create
parameters are ``Author, Body, DateCreated, DateUpdated, Attributes, MediaSid,
ContentSid, ContentVariables, Subject`` and none of them threads a reply.
Inbound reply context is readable and is surfaced on the event, so the agent
can *see* what a customer replied to; ``reply_to`` on the way out is accepted
and ignored rather than faked with a quoted-text prefix.

## Identity

A customer is addressed ``whatsapp:+E164`` and that address is the adapter's
``chat_id``: stable, human-meaningful, and what an allow-list or a
notification target names. The Conversation SID behind it is an implementation
detail, resolved on demand and cached. Inbound webhooks carry the SID already;
a proactive send resolves it through ``GET /v1/ParticipantConversations`` and
creates the conversation when there is none.

Only messages whose ``Author`` is a ``whatsapp:`` address are processed. A
message this runtime posted is authored ``system`` by default, so that one rule
is the echo guard, with no need to compare against our own number. (Twilio
also sends no webhook for a message created through the REST API unless asked
to, which is a second belt, not the one relied on.)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    import aiohttp

from olano.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from olano.platforms.config import Platform, PlatformConfig
from olano.platforms.helpers import redact_phone
from olano.platforms.twilio_content import (
    MAX_CALL_TO_ACTION_BODY,
    MAX_TEMPLATE_BODY,
    STATUS_HELP,
    Template,
    TwilioContentClient,
    clean_variable,
    describe_error_code,
    is_whatsapp_provided_number,
    render,
    twilio_error,
    whatsapp_provided_number_problem,
)
from olano.platforms.twilio_signature import validate_signature

logger = logging.getLogger(__name__)

CONVERSATIONS_API_BASE = "https://conversations.twilio.com/v1"
# The Media Content Service lives on its own regional subdomain and is not
# served by the Twilio SDKs or CLI. Raw HTTP is the only client, which costs us
# nothing: every other call in this adapter is raw HTTP too.
MCS_API_TEMPLATE = "https://mcs.{region}.twilio.com/v1/Services/{service_sid}/Media"
DEFAULT_MCS_REGION = "us1"

#: Where Twilio delivers inbound messages, on the runtime's own public origin.
#: ``runtime/twilio_webhook_routes.WHATSAPP_ROUTE`` serves the same path, and a
#: test pins the two together.
WEBHOOK_PATH_TEMPLATE = "/api/twilio/whatsapp/{agent}"

#: The one Conversations event that is a turn.
WEBHOOK_FILTER = "onMessageAdded"

#: Twilio's shared WhatsApp sandbox sender. Every account's sandbox uses this
#: one number, which is why an account cannot hold an Address Configuration for
#: it and why inbound has to be wired per tester instead.
SANDBOX_ADDRESSES = frozenset({"whatsapp:+14155238886"})
SANDBOX_DISPLAY_NUMBER = "+1 415 523 8886"

# Background inbound setup: first attempt at connect, then retries with backoff
# (a sandbox tester who joins after the connector starts is picked up here).
ROUTING_RETRY_INITIAL_SECONDS = 60.0
ROUTING_RETRY_MAX_SECONDS = 15 * 60.0

# The credential probe at connect is bounded: a slow Twilio must not hold up
# every other connector's boot, since platforms connect one after another.
CREDENTIAL_CHECK_TIMEOUT_SECONDS = 10.0

# Conversations caps a message body at 1600 characters.
MAX_BODY_LENGTH = 1600

# WhatsApp media caps, per Twilio's guidance. Images are the tight one; the
# 16 MB ceiling covers audio, video and documents, under WhatsApp's own 20 MB
# hard limit so a rejection surfaces here rather than as a Twilio 400.
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_MEDIA_BYTES = 16 * 1024 * 1024

# The free-form messaging window. Meta's rule, not ours.
SESSION_WINDOW_SECONDS = 24 * 60 * 60

_IMAGE_PREFIX = "image/"
_AUDIO_PREFIX = "audio/"
_VIDEO_PREFIX = "video/"

_NO_PUBLIC_ORIGIN = (
    "this deployment has no public web address yet. Managed deployments have "
    "one automatically; on a self-hosted install, set LIONCLAW_PUBLIC_URL to its "
    "public https address and restart, or open the dashboard once through that "
    "address"
)


def check_whatsapp_twilio_requirements() -> bool:
    """True when the adapter's dependencies and credentials are present."""
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return False
    return bool(os.getenv("TWILIO_ACCOUNT_SID") and os.getenv("TWILIO_AUTH_TOKEN"))


def normalize_whatsapp_address(value: Any) -> str:
    """Return *value* as a ``whatsapp:+E164`` address.

    Accepts a bare number, a ``+``-prefixed number, or an address that already
    carries the prefix, so an operator typing a phone number into the dashboard
    and an inbound webhook's ``Author`` normalize to the same string. Without
    this the same customer would key two conversations.

    **Anything that is not a phone number returns ``""``**, and that is
    load-bearing rather than mere tidiness: the inbound echo guard is "the
    author is a ``whatsapp:`` address", so a lenient normalizer that turned the
    literal author ``system`` (what Twilio calls a message this runtime posted)
    into ``whatsapp:+system`` would let the agent answer its own replies in a
    loop. Digits only, optional leading ``+``.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith("whatsapp:"):
        raw = raw.split(":", 1)[1].strip()
    for junk in (" ", "-", "(", ")", "."):
        raw = raw.replace(junk, "")
    number = raw[1:] if raw.startswith("+") else raw
    if not number or not number.isdigit():
        return ""
    return f"whatsapp:+{number}"


def media_kind_for(content_type: str) -> tuple[MessageType, str]:
    """Map a MIME type to an Olano ``MessageType`` and a media kind label."""
    mime = (content_type or "").lower()
    if mime.startswith(_IMAGE_PREFIX):
        return MessageType.PHOTO, "image"
    if mime.startswith(_AUDIO_PREFIX):
        # WhatsApp voice notes arrive as audio/ogg; a forwarded music file is
        # also audio/*. Both transcribe, so both are VOICE for the runtime.
        return MessageType.VOICE, "voice"
    if mime.startswith(_VIDEO_PREFIX):
        return MessageType.VIDEO, "video"
    return MessageType.DOCUMENT, "document"


def public_webhook_base(origin: Optional[str]) -> tuple[str, str]:
    """Return ``(origin, "")`` when Twilio could reach *origin*, else ``("", why)``.

    Registering an address Twilio cannot reach is worse than registering
    nothing: the account would point at it and every message would vanish
    without a trace on either side. A loopback, private or single-label host
    (``localhost``, ``127.0.0.1``, ``10.x``, ``my-box``) is the usual shape of
    an origin learned from a dashboard opened on the box itself.
    """
    import ipaddress
    import urllib.parse

    raw = (origin or "").strip().rstrip("/")
    if not raw:
        return "", _NO_PUBLIC_ORIGIN
    parsed = urllib.parse.urlparse(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        return "", f"the public address {raw!r} is not an http(s) URL"
    unreachable = f"the public address {raw!r} is not reachable from the internet"
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return "", unreachable
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            return "", unreachable
    else:
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_unspecified
            or ip.is_reserved
            or ip.is_multicast
        ):
            return "", unreachable
    return raw, ""


#: One copy of "Twilio's own error sentence", shared with the template client.
_twilio_error = twilio_error


class WhatsAppTwilioAdapter(BasePlatformAdapter):
    """WhatsApp <-> Olano over the Twilio Conversations API."""

    MAX_MESSAGE_LENGTH = MAX_BODY_LENGTH

    # Live progress is deliberately off. Every push on this channel is a billed
    # WhatsApp message to a real customer, and Conversations has no edit that
    # renders as an update in the WhatsApp client, so a progress view would be
    # a stream of separate chat bubbles the customer pays attention to and the
    # operator pays for. The Baileys adapter can afford it; this one cannot.
    PROGRESS_MODE: str = "off"

    def __init__(self, config: PlatformConfig, agent_name: str = ""):
        super().__init__(config, Platform.WHATSAPP_TWILIO, agent_name=agent_name)

        extra = config.extra or {}
        # The auth token is vaulted (``config/channel_secrets``) and injected
        # back into the connector config at boot, so the config field wins and
        # the environment is the fallback for an operator who set it globally
        # once and shares it with the SMS connector.
        self._account_sid: str = str(
            extra.get("account_sid") or os.environ.get("TWILIO_ACCOUNT_SID", "")
        ).strip()
        self._auth_token: str = str(
            extra.get("auth_token") or os.environ.get("TWILIO_AUTH_TOKEN", "")
        ).strip()
        self._from_address: str = normalize_whatsapp_address(
            extra.get("from_number") or os.getenv("TWILIO_WHATSAPP_NUMBER", "")
        )
        # Optional: pin a Conversations Service so each agent owns its own
        # namespace. Unset means Twilio's default service, which is correct for
        # a single-agent box and is what the sandbox hands you.
        self._service_sid: str = str(
            extra.get("service_sid") or os.getenv("TWILIO_CONVERSATIONS_SERVICE_SID", "")
        ).strip()
        self._mcs_region: str = str(
            extra.get("mcs_region") or os.getenv("TWILIO_MCS_REGION", DEFAULT_MCS_REGION)
        ).strip()
        # The first version wrapped an ordinary message sent outside the 24-hour
        # window in one configured template. The agent now picks the template
        # itself, so a leftover setting is ignored, and said so once.
        if extra.get("template_sid") or os.getenv("TWILIO_WHATSAPP_TEMPLATE_SID"):
            logger.warning(
                "[whatsapp-twilio] template_sid is no longer used and is ignored: outside "
                "WhatsApp's 24-hour window the agent sends an approved template with its "
                "WhatsApp template tools"
            )

        self._allowed_users: set[str] = self._compile_allowed(
            extra.get("allowed_users", os.getenv("WHATSAPP_TWILIO_ALLOWED_USERS", ""))
        )
        # Owners (extra.owner_users): trusted by the runtime (risky actions
        # approved, or asked in chat with chat_approvals), never a gate. They
        # are admitted even when allowed_users omits them, so naming an owner
        # can never lock that owner out.
        self._owner_users: set[str] = self._compile_allowed(extra.get("owner_users", ""))
        self._allow_all: bool = str(
            extra.get("allow_all_users", os.getenv("WHATSAPP_TWILIO_ALLOW_ALL_USERS", "true"))
        ).strip().lower() in ("1", "true", "yes")

        self._http_session: Optional[aiohttp.ClientSession] = None
        # chat_id (whatsapp:+E164) -> ConversationSid
        self._conversations: dict[str, str] = {}
        # ConversationSid -> chat_service_sid: builds MCS URLs, and addresses a
        # conversation through its own service whatever the pinned one is.
        self._chat_services: dict[str, str] = {}
        # chat_id -> epoch of the last inbound message. This is what the
        # 24-hour window is measured against.
        self._last_inbound: dict[str, float] = {}
        self._seen_messages: set[str] = set()

        # Inbound routing. `_webhook_url` is the URL registered with Twilio (and
        # what a signature is checked against); earlier ones this process
        # registered stay accepted so a conversation still carrying one is not
        # refused while it is being moved over.
        self._webhook_url: str = ""
        self._previous_webhook_urls: list[str] = []
        self._default_service_sid: str = ""
        self._credentials_verified: Optional[bool] = None
        # Conversation SIDs known to carry our webhook (this URL), and the
        # locks that keep two callers from opening or wiring one twice.
        self._webhooked: set[str] = set()
        self._resolve_locks: dict[str, asyncio.Lock] = {}
        self._webhook_locks: dict[str, asyncio.Lock] = {}
        # chat_id -> why Twilio would not open a conversation for it.
        self._wire_errors: dict[str, str] = {}
        self._routing_task: Optional[asyncio.Task] = None
        self._inbound_state: str = "pending"
        self._inbound_message: str = "Setting up incoming messages with Twilio."
        # Why nothing can be sent from the configured number, when Twilio's
        # own list of WhatsApp senders says so (not registered on this account,
        # or not online); empty when the number is fine or the check could not
        # be made. Set by ``_verify_sender`` at connect, read by every send.
        self._sender_problem: str = ""
        self._sender_info: dict[str, Any] = {}
        # Why the number, though a sender, is one Twilio does not support (a
        # free 555 number from WhatsApp). Reported, never enforced: see
        # ``_verify_sender``.
        self._sender_caveat: str = ""

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compile_allowed(raw: Any) -> set[str]:
        """Normalize an allow-list from a list or a comma/newline string."""
        if not raw:
            return set()
        if isinstance(raw, str):
            parts = [p for chunk in raw.split("\n") for p in chunk.split(",")]
        else:
            parts = [str(p) for p in raw]
        return {addr for p in parts if (addr := normalize_whatsapp_address(p))}

    def _is_allowed(self, address: str) -> bool:
        if self._allowed_users:
            # An explicit list always wins: configuring one and leaving
            # allow_all at its default must not silently admit everyone.
            return address in self._allowed_users or address in self._owner_users
        return self._allow_all

    def _auth_headers(self) -> dict[str, str]:
        """HTTP Basic auth for the REST API and for the media service.

        Built by hand rather than with ``aiohttp.BasicAuth`` (deprecated in
        aiohttp 4) and shared by both hosts: ``conversations.twilio.com`` and
        the ``mcs.*`` media endpoint take the same account credentials.
        """
        import base64

        creds = f"{self._account_sid}:{self._auth_token}"
        encoded = base64.b64encode(creds.encode("ascii")).decode("ascii")
        return {"Authorization": f"Basic {encoded}"}

    def _conversations_base(self) -> str:
        """Service-scoped base URL when a service is pinned, else the default."""
        if self._service_sid:
            return f"{CONVERSATIONS_API_BASE}/Services/{self._service_sid}"
        return CONVERSATIONS_API_BASE

    def _conversation_url(self, conversation_sid: str) -> str:
        """The URL of one conversation, through the service it lives in.

        A conversation is addressed through its own service whenever that is
        known (every inbound webhook names it), so a reply reaches it even
        when it was created somewhere other than the pinned or default
        service, for instance by an address configuration someone pointed at
        another service in the Twilio Console.
        """
        service = self._chat_services.get(conversation_sid)
        if service:
            return f"{CONVERSATIONS_API_BASE}/Services/{service}/Conversations/{conversation_sid}"
        return f"{self._conversations_base()}/Conversations/{conversation_sid}"

    def _session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            raise RuntimeError(f"{self.name}: HTTP session is not open (adapter not connected)")
        return self._http_session

    @property
    def is_sandbox(self) -> bool:
        """True when sending from Twilio's shared WhatsApp sandbox number."""
        return self._from_address in SANDBOX_ADDRESSES

    @property
    def sender_address(self) -> str:
        """The ``whatsapp:+E164`` address this connector sends from."""
        return self._from_address

    def webhook_path(self) -> str:
        """The path Twilio posts this agent's inbound messages to."""
        from urllib.parse import quote

        return WEBHOOK_PATH_TEMPLATE.format(agent=quote(self.agent_name or "", safe=""))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        import aiohttp

        if not self._account_sid or not self._auth_token:
            message = (
                "Twilio Account SID and Auth Token are not set. Copy both from the "
                "Account Info panel on the Twilio Console home page into this connector."
            )
            logger.error("[whatsapp-twilio] %s", message)
            self._set_fatal_error("whatsapp_twilio_missing_credentials", message, retryable=False)
            return False
        if not self._from_address:
            message = (
                "No WhatsApp sender number is configured. Enter the WhatsApp number "
                f"this agent sends from ({SANDBOX_DISPLAY_NUMBER} for the Twilio sandbox)."
            )
            logger.error("[whatsapp-twilio] %s", message)
            self._set_fatal_error("whatsapp_twilio_missing_sender", message, retryable=False)
            return False

        # A second connect on the same adapter must not leak the first
        # session or run two setup loops against one Twilio account.
        await self._stop_routing()
        if self._http_session is not None and not self._http_session.closed:
            await self._http_session.close()
        self._http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60),
        )
        verdict, detail = await self._verify_credentials()
        self._credentials_verified = verdict
        if verdict is False:
            message = (
                f"Twilio refused these credentials ({detail}). Check the Account SID "
                "(it starts with AC) and the Auth Token on the Twilio Console home page. "
                "If the token was rotated, paste the new one."
            )
            logger.error("[whatsapp-twilio] %s", message)
            await self._http_session.close()
            self._http_session = None
            self._set_fatal_error("whatsapp_twilio_auth_refused", message, retryable=False)
            return False

        await self._verify_sender()
        self._mark_connected()
        # Inbound setup talks to Twilio several times and may have to wait for
        # a tester to join the sandbox, so it never runs on the boot path.
        self._routing_task = asyncio.create_task(
            self._inbound_routing_loop(), name=f"whatsapp-twilio-routing-{self.agent_name}"
        )
        logger.info(
            "[whatsapp-twilio] connected: sender %s%s%s",
            redact_phone(self._from_address),
            " (Twilio sandbox)" if self.is_sandbox else "",
            f", service {self._service_sid}" if self._service_sid else "",
        )
        return True

    async def _verify_sender(self) -> None:
        """Check the configured number against the account's WhatsApp senders.

        A number that is not a WhatsApp sender on this account (a typo, a
        number registered on another account) fails in the worst way: Twilio
        accepts every message and WhatsApp refuses each one later. The Senders
        API knows, so the connector asks once at connect and every send then
        refuses with the reason instead. The shared sandbox is not a sender
        and is skipped. An answer that cannot be had (a transport error, a
        refused list) leaves the number trusted: a check that fails open
        costs a wrong send, one that fails closed costs every send.
        ``LIONCLAW_TWILIO_SENDER_CHECK=0`` disables it.

        **A free 555 number from WhatsApp passes that check and still fails**:
        it is a real sender, ONLINE on the account's WhatsApp Business
        Account, and Twilio does not support it (inbound setup is refused as
        "Invalid Address", templates fail with 63027). That was the reported
        setup, and this row used to read *OK* for it. It is recognized from
        the number alone, so it needs no answer from Twilio, and it is a
        caveat on the diagnostics and in every explanation rather than a
        refusal: Twilio's guide says "at this time", and a block written into
        the product would outlive the day Twilio starts supporting them.
        """
        self._sender_problem = ""
        self._sender_info = {}
        self._sender_caveat = ""
        if self.is_sandbox:
            return
        if is_whatsapp_provided_number(self._from_address):
            self._sender_caveat = whatsapp_provided_number_problem(self._from_address)
            logger.warning("[whatsapp-twilio] %s", self._sender_caveat)
        if os.getenv("LIONCLAW_TWILIO_SENDER_CHECK", "1").strip().lower() in ("0", "false", "no"):
            return
        try:
            senders = await self.content_client().list_senders()
        except Exception as exc:
            logger.debug("[whatsapp-twilio] sender check skipped: %s", exc)
            return
        console = "the Twilio Console (Messaging, Senders, WhatsApp senders)"
        mine = next((s for s in senders if s.get("number") == self._from_address), None)
        if mine is None:
            numbers = [str(s.get("number") or "") for s in senders if s.get("number")]
            self._sender_problem = (
                f"{self._from_address} is not a WhatsApp sender on this Twilio account, so "
                "WhatsApp delivers nothing from it and Twilio cannot route incoming messages "
                "to it. "
                + (
                    f"The account's WhatsApp senders are {', '.join(numbers)}: enter one of "
                    "them as the connector's WhatsApp sender number"
                    if numbers
                    else "No WhatsApp number is registered on the account yet: register one "
                    f"in {console}, or use {SANDBOX_DISPLAY_NUMBER}, the Twilio sandbox, to test"
                )
                + "."
            )
            logger.error("[whatsapp-twilio] %s", self._sender_problem)
            return
        status = str(mine.get("status") or "")
        self._sender_info = {"status": status, "waba_id": str(mine.get("waba_id") or "")}
        if status != "ONLINE":
            reasons = "; ".join(str(r) for r in mine.get("offline_reasons") or [])
            self._sender_problem = (
                f"{self._from_address} is {status}, not online "
                f"({mine.get('status_means') or 'not sending'})"
                + (f": {reasons}" if reasons else "")
                + f". WhatsApp delivers nothing from it until it is online; finish its setup in "
                f"{console}."
            )
            logger.error("[whatsapp-twilio] %s", self._sender_problem)

    @property
    def sender_problem(self) -> str:
        """Why nothing can be sent from the configured number, or ``""``."""
        return self._sender_problem

    @property
    def sender_caveat(self) -> str:
        """Why the number, though a sender, is one Twilio does not support, or ``""``."""
        return self._sender_caveat

    async def disconnect(self) -> None:
        await self._stop_routing()
        if self._http_session:
            await self._http_session.close()
            self._http_session = None
        self._mark_disconnected()
        logger.info("[whatsapp-twilio] Disconnected")

    async def _stop_routing(self) -> None:
        task, self._routing_task = self._routing_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _verify_credentials(self) -> tuple[Optional[bool], str]:
        """Ask Twilio whether the credentials work: ``(True|False|None, detail)``.

        ``None`` means "could not tell" (network trouble, a Twilio 5xx) and
        does not stop the connector: a transient outage at boot must not turn
        into a dead channel until the next restart. Only a definite refusal
        does. The answer also names the account's default conversation
        service, which is where unpinned conversations live.
        """
        import aiohttp

        try:
            async with self._session().get(
                f"{CONVERSATIONS_API_BASE}/Configuration",
                headers=self._auth_headers(),
                timeout=aiohttp.ClientTimeout(total=CREDENTIAL_CHECK_TIMEOUT_SECONDS),
            ) as resp:
                body = await resp.text()
                if resp.status in (401, 403):
                    return False, _twilio_error(resp.status, body)
                if resp.status >= 400:
                    logger.warning(
                        "[whatsapp-twilio] credential check inconclusive: %s",
                        _twilio_error(resp.status, body),
                    )
                    return None, _twilio_error(resp.status, body)
        except Exception as exc:
            logger.warning("[whatsapp-twilio] credential check could not reach Twilio: %s", exc)
            return None, str(exc) or type(exc).__name__
        try:
            payload = json.loads(body)
        except Exception:
            payload = {}
        if isinstance(payload, dict) and payload.get("default_chat_service_sid"):
            self._default_service_sid = str(payload["default_chat_service_sid"])
        return True, ""

    # ------------------------------------------------------------------
    # Small REST helpers
    # ------------------------------------------------------------------

    async def _post_form(self, url: str, fields: dict[str, str]) -> tuple[bool, str]:
        """POST form fields; ``(ok, Twilio's error sentence when not ok)``."""
        try:
            async with self._session().post(url, data=fields, headers=self._auth_headers()) as resp:
                body = await resp.text()
                if resp.status < 400:
                    return True, ""
                return False, _twilio_error(resp.status, body)
        except Exception as exc:
            return False, str(exc) or type(exc).__name__

    async def _delete(self, url: str) -> bool:
        try:
            async with self._session().delete(url, headers=self._auth_headers()) as resp:
                if resp.status < 400 or resp.status == 404:
                    return True
                logger.warning(
                    "[whatsapp-twilio] delete %s failed: %s",
                    url,
                    _twilio_error(resp.status, await resp.text()),
                )
        except Exception as exc:
            logger.warning("[whatsapp-twilio] delete %s error: %s", url, exc)
        return False

    # ------------------------------------------------------------------
    # Inbound routing: Twilio is told where to deliver, by us
    # ------------------------------------------------------------------

    def _current_webhook_url(self) -> tuple[str, str]:
        """``(url, "")`` for this agent's public webhook, or ``("", why not)``."""
        from olano.core.mcp_setup import public_origin

        if not self.agent_name:
            return "", "this connector is not attached to a named agent"
        base, problem = public_webhook_base(public_origin())
        if not base:
            return "", problem
        return f"{base}{self.webhook_path()}", ""

    def _adopt_webhook_url(self, url: str) -> None:
        if url == self._webhook_url:
            return
        if self._webhook_url:
            self._previous_webhook_urls = [
                u for u in [self._webhook_url, *self._previous_webhook_urls] if u != url
            ][:4]
        self._webhook_url = url
        # Conversations checked against the old URL must be checked again.
        self._webhooked.clear()

    def _set_inbound(self, state: str, message: str) -> None:
        """Record the inbound setup result for diagnostics; log changes once."""
        changed = (state, message) != (self._inbound_state, self._inbound_message)
        self._inbound_state = state
        self._inbound_message = message
        if not changed:
            return
        if state == "ready":
            logger.info("[whatsapp-twilio] %s", message)
        else:
            logger.warning("[whatsapp-twilio] %s", message)

    async def _inbound_routing_loop(self) -> None:
        """Run :meth:`_ensure_inbound_routing` until it has nothing left to do."""
        delay = ROUTING_RETRY_INITIAL_SECONDS
        while self._running:
            try:
                done = await self._ensure_inbound_routing()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("[whatsapp-twilio] inbound setup failed")
                self._set_inbound(
                    "error",
                    f"Setting up incoming messages failed ({exc}). Retrying automatically.",
                )
                done = False
            if done:
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, ROUTING_RETRY_MAX_SECONDS)

    async def _ensure_inbound_routing(self) -> bool:
        """Point Twilio at this agent's webhook. True when nothing is left to retry."""
        url, problem = self._current_webhook_url()
        if not url:
            self._set_inbound(
                "error",
                f"Incoming messages cannot be set up: {problem}. Sending still works.",
            )
            return False
        self._adopt_webhook_url(url)
        # A conversation this process opened before the address was known (a
        # template send at boot) is wired now rather than never.
        for sid in list(dict.fromkeys(self._conversations.values())):
            await self._ensure_conversation_webhook(sid)
        if self.is_sandbox:
            return await self._wire_allowed_numbers(sandbox=True)
        return await self._configure_address()

    def _address_fields(self) -> dict[str, str]:
        fields = {
            "FriendlyName": f"Olano agent {self.agent_name}"[:256],
            "AutoCreation.Enabled": "true",
            "AutoCreation.Type": "webhook",
            "AutoCreation.WebhookUrl": self._webhook_url,
            "AutoCreation.WebhookMethod": "post",
            "AutoCreation.WebhookFilters": WEBHOOK_FILTER,
        }
        # Name the service explicitly, even the default one: an address
        # configuration someone edited in the Console may point elsewhere, and
        # an update only changes the fields it sends.
        service = self._service_sid or self._default_service_sid
        if service:
            fields["AutoCreation.ConversationServiceSid"] = service
        return fields

    def _address_matches(self, row: dict[str, Any]) -> bool:
        auto = row.get("auto_creation")
        if not isinstance(auto, dict):
            return False
        filters = auto.get("webhook_filters") or []
        service = self._service_sid or self._default_service_sid
        return (
            bool(auto.get("enabled"))
            and str(auto.get("type") or "").lower() == "webhook"
            and str(auto.get("webhook_url") or "") == self._webhook_url
            and str(auto.get("webhook_method") or "").lower() == "post"
            and isinstance(filters, list)
            and WEBHOOK_FILTER in filters
            and (not service or str(auto.get("conversation_service_sid") or "") == service)
        )

    async def _find_address_configuration(self) -> tuple[bool, Optional[dict[str, Any]], str]:
        """``(ok, row or None, error)`` for our sender's Address Configuration."""
        url: Optional[str] = f"{CONVERSATIONS_API_BASE}/Configuration/Addresses"
        params: Optional[dict[str, str]] = {"Type": "whatsapp", "PageSize": "50"}
        pages = 0
        while url and pages < 20:
            pages += 1
            try:
                async with self._session().get(
                    url, params=params, headers=self._auth_headers()
                ) as resp:
                    if resp.status >= 400:
                        return False, None, _twilio_error(resp.status, await resp.text())
                    payload = await resp.json()
            except Exception as exc:
                return False, None, str(exc) or type(exc).__name__
            if not isinstance(payload, dict):
                return True, None, ""
            for row in payload.get("address_configurations") or []:
                if not isinstance(row, dict):
                    continue
                if normalize_whatsapp_address(row.get("address")) == self._from_address:
                    return True, row, ""
            meta = payload.get("meta") or {}
            next_url = meta.get("next_page_url") if isinstance(meta, dict) else None
            url, params = (str(next_url), None) if next_url else (None, None)
        return True, None, ""

    async def _configure_address(self) -> bool:
        """Turn on autocreation with our webhook for an owned WhatsApp number."""
        found, row, error = await self._find_address_configuration()
        if not found:
            self._set_inbound(
                "error",
                f"Could not read the incoming-message settings of {self._from_address} "
                f"from Twilio ({error}). Retrying automatically.",
            )
            return False
        if row is not None and self._address_matches(row):
            self._set_inbound("ready", self._ready_message())
            return True

        fields = self._address_fields()
        previous = ""
        if row is not None and row.get("sid"):
            auto = row.get("auto_creation")
            auto = auto if isinstance(auto, dict) else {}
            previous_url = str(auto.get("webhook_url") or "")
            # Someone else's routing (a Studio flow, another app's webhook) is
            # replaced, because connecting the number to this agent is the
            # request to deliver its messages here; it is logged and shown on
            # the diagnostics so it can be put back.
            if auto.get("enabled") and not previous_url.endswith(self.webhook_path()):
                previous = str(auto.get("type") or "autocreation") + (
                    f" to {previous_url}" if previous_url else ""
                )
            ok, detail = await self._post_form(
                f"{CONVERSATIONS_API_BASE}/Configuration/Addresses/{row['sid']}", fields
            )
        else:
            ok, detail = await self._post_form(
                f"{CONVERSATIONS_API_BASE}/Configuration/Addresses",
                {"Type": "whatsapp", "Address": self._from_address, **fields},
            )

        if not ok:
            refusal = (
                f"Twilio would not deliver incoming messages for {self._from_address} to "
                f"this agent ({detail}). "
                + (
                    self._sender_caveat
                    or "Check that it is an approved WhatsApp sender on this Twilio account."
                )
            )
            if self._allowed_users and await self._wire_allowed_numbers(sandbox=False):
                # Numbers named in the allow-list still work: each gets its own
                # conversation, the sandbox way.
                self._set_inbound(
                    "partial",
                    f"{refusal} Only the numbers in Allowed sender numbers can reach the "
                    "agent until then. Retrying automatically.",
                )
            else:
                self._set_inbound("error", f"{refusal} Retrying automatically.")
            return False

        if previous:
            logger.warning(
                "[whatsapp-twilio] incoming messages for %s were routed by %s; they "
                "now go to agent %r",
                redact_phone(self._from_address),
                previous,
                self.agent_name,
            )
        self._set_inbound("ready", self._ready_message(previous))
        return True

    def _ready_message(self, previous: str = "") -> str:
        message = (
            "Incoming messages: set up automatically. Twilio delivers messages sent to "
            f"{self._from_address} to {self._webhook_url}."
        )
        if previous:
            message += f" This replaced the previous routing ({previous})."
        return message

    async def _wire_allowed_numbers(self, *, sandbox: bool) -> bool:
        """Open a conversation, with our webhook, for every allow-listed number.

        The sandbox path: Twilio routes an inbound message into a conversation
        only when one already binds that sender to this number, and cannot
        create one on its own for the shared sandbox number.
        """
        if not self._allowed_users:
            if sandbox:
                self._set_inbound(
                    "error",
                    "Twilio's WhatsApp sandbox cannot open a conversation with a new number "
                    "on its own, so the agent cannot hear anyone yet. Add your WhatsApp "
                    "number under Allowed sender numbers and restart the agent.",
                )
            # Nothing a retry could change: the list is fixed until a restart.
            return sandbox
        pending: dict[str, str] = {}
        for address in sorted(self._allowed_users):
            sid = await self._resolve_conversation(address, create=True)
            if not sid:
                pending[address] = self._wire_errors.get(address) or "no conversation"
            elif not await self._ensure_conversation_webhook(sid):
                pending[address] = "the delivery webhook could not be attached"
        wired = len(self._allowed_users) - len(pending)
        if not sandbox:
            return not pending
        if not pending:
            self._set_inbound(
                "ready",
                "Incoming messages: set up automatically on the Twilio sandbox for "
                f"{wired} allowed number{'s' if wired != 1 else ''}.",
            )
            return True
        waiting = "; ".join(f"{redact_phone(a)}: {why}" for a, why in sorted(pending.items()))
        self._set_inbound(
            "partial" if wired else "error",
            f"Incoming messages are not set up yet for {len(pending)} of "
            f"{len(self._allowed_users)} allowed numbers ({waiting}). A number has to "
            f"join the sandbox first: send 'join <your sandbox code>' to "
            f"{SANDBOX_DISPLAY_NUMBER} on WhatsApp from that phone. Retrying automatically.",
        )
        return False

    async def _ensure_conversation_webhook(self, conversation_sid: str) -> bool:
        """Make sure *conversation_sid* delivers its messages to this agent.

        Adds our conversation-scoped webhook when it is missing and removes a
        webhook of ours left from an earlier public address (same path,
        different origin), so Twilio stops posting to a dead one. Webhooks
        anybody else configured are never touched.
        """
        if not self._webhook_url or not conversation_sid:
            return False
        if conversation_sid in self._webhooked:
            return True
        lock = self._webhook_locks.setdefault(conversation_sid, asyncio.Lock())
        async with lock:
            if conversation_sid in self._webhooked:
                return True
            return await self._ensure_conversation_webhook_locked(conversation_sid)

    async def _ensure_conversation_webhook_locked(self, conversation_sid: str) -> bool:
        from urllib.parse import urlparse

        url = self._webhook_url
        hooks_url = f"{self._conversation_url(conversation_sid)}/Webhooks"
        try:
            async with self._session().get(
                hooks_url, params={"PageSize": "50"}, headers=self._auth_headers()
            ) as resp:
                if resp.status >= 400:
                    logger.warning(
                        "[whatsapp-twilio] could not list webhooks of %s: %s",
                        conversation_sid,
                        _twilio_error(resp.status, await resp.text()),
                    )
                    return False
                payload = await resp.json()
        except Exception as exc:
            logger.warning(
                "[whatsapp-twilio] could not list webhooks of %s: %s", conversation_sid, exc
            )
            return False

        present = False
        stale: list[str] = []
        path = self.webhook_path()
        rows = payload.get("webhooks") if isinstance(payload, dict) else None
        for row in rows or []:
            if not isinstance(row, dict) or str(row.get("target") or "").lower() != "webhook":
                continue
            config = row.get("configuration")
            hook_url = str(config.get("url") or "") if isinstance(config, dict) else ""
            if hook_url == url:
                present = True
            elif hook_url and urlparse(hook_url).path == path and row.get("sid"):
                stale.append(str(row["sid"]))
        for hook_sid in stale:
            await self._delete(f"{hooks_url}/{hook_sid}")
        if not present:
            ok, detail = await self._post_form(
                hooks_url,
                {
                    "Target": "webhook",
                    "Configuration.Url": url,
                    "Configuration.Method": "post",
                    "Configuration.Filters": WEBHOOK_FILTER,
                },
            )
            if not ok:
                logger.warning(
                    "[whatsapp-twilio] could not attach the delivery webhook to %s: %s",
                    conversation_sid,
                    detail,
                )
                return False
        self._webhooked.add(conversation_sid)
        return True

    # ------------------------------------------------------------------
    # Conversation resolution
    # ------------------------------------------------------------------

    async def _resolve_conversation(self, chat_id: str, *, create: bool = True) -> Optional[str]:
        """Return the Conversation SID for *chat_id*, creating one if asked.

        Inbound traffic never needs this: the webhook carries the SID, and
        :meth:`receive_webhook` caches it. It exists for the proactive path
        (``notification_send_*``, PULSE, a scheduled job) where the only thing
        we hold is a phone number, and for the sandbox wiring.
        """
        cached = self._conversations.get(chat_id)
        if cached:
            return cached
        lock = self._resolve_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            cached = self._conversations.get(chat_id)
            if cached:
                return cached
            return await self._resolve_conversation_locked(chat_id, create=create)

    async def _resolve_conversation_locked(self, chat_id: str, *, create: bool) -> Optional[str]:
        session = self._session()
        # ParticipantConversations indexes conversations by participant address,
        # which is exactly the question being asked here.
        url = f"{self._conversations_base()}/ParticipantConversations"
        adopted = ""
        try:
            async with session.get(
                url, params={"Address": chat_id}, headers=self._auth_headers()
            ) as resp:
                if resp.status < 400:
                    payload = await resp.json()
                    for row in payload.get("conversations", []) or []:
                        sid = row.get("conversation_sid")
                        # A closed conversation accepts no messages; adopting
                        # one pins every later send to a Twilio 400.
                        if str(row.get("conversation_state") or "").lower() == "closed":
                            continue
                        # Only adopt a conversation bound to *our* sender: a
                        # number can talk to several senders on one account.
                        # The binding is documented nullable (a chat-only
                        # participant carries none), and a None here used to
                        # raise out of the loop and create a duplicate.
                        binding = row.get("participant_messaging_binding") or {}
                        proxy = normalize_whatsapp_address(binding.get("proxy_address") or "")
                        if sid and (not proxy or proxy == self._from_address):
                            adopted = str(sid)
                            if row.get("chat_service_sid"):
                                self._chat_services[adopted] = str(row["chat_service_sid"])
                            break
                else:
                    logger.warning(
                        "[whatsapp-twilio] participant lookup failed (%s): %s",
                        resp.status,
                        (await resp.text())[:200],
                    )
        except Exception as exc:
            logger.warning("[whatsapp-twilio] participant lookup error: %s", exc)

        if adopted:
            self._conversations[chat_id] = adopted
            # An adopted conversation may predate this agent (or this public
            # address); without our webhook it would keep the customer's
            # replies from ever arriving. A failure is logged, never fatal:
            # sending through the conversation still works.
            await self._ensure_conversation_webhook(adopted)
            return adopted
        if not create:
            return None
        return await self._create_conversation(chat_id)

    async def _create_conversation(self, chat_id: str) -> Optional[str]:
        """Create a conversation bound to our sender, with our webhook on it.

        **The webhook goes on before the participant.** Once the participant
        exists, the customer's next message lands in this conversation, and a
        conversation without our webhook delivers it to nobody. A participant
        Twilio refuses (a sandbox tester who has not joined yet, a number pair
        already bound elsewhere) deletes the conversation again, so a refusal
        leaves nothing behind on the account.
        """
        session = self._session()
        try:
            async with session.post(
                f"{self._conversations_base()}/Conversations",
                data={"FriendlyName": f"WhatsApp {chat_id}"},
                headers=self._auth_headers(),
            ) as resp:
                if resp.status >= 400:
                    detail = _twilio_error(resp.status, await resp.text())
                    logger.error("[whatsapp-twilio] conversation create failed: %s", detail)
                    self._wire_errors[chat_id] = detail
                    return None
                convo = await resp.json()
            sid = str(convo.get("sid") or "")
            if not sid:
                return None
            if convo.get("chat_service_sid"):
                self._chat_services[sid] = str(convo["chat_service_sid"])

            if self._webhook_url and not await self._ensure_conversation_webhook(sid):
                logger.warning(
                    "[whatsapp-twilio] replies from %s may not reach the agent: the "
                    "delivery webhook could not be attached to %s",
                    redact_phone(chat_id),
                    sid,
                )

            ok, detail = await self._post_form(
                f"{self._conversation_url(sid)}/Participants",
                {
                    "MessagingBinding.Address": chat_id,
                    "MessagingBinding.ProxyAddress": self._from_address,
                },
            )
            if not ok:
                logger.error(
                    "[whatsapp-twilio] participant add failed for %s: %s",
                    redact_phone(chat_id),
                    detail,
                )
                self._wire_errors[chat_id] = detail
                await self._delete_conversation(sid)
                return None
            self._wire_errors.pop(chat_id, None)
            self._conversations[chat_id] = sid
            return sid
        except Exception as exc:
            logger.error("[whatsapp-twilio] conversation create error: %s", exc)
            self._wire_errors[chat_id] = str(exc) or type(exc).__name__
            return None

    async def _delete_conversation(self, conversation_sid: str) -> None:
        await self._delete(self._conversation_url(conversation_sid))
        self._chat_services.pop(conversation_sid, None)
        self._webhooked.discard(conversation_sid)

    async def _chat_service_for(self, conversation_sid: str) -> Optional[str]:
        """Return the Conversation's chat service SID (needed to build MCS URLs)."""
        cached = self._chat_services.get(conversation_sid)
        if cached:
            return cached
        if self._service_sid:
            self._chat_services[conversation_sid] = self._service_sid
            return self._service_sid
        try:
            async with self._session().get(
                f"{CONVERSATIONS_API_BASE}/Conversations/{conversation_sid}",
                headers=self._auth_headers(),
            ) as resp:
                if resp.status >= 400:
                    return None
                convo = await resp.json()
            sid = convo.get("chat_service_sid")
            if not sid:
                return None
            self._chat_services[conversation_sid] = str(sid)
            return str(sid)
        except Exception as exc:
            logger.warning("[whatsapp-twilio] chat service lookup failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # The 24-hour window
    # ------------------------------------------------------------------

    def _window_is_open(self, chat_id: str) -> bool:
        last = self._last_inbound.get(chat_id)
        return bool(last and (time.time() - last) < SESSION_WINDOW_SECONDS)

    async def _recover_window(self, chat_id: str, conversation_sid: str) -> None:
        """Rebuild the window stamp from Twilio when this process has none.

        ``_last_inbound`` is in-memory, so after a restart every number reads
        as "never wrote in" and a reply to a message that arrived minutes
        before the restart, or any scheduled notification, is refused for up
        to 24 hours. The conversation's message log is the durable record:
        the newest message authored by the customer dates the window.

        Runs only on a cache miss (a stamp this process recorded is newer
        than anything Twilio can add: every inbound arrives as a webhook), and
        memoizes a definite "no inbound" as ``0.0`` so a closed window costs
        one lookup per number per process rather than one per send. A
        transport error leaves the entry unset so the next send retries.
        """
        if chat_id in self._last_inbound:
            return
        from datetime import datetime

        url = f"{self._conversation_url(conversation_sid)}/Messages"
        try:
            async with self._session().get(
                url,
                params={"Order": "desc", "PageSize": "50"},
                headers=self._auth_headers(),
            ) as resp:
                if resp.status >= 400:
                    logger.warning(
                        "[whatsapp-twilio] window recovery failed (%s): %s",
                        resp.status,
                        (await resp.text())[:200],
                    )
                    return
                payload = await resp.json()
        except Exception as exc:
            logger.warning("[whatsapp-twilio] window recovery error: %s", exc)
            return

        stamp = 0.0
        for row in (payload.get("messages") if isinstance(payload, dict) else None) or []:
            if not isinstance(row, dict):
                continue
            if normalize_whatsapp_address(row.get("author")) != chat_id:
                continue
            created = str(row.get("date_created") or "")
            try:
                stamp = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
            except ValueError:
                logger.warning("[whatsapp-twilio] unparseable date_created %r", created)
            break
        self._last_inbound[chat_id] = stamp

    def _closed_window_error(self, chat_id: str) -> str:
        return (
            f"WhatsApp's 24-hour messaging window is closed for {redact_phone(chat_id)} "
            "(no inbound message from this number in the last 24 hours). WhatsApp only "
            "delivers an approved template message outside that window, and a free-form "
            "message would be dropped without an error. Send an approved template instead: "
            "whatsapp_template_list shows them and whatsapp_template_send sends one, when "
            "this agent has those tools. Otherwise wait for the customer to write first."
        )

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
        """Send a text message.

        ``reply_to`` is accepted and ignored: Conversations has no parameter
        that threads a reply, and prefixing quoted text would fake a WhatsApp
        affordance the customer's client would not render as a quote.
        """
        address = normalize_whatsapp_address(chat_id)
        if not address:
            return SendResult(success=False, error=f"{self.name}: invalid recipient {chat_id!r}")

        # Look up, never create, before the window is known: a refused send
        # must not leave a conversation plus participant binding behind on the
        # account. No conversation at all means the customer never wrote in.
        conversation_sid = await self._resolve_conversation(address, create=False)
        if conversation_sid:
            await self._recover_window(address, conversation_sid)

        formatted = self.format_message(content)
        chunks = self.truncate_message(formatted, self.MAX_MESSAGE_LENGTH)

        if not self._window_is_open(address):
            return SendResult(success=False, error=self._closed_window_error(address))

        if not conversation_sid:
            # An open window means the customer wrote in, so a conversation
            # exists; the lookup failed rather than found nothing, and creating
            # a second one would collide with it.
            return SendResult(
                success=False,
                error=f"{self.name}: could not open a conversation with {redact_phone(address)}",
            )

        last = SendResult(success=True)
        for chunk in chunks:
            last = await self._post_message(conversation_sid, {"Body": chunk})
            if not last.success:
                return last
        return last

    # ------------------------------------------------------------------
    # Templates (the only thing WhatsApp delivers outside the window)
    # ------------------------------------------------------------------

    def content_client(self) -> TwilioContentClient:
        """A template (Content API) client on this connector's own account."""
        return TwilioContentClient(self._account_sid, self._auth_token)

    async def send_template(
        self,
        chat_id: str,
        template: Template,
        variables: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Send an approved template to *chat_id*, inside or outside the window.

        A template is how a business starts a conversation, so unlike a
        free-form send this opens one (webhook first, then the participant)
        when none exists. Every placeholder the body uses must have a value:
        a missing one would otherwise go out as the template's *sample* text,
        to a real customer. Values are cleaned of line breaks and runs of
        spaces, which WhatsApp refuses in a variable. On success
        ``raw_response`` carries the conversation SID (for
        :meth:`delivery_receipt`) and the rendered text.
        """
        address = normalize_whatsapp_address(chat_id)
        if not address:
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
        values = {str(k): clean_variable(v) for k, v in (variables or {}).items()}
        # The body's placeholders and its buttons' (a website button may end
        # with one): an unfilled one would go out as the template's sample.
        keys = template.placeholders
        buttons_text = "; ".join(
            f"{b.title} ({b.value})" if b.kind in ("url", "phone") else b.title
            for b in template.buttons
        )
        missing = [k for k in keys if not values.get(k)]
        if missing:
            return SendResult(
                success=False,
                error=(
                    f"Template {template.name} needs a value for "
                    + ", ".join("{{" + k + "}}" for k in missing)
                    + f". It reads: {template.body}"
                    + (f" Its buttons: {buttons_text}" if buttons_text else "")
                ),
            )
        body = render(template.body, values)
        limit = (
            MAX_CALL_TO_ACTION_BODY
            if template.content_type == "twilio/call-to-action"
            else MAX_TEMPLATE_BODY
        )
        if len(body) > limit:
            return SendResult(
                success=False,
                error=(
                    f"With these values the message is {len(body)} characters; WhatsApp "
                    f"allows {limit}. Shorten the variable values."
                ),
            )
        rendered = body
        if template.buttons:
            rendered += "\n" + " ".join(f"[{render(b.title, values)}]" for b in template.buttons)
        conversation_sid = await self._resolve_conversation(address, create=False)
        if not conversation_sid:
            conversation_sid = await self._resolve_conversation(address, create=True)
        if not conversation_sid:
            why = self._wire_errors.get(address)
            return SendResult(
                success=False,
                error=(
                    f"{self.name}: could not open a conversation with {redact_phone(address)}"
                    + (f" ({why})" if why else "")
                ),
            )
        fields = {"ContentSid": template.sid}
        if keys:
            fields["ContentVariables"] = json.dumps({k: values[k] for k in keys})
        result = await self._post_message(conversation_sid, fields)
        if result.success:
            result.raw_response = {"conversation_sid": conversation_sid, "rendered": rendered}
        return result

    async def message_status(self, chat_id: str, message_sid: str) -> tuple[str, str]:
        """``(status, detail)`` of a message sent to *chat_id* earlier.

        The conversation is looked up, never created. ``("", reason)`` when
        there is nothing to ask: no conversation with that number, or no
        receipt from WhatsApp yet.
        """
        address = normalize_whatsapp_address(chat_id)
        if not address:
            return "", f"{chat_id!r} is not a WhatsApp number"
        conversation_sid = await self._resolve_conversation(address, create=False)
        if not conversation_sid:
            return "", f"there is no conversation with {redact_phone(address)} on this number"
        status, detail = await self.delivery_receipt(conversation_sid, message_sid)
        return status, detail or ("" if status else "WhatsApp has not reported on it yet")

    async def window_status(self, chat_id: str) -> dict[str, Any]:
        """Whether the 24-hour window is open for *chat_id*, and until when.

        Looks the conversation up, never creates one, and recovers the stamp
        from Twilio's message log after a restart like a send does. ``error``
        when the number is not a WhatsApp address.
        """
        from datetime import datetime, timezone

        address = normalize_whatsapp_address(chat_id)
        if not address:
            return {"to": chat_id, "error": f"{chat_id!r} is not a WhatsApp number"}
        conversation_sid = await self._resolve_conversation(address, create=False)
        if conversation_sid:
            await self._recover_window(address, conversation_sid)
        last = float(self._last_inbound.get(address) or 0.0)

        def _iso(stamp: float) -> str:
            return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat(timespec="seconds")

        out: dict[str, Any] = {
            "to": address.split(":", 1)[-1],
            "window_open": self._window_is_open(address),
            "conversation_exists": bool(conversation_sid),
            "last_message_from_them": _iso(last) if last else None,
        }
        if last:
            out["window_closes_at"] = _iso(last + SESSION_WINDOW_SECONDS)
        return out

    async def delivery_receipt(self, conversation_sid: str, message_sid: str) -> tuple[str, str]:
        """``(status, detail)`` of a sent message as WhatsApp last reported it.

        Twilio accepts a message before WhatsApp has looked at it, so a
        refusal (sandbox number not joined, window closed, a variable
        mismatch) only shows up here. ``("", "")`` means no receipt yet.
        """
        url = f"{self._conversation_url(conversation_sid)}/Messages/{message_sid}/Receipts"
        try:
            async with self._session().get(url, headers=self._auth_headers()) as resp:
                if resp.status >= 400:
                    return "", ""
                payload = await resp.json()
        except Exception as exc:
            logger.debug("[whatsapp-twilio] receipt lookup failed: %s", exc)
            return "", ""
        rows = payload.get("delivery_receipts") if isinstance(payload, dict) else None
        statuses: list[tuple[str, Any]] = [
            (str(r.get("status") or "").lower(), r.get("error_code"))
            for r in rows or []
            if isinstance(r, dict)
        ]
        for status, code in statuses:
            if status in ("failed", "undelivered"):
                return status, describe_error_code(code) if code else ""
        for wanted in ("read", "delivered", "sent"):
            if any(status == wanted for status, _ in statuses):
                return wanted, ""
        return "", ""

    async def _post_message(self, conversation_sid: str, fields: dict[str, str]) -> SendResult:
        """POST one message to a conversation and classify the outcome."""
        if self._sender_problem:
            # Twilio would accept it and WhatsApp refuse it later; say so now.
            return SendResult(success=False, error=self._sender_problem, retryable=False)
        url = f"{self._conversation_url(conversation_sid)}/Messages"
        try:
            async with self._session().post(url, data=fields, headers=self._auth_headers()) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    detail = _twilio_error(resp.status, body)
                    logger.error("[whatsapp-twilio] send failed: %s", detail)
                    return SendResult(
                        success=False,
                        error=detail,
                        # 429 and 5xx are worth another attempt; a 400 is a bad
                        # request that will fail identically on a retry.
                        retryable=resp.status == 429 or resp.status >= 500,
                    )
                try:
                    payload = json.loads(body)
                except Exception:
                    payload = {}
                return SendResult(success=True, message_id=payload.get("sid", ""))
        except Exception as exc:
            logger.error("[whatsapp-twilio] send error: %s", exc)
            return SendResult(success=False, error=str(exc), retryable=True)

    # ------------------------------------------------------------------
    # Media upload (the reason this adapter uses Conversations)
    # ------------------------------------------------------------------

    async def _upload_and_attach(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str],
        kind: str,
    ) -> SendResult:
        """Upload a local file to MCS and attach it to the conversation.

        The two calls stay in one method on purpose: Twilio garbage-collects an
        uploaded Media instance after five minutes if no message references it,
        so the upload must never be reachable as a step on its own.
        """
        address = normalize_whatsapp_address(chat_id)
        if not address:
            return SendResult(success=False, error=f"{self.name}: invalid recipient {chat_id!r}")

        path = Path(file_path)
        if not path.is_file():
            return SendResult(success=False, error=f"{self.name}: no such file: {file_path}")

        import mimetypes

        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        limit = MAX_IMAGE_BYTES if mime.startswith(_IMAGE_PREFIX) else MAX_MEDIA_BYTES
        # Size from the inode, before any read: an oversized file is refused
        # without loading it into memory first.
        size = path.stat().st_size
        if size > limit:
            from olano.uploads import human_size

            return SendResult(
                success=False,
                error=(
                    f"{self.name}: {path.name} is {human_size(size)}, over WhatsApp's "
                    f"{human_size(limit)} limit for {kind} attachments."
                ),
            )

        # Same order as `send`: look up without creating, then decide. A
        # template cannot carry arbitrary media, so unlike `send` there is no
        # fallback outside the window: say so rather than dropping the file.
        conversation_sid = await self._resolve_conversation(address, create=False)
        if conversation_sid:
            await self._recover_window(address, conversation_sid)
        if not self._window_is_open(address):
            return SendResult(success=False, error=self._closed_window_error(address))
        if not conversation_sid:
            return SendResult(
                success=False,
                error=f"{self.name}: could not open a conversation with {redact_phone(address)}",
            )

        data = path.read_bytes()
        service_sid = await self._chat_service_for(conversation_sid)
        if not service_sid:
            return SendResult(
                success=False,
                error=f"{self.name}: could not resolve the Conversations service for upload",
            )

        media_sid = await self._upload_media(service_sid, path.name, mime, data)
        if not media_sid:
            return SendResult(success=False, error=f"{self.name}: media upload failed")

        fields: dict[str, str] = {"MediaSid": media_sid}
        if caption:
            # WhatsApp renders a caption on the same bubble as the media.
            fields["Body"] = self.format_message(caption)[:MAX_BODY_LENGTH]
        return await self._post_message(conversation_sid, fields)

    async def _upload_media(
        self, service_sid: str, filename: str, mime: str, data: bytes
    ) -> Optional[str]:
        """POST raw bytes to the Media Content Service, return the Media SID."""
        from olano.uploads import sanitize_filename

        url = MCS_API_TEMPLATE.format(region=self._mcs_region, service_sid=service_sid)
        # The name goes into a quoted header value: a `"` breaks the quoting
        # and a control character makes aiohttp refuse the request outright,
        # which used to surface as a bare "media upload failed". The sanitizer
        # strips exactly that set and keeps the extension.
        safe_name = sanitize_filename(filename, mime, media_kind_for(mime)[1])
        headers = {
            **self._auth_headers(),
            "Content-Type": mime,
            # MCS reads the filename from the disposition header; without it a
            # document arrives named after its SID.
            "Content-Disposition": f'attachment; filename="{safe_name}"',
        }
        try:
            async with self._session().post(url, data=data, headers=headers) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    logger.error(
                        "[whatsapp-twilio] media upload failed (%s): %s",
                        resp.status,
                        body[:300],
                    )
                    return None
                media_sid = json.loads(body).get("sid")
                return str(media_sid) if media_sid else None
        except Exception as exc:
            logger.error("[whatsapp-twilio] media upload error: %s", exc)
            return None

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        return await self._upload_and_attach(chat_id, image_path, caption, "image")

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        return await self._upload_and_attach(chat_id, video_path, caption, "video")

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        return await self._upload_and_attach(chat_id, file_path, caption, "document")

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        *,
        as_voice: bool = True,
        **kwargs,
    ) -> SendResult:
        """Send audio (TTS output, a recording) as a WhatsApp audio message.

        ``as_voice`` is honoured only as far as the container allows: WhatsApp
        renders an Ogg/Opus attachment as a playable voice bubble and anything
        else (an mp3 from the TTS tool) as a plain audio file. Twilio exposes no
        push-to-talk flag to force the bubble, so the extension decides.
        """
        return await self._upload_and_attach(chat_id, audio_path, caption, "audio")

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Send a remote image by URL.

        Remote URLs are the one case where uploading is not required: the bytes
        already live somewhere Twilio can reach. They are still downloaded and
        re-uploaded so a single code path owns delivery, and so a URL that is
        private, expiring or missing a ``Content-Type`` fails here with a clear
        message instead of being silently rejected by Twilio's fetcher.
        """
        try:
            from olano.platforms.base import cache_image_from_url

            local = await cache_image_from_url(image_url, ext=".jpg")
        except Exception as exc:
            return SendResult(
                success=False,
                error=f"{self.name}: could not fetch image {image_url}: {exc}",
            )
        return await self._upload_and_attach(chat_id, local, caption, "image")

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    def format_message(self, content: str) -> str:
        """Convert Markdown to WhatsApp's own limited markup.

        WhatsApp understands ``*bold*``, ``_italic_``, ``~strike~`` and triple
        backticks, so Markdown's ``**bold**`` must collapse to one asterisk or
        it renders as literal asterisks in the chat. The rules are the Baileys
        adapter's, shared via ``helpers.markdown_to_whatsapp`` (fences and
        inline code protected, ``__bold__`` and ``~~strike~~`` handled), since
        both adapters render on the same client.
        """
        from olano.platforms.helpers import markdown_to_whatsapp

        return markdown_to_whatsapp(content or "").strip()

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": normalize_whatsapp_address(chat_id), "type": "dm"}

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def diagnostic_snapshot(self) -> dict[str, Any]:
        """Connector health for the dashboard's diagnose panel.

        The generic snapshot says only "connected", and for this connector
        that is the half that cannot fail quietly: sending errors are returned
        to the agent. Receiving is the half that can, since a message Twilio
        cannot deliver leaves no trace on this side at all. So the inbound
        setup result is its own row, in words an operator can act on.
        """
        connected = bool(self.is_connected)
        checks: list[dict[str, Any]] = [
            {"id": "runtime", "ok": True, "message": "Adapter is running."},
            {
                "id": "connection",
                "ok": connected,
                "message": (
                    "Twilio accepted the Account SID and Auth Token."
                    if connected and self._credentials_verified
                    else (
                        "Running, but Twilio could not be reached at startup to verify "
                        "the credentials; they are checked again on every request."
                        if connected
                        else "Not connected to Twilio."
                    )
                ),
            },
            {
                "id": "sender",
                "ok": not (self._sender_problem or self._sender_caveat),
                "message": self._sender_problem
                or self._sender_caveat
                or (
                    f"Sending as {self._from_address}"
                    + (
                        " (Twilio's shared sandbox number)."
                        if self.is_sandbox
                        else (
                            f" (online, WhatsApp Business Account "
                            f"{self._sender_info.get('waba_id') or 'unknown'})."
                            if self._sender_info.get("status") == "ONLINE"
                            else "."
                        )
                    )
                ),
            },
            {
                "id": "inbound",
                # Neutral while the first attempt is still running: the row's
                # badge turns red on any False, and "not finished yet" is not
                # an issue anyone should go and fix.
                "ok": None if self._inbound_state == "pending" else self._inbound_state == "ready",
                "message": self._inbound_message,
            },
            {
                "id": "template",
                "ok": True,
                "message": (
                    "Outside WhatsApp's 24-hour window only an approved template is "
                    "delivered: the agent lists the account's approved templates and sends "
                    "one with its WhatsApp template tools. An ordinary message there is "
                    "refused with the reason, since WhatsApp would drop it silently."
                ),
            },
        ]
        if self.is_sandbox:
            checks.append(
                {
                    "id": "sandbox",
                    "ok": None,
                    "message": (
                        "Sandbox reminders: each tester joins by sending 'join <your "
                        f"sandbox code>' to {SANDBOX_DISPLAY_NUMBER} and has to send it "
                        "again after 3 days. Clear the 'When a message comes in' URL in "
                        "Twilio's Sandbox settings, or Twilio's demo reply answers too."
                    ),
                }
            )
        fatal = self.fatal_error_message
        if fatal:
            checks.append({"id": "fatal_error", "ok": False, "message": str(fatal)})
        return {
            "adapter": f"platform:{self.platform.value}",
            "live": connected,
            "bot_user": self._from_address or None,
            "checks": checks,
        }

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    async def receive_webhook(
        self,
        raw: bytes,
        signature: str,
        *,
        candidate_urls: Iterable[str] = (),
    ) -> int:
        """Validate and accept one Conversations webhook; return the HTTP status.

        Called by the public route (``runtime/twilio_webhook_routes.py``) with
        the raw form body. The signature is checked against the URL this
        adapter registered, earlier URLs it registered in this process, and
        whatever the route saw the request arrive on, in that order. Nothing
        else runs before the check passes.
        """
        import urllib.parse

        try:
            form = {
                k: v[0]
                for k, v in urllib.parse.parse_qs(
                    raw.decode("utf-8"), keep_blank_values=True
                ).items()
                if v
            }
        except Exception as exc:
            logger.error("[whatsapp-twilio] webhook parse error: %s", exc)
            return 400

        urls = [
            u
            for u in dict.fromkeys(
                [self._webhook_url, *self._previous_webhook_urls, *candidate_urls]
            )
            if u
        ]
        signed_for = next(
            (u for u in urls if validate_signature(self._auth_token, u, form, signature)), ""
        )
        if not signed_for:
            logger.warning("[whatsapp-twilio] rejected: bad or missing X-Twilio-Signature")
            return 403

        # Conversations fires several event types on one webhook; only a new
        # message is a turn. Everything else (participant added, conversation
        # state) is acknowledged and dropped.
        if form.get("EventType") != WEBHOOK_FILTER:
            return 200

        author = normalize_whatsapp_address(form.get("Author", ""))
        if not author:
            # Authored by `system`, i.e. a message this runtime posted coming
            # back as an event. This single rule is the echo guard.
            return 200

        message_sid = form.get("MessageSid") or form.get("Sid") or ""
        if message_sid:
            if message_sid in self._seen_messages:
                return 200
            self._seen_messages.add(message_sid)
            if len(self._seen_messages) > 5000:
                self._seen_messages.clear()

        if not self._is_allowed(author):
            logger.info(
                "[whatsapp-twilio] ignoring message from %s (not allowed)", redact_phone(author)
            )
            return 200

        conversation_sid = form.get("ConversationSid", "")
        if conversation_sid:
            self._conversations[author] = conversation_sid
            if form.get("ChatServiceSid"):
                self._chat_services[conversation_sid] = form["ChatServiceSid"]
            # A delivery signed for the current URL proves that webhook is on
            # the conversation. One that came through an older address proves
            # only the old one, which the next adoption replaces.
            if signed_for == self._webhook_url:
                self._webhooked.add(conversation_sid)
        # Opens the 24-hour window. Recorded before the agent runs so a reply
        # produced by a long turn is still measured from the customer's message.
        self._last_inbound[author] = time.time()

        task = asyncio.create_task(self._dispatch(form, author, conversation_sid, message_sid))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return 200

    async def _dispatch(
        self, form: dict[str, str], author: str, conversation_sid: str, message_sid: str
    ) -> None:
        """Build the MessageEvent (downloading media) and hand it to the runtime."""
        try:
            event = await self._build_event(form, author, conversation_sid, message_sid)
            if event is not None:
                await self.handle_message(event)
        except Exception:
            logger.exception("[whatsapp-twilio] failed to dispatch inbound message")

    async def _build_event(
        self, form: dict[str, str], author: str, conversation_sid: str, message_sid: str
    ) -> Optional[MessageEvent]:
        body = (form.get("Body") or "").strip()
        media_urls: list[str] = []
        media_types: list[str] = []
        msg_type = MessageType.TEXT
        notes: list[str] = []

        raw_media = form.get("Media") or ""
        if raw_media:
            try:
                entries = json.loads(raw_media)
            except Exception:
                entries = []
                notes.append("[an attachment arrived but its metadata could not be read]")
            for entry in entries or []:
                saved, mime, failure = await self._fetch_media(entry, conversation_sid)
                if saved:
                    media_urls.append(saved)
                    media_types.append(mime)
                    msg_type, _ = media_kind_for(mime)
                elif failure:
                    # Never silence a missing attachment: a voice note carries no
                    # body, so dropping it quietly produces an agent turn with
                    # nothing in it and a reply that ignores what was sent.
                    notes.append(failure)

        if notes:
            body = (body + "\n" + "\n".join(notes)).strip() if body else "\n".join(notes)
        if not body and not media_urls:
            return None

        source = self.build_source(
            chat_id=author,
            chat_name=author,
            chat_type="dm",
            user_id=author,
            user_name=form.get("AuthorName") or author,
        )
        event = MessageEvent(
            text=body,
            message_type=msg_type,
            source=source,
            raw_message=form,
            message_id=message_sid,
            media_urls=media_urls,
            media_types=media_types,
        )
        # Twilio surfaces the quoted message on inbound WhatsApp replies. There
        # is no outbound equivalent, but the agent can still use the context.
        replied = form.get("OriginalRepliedMessageSid") or ""
        if replied:
            event.reply_to_message_id = replied
        return event

    async def _fetch_media(
        self, entry: Any, conversation_sid: str
    ) -> tuple[Optional[str], str, Optional[str]]:
        """Download one inbound attachment into the agent's uploads directory.

        Returns ``(local_path, mime, failure_note)``. Exactly one of the path or
        the note is set, so a caller can never mistake a failed download for an
        attachment that was not there.
        """
        if not isinstance(entry, dict):
            return None, "", "[an attachment arrived in an unrecognized format]"
        media_sid = entry.get("Sid") or entry.get("sid") or ""
        # The declared MIME is the transcription contract: `speech.transcribe_file`
        # posts the file under its own name and the provider reads the container
        # off that extension, so an audio/mpeg written `.ogg` is a format error
        # rather than a transcript.
        mime = entry.get("ContentType") or entry.get("content_type") or "application/octet-stream"
        filename = entry.get("Filename") or entry.get("filename") or ""
        if not media_sid:
            return (
                None,
                "",
                "[an attachment arrived without an identifier and could not be fetched]",
            )

        service_sid = await self._chat_service_for(conversation_sid)
        if not service_sid:
            return None, "", "[an attachment could not be fetched: unknown media service]"

        base = MCS_API_TEMPLATE.format(region=self._mcs_region, service_sid=service_sid)
        try:
            # Two steps: the Media instance names a short-lived direct URL, and
            # that URL serves the bytes.
            async with self._session().get(
                f"{base}/{media_sid}", headers=self._auth_headers()
            ) as resp:
                if resp.status >= 400:
                    return None, "", "[an attachment could not be fetched from WhatsApp]"
                meta = await resp.json()
            mime = meta.get("content_type") or mime
            filename = filename or meta.get("filename") or ""
            size = int(meta.get("size") or 0)
            if size > MAX_MEDIA_BYTES:
                from olano.uploads import human_size

                return (
                    None,
                    "",
                    f"[an attachment of {human_size(size)} was too large to download]",
                )
            direct = (meta.get("links") or {}).get("content_direct_temporary")
            if not direct:
                return None, "", "[an attachment could not be fetched: no download link]"

            async with self._session().get(direct) as resp:
                if resp.status >= 400:
                    return None, "", "[an attachment download failed]"
                data = await resp.read()
        except Exception as exc:
            logger.warning("[whatsapp-twilio] media download error: %s", exc)
            return None, "", "[an attachment could not be downloaded]"

        return self._cache_inbound(data, mime, filename, media_sid)

    @staticmethod
    def _cache_inbound(
        data: bytes, mime: str, filename: str, media_sid: str
    ) -> tuple[Optional[str], str, Optional[str]]:
        """Write downloaded bytes into the adapter media cache.

        An adapter hands the runtime **absolute local paths** on
        ``media_urls``; copying them into ``<workspace>/uploads/`` is
        ``inbound.build_agent_message``'s job, not ours (it is the one place
        that knows the receiving agent's workspace). Writing here too would put
        every attachment on disk twice.

        The **extension comes from the declared MIME**, never from the coarse
        media kind. ``speech.transcribe_file`` posts the file under its own
        name and the provider reads the audio container off that suffix, so an
        ``audio/mpeg`` voice note written ``.ogg`` is a format error rather
        than a transcript.
        """
        from olano.platforms.base import (
            cache_audio_from_bytes,
            cache_document_from_bytes,
            cache_image_from_bytes,
        )
        from olano.uploads import sanitize_filename

        _msg_type, kind = media_kind_for(mime)
        safe_name = sanitize_filename(filename or f"{kind}-{media_sid}", mime, kind)
        ext = Path(safe_name).suffix
        try:
            if kind == "image":
                # Raises when the bytes are not an image (an HTML error page
                # served in place of the file); the note below reports it
                # rather than caching a fake attachment.
                path = cache_image_from_bytes(data, ext=ext or ".jpg")
            elif kind == "voice":
                path = cache_audio_from_bytes(data, ext=ext or ".ogg")
            else:
                # Video and documents both keep their human-readable filename.
                path = cache_document_from_bytes(data, safe_name)
        except Exception as exc:
            logger.warning("[whatsapp-twilio] could not cache attachment: %s", exc)
            return None, "", "[an attachment arrived but could not be saved]"

        logger.info("[whatsapp-twilio] cached inbound %s (%s) at %s", kind, mime, path)
        return path, mime, None
