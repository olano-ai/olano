"""WhatsApp (Twilio Conversations) adapter.

Covers the behaviours that distinguish this adapter from ``sms.py`` and that a
regression would make silent rather than loud:

* the **24-hour window**, where WhatsApp drops a non-template message with no
  error at all, so the adapter has to refuse the send itself;
* **upload-then-attach**, the Conversations-only media path (``MediaSid``)
  that is the reason this connector is not a subclass of ``SmsAdapter``;
* the **echo guard**, which is a single rule about the message author;
* **signature validation**, shared with ``sms.py`` and easy to break in one
  place while the other keeps passing;
* **inbound routing**, which the adapter sets up on Twilio by itself, so a
  mistake there is a customer whose messages reach nobody and a log that
  says nothing.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from olano.platforms.config import PlatformConfig
from olano.platforms.twilio_signature import compute_signature, validate_signature
from olano.platforms.whatsapp_twilio import (
    MAX_IMAGE_BYTES,
    SESSION_WINDOW_SECONDS,
    WEBHOOK_PATH_TEMPLATE,
    WhatsAppTwilioAdapter,
    media_kind_for,
    normalize_whatsapp_address,
    public_webhook_base,
)

AGENT = "wa-twilio"
ACCOUNT_SID = "AC" + "0" * 32
AUTH_TOKEN = "test-auth-token"
SANDBOX = "whatsapp:+14155238886"
OWNED = "whatsapp:+12025550111"
#: A free number from WhatsApp (area code 555), which Twilio does not support.
FREE_555 = "whatsapp:+15550001234"
CUSTOMER = "whatsapp:+15551234567"
CONVERSATION = "CH" + "1" * 32
CHAT_SERVICE = "IS" + "2" * 32
DEFAULT_SERVICE = "IS" + "3" * 32
ORIGIN = "https://acme.example"
WEBHOOK_URL = f"{ORIGIN}/api/twilio/whatsapp/{AGENT}"


# ── fakes ────────────────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status: int = 200, payload: Any = None, text: str | None = None):
        self.status = status
        self._payload = payload if payload is not None else {}
        self._text = text if text is not None else json.dumps(self._payload)

    async def json(self) -> Any:
        return self._payload

    async def text(self) -> str:
        return self._text

    async def read(self) -> bytes:
        return self._text.encode()

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeSession:
    """Records every call and answers from a routing function."""

    def __init__(self, route=None):
        self.calls: list[tuple[str, str, dict]] = []
        self._route = route or (lambda method, url, kw: _FakeResponse(200, {}))
        self.closed = False

    def post(self, url: str, **kw: Any) -> _FakeResponse:
        self.calls.append(("POST", url, kw))
        return self._route("POST", url, kw)

    def get(self, url: str, **kw: Any) -> _FakeResponse:
        self.calls.append(("GET", url, kw))
        return self._route("GET", url, kw)

    def delete(self, url: str, **kw: Any) -> _FakeResponse:
        self.calls.append(("DELETE", url, kw))
        return self._route("DELETE", url, kw)

    async def close(self) -> None:
        self.closed = True

    def posts_to(self, fragment: str) -> list[tuple[str, str, dict]]:
        return [c for c in self.calls if c[0] == "POST" and fragment in c[1]]

    def writes(self) -> list[tuple[str, str, dict]]:
        return [c for c in self.calls if c[0] in ("POST", "DELETE")]


def _adapter(monkeypatch, extra: dict[str, Any] | None = None) -> WhatsAppTwilioAdapter:
    for name in (
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_WHATSAPP_NUMBER",
        "TWILIO_CONVERSATIONS_SERVICE_SID",
        "TWILIO_WHATSAPP_TEMPLATE_SID",
        "WHATSAPP_TWILIO_ALLOWED_USERS",
        "WHATSAPP_TWILIO_ALLOW_ALL_USERS",
        "LIONCLAW_PUBLIC_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    from olano.core import mcp_setup

    monkeypatch.setattr(mcp_setup, "_LAST_SEEN_ORIGIN", None)
    base = {
        "account_sid": ACCOUNT_SID,
        "auth_token": AUTH_TOKEN,
        "from_number": SANDBOX,
    }
    base.update(extra or {})
    return WhatsAppTwilioAdapter(PlatformConfig(enabled=True, extra=base), agent_name=AGENT)


def _wire(adapter: WhatsAppTwilioAdapter, session: _FakeSession) -> None:
    """Attach a fake HTTP session, pre-resolve the conversation, know the URL."""
    adapter._http_session = session  # type: ignore[assignment]
    adapter._conversations[CUSTOMER] = CONVERSATION
    adapter._chat_services[CONVERSATION] = CHAT_SERVICE
    adapter._webhook_url = WEBHOOK_URL


def _open_window(adapter: WhatsAppTwilioAdapter) -> None:
    import time

    adapter._last_inbound[CUSTOMER] = time.time()


def _signed(fields: dict[str, str], url: str = WEBHOOK_URL) -> tuple[bytes, str]:
    """A form body and the signature Twilio would send for it at *url*."""
    import urllib.parse

    return urllib.parse.urlencode(fields).encode(), compute_signature(AUTH_TOKEN, url, fields)


async def _deliver(adapter: WhatsAppTwilioAdapter, fields: dict[str, str]) -> int:
    raw, signature = _signed(fields)
    return await adapter.receive_webhook(raw, signature)


def _capture(monkeypatch, adapter: WhatsAppTwilioAdapter) -> list[Any]:
    captured: list[Any] = []

    async def _handle(event):
        captured.append(event)

    monkeypatch.setattr(adapter, "handle_message", _handle)
    return captured


async def _drain(adapter: WhatsAppTwilioAdapter) -> None:
    """Let the webhook's detached dispatch task finish."""
    for _ in range(5):
        await asyncio.sleep(0)
    pending = [t for t in adapter._background_tasks if not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


# ── pure helpers ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+14155238886", "whatsapp:+14155238886"),
        ("14155238886", "whatsapp:+14155238886"),
        ("whatsapp:+14155238886", "whatsapp:+14155238886"),
        ("whatsapp:14155238886", "whatsapp:+14155238886"),
        ("+1 415-523-8886", "whatsapp:+14155238886"),
        ("", ""),
        (None, ""),
    ],
)
def test_address_normalization_collapses_every_spelling(raw, expected):
    """One customer must key one conversation however their number is written."""
    assert normalize_whatsapp_address(raw) == expected


@pytest.mark.parametrize(
    "mime,kind",
    [
        ("image/jpeg", "image"),
        ("image/png", "image"),
        ("audio/ogg", "voice"),
        ("audio/mpeg", "voice"),
        ("video/mp4", "video"),
        ("application/pdf", "document"),
        ("", "document"),
    ],
)
def test_media_kind_reads_the_declared_mime(mime, kind):
    assert media_kind_for(mime)[1] == kind


@pytest.mark.parametrize(
    "origin,expected",
    [
        ("https://acme.olano.run", "https://acme.olano.run"),
        ("https://acme.olano.run/", "https://acme.olano.run"),
        ("https://abc123.ngrok-free.app", "https://abc123.ngrok-free.app"),
        ("http://93.184.216.34", "http://93.184.216.34"),
        # Documentation ranges (TEST-NET) are reserved, so refused too.
        ("http://203.0.113.7", ""),
        (None, ""),
        ("", ""),
        ("http://localhost:3000", ""),
        ("http://127.0.0.1:8765", ""),
        ("https://10.0.0.5", ""),
        ("http://192.168.1.20:8765", ""),
        ("http://my-box:8765", ""),
        ("http://olano.local", ""),
        ("ftp://acme.example", ""),
    ],
)
def test_public_webhook_base_refuses_what_twilio_cannot_reach(origin, expected):
    """Registering an unreachable URL would make every message vanish."""
    base, problem = public_webhook_base(origin)
    assert base == expected
    assert bool(problem) is (expected == "")


def test_webhook_path_matches_the_public_route():
    """The adapter registers this path with Twilio; the route must serve it."""
    from olano.runtime.twilio_webhook_routes import WHATSAPP_ROUTE

    assert WEBHOOK_PATH_TEMPLATE == WHATSAPP_ROUTE


def test_webhook_url_is_built_on_the_public_origin(monkeypatch):
    adapter = _adapter(monkeypatch)
    monkeypatch.setenv("LIONCLAW_PUBLIC_URL", "https://acme.olano.run/")
    assert adapter._current_webhook_url() == (
        f"https://acme.olano.run/api/twilio/whatsapp/{AGENT}",
        "",
    )


# ── signature validation (shared with sms.py) ────────────────────────────────


def test_signature_accepts_both_default_port_spellings():
    """Twilio may sign with or without :443; rejecting one breaks live traffic."""
    params = {"Body": "hi", "EventType": "onMessageAdded"}
    signature = compute_signature(AUTH_TOKEN, WEBHOOK_URL, params)
    assert validate_signature(AUTH_TOKEN, WEBHOOK_URL, params, signature)
    assert validate_signature(
        AUTH_TOKEN, f"https://acme.example:443/api/twilio/whatsapp/{AGENT}", params, signature
    )


def test_signature_rejects_wrong_empty_and_tampered():
    params = {"Body": "hi"}
    good = compute_signature(AUTH_TOKEN, WEBHOOK_URL, params)
    assert not validate_signature(AUTH_TOKEN, WEBHOOK_URL, params, "wrong")
    assert not validate_signature(AUTH_TOKEN, WEBHOOK_URL, params, "")
    # A changed parameter must invalidate the signature.
    assert not validate_signature(AUTH_TOKEN, WEBHOOK_URL, {"Body": "bye"}, good)
    # A non-standard port is never rewritten, so its signature must not match.
    assert not validate_signature(
        AUTH_TOKEN, f"https://acme.example:8443/api/twilio/whatsapp/{AGENT}", params, good
    )


def test_sms_adapter_uses_the_shared_validator():
    """One copy of the check, so the two Twilio surfaces cannot drift apart."""
    import inspect

    from olano.platforms import sms

    source = inspect.getsource(sms.SmsAdapter._validate_twilio_signature)
    assert "validate_signature(" in source
    assert not hasattr(sms.SmsAdapter, "_check_signature")
    assert not hasattr(sms.SmsAdapter, "_port_variant_url")


# ── the 24-hour window ───────────────────────────────────────────────────────


async def test_send_outside_the_window_refuses_instead_of_reporting_success(monkeypatch):
    """WhatsApp drops a free-form message outside 24h and reports nothing.

    Returning success there would log a delivered turn against a message the
    customer never saw, which is the defect shape this adapter exists to avoid.
    """
    adapter = _adapter(monkeypatch)
    session = _FakeSession()
    _wire(adapter, session)
    # No inbound recorded, so the window has never opened.

    result = await adapter.send(CUSTOMER, "are you still there?")

    assert result.success is False
    assert "24-hour" in (result.error or "")
    assert session.posts_to("/Messages") == [], "nothing may be sent outside the window"


async def test_send_inside_the_window_posts_the_body(monkeypatch):
    adapter = _adapter(monkeypatch)
    session = _FakeSession(lambda m, u, kw: _FakeResponse(201, {"sid": "IM123"}))
    _wire(adapter, session)
    _open_window(adapter)

    result = await adapter.send(CUSTOMER, "hello there")

    assert result.success is True
    assert result.message_id == "IM123"
    posts = session.posts_to(f"/Conversations/{CONVERSATION}/Messages")
    assert len(posts) == 1
    assert posts[0][2]["data"]["Body"] == "hello there"
    # Addressed through the conversation's own service, which every inbound
    # webhook names, whatever service is pinned on the connector.
    assert f"/Services/{CHAT_SERVICE}/Conversations/{CONVERSATION}/Messages" in posts[0][1]


async def test_send_failure_carries_twilios_own_sentence(monkeypatch):
    adapter = _adapter(monkeypatch)
    session = _FakeSession(
        lambda m, u, kw: _FakeResponse(400, {"message": "Invalid body", "code": 21602})
    )
    _wire(adapter, session)
    _open_window(adapter)

    result = await adapter.send(CUSTOMER, "hello")

    assert result.success is False
    assert result.error == "Twilio 400: Invalid body (error 21602)"
    assert result.retryable is False


async def test_window_expires_after_24_hours(monkeypatch):
    import time

    adapter = _adapter(monkeypatch)
    session = _FakeSession()
    _wire(adapter, session)
    adapter._last_inbound[CUSTOMER] = time.time() - (SESSION_WINDOW_SECONDS + 60)

    result = await adapter.send(CUSTOMER, "following up")
    assert result.success is False
    assert session.posts_to("/Messages") == []


async def test_closed_window_points_the_agent_at_its_template_tools(monkeypatch, caplog):
    """Outside the window an ordinary message is refused, never wrapped.

    The first version wrapped it in one configured template (``template_sid``),
    a whole free-form reply inside a catch-all placeholder. The agent now picks
    a fitting approved template itself, so a leftover setting is ignored (and
    logged), and the refusal names the tools that send a template.
    """
    import logging

    with caplog.at_level(logging.WARNING):
        adapter = _adapter(monkeypatch, {"template_sid": "HX" + "9" * 32})
    assert "template_sid is no longer used" in caplog.text
    session = _FakeSession(lambda m, u, kw: _FakeResponse(201, {"sid": "IM999"}))
    _wire(adapter, session)

    result = await adapter.send(CUSTOMER, "your order shipped")

    assert result.success is False
    assert "whatsapp_template_send" in (result.error or "")
    assert session.writes() == []


# ── media: upload, then attach ───────────────────────────────────────────────


async def test_media_is_uploaded_then_attached_by_sid(monkeypatch, tmp_path):
    """The Conversations-only path: bytes to MCS, then MediaSid on the message.

    No public URL is involved anywhere, which is the whole reason this adapter
    uses Conversations instead of Programmable Messaging.
    """
    adapter = _adapter(monkeypatch)

    def route(method, url, kw):
        if "mcs." in url:
            return _FakeResponse(201, {"sid": "ME" + "7" * 32})
        return _FakeResponse(201, {"sid": "IM777"})

    session = _FakeSession(route)
    _wire(adapter, session)
    _open_window(adapter)

    png = tmp_path / "chart.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    result = await adapter.send_image_file(CUSTOMER, str(png), caption="Here it is")

    assert result.success is True
    uploads = [c for c in session.calls if "mcs." in c[1]]
    assert len(uploads) == 1, "exactly one MCS upload"
    assert CHAT_SERVICE in uploads[0][1]
    assert uploads[0][2]["data"] == png.read_bytes(), "raw bytes, not a URL"
    assert uploads[0][2]["headers"]["Content-Disposition"] == 'attachment; filename="chart.png"'

    sent = session.posts_to(f"/Conversations/{CONVERSATION}/Messages")[0][2]["data"]
    assert sent["MediaSid"] == "ME" + "7" * 32
    assert sent["Body"] == "Here it is"
    assert "MediaUrl" not in sent, "Conversations attaches by SID, never by URL"


async def test_oversize_image_is_refused_before_upload(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch)
    session = _FakeSession()
    _wire(adapter, session)
    _open_window(adapter)

    big = tmp_path / "huge.png"
    big.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * (MAX_IMAGE_BYTES + 1))

    result = await adapter.send_image_file(CUSTOMER, str(big))

    assert result.success is False
    assert "limit" in (result.error or "").lower()
    assert session.calls == [], "an oversize file must not reach Twilio"


async def test_missing_file_is_reported_not_silently_skipped(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch)
    session = _FakeSession()
    _wire(adapter, session)
    _open_window(adapter)

    result = await adapter.send_document(CUSTOMER, str(tmp_path / "nope.pdf"))
    assert result.success is False
    assert "no such file" in (result.error or "").lower()


async def test_media_outside_the_window_is_refused(monkeypatch, tmp_path):
    """WhatsApp would drop it, and a template cannot carry an arbitrary file."""
    adapter = _adapter(monkeypatch)
    session = _FakeSession()
    _wire(adapter, session)

    png = tmp_path / "x.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    result = await adapter.send_image_file(CUSTOMER, str(png))

    assert result.success is False
    assert "24-hour" in (result.error or "")
    # The adapter may *read* the conversation's message log to recover the
    # window after a restart; it must never write (no upload, no message).
    assert session.writes() == []


async def test_window_is_recovered_from_the_message_log_after_a_restart(monkeypatch):
    """`_last_inbound` is in-memory; the conversation's messages are durable.

    A restart used to close every window until the customer wrote again, so
    a reply to a message that arrived minutes before the restart, or any
    scheduled notification, was refused for up to 24 hours.
    """
    from datetime import datetime, timedelta, timezone

    adapter = _adapter(monkeypatch)
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")

    def route(method, url, kw):
        if method == "GET" and url.endswith(f"/Conversations/{CONVERSATION}/Messages"):
            return _FakeResponse(
                200,
                {
                    "messages": [
                        {"author": "system", "date_created": recent},
                        {"author": CUSTOMER, "date_created": recent},
                    ]
                },
            )
        return _FakeResponse(201, {"sid": "IM-recovered"})

    session = _FakeSession(route)
    _wire(adapter, session)
    # No in-memory stamp: this is a fresh process.

    result = await adapter.send(CUSTOMER, "sorry for the delay")

    assert result.success is True
    assert adapter._window_is_open(CUSTOMER)
    assert len(session.posts_to("/Messages")) == 1


async def test_refused_send_never_creates_a_conversation(monkeypatch):
    """Outside the window, an ordinary send may write nothing to Twilio.

    The conversation used to be created (plus a participant binding) before
    the window was checked, so every refused notification left an empty
    conversation behind on the account.
    """
    adapter = _adapter(monkeypatch)
    session = _FakeSession(lambda m, u, kw: _FakeResponse(200, {"conversations": []}))
    adapter._http_session = session  # type: ignore[assignment]
    # Nothing cached: the lookup runs and finds no conversation at all.

    result = await adapter.send(CUSTOMER, "are you there?")

    assert result.success is False
    assert "24-hour" in (result.error or "")
    assert session.writes() == []


async def test_lookup_skips_null_bindings_and_closed_conversations(monkeypatch):
    """A null ``participant_messaging_binding`` is documented; it used to raise
    out of the loop and fall through to creating a duplicate conversation."""
    adapter = _adapter(monkeypatch)
    ours = {
        "conversation_sid": CONVERSATION,
        "conversation_state": "active",
        "chat_service_sid": CHAT_SERVICE,
        "participant_messaging_binding": {"proxy_address": SANDBOX},
    }

    # A null binding must not raise. An unknown proxy is adopted (the
    # adapter's standing rule: safer than creating a duplicate), so the point
    # here is that the lookup answers instead of falling through to a create.
    rows = {
        "conversations": [
            {"conversation_sid": "CH-chat", "participant_messaging_binding": None},
            ours,
        ]
    }
    session = _FakeSession(lambda m, u, kw: _FakeResponse(200, rows))
    adapter._http_session = session  # type: ignore[assignment]
    sid = await adapter._resolve_conversation(CUSTOMER, create=True)
    assert sid == "CH-chat"
    assert session.writes() == [], "never created"

    # A closed conversation is skipped for the next live one bound to us.
    adapter._conversations.clear()
    rows["conversations"] = [
        {
            "conversation_sid": "CH-closed",
            "conversation_state": "closed",
            "participant_messaging_binding": {"proxy_address": SANDBOX},
        },
        {
            "conversation_sid": "CH-other-sender",
            "conversation_state": "active",
            "participant_messaging_binding": {"proxy_address": "whatsapp:+19990000000"},
        },
        ours,
    ]
    sid = await adapter._resolve_conversation(CUSTOMER, create=True)
    assert sid == CONVERSATION
    assert adapter._chat_services[CONVERSATION] == CHAT_SERVICE
    assert session.writes() == [], "never created"


# ── inbound webhook ──────────────────────────────────────────────────────────


async def test_webhook_rejects_a_bad_signature(monkeypatch):
    adapter = _adapter(monkeypatch)
    _wire(adapter, _FakeSession())
    captured = _capture(monkeypatch, adapter)

    raw, _ = _signed({"EventType": "onMessageAdded", "Author": CUSTOMER, "Body": "hi"})
    status = await adapter.receive_webhook(raw, "forged")

    assert status == 403
    await _drain(adapter)
    assert captured == []


def _impersonation(kind: str) -> tuple[bytes, str]:
    """A request from someone pretending to be Twilio, by method of attack."""
    import urllib.parse

    fields = {"EventType": "onMessageAdded", "Author": CUSTOMER, "Body": "wire me $500"}
    raw = urllib.parse.urlencode(fields).encode()
    if kind == "no signature":
        return raw, ""
    if kind == "guessed token":
        return raw, compute_signature("not-the-auth-token", WEBHOOK_URL, fields)
    if kind == "tampered body":
        # A genuine signature, lifted from a real message, on a changed body.
        genuine = compute_signature(AUTH_TOKEN, WEBHOOK_URL, {**fields, "Body": "hi"})
        return raw, genuine
    if kind == "another agent's url":
        other = f"{ORIGIN}/api/twilio/whatsapp/some-other-agent"
        return raw, compute_signature(AUTH_TOKEN, other, fields)
    raise AssertionError(kind)


@pytest.mark.parametrize(
    "kind", ["no signature", "guessed token", "tampered body", "another agent's url"]
)
async def test_nobody_but_twilio_can_start_a_turn(monkeypatch, kind):
    """Pretending to be Twilio needs the account's Auth Token.

    Every inbound request carries ``X-Twilio-Signature``, an HMAC over the
    exact URL and every form field, keyed by the Auth Token that only Twilio
    and the vault hold. Without it, or with any field changed, the request is
    refused before anything reaches the agent.
    """
    adapter = _adapter(monkeypatch)
    _wire(adapter, _FakeSession())
    captured = _capture(monkeypatch, adapter)

    raw, signature = _impersonation(kind)
    status = await adapter.receive_webhook(raw, signature)

    assert status == 403
    await _drain(adapter)
    assert captured == []
    assert CUSTOMER not in adapter._last_inbound, "a forgery must not even open the window"


async def test_delivery_through_an_old_address_does_not_count_as_wired(monkeypatch):
    """Only a delivery signed for the current URL proves that webhook exists."""
    adapter = _adapter(monkeypatch)
    _wire(adapter, _FakeSession())
    _capture(monkeypatch, adapter)
    old = f"https://old.example/api/twilio/whatsapp/{AGENT}"
    fields = {
        "EventType": "onMessageAdded",
        "Author": CUSTOMER,
        "Body": "hi",
        "ConversationSid": CONVERSATION,
        "MessageSid": "IM-old",
    }

    raw, signature = _signed(fields, url=old)
    assert await adapter.receive_webhook(raw, signature, candidate_urls=[old]) == 200
    assert CONVERSATION not in adapter._webhooked

    raw, signature = _signed({**fields, "MessageSid": "IM-new"})
    assert await adapter.receive_webhook(raw, signature) == 200
    assert CONVERSATION in adapter._webhooked
    await _drain(adapter)


async def test_webhook_refuses_everything_while_no_url_is_known(monkeypatch):
    """Nothing to check a signature against means nothing gets in."""
    adapter = _adapter(monkeypatch)
    adapter._http_session = _FakeSession()  # type: ignore[assignment]
    captured = _capture(monkeypatch, adapter)

    status = await _deliver(adapter, {"EventType": "onMessageAdded", "Author": CUSTOMER})

    assert status == 403
    await _drain(adapter)
    assert captured == []


async def test_webhook_accepts_a_previous_url_and_the_url_it_arrived_on(monkeypatch):
    """A conversation may still carry an earlier registration; both must work.

    The public origin can change (a dashboard opened on a new address, a
    changed LIONCLAW_PUBLIC_URL), and a conversation keeps the webhook it was
    given until the adapter next touches it.
    """
    adapter = _adapter(monkeypatch)
    _wire(adapter, _FakeSession())
    captured = _capture(monkeypatch, adapter)
    adapter._adopt_webhook_url(f"https://new.example/api/twilio/whatsapp/{AGENT}")

    fields = {"EventType": "onMessageAdded", "Author": CUSTOMER, "MessageSid": "IM-a", "Body": "a"}
    raw, signature = _signed(fields, url=WEBHOOK_URL)
    assert await adapter.receive_webhook(raw, signature) == 200

    other = f"https://other.example/api/twilio/whatsapp/{AGENT}"
    fields = {"EventType": "onMessageAdded", "Author": CUSTOMER, "MessageSid": "IM-b", "Body": "b"}
    raw, signature = _signed(fields, url=other)
    assert await adapter.receive_webhook(raw, signature) == 403
    assert await adapter.receive_webhook(raw, signature, candidate_urls=[other]) == 200

    await _drain(adapter)
    assert [e.text for e in captured] == ["a", "b"]


async def test_webhook_ignores_non_message_events(monkeypatch):
    """Conversations fires many event types; only a new message is a turn."""
    adapter = _adapter(monkeypatch)
    _wire(adapter, _FakeSession())
    captured = _capture(monkeypatch, adapter)

    status = await _deliver(
        adapter, {"EventType": "onParticipantAdded", "Author": CUSTOMER, "Body": "hi"}
    )

    assert status == 200
    await _drain(adapter)
    assert captured == []


async def test_webhook_ignores_our_own_messages(monkeypatch):
    """The echo guard: a message we posted is authored ``system``, not whatsapp:."""
    adapter = _adapter(monkeypatch)
    _wire(adapter, _FakeSession())
    captured = _capture(monkeypatch, adapter)

    await _deliver(adapter, {"EventType": "onMessageAdded", "Author": "system", "Body": "ours"})
    await _drain(adapter)
    assert captured == [], "a message we sent must never start a turn"


async def test_inbound_text_opens_the_window_and_dispatches(monkeypatch):
    adapter = _adapter(monkeypatch)
    _wire(adapter, _FakeSession())
    captured = _capture(monkeypatch, adapter)

    await _deliver(
        adapter,
        {
            "EventType": "onMessageAdded",
            "Author": CUSTOMER,
            "Body": "do you deliver?",
            "ConversationSid": CONVERSATION,
            "ChatServiceSid": CHAT_SERVICE,
            "MessageSid": "IM001",
        },
    )
    await _drain(adapter)

    assert len(captured) == 1
    assert captured[0].text == "do you deliver?"
    assert captured[0].source.chat_id == CUSTOMER
    assert adapter._window_is_open(CUSTOMER), "an inbound message opens the 24h window"
    # A delivery proves the webhook is attached; nothing re-checks it.
    assert CONVERSATION in adapter._webhooked


async def test_inbound_is_deduplicated_by_message_sid(monkeypatch):
    """Twilio retries webhooks; a retry must not re-run the agent."""
    adapter = _adapter(monkeypatch)
    _wire(adapter, _FakeSession())
    captured = _capture(monkeypatch, adapter)
    fields = {
        "EventType": "onMessageAdded",
        "Author": CUSTOMER,
        "Body": "hello",
        "ConversationSid": CONVERSATION,
        "MessageSid": "IM-dup",
    }
    await _deliver(adapter, fields)
    await _deliver(adapter, fields)
    await _drain(adapter)

    assert len(captured) == 1


async def test_allow_list_blocks_unlisted_numbers(monkeypatch):
    adapter = _adapter(monkeypatch, {"allowed_users": ["+19995550000"]})
    _wire(adapter, _FakeSession())
    captured = _capture(monkeypatch, adapter)

    await _deliver(adapter, {"EventType": "onMessageAdded", "Author": CUSTOMER, "Body": "let me"})
    await _drain(adapter)
    assert captured == []

    # The listed number gets through, normalized from its bare form.
    await _deliver(
        adapter,
        {
            "EventType": "onMessageAdded",
            "Author": "whatsapp:+19995550000",
            "Body": "hi",
            "MessageSid": "IM-ok",
        },
    )
    await _drain(adapter)
    assert len(captured) == 1


async def test_inbound_media_is_downloaded_and_carries_its_real_mime(monkeypatch):
    """The declared MIME picks the extension, which is the transcription contract."""
    adapter = _adapter(monkeypatch)
    audio = b"OggS" + b"\x00" * 64

    def route(method, url, kw):
        if url.endswith("/Media/ME123"):
            return _FakeResponse(
                200,
                {
                    "content_type": "audio/ogg",
                    "filename": "voice-note.ogg",
                    "size": len(audio),
                    "links": {"content_direct_temporary": "https://media.example/tmp"},
                },
            )
        if "media.example" in url:
            return _FakeResponse(200, {}, text=audio.decode("latin-1"))
        return _FakeResponse(200, {})

    session = _FakeSession(route)
    _wire(adapter, session)
    captured = _capture(monkeypatch, adapter)

    await _deliver(
        adapter,
        {
            "EventType": "onMessageAdded",
            "Author": CUSTOMER,
            "Body": "",
            "ConversationSid": CONVERSATION,
            "MessageSid": "IM-media",
            "Media": json.dumps(
                [{"Sid": "ME123", "ContentType": "audio/ogg", "Filename": "voice-note.ogg"}]
            ),
        },
    )
    await _drain(adapter)

    assert len(captured) == 1
    event = captured[0]
    assert event.media_types == ["audio/ogg"]
    assert event.media_urls and event.media_urls[0].endswith(".ogg")
    assert event.message_type.value == "voice"


async def test_failed_media_download_leaves_a_note_never_silence(monkeypatch):
    """A voice note has no body, so a dropped attachment would be an empty turn.

    That is exactly how the Baileys adapter shipped ``chars=0`` replies, so a
    failure here must reach the agent as text rather than vanishing.
    """
    adapter = _adapter(monkeypatch)
    session = _FakeSession(lambda m, u, kw: _FakeResponse(404, {}, text="gone"))
    _wire(adapter, session)
    captured = _capture(monkeypatch, adapter)

    await _deliver(
        adapter,
        {
            "EventType": "onMessageAdded",
            "Author": CUSTOMER,
            "Body": "",
            "ConversationSid": CONVERSATION,
            "MessageSid": "IM-fail",
            "Media": json.dumps([{"Sid": "ME404", "ContentType": "audio/ogg"}]),
        },
    )
    await _drain(adapter)

    assert len(captured) == 1, "the turn must still happen"
    assert captured[0].media_urls == []
    assert "attachment" in captured[0].text.lower()


async def test_inbound_reply_context_is_surfaced(monkeypatch):
    """Twilio gives inbound reply context even though outbound quoting cannot."""
    adapter = _adapter(monkeypatch)
    _wire(adapter, _FakeSession())
    captured = _capture(monkeypatch, adapter)
    await _deliver(
        adapter,
        {
            "EventType": "onMessageAdded",
            "Author": CUSTOMER,
            "Body": "yes that one",
            "MessageSid": "IM-reply",
            "OriginalRepliedMessageSid": "IM-original",
        },
    )
    await _drain(adapter)

    assert captured[0].reply_to_message_id == "IM-original"


# ── inbound routing: set up on Twilio by the adapter itself ──────────────────


def _routing_adapter(monkeypatch, session: _FakeSession, **extra: Any) -> WhatsAppTwilioAdapter:
    adapter = _adapter(monkeypatch, extra)
    monkeypatch.setenv("LIONCLAW_PUBLIC_URL", ORIGIN)
    adapter._http_session = session  # type: ignore[assignment]
    adapter._running = True
    return adapter


async def test_no_public_address_is_reported_and_nothing_is_registered(monkeypatch):
    """An unreachable URL on Twilio is worse than none: say so instead."""
    adapter = _adapter(monkeypatch, {"from_number": OWNED})
    session = _FakeSession()
    adapter._http_session = session  # type: ignore[assignment]

    done = await adapter._ensure_inbound_routing()

    assert done is False, "retried: the address may become known later"
    assert session.calls == []
    assert adapter._inbound_state == "error"
    assert "public web address" in adapter._inbound_message
    assert "Sending still works" in adapter._inbound_message


async def test_owned_number_gets_an_address_configuration(monkeypatch):
    """Twilio autocreates a conversation per new customer, webhook attached."""
    session = _FakeSession(
        lambda m, u, kw: _FakeResponse(
            201 if m == "POST" else 200,
            {"address_configurations": [], "meta": {"next_page_url": None}},
        )
    )
    adapter = _routing_adapter(monkeypatch, session, from_number=OWNED)
    adapter._default_service_sid = DEFAULT_SERVICE

    assert await adapter._ensure_inbound_routing() is True

    (post,) = session.posts_to("/Configuration/Addresses")
    assert post[1].endswith("/Configuration/Addresses")
    assert post[2]["data"] == {
        "Type": "whatsapp",
        "Address": OWNED,
        "FriendlyName": f"Olano agent {AGENT}",
        "AutoCreation.Enabled": "true",
        "AutoCreation.Type": "webhook",
        "AutoCreation.WebhookUrl": WEBHOOK_URL,
        "AutoCreation.WebhookMethod": "post",
        "AutoCreation.WebhookFilters": "onMessageAdded",
        "AutoCreation.ConversationServiceSid": DEFAULT_SERVICE,
    }
    assert adapter._inbound_state == "ready"
    assert WEBHOOK_URL in adapter._inbound_message


async def test_matching_address_configuration_is_left_alone(monkeypatch):
    row = {
        "sid": "IG" + "4" * 32,
        "address": OWNED,
        "auto_creation": {
            "enabled": True,
            "type": "webhook",
            "webhook_url": WEBHOOK_URL,
            "webhook_method": "POST",
            "webhook_filters": ["onMessageAdded"],
            "conversation_service_sid": DEFAULT_SERVICE,
        },
    }
    session = _FakeSession(lambda m, u, kw: _FakeResponse(200, {"address_configurations": [row]}))
    adapter = _routing_adapter(monkeypatch, session, from_number=OWNED)
    adapter._default_service_sid = DEFAULT_SERVICE

    assert await adapter._ensure_inbound_routing() is True
    assert session.writes() == [], "a restart must not rewrite a correct configuration"
    assert adapter._inbound_state == "ready"


async def test_someone_elses_routing_is_replaced_and_reported(monkeypatch):
    """Connecting the number to the agent is the request to deliver it here."""
    row = {
        "sid": "IG" + "5" * 32,
        "address": "+12025550111",
        "auto_creation": {"enabled": True, "type": "studio", "studio_flow_sid": "FW1"},
    }
    session = _FakeSession(
        lambda m, u, kw: _FakeResponse(200, {"address_configurations": [row], "meta": {}})
    )
    adapter = _routing_adapter(monkeypatch, session, from_number=OWNED)

    assert await adapter._ensure_inbound_routing() is True

    (post,) = session.posts_to("/Configuration/Addresses/")
    assert post[1].endswith(f"/Configuration/Addresses/{row['sid']}"), "updated, not duplicated"
    assert post[2]["data"]["AutoCreation.WebhookUrl"] == WEBHOOK_URL
    assert "Type" not in post[2]["data"] and "Address" not in post[2]["data"]
    assert "replaced the previous routing (studio)" in adapter._inbound_message


async def test_refused_address_configuration_says_why_and_retries(monkeypatch):
    def route(method, url, kw):
        if method == "POST":
            return _FakeResponse(400, {"message": "Address is not valid", "code": 50407})
        return _FakeResponse(200, {"address_configurations": []})

    session = _FakeSession(route)
    adapter = _routing_adapter(monkeypatch, session, from_number=OWNED)

    assert await adapter._ensure_inbound_routing() is False
    assert adapter._inbound_state == "error"
    assert "Address is not valid (error 50407)" in adapter._inbound_message
    assert "Retrying automatically" in adapter._inbound_message


async def test_a_refused_address_for_a_free_555_number_names_the_real_cause(monkeypatch):
    """The reported setup: an ONLINE sender whose Address Configuration Twilio
    refused as "Invalid Address (error 51001)". The hint read "check that it
    is an approved WhatsApp sender", which it was; the number's kind is the
    cause, and it is recognized without asking Twilio."""

    def route(method, url, kw):
        if method == "POST":
            return _FakeResponse(400, {"message": "Invalid Address", "code": 51001})
        return _FakeResponse(200, {"address_configurations": []})

    session = _FakeSession(route)
    adapter = _routing_adapter(monkeypatch, session, from_number=FREE_555)
    await adapter._verify_sender()

    assert await adapter._ensure_inbound_routing() is False
    message = adapter._inbound_message
    assert "Invalid Address (error 51001)" in message
    assert "Twilio does not support these numbers" in message
    assert "approved WhatsApp sender" not in message
    assert "Retrying automatically" in message


def _sandbox_route(*, participant_status: int = 201, hooks: list[dict] | None = None):
    def route(method, url, kw):
        if method == "GET" and url.endswith("/ParticipantConversations"):
            return _FakeResponse(200, {"conversations": []})
        if method == "POST" and url.endswith("/Conversations"):
            return _FakeResponse(201, {"sid": CONVERSATION, "chat_service_sid": CHAT_SERVICE})
        if method == "GET" and url.endswith("/Webhooks"):
            return _FakeResponse(200, {"webhooks": hooks or []})
        if method == "POST" and url.endswith("/Participants"):
            if participant_status >= 400:
                return _FakeResponse(
                    participant_status,
                    {"message": "Participant address is not valid", "code": 50407},
                )
            return _FakeResponse(participant_status, {"sid": "MB1"})
        return _FakeResponse(201 if method == "POST" else 204, {})

    return route


async def test_sandbox_opens_a_conversation_per_allowed_number(monkeypatch):
    """The sandbox cannot autocreate, so the adapter does what the tutorial does.

    The webhook must be on the conversation before the participant: from the
    moment the participant exists, the tester's messages land in it.
    """
    session = _FakeSession(_sandbox_route())
    adapter = _routing_adapter(monkeypatch, session, allowed_users=[CUSTOMER])

    assert await adapter._ensure_inbound_routing() is True

    writes = [(m, u) for m, u, _ in session.writes()]
    assert [u.rsplit("/", 1)[-1] for _, u in writes] == [
        "Conversations",
        "Webhooks",
        "Participants",
    ]
    hook = session.posts_to("/Webhooks")[0][2]["data"]
    assert hook == {
        "Target": "webhook",
        "Configuration.Url": WEBHOOK_URL,
        "Configuration.Method": "post",
        "Configuration.Filters": "onMessageAdded",
    }
    participant = session.posts_to("/Participants")[0][2]["data"]
    assert participant == {
        "MessagingBinding.Address": CUSTOMER,
        "MessagingBinding.ProxyAddress": SANDBOX,
    }
    assert session.posts_to("/Configuration/Addresses") == [], "the sandbox has no address config"
    assert adapter._inbound_state == "ready"
    assert adapter._conversations[CUSTOMER] == CONVERSATION


async def test_sandbox_without_allowed_numbers_explains_what_to_do(monkeypatch):
    session = _FakeSession()
    adapter = _routing_adapter(monkeypatch, session)

    assert await adapter._ensure_inbound_routing() is True, "a retry cannot change this"
    assert session.calls == []
    assert adapter._inbound_state == "error"
    assert "Allowed sender numbers" in adapter._inbound_message


async def test_refused_participant_deletes_the_conversation_and_retries(monkeypatch):
    """A tester who has not joined yet: nothing left behind, retried later."""
    session = _FakeSession(_sandbox_route(participant_status=400))
    adapter = _routing_adapter(monkeypatch, session, allowed_users=[CUSTOMER])

    assert await adapter._ensure_inbound_routing() is False

    deletes = [u for m, u, _ in session.calls if m == "DELETE"]
    assert deletes and deletes[-1].endswith(f"/Conversations/{CONVERSATION}")
    assert CUSTOMER not in adapter._conversations
    assert adapter._inbound_state == "error"
    assert "join" in adapter._inbound_message
    assert "Participant address is not valid" in adapter._inbound_message


async def test_adopted_conversation_gets_our_webhook_and_loses_a_stale_one(monkeypatch):
    """A conversation predating this address must still deliver to the agent.

    A webhook of ours from an earlier origin is removed (Twilio would keep
    posting to a dead address); anyone else's is never touched.
    """
    hooks = [
        {
            "sid": "WH-stale",
            "target": "webhook",
            "configuration": {"url": f"https://old.example/api/twilio/whatsapp/{AGENT}"},
        },
        {
            "sid": "WH-foreign",
            "target": "webhook",
            "configuration": {"url": "https://crm.example/hook"},
        },
        {"sid": "WH-studio", "target": "studio", "configuration": {"flow_sid": "FW1"}},
    ]

    def route(method, url, kw):
        if method == "GET" and url.endswith("/ParticipantConversations"):
            return _FakeResponse(
                200,
                {
                    "conversations": [
                        {
                            "conversation_sid": CONVERSATION,
                            "chat_service_sid": CHAT_SERVICE,
                            "participant_messaging_binding": {"proxy_address": SANDBOX},
                        }
                    ]
                },
            )
        if method == "GET" and url.endswith("/Webhooks"):
            return _FakeResponse(200, {"webhooks": hooks})
        return _FakeResponse(201 if method == "POST" else 204, {})

    session = _FakeSession(route)
    adapter = _routing_adapter(monkeypatch, session, allowed_users=[CUSTOMER])

    assert await adapter._ensure_inbound_routing() is True

    deletes = [u for m, u, _ in session.calls if m == "DELETE"]
    assert deletes == [
        f"https://conversations.twilio.com/v1/Services/{CHAT_SERVICE}"
        f"/Conversations/{CONVERSATION}/Webhooks/WH-stale"
    ]
    assert len(session.posts_to("/Webhooks")) == 1
    assert session.posts_to("/Participants") == [], "adopted, never re-created"


async def test_a_conversation_already_carrying_our_webhook_is_not_touched(monkeypatch):
    hooks = [{"sid": "WH1", "target": "webhook", "configuration": {"url": WEBHOOK_URL}}]
    session = _FakeSession(_sandbox_route(hooks=hooks))
    adapter = _routing_adapter(monkeypatch, session)
    adapter._webhook_url = WEBHOOK_URL

    assert await adapter._ensure_conversation_webhook(CONVERSATION) is True
    assert await adapter._ensure_conversation_webhook(CONVERSATION) is True
    assert session.writes() == []
    assert len([c for c in session.calls if c[1].endswith("/Webhooks")]) == 1, "memoized"


async def test_template_send_to_a_new_number_wires_the_conversation(monkeypatch):
    """A conversation opened by a template must hear the answer to it."""
    from olano.platforms.twilio_content import Template

    session = _FakeSession(_sandbox_route())
    adapter = _routing_adapter(monkeypatch, session)
    adapter._webhook_url = WEBHOOK_URL
    shipped = Template(
        sid="HX" + "9" * 32,
        friendly_name="order_shipped",
        body="Hi {{1}}, your order has shipped and is on its way to you.",
        status="approved",
    )

    result = await adapter.send_template(CUSTOMER, shipped, {"1": "Maria"})

    assert result.success is True
    endpoints = [u.rsplit("/", 1)[-1] for _, u, _ in session.writes()]
    assert endpoints == ["Conversations", "Webhooks", "Participants", "Messages"]


# ── connect ──────────────────────────────────────────────────────────────────


def _patch_client(monkeypatch, session: _FakeSession) -> None:
    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **kw: session)


async def test_connect_refuses_credentials_twilio_rejects(monkeypatch):
    adapter = _adapter(monkeypatch)
    session = _FakeSession(
        lambda m, u, kw: _FakeResponse(401, {"message": "Authenticate", "code": 20003})
    )
    _patch_client(monkeypatch, session)

    assert await adapter.connect() is False

    assert adapter.is_connected is False
    assert "refused these credentials" in (adapter.fatal_error_message or "")
    assert "Authenticate (error 20003)" in (adapter.fatal_error_message or "")
    assert session.closed is True
    assert adapter._routing_task is None


async def test_connect_survives_an_unreachable_twilio(monkeypatch):
    """A network blip at boot must not become a dead channel until restart."""
    adapter = _adapter(monkeypatch)

    def route(method, url, kw):
        raise OSError("network unreachable")

    session = _FakeSession(route)
    _patch_client(monkeypatch, session)
    started: list[bool] = []

    async def _loop():
        started.append(True)

    monkeypatch.setattr(adapter, "_inbound_routing_loop", _loop)

    assert await adapter.connect() is True
    await asyncio.sleep(0)
    assert started == [True]
    assert adapter._credentials_verified is None
    await adapter.disconnect()


async def test_connect_verifies_then_sets_up_inbound_in_the_background(monkeypatch):
    adapter = _adapter(monkeypatch)
    session = _FakeSession(
        lambda m, u, kw: _FakeResponse(200, {"default_chat_service_sid": DEFAULT_SERVICE})
    )
    _patch_client(monkeypatch, session)
    gate = asyncio.Event()

    async def _loop():
        await gate.wait()

    monkeypatch.setattr(adapter, "_inbound_routing_loop", _loop)

    assert await adapter.connect() is True

    assert adapter.is_connected is True
    assert adapter._default_service_sid == DEFAULT_SERVICE
    assert session.calls[0][1] == "https://conversations.twilio.com/v1/Configuration"
    task = adapter._routing_task
    assert task is not None and not task.done(), "setup runs off the boot path"

    await adapter.disconnect()
    assert task.cancelled()
    assert adapter.is_connected is False


async def test_a_second_connect_leaves_one_session_and_one_setup_loop(monkeypatch):
    import aiohttp

    adapter = _adapter(monkeypatch)
    sessions = [
        _FakeSession(lambda m, u, kw: _FakeResponse(200, {})),
        _FakeSession(lambda m, u, kw: _FakeResponse(200, {})),
    ]
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **kw: sessions.pop(0))
    gate = asyncio.Event()

    async def _loop():
        await gate.wait()

    monkeypatch.setattr(adapter, "_inbound_routing_loop", _loop)

    assert await adapter.connect() is True
    first_session, first_task = adapter._http_session, adapter._routing_task
    assert await adapter.connect() is True

    assert first_session.closed is True  # type: ignore[union-attr]
    assert first_task is not None and first_task.cancelled()
    assert adapter._routing_task is not first_task
    await adapter.disconnect()


@pytest.mark.parametrize(
    "extra,needle",
    [
        ({"auth_token": ""}, "Account SID and Auth Token"),
        ({"from_number": ""}, "WhatsApp sender number"),
    ],
)
async def test_connect_names_what_is_missing(monkeypatch, extra, needle):
    adapter = _adapter(monkeypatch, extra)
    session = _FakeSession()
    _patch_client(monkeypatch, session)

    assert await adapter.connect() is False
    assert needle in (adapter.fatal_error_message or "")
    assert session.calls == []


async def test_routing_loop_retries_until_done(monkeypatch):
    from olano.platforms import whatsapp_twilio as mod

    adapter = _adapter(monkeypatch)
    adapter._running = True
    results = iter([False, False, True])
    calls: list[int] = []

    async def _ensure():
        calls.append(1)
        return next(results)

    monkeypatch.setattr(adapter, "_ensure_inbound_routing", _ensure)
    monkeypatch.setattr(mod, "ROUTING_RETRY_INITIAL_SECONDS", 0.0)

    await asyncio.wait_for(adapter._inbound_routing_loop(), timeout=5)
    assert len(calls) == 3


# ── diagnostics ──────────────────────────────────────────────────────────────


def test_diagnostics_name_the_inbound_state_in_plain_words(monkeypatch):
    adapter = _adapter(monkeypatch, {"allowed_users": [CUSTOMER]})
    adapter._running = True
    adapter._credentials_verified = True
    adapter._set_inbound("partial", "Incoming messages are not set up yet for 1 of 2.")

    snapshot = adapter.diagnostic_snapshot()

    checks = {c["id"]: c for c in snapshot["checks"]}
    assert snapshot["adapter"] == "platform:whatsapp_twilio"
    assert snapshot["live"] is True
    assert checks["connection"]["ok"] is True
    assert checks["inbound"] == {
        "id": "inbound",
        "ok": False,
        "message": "Incoming messages are not set up yet for 1 of 2.",
    }
    assert "join" in checks["sandbox"]["message"]
    assert "3 days" in checks["sandbox"]["message"]
    assert "WhatsApp template tools" in checks["template"]["message"]


# ── formatting ───────────────────────────────────────────────────────────────


def test_markdown_is_converted_to_whatsapp_markup(monkeypatch):
    """WhatsApp uses *bold*, so Markdown's ** would render as literal asterisks."""
    adapter = _adapter(monkeypatch)
    assert adapter.format_message("**bold**") == "*bold*"
    assert adapter.format_message("## Heading") == "*Heading*"
    assert adapter.format_message("[docs](https://x.example)") == "docs (https://x.example)"
    # Shared with the Baileys adapter: fences are protected, strike converts.
    assert adapter.format_message("```\n**kwargs\n```") == "```\n**kwargs\n```"
    assert adapter.format_message("~~old~~ price") == "~old~ price"


def test_a_connector_saved_by_the_first_form_still_reaches_the_adapter():
    """The first dashboard form saved plain fields at the top level.

    ``PlatformConfig.from_dict`` keeps only ``extra`` and a few named keys, so
    those values never reached the adapter and a connector set up in the
    dashboard refused to start for want of an Account SID. The boot path folds
    them into ``extra``; an explicit ``extra`` value still wins.
    """
    from olano.runtime.server import _fold_credentials_into_extra

    saved = {
        "type": "platform:whatsapp_twilio",
        "account_sid": ACCOUNT_SID,
        "from_number": "+12025550111",
        "allowed_users": ["+15551234567"],
        "auth_token": AUTH_TOKEN,
        "extra": {"from_number": "+12025559888"},
    }
    folded = _fold_credentials_into_extra("whatsapp_twilio", saved)
    config = PlatformConfig.from_dict({"enabled": True, **folded})

    assert config.extra == {
        "account_sid": ACCOUNT_SID,
        "from_number": "+12025559888",
        "allowed_users": ["+15551234567"],
        "auth_token": AUTH_TOKEN,
    }
    adapter = WhatsAppTwilioAdapter(config, agent_name=AGENT)
    assert adapter._account_sid == ACCOUNT_SID
    assert adapter._allowed_users == {CUSTOMER}


def test_registered_in_the_platform_map():
    """A connector nothing can boot is not a connector."""
    from olano.platforms.config import Platform
    from olano.runtime.server import PLATFORM_MAP

    dotted, enum_name = PLATFORM_MAP["whatsapp_twilio"]
    assert dotted == "olano.platforms.whatsapp_twilio.WhatsAppTwilioAdapter"
    assert getattr(Platform, enum_name) is Platform.WHATSAPP_TWILIO


# ── the configured number, checked against the account's senders ────────────


def _senders_client(rows: list[dict[str, Any]] | Exception):
    """A Content client whose Senders API answers with *rows* (or raises)."""
    import httpx

    from olano.platforms.twilio_content import TwilioContentClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "messaging.twilio.com", request.url
        if isinstance(rows, Exception):
            raise rows
        return httpx.Response(200, json={"senders": rows, "meta": {}})

    return TwilioContentClient(ACCOUNT_SID, AUTH_TOKEN, transport=httpx.MockTransport(handler))


async def _connect_with_senders(
    monkeypatch, extra: dict[str, Any], rows
) -> tuple[WhatsAppTwilioAdapter, _FakeSession]:
    monkeypatch.setenv("LIONCLAW_TWILIO_SENDER_CHECK", "1")
    adapter = _adapter(monkeypatch, extra)
    session = _FakeSession(lambda m, u, kw: _FakeResponse(201, {"sid": "IM1"}))
    _patch_client(monkeypatch, session)
    monkeypatch.setattr(adapter, "content_client", lambda: _senders_client(rows))

    async def _loop():
        return None

    monkeypatch.setattr(adapter, "_inbound_routing_loop", _loop)
    assert await adapter.connect() is True
    return adapter, session


async def test_a_number_that_is_not_a_sender_on_the_account_is_refused_with_the_list(monkeypatch):
    """A number typed as the sender that the account does not have (a typo, one
    registered on another account): Twilio accepts every message and WhatsApp
    refuses each one later, so the connector refuses up front and names the
    numbers the account does have."""
    rows = [{"sid": "XE1", "sender_id": "whatsapp:+12025550999", "status": "ONLINE"}]
    adapter, session = await _connect_with_senders(monkeypatch, {"from_number": OWNED}, rows)
    try:
        assert "not a WhatsApp sender on this Twilio account" in adapter.sender_problem
        assert "+12025550999" in adapter.sender_problem, "the numbers the account does have"
        checks = {c["id"]: c for c in adapter.diagnostic_snapshot()["checks"]}
        assert checks["sender"]["ok"] is False
        assert checks["sender"]["message"] == adapter.sender_problem

        _wire(adapter, session)
        _open_window(adapter)
        result = await adapter.send(CUSTOMER, "hello")
        assert result.success is False and "not a WhatsApp sender" in (result.error or "")
        assert session.posts_to("/Messages") == [], "nothing is handed to Twilio to lose"
    finally:
        await adapter.disconnect()


async def test_a_sender_that_is_not_online_is_refused_with_its_state(monkeypatch):
    rows = [
        {
            "sid": "XE1",
            "sender_id": OWNED,
            "status": "OFFLINE",
            "offline_reasons": [{"code": 30503, "message": "Display name rejected"}],
        }
    ]
    adapter, _ = await _connect_with_senders(monkeypatch, {"from_number": OWNED}, rows)
    try:
        assert "OFFLINE" in adapter.sender_problem
        assert "Display name rejected" in adapter.sender_problem
    finally:
        await adapter.disconnect()


async def test_an_online_sender_passes_and_the_diagnostics_name_its_account(monkeypatch):
    rows = [
        {"sid": "XE1", "sender_id": OWNED, "status": "ONLINE", "configuration": {"waba_id": "777"}}
    ]
    adapter, session = await _connect_with_senders(monkeypatch, {"from_number": OWNED}, rows)
    try:
        assert adapter.sender_problem == ""
        checks = {c["id"]: c for c in adapter.diagnostic_snapshot()["checks"]}
        assert checks["sender"]["ok"] is True and "777" in checks["sender"]["message"]
        _wire(adapter, session)
        _open_window(adapter)
        assert (await adapter.send(CUSTOMER, "hello")).success is True
    finally:
        await adapter.disconnect()


async def test_a_free_555_number_passes_the_check_and_is_flagged_not_blocked(monkeypatch):
    """The reported setup: the Senders API lists the number ONLINE on the
    account's WhatsApp Business Account, and Twilio does not support it. The
    sender row read OK; it is red now with the reason and the fix. Sends are
    not refused, since Twilio's guide says "at this time"."""
    rows = [
        {
            "sid": "XE1",
            "sender_id": FREE_555,
            "status": "ONLINE",
            "configuration": {"waba_id": "777"},
        }
    ]
    adapter, session = await _connect_with_senders(monkeypatch, {"from_number": FREE_555}, rows)
    try:
        assert adapter.sender_problem == ""
        assert "Twilio does not support these numbers" in adapter.sender_caveat
        assert "WhatsApp sender number" in adapter.sender_caveat, "names the fix"
        checks = {c["id"]: c for c in adapter.diagnostic_snapshot()["checks"]}
        assert checks["sender"]["ok"] is False
        assert checks["sender"]["message"] == adapter.sender_caveat
        _wire(adapter, session)
        _open_window(adapter)
        assert (await adapter.send(CUSTOMER, "hello")).success is True
    finally:
        await adapter.disconnect()


async def test_a_free_555_number_is_flagged_without_asking_twilio(monkeypatch):
    """Recognized from the number alone, so an unanswered or switched-off
    check still flags it."""
    adapter, _ = await _connect_with_senders(
        monkeypatch, {"from_number": FREE_555}, OSError("proxy down")
    )
    try:
        assert adapter.sender_problem == ""
        assert "Twilio does not support these numbers" in adapter.sender_caveat
    finally:
        await adapter.disconnect()


async def test_an_unanswered_sender_check_trusts_the_number(monkeypatch):
    """Failing closed would cost every send over a blip; open costs one wrong send."""
    adapter, _ = await _connect_with_senders(
        monkeypatch, {"from_number": OWNED}, OSError("proxy down")
    )
    try:
        assert adapter.sender_problem == ""
        checks = {c["id"]: c for c in adapter.diagnostic_snapshot()["checks"]}
        assert checks["sender"]["ok"] is True
    finally:
        await adapter.disconnect()


async def test_the_sandbox_is_never_checked_and_the_switch_turns_the_check_off(monkeypatch):
    asked: list[int] = []

    def boom():
        asked.append(1)
        raise AssertionError("the Senders API must not be asked")

    async def _loop():
        return None

    for extra, env in (({}, "1"), ({"from_number": OWNED}, "0")):
        monkeypatch.setenv("LIONCLAW_TWILIO_SENDER_CHECK", env)
        adapter = _adapter(monkeypatch, extra)
        _patch_client(monkeypatch, _FakeSession())
        monkeypatch.setattr(adapter, "content_client", boom)
        monkeypatch.setattr(adapter, "_inbound_routing_loop", _loop)
        assert await adapter.connect() is True
        try:
            assert asked == [] and adapter.sender_problem == ""
        finally:
            await adapter.disconnect()


def test_owner_numbers_are_admitted_even_when_the_allowlist_omits_them(monkeypatch) -> None:
    adapter = _adapter(
        monkeypatch,
        {"allowed_users": ["+15550000001"], "owner_users": ["+1 555 000 0099"]},
    )
    assert adapter._is_allowed("whatsapp:+15550000001")
    assert adapter._is_allowed("whatsapp:+15550000099")
    assert not adapter._is_allowed("whatsapp:+15550000002")
