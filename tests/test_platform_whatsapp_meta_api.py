"""WhatsApp (Meta Cloud API) connector: the Graph helpers, the adapter, its rules.

Meta is faked at the HTTP layer (``httpx.MockTransport``) so every request the
connector makes is the one it would send to graph.facebook.com, and every
answer is shaped like Meta's documented responses. The surface under test is
what an operator and an agent depend on: the connect checks (token, number,
Business Account, permissions), the automatic webhook setup and what it
refuses to touch, inbound messages (signature, window, media, BSUIDs, the
allow-list), sending inside and outside the window, templates, and the
delivery record that survives a restart.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import httpx
import pytest

from olano.platforms import whatsapp_meta_graph as mg
from olano.platforms.config import PlatformConfig
from olano.platforms.whatsapp_meta_api import (
    SESSION_WINDOW_SECONDS,
    WEBHOOK_PATH_TEMPLATE,
    WhatsAppMetaAPIAdapter,
)
from olano.platforms.whatsapp_templates import Button

AGENT = "shop"
TOKEN = "EAAtest-token"
APP_SECRET = "app-secret-123"
WABA = "102290129340398"
PHONE_ID = "106540352242922"
BUSINESS_NUMBER = "15550783881"
CUSTOMER = "+12025550143"
BSUID = "SG.13491208655302741918"
ORIGIN = "https://acme.example"
WEBHOOK_URL = f"{ORIGIN}/api/meta/whatsapp/{AGENT}"
V = mg.DEFAULT_GRAPH_VERSION


def _sig(body: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ── a fake Graph API ─────────────────────────────────────────────────────────


class FakeGraph:
    """Meta's Graph API for one Business Account and one number."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.number: dict[str, Any] = {
            "id": PHONE_ID,
            "display_phone_number": "+1 555-078-3881",
            "verified_name": "Birdhouse",
            "status": "CONNECTED",
            "name_status": "APPROVED",
            "quality_rating": "GREEN",
            "whatsapp_business_manager_messaging_limit": {"current_limit": "TIER_2K"},
            "webhook_configuration": {"application": "https://other.example/hook"},
        }
        self.waba_numbers = [{"id": PHONE_ID, "display_phone_number": "+1 555-078-3881"}]
        self.subscribed: list[dict[str, Any]] = []
        self.debug: dict[str, Any] = {
            "type": "SYSTEM_USER",
            "expires_at": 0,
            "is_valid": True,
            "scopes": [mg.PERMISSION_MESSAGING, mg.PERMISSION_MANAGEMENT],
            "granular_scopes": [
                {"scope": mg.PERMISSION_MESSAGING, "target_ids": [WABA]},
                {"scope": mg.PERMISSION_MANAGEMENT, "target_ids": [WABA]},
            ],
        }
        self.templates: list[dict[str, Any]] = []
        self.number_error: tuple[int, dict[str, Any]] | None = None
        self.callback_error: tuple[int, dict[str, Any]] | None = None
        self.send_error: tuple[int, dict[str, Any]] | None = None
        self.sent: list[dict[str, Any]] = []
        self.media: dict[str, tuple[bytes, str]] = {}
        self._next = 0

    def json(self, request: httpx.Request) -> dict[str, Any]:
        return json.loads(request.content or b"{}")

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        method = request.method
        if request.url.host == "lookaside.fbsbx.com":
            data, _mime = self.media[path.rsplit("/", 1)[-1]]
            return httpx.Response(200, content=data)
        assert path.startswith(f"/{V}/"), path
        path = path[len(f"/{V}/") :]
        if path == PHONE_ID and method == "GET":
            if self.number_error:
                return httpx.Response(self.number_error[0], json=self.number_error[1])
            return httpx.Response(200, json=self.number)
        if path == PHONE_ID and method == "POST":
            if self.callback_error:
                return httpx.Response(self.callback_error[0], json=self.callback_error[1])
            body = self.json(request)
            url = body["webhook_configuration"]["override_callback_uri"]
            self.number["webhook_configuration"]["phone_number"] = url
            return httpx.Response(200, json={"success": True})
        if path == f"{WABA}/phone_numbers":
            return httpx.Response(200, json={"data": self.waba_numbers})
        if path == "app":
            return httpx.Response(200, json={"id": "APP1", "name": "Olano"})
        if path == "debug_token":
            return httpx.Response(200, json={"data": self.debug})
        if path == "me/permissions":
            return httpx.Response(200, json={"data": []})
        if path == f"{WABA}/subscribed_apps":
            if method == "POST":
                self.subscribed = [{"whatsapp_business_api_data": {"id": "APP1"}}]
                return httpx.Response(200, json={"success": True})
            return httpx.Response(200, json={"data": self.subscribed})
        if path == f"{WABA}/message_templates":
            if method == "GET":
                return httpx.Response(200, json={"data": self.templates})
            if method == "POST":
                body = self.json(request)
                self.templates.append(
                    {"id": "999", "status": "PENDING", "category": body["category"], **body}
                )
                return httpx.Response(
                    200, json={"id": "999", "status": "PENDING", "category": body["category"]}
                )
            if method == "DELETE":
                return httpx.Response(200, json={"success": True})
        if path == f"{PHONE_ID}/messages":
            body = self.json(request)
            if body.get("status") == "read":
                self.sent.append(body)
                return httpx.Response(200, json={"success": True})
            if self.send_error:
                return httpx.Response(self.send_error[0], json=self.send_error[1])
            self._next += 1
            self.sent.append(body)
            return httpx.Response(200, json={"messages": [{"id": f"wamid.OUT{self._next}"}]})
        if path == f"{PHONE_ID}/media":
            return httpx.Response(200, json={"id": "MEDIA-UP"})
        if path == f"{PHONE_ID}/whatsapp_business_profile":
            if method == "POST":
                return httpx.Response(200, json={"success": True})
            return httpx.Response(200, json={"data": [{"about": "Birds", "vertical": "RETAIL"}]})
        if path in self.media:
            data, mime = self.media[path]
            return httpx.Response(
                200,
                json={
                    "url": f"https://lookaside.fbsbx.com/whatsapp/{path}",
                    "mime_type": mime,
                    "file_size": len(data),
                },
            )
        if path.isdigit():  # one template
            row = next(t for t in self.templates if t.get("id") == path)
            return httpx.Response(200, json=row)
        return httpx.Response(404, json={"error": {"message": f"unhandled {method} {path}"}})

    def client(self, **kw: Any) -> mg.MetaGraphClient:
        return mg.MetaGraphClient(
            TOKEN,
            waba_id=WABA,
            phone_number_id=PHONE_ID,
            app_secret=APP_SECRET,
            transport=httpx.MockTransport(self.handler),
            **kw,
        )

    def paths(self, method: str = "") -> list[str]:
        return [
            r.url.path.replace(f"/{V}/", "")
            for r in self.requests
            if not method or r.method == method
        ]


def _adapter(monkeypatch, graph: FakeGraph, **extra: Any) -> WhatsAppMetaAPIAdapter:
    for name in (
        "WHATSAPP_META_ACCESS_TOKEN",
        "WHATSAPP_META_APP_SECRET",
        "WHATSAPP_META_WABA_ID",
        "WHATSAPP_META_PHONE_NUMBER_ID",
        "LIONCLAW_PUBLIC_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    from olano.core import mcp_setup

    monkeypatch.setattr(mcp_setup, "_LAST_SEEN_ORIGIN", None)
    config = {
        "access_token": TOKEN,
        "app_secret": APP_SECRET,
        "waba_id": WABA,
        "phone_number_id": PHONE_ID,
        **extra,
    }
    adapter = WhatsAppMetaAPIAdapter(PlatformConfig(enabled=True, extra=config), agent_name=AGENT)
    monkeypatch.setattr(adapter, "_new_graph", lambda: graph.client())
    return adapter


async def _connect(monkeypatch, graph: FakeGraph, **extra: Any) -> WhatsAppMetaAPIAdapter:
    adapter = _adapter(monkeypatch, graph, **extra)

    async def _no_loop() -> None:
        return None

    monkeypatch.setattr(adapter, "_inbound_routing_loop", _no_loop)
    assert await adapter.connect() is True
    return adapter


def _inbound(message: dict[str, Any], *, contact: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": BUSINESS_NUMBER, "phone_number_id": PHONE_ID},
        "contacts": [contact or {"profile": {"name": "Maria"}, "wa_id": CUSTOMER[1:]}],
        "messages": [message],
    }


def _text(body: str, wamid: str = "wamid.IN1", **extra: Any) -> dict[str, Any]:
    return {
        "from": CUSTOMER[1:],
        "id": wamid,
        "timestamp": str(int(time.time())),
        "type": "text",
        "text": {"body": body},
        **extra,
    }


def _collect(monkeypatch, adapter: WhatsAppMetaAPIAdapter) -> list[Any]:
    received: list[Any] = []

    async def _handle(event: Any) -> None:
        received.append(event)

    monkeypatch.setattr(adapter, "handle_message", _handle)
    return received


def _open_window(adapter: WhatsAppMetaAPIAdapter, chat_id: str = CUSTOMER) -> None:
    adapter._last_inbound[chat_id] = time.time()


# ── pure helpers ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value, expected",
    [
        ("+1 202 555 0143", "+12025550143"),
        ("12025550143", "+12025550143"),
        ("whatsapp:+12025550143", "+12025550143"),
        (BSUID, BSUID),
        ("US.ENT.abc123", "US.ENT.abc123"),
        ("hello", ""),
        ("+12", ""),
        ("", ""),
    ],
)
def test_recipients_normalize_to_e164_or_a_bsuid(value, expected):
    assert mg.normalize_recipient(value) == expected


def test_a_bsuid_goes_in_recipient_never_in_to():
    """Meta accepts a BSUID in `to` and then drops the message silently."""
    assert mg.recipient_fields(BSUID) == {"recipient_type": "individual", "recipient": BSUID}
    assert mg.recipient_fields(CUSTOMER) == {"recipient_type": "individual", "to": CUSTOMER[1:]}


def test_signatures_are_checked_against_the_app_secret():
    body = b'{"object":"whatsapp_business_account"}'
    assert mg.verify_signature(APP_SECRET, body, _sig(body))
    assert not mg.verify_signature(APP_SECRET, body, _sig(body, "other"))
    assert not mg.verify_signature(APP_SECRET, body, "")
    assert not mg.verify_signature("", body, _sig(body))


POSITIONAL = {
    "id": "111",
    "name": "order_update",
    "language": "en_US",
    "status": "APPROVED",
    "category": "MARKETING",
    "previous_category": "UTILITY",
    "parameter_format": "POSITIONAL",
    "quality_score": {"score": "GREEN"},
    "components": [
        {
            "type": "HEADER",
            "format": "TEXT",
            "text": "Order {{1}}",
            "example": {"header_text": ["#1042"]},
        },
        {
            "type": "BODY",
            "text": "Hi {{1}}, your order is ready for pickup on {{2}} at our store.",
            "example": {"body_text": [["Maria", "Friday"]]},
        },
        {"type": "FOOTER", "text": "Reply STOP to opt out"},
        {
            "type": "BUTTONS",
            "buttons": [
                {
                    "type": "URL",
                    "text": "Track",
                    "url": "https://shop.example/track/{{1}}",
                    "example": ["https://shop.example/track/A1"],
                },
                {"type": "PHONE_NUMBER", "text": "Call us", "phone_number": "+15550001111"},
            ],
        },
    ],
}

NAMED = {
    "id": "222",
    "name": "welcome",
    "language": "en",
    "status": "APPROVED",
    "category": "UTILITY",
    "parameter_format": "NAMED",
    "components": [
        {
            "type": "HEADER",
            "format": "TEXT",
            "text": "Welcome {{first_name}}",
            "example": {
                "header_text_named_params": [{"param_name": "first_name", "example": "Ann"}]
            },
        },
        {
            "type": "BODY",
            "text": "Thanks {{first_name}}, your order {{order_number}} is on its way to you.",
            "example": {
                "body_text_named_params": [
                    {"param_name": "first_name", "example": "Ann"},
                    {"param_name": "order_number", "example": "#7"},
                ]
            },
        },
        {
            "type": "BUTTONS",
            "buttons": [
                {"type": "QUICK_REPLY", "text": "Thanks"},
                {
                    "type": "URL",
                    "text": "Track",
                    "url": "https://shop.example/o?name={{customer_name}}",
                    "example": ["https://shop.example/o?name=Ann"],
                },
            ],
        },
    ],
}

IMAGE_HEADER = {
    "id": "333",
    "name": "spring_sale",
    "language": "en",
    "status": "APPROVED",
    "category": "MARKETING",
    "components": [
        {"type": "HEADER", "format": "IMAGE", "example": {"header_handle": ["4::aW..."]}},
        {"type": "BODY", "text": "Our spring sale starts today, come and see what is new."},
    ],
}


def test_a_positional_template_gets_separate_keys_for_its_header_and_button():
    """Meta numbers a header's and a button's variable apart from the body's."""
    t = mg.template_from_graph(POSITIONAL)
    assert t.approved and t.language == "en_US" and t.sid == "111"
    assert t.header == "Order {{header}}"
    assert t.placeholders == ["header", "1", "2", "button_1"]
    assert t.samples == {"header": "#1042", "1": "Maria", "2": "Friday", "button_1": "A1"}
    row = t.describe()
    assert row["sid"] == "111" and row["footer"] == "Reply STOP to opt out"
    assert row["category"] == "MARKETING" and row["category_changed_from"] == "UTILITY"
    assert "billed as MARKETING" in row["category_note"]
    assert row["quality"] == "GREEN"


def test_positional_send_components_fill_header_body_and_the_encoded_button():
    t = mg.template_from_graph(POSITIONAL)
    values = {"header": "#9", "1": "Ann", "2": "Monday", "button_1": "A 1:2"}
    components = mg.template_send_components(t, values)
    assert components == [
        {"type": "header", "parameters": [{"type": "text", "text": "#9"}]},
        {
            "type": "body",
            "parameters": [{"type": "text", "text": "Ann"}, {"type": "text", "text": "Monday"}],
        },
        {
            "type": "button",
            "sub_type": "url",
            "index": "0",
            "parameters": [{"type": "text", "text": "A%201%3A2"}],
        },
    ]


def test_named_send_components_carry_every_parameter_name():
    t = mg.template_from_graph(NAMED)
    assert t.placeholders == ["header", "first_name", "order_number", "button_2"]
    components = mg.template_send_components(
        t, {"header": "Ann", "first_name": "Ann", "order_number": "#7", "button_2": "Ann B"}
    )
    header, body, button = components
    assert header["parameters"] == [{"type": "text", "text": "Ann", "parameter_name": "first_name"}]
    assert [p["parameter_name"] for p in body["parameters"]] == ["first_name", "order_number"]
    assert button["index"] == "1"
    assert button["parameters"] == [
        {"type": "text", "text": "Ann%20B", "parameter_name": "customer_name"}
    ]


def test_a_media_header_takes_a_link():
    t = mg.template_from_graph(IMAGE_HEADER)
    assert t.header_format == "IMAGE" and t.placeholders == ["header"]
    (header,) = mg.template_send_components(t, {"header": "https://cdn.example/a.jpg"})
    assert header == {
        "type": "header",
        "parameters": [{"type": "image", "image": {"link": "https://cdn.example/a.jpg"}}],
    }


def test_create_payloads_follow_metas_shapes():
    positional = mg.template_create_payload(
        name="order_ready",
        language="en",
        category="UTILITY",
        body="Hi {{1}}, your order {{2}} is ready for pickup at our store today.",
        samples={"1": "Maria", "2": "#1042", "3": "A1"},
        buttons=[Button("url", "Track", "https://shop.example/track/{{3}}")],
    )
    assert "parameter_format" not in positional
    body, buttons = positional["components"]
    assert body["example"] == {"body_text": [["Maria", "#1042"]]}
    assert buttons["buttons"] == [
        {
            "type": "URL",
            "text": "Track",
            "url": "https://shop.example/track/{{1}}",
            "example": ["https://shop.example/track/A1"],
        }
    ]
    named = mg.template_create_payload(
        name="welcome",
        language="en",
        category="UTILITY",
        body="Thanks {{first_name}}, your order is on its way to you today.",
        samples={"first_name": "Ann"},
        buttons=[Button("reply", "Thanks", "thanks")],
    )
    assert named["parameter_format"] == "named"
    assert named["components"][0]["example"] == {
        "body_text_named_params": [{"param_name": "first_name", "example": "Ann"}]
    }
    assert named["components"][1]["buttons"] == [{"type": "QUICK_REPLY", "text": "Thanks"}]


def test_meta_errors_read_as_sentences():
    assert "24 hours" in mg.describe_meta_error(131047)
    assert "not approved" in mg.describe_meta_error(
        132001
    ) or "no approved" in mg.describe_meta_error(132001)
    err = mg.graph_error(
        400,
        {
            "error": {
                "message": "(#131047) Re-engagement message",
                "code": 131047,
                "error_data": {"details": "x"},
            }
        },
    )
    assert err.code == 131047 and not err.auth_refused
    assert mg.graph_error(401, {"error": {"message": "bad", "code": 190}}).auth_refused


# ── connect ──────────────────────────────────────────────────────────────────


async def test_missing_settings_stop_the_connector_with_the_fix(monkeypatch):
    graph = FakeGraph()
    adapter = _adapter(monkeypatch, graph, waba_id="", phone_number_id="")
    assert await adapter.connect() is False
    assert "WhatsApp Business Account ID" in (adapter.fatal_error_message or "")
    assert "phone number ID" in (adapter.fatal_error_message or "")
    assert graph.requests == [], "nothing is asked of Meta without the settings"


async def test_a_refused_token_stops_the_connector(monkeypatch):
    graph = FakeGraph()
    graph.number_error = (401, {"error": {"message": "Session has expired", "code": 190}})
    adapter = _adapter(monkeypatch, graph)
    assert await adapter.connect() is False
    assert "refused the access token" in (adapter.fatal_error_message or "")
    assert "system user token" in (adapter.fatal_error_message or "")


async def test_a_wrong_phone_number_id_stops_the_connector(monkeypatch):
    graph = FakeGraph()
    graph.number_error = (
        400,
        {"error": {"message": "Unsupported get request.", "code": 100, "error_subcode": 33}},
    )
    adapter = _adapter(monkeypatch, graph)
    assert await adapter.connect() is False
    assert "not the phone number itself" in (adapter.fatal_error_message or "")


async def test_a_healthy_setup_connects_and_the_diagnostics_say_so(monkeypatch):
    graph = FakeGraph()
    adapter = await _connect(monkeypatch, graph)
    try:
        assert adapter.sender_address == "+15550783881"
        assert adapter.sender_problem == "" and adapter.sender_caveat == ""
        checks = {c["id"]: c for c in adapter.diagnostic_snapshot()["checks"]}
        assert checks["connection"]["ok"] is True
        assert "does not expire" in checks["connection"]["message"]
        assert checks["permissions"]["ok"] is True and WABA in checks["permissions"]["message"]
        assert checks["sender"]["ok"] is True
        assert (
            "Birdhouse" in checks["sender"]["message"] and "TIER_2K" in checks["sender"]["message"]
        )
        assert "0 people have written" in checks["window"]["message"]
    finally:
        await adapter.disconnect()


async def test_a_number_outside_the_business_account_is_named(monkeypatch):
    graph = FakeGraph()
    graph.waba_numbers = [{"id": "777", "display_phone_number": "+1 202-555-0111"}]
    adapter = await _connect(monkeypatch, graph)
    try:
        assert f"not on WhatsApp Business Account {WABA}" in adapter.sender_problem
        assert "+12025550111 (id 777)" in adapter.sender_problem
        _open_window(adapter)
        result = await adapter.send(CUSTOMER, "hi")
        assert result.success is False and "not on WhatsApp Business Account" in (
            result.error or ""
        )
        assert not [b for b in graph.sent if b.get("type") == "text"]
    finally:
        await adapter.disconnect()


async def test_a_token_without_the_messaging_permission_cannot_send(monkeypatch):
    graph = FakeGraph()
    graph.debug["scopes"] = [mg.PERMISSION_MANAGEMENT]
    graph.debug["granular_scopes"] = [{"scope": mg.PERMISSION_MANAGEMENT, "target_ids": [WABA]}]
    adapter = await _connect(monkeypatch, graph)
    try:
        checks = {c["id"]: c for c in adapter.diagnostic_snapshot()["checks"]}
        assert checks["permissions"]["ok"] is False
        assert "lacks whatsapp_business_messaging" in checks["permissions"]["message"]
        assert "whatsapp_business_messaging" in adapter.sender_problem
    finally:
        await adapter.disconnect()


async def test_a_temporary_token_is_flagged_before_it_expires(monkeypatch):
    graph = FakeGraph()
    graph.debug["expires_at"] = int(time.time()) + 3600
    adapter = await _connect(monkeypatch, graph)
    try:
        assert "temporary token" in adapter.sender_caveat
        checks = {c["id"]: c for c in adapter.diagnostic_snapshot()["checks"]}
        assert (
            checks["sender"]["ok"] is False and "system user token" in checks["sender"]["message"]
        )
    finally:
        await adapter.disconnect()


# ── inbound setup ────────────────────────────────────────────────────────────


async def test_inbound_setup_subscribes_the_app_and_points_the_number_here(monkeypatch):
    graph = FakeGraph()
    adapter = await _connect(monkeypatch, graph)
    monkeypatch.setenv("LIONCLAW_PUBLIC_URL", ORIGIN)
    try:
        assert adapter.webhook_path() == WEBHOOK_PATH_TEMPLATE.format(agent=AGENT)
        assert await adapter._ensure_inbound_routing() is True
        posts = graph.paths("POST")
        assert posts == [f"{WABA}/subscribed_apps", PHONE_ID], "subscribe first, then override"
        override = json.loads(
            next(
                r for r in graph.requests if r.method == "POST" and r.url.path.endswith(PHONE_ID)
            ).content
        )
        assert override == {
            "webhook_configuration": {
                "override_callback_uri": WEBHOOK_URL,
                "verify_token": adapter.verify_token,
            }
        }
        assert adapter._inbound_state == "ready" and WEBHOOK_URL in adapter._inbound_message
        # Every call made with the token carries the app secret proof (the
        # token check authenticates as the app instead).
        assert all(
            "appsecret_proof" in str(r.url)
            for r in graph.requests
            if "lookaside" not in str(r.url) and not r.url.path.endswith("debug_token")
        )
    finally:
        await adapter.disconnect()


async def test_an_existing_subscription_is_never_resubscribed(monkeypatch):
    """Subscribing again with no body clears a Business Account override somebody relies on."""
    graph = FakeGraph()
    graph.subscribed = [
        {"whatsapp_business_api_data": {"id": "APP1"}, "override_callback_uri": "https://x.example"}
    ]
    graph.number["webhook_configuration"]["phone_number"] = WEBHOOK_URL
    adapter = await _connect(monkeypatch, graph)
    monkeypatch.setenv("LIONCLAW_PUBLIC_URL", ORIGIN)
    try:
        assert await adapter._ensure_inbound_routing() is True
        assert graph.paths("POST") == [], "already ours: nothing written"
        assert adapter._inbound_state == "ready"
    finally:
        await adapter.disconnect()


async def test_a_refused_override_gives_the_manual_setup(monkeypatch):
    graph = FakeGraph()
    graph.callback_error = (403, {"error": {"message": "(#200) Permissions error", "code": 200}})
    adapter = await _connect(monkeypatch, graph)
    monkeypatch.setenv("LIONCLAW_PUBLIC_URL", ORIGIN)
    try:
        assert await adapter._ensure_inbound_routing() is False, "retried"
        message = adapter._inbound_message
        assert "whatsapp_business_management" in message
        assert WEBHOOK_URL in message and adapter.verify_token in message
        assert "Retrying automatically" in message
        assert f"{WABA}/subscribed_apps" in graph.paths("POST")
        assert not any(
            b"override_callback_uri" in r.content and r.url.path.endswith("subscribed_apps")
            for r in graph.requests
        ), "the Business Account-level override is never set automatically"
    finally:
        await adapter.disconnect()


async def test_without_the_app_secret_nothing_inbound_is_accepted(monkeypatch):
    graph = FakeGraph()
    adapter = await _connect(monkeypatch, graph, app_secret="")
    monkeypatch.setenv("LIONCLAW_PUBLIC_URL", ORIGIN)
    try:
        assert await adapter._ensure_inbound_routing() is True, "nothing to retry"
        assert adapter._inbound_state == "error" and "app secret" in adapter._inbound_message
        assert graph.paths("POST") == []
        body = b"{}"
        assert adapter.signature_valid(body, _sig(body)) is False
    finally:
        await adapter.disconnect()


def test_the_verify_token_is_stable_and_checked(monkeypatch):
    graph = FakeGraph()
    a = _adapter(monkeypatch, graph)
    b = _adapter(monkeypatch, graph)
    assert a.verify_token == b.verify_token and len(a.verify_token) == 40
    assert a.verify_subscription("subscribe", a.verify_token, "12345") == "12345"
    assert a.verify_subscription("subscribe", "wrong", "12345") is None
    assert a.verify_subscription("unsubscribe", a.verify_token, "12345") is None


# ── inbound messages ─────────────────────────────────────────────────────────


async def test_a_text_message_reaches_the_agent_and_opens_the_window(monkeypatch):
    graph = FakeGraph()
    adapter = await _connect(monkeypatch, graph)
    received = _collect(monkeypatch, adapter)
    try:
        await adapter.handle_webhook_value(_inbound(_text("hello", context={"id": "wamid.OUT1"})))
        await adapter.handle_webhook_value(_inbound(_text("hello")))  # Meta redelivers
        (event,) = received
        assert event.text == "hello" and event.message_id == "wamid.IN1"
        assert event.source.chat_id == CUSTOMER and event.source.user_name == "Maria"
        assert event.reply_to_message_id == "wamid.OUT1"
        assert adapter._window_is_open(CUSTOMER)
        status = await adapter.window_status(CUSTOMER)
        assert status["window_open"] is True and status["window_closes_at"]
        await _drain(adapter)
        assert {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": "wamid.IN1",
        } in graph.sent
    finally:
        await adapter.disconnect()


async def _drain(adapter: WhatsAppMetaAPIAdapter) -> None:
    import asyncio

    for _ in range(5):
        pending = [t for t in adapter._background_tasks if not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


async def test_read_receipts_can_be_switched_off(monkeypatch):
    graph = FakeGraph()
    adapter = await _connect(monkeypatch, graph, mark_read="false")
    _collect(monkeypatch, adapter)
    try:
        await adapter.handle_webhook_value(_inbound(_text("hi")))
        await adapter.send_typing(CUSTOMER)
        await _drain(adapter)
        assert not [b for b in graph.sent if b.get("status") == "read"]
    finally:
        await adapter.disconnect()


async def test_a_person_without_a_phone_number_is_keyed_by_their_bsuid(monkeypatch):
    graph = FakeGraph()
    adapter = await _connect(monkeypatch, graph)
    received = _collect(monkeypatch, adapter)
    try:
        message = _text("hi from a username")
        message.pop("from")
        message["from_user_id"] = BSUID
        contact = {"profile": {"name": "", "username": "maria.b"}, "user_id": BSUID}
        await adapter.handle_webhook_value(_inbound(message, contact=contact))
        (event,) = received
        assert event.source.chat_id == BSUID and event.source.user_name == "maria.b"
        result = await adapter.send(BSUID, "hello")
        assert result.success is True
        sent = [b for b in graph.sent if b.get("type") == "text"][-1]
        assert sent["recipient"] == BSUID and "to" not in sent
    finally:
        await adapter.disconnect()


async def test_the_allow_list_is_a_real_gate(monkeypatch):
    graph = FakeGraph()
    adapter = await _connect(monkeypatch, graph, allowed_users="+6511112222")
    received = _collect(monkeypatch, adapter)
    try:
        await adapter.handle_webhook_value(_inbound(_text("let me in")))
        assert received == []
        assert not adapter._window_is_open(CUSTOMER)
    finally:
        await adapter.disconnect()


async def test_a_voice_note_is_downloaded_with_its_real_extension(monkeypatch):
    graph = FakeGraph()
    graph.media["MEDIA1"] = (b"OggS" + b"\0" * 64, "audio/ogg; codecs=opus")
    adapter = await _connect(monkeypatch, graph)
    received = _collect(monkeypatch, adapter)
    try:
        message = {
            "from": CUSTOMER[1:],
            "id": "wamid.VOICE",
            "timestamp": str(int(time.time())),
            "type": "audio",
            "audio": {"id": "MEDIA1", "mime_type": "audio/ogg; codecs=opus", "voice": True},
        }
        await adapter.handle_webhook_value(_inbound(message))
        (event,) = received
        assert event.media_types == ["audio/ogg"]
        assert event.media_urls[0].endswith(".ogg")
        media_get = [r for r in graph.requests if r.url.host == "lookaside.fbsbx.com"]
        assert media_get and media_get[0].headers["authorization"] == f"Bearer {TOKEN}"
    finally:
        await adapter.disconnect()


async def test_an_attachment_that_cannot_be_fetched_is_never_silent(monkeypatch):
    graph = FakeGraph()
    adapter = await _connect(monkeypatch, graph)
    received = _collect(monkeypatch, adapter)
    try:
        message = {
            "from": CUSTOMER[1:],
            "id": "wamid.GONE",
            "timestamp": str(int(time.time())),
            "type": "audio",
            "audio": {"id": "MISSING"},
        }
        await adapter.handle_webhook_value(_inbound(message))
        (event,) = received
        assert "could not be downloaded" in event.text
    finally:
        await adapter.disconnect()


# ── sending ──────────────────────────────────────────────────────────────────


async def test_outside_the_window_an_ordinary_message_is_refused_with_the_way_forward(monkeypatch):
    graph = FakeGraph()
    adapter = await _connect(monkeypatch, graph)
    try:
        result = await adapter.send(CUSTOMER, "hello")
        assert result.success is False and "whatsapp_template_send" in (result.error or "")
        assert not [b for b in graph.sent if b.get("type") == "text"]
    finally:
        await adapter.disconnect()


async def test_inside_the_window_text_is_sent_and_recorded(monkeypatch):
    graph = FakeGraph()
    adapter = await _connect(monkeypatch, graph)
    _open_window(adapter)
    try:
        result = await adapter.send(CUSTOMER, "**Ready** now", reply_to="wamid.IN1")
        assert result.success and result.message_id == "wamid.OUT1"
        sent = graph.sent[-1]
        assert sent == {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": CUSTOMER[1:],
            "type": "text",
            "text": {"body": "*Ready* now", "preview_url": True},
        }, "no quote unless switched on"
        assert adapter.message_record("wamid.OUT1")["status"] == "accepted"
    finally:
        await adapter.disconnect()


async def test_quoting_can_be_switched_on(monkeypatch):
    graph = FakeGraph()
    adapter = await _connect(monkeypatch, graph, quote_replies="true")
    _open_window(adapter)
    try:
        await adapter.send(CUSTOMER, "yes", reply_to="wamid.IN1")
        assert graph.sent[-1]["context"] == {"message_id": "wamid.IN1"}
    finally:
        await adapter.disconnect()


async def test_a_closed_window_refusal_from_meta_reads_as_the_window(monkeypatch):
    graph = FakeGraph()
    graph.send_error = (
        400,
        {"error": {"message": "(#131047) Re-engagement message", "code": 131047}},
    )
    adapter = await _connect(monkeypatch, graph)
    _open_window(adapter)
    try:
        result = await adapter.send(CUSTOMER, "hello")
        assert result.success is False and "24-hour messaging window is closed" in (
            result.error or ""
        )
    finally:
        await adapter.disconnect()


async def test_a_file_is_uploaded_then_sent_by_id(monkeypatch, tmp_path):
    graph = FakeGraph()
    adapter = await _connect(monkeypatch, graph)
    _open_window(adapter)
    doc = tmp_path / "invoice.pdf"
    doc.write_bytes(b"%PDF-1.4 test")
    try:
        result = await adapter.send_document(CUSTOMER, str(doc), caption="Your invoice")
        assert result.success
        sent = graph.sent[-1]
        assert sent["type"] == "document"
        assert sent["document"] == {
            "id": "MEDIA-UP",
            "caption": "Your invoice",
            "filename": "invoice.pdf",
        }
    finally:
        await adapter.disconnect()


async def test_a_template_needs_every_value_and_sends_its_components(monkeypatch):
    graph = FakeGraph()
    adapter = await _connect(monkeypatch, graph)
    template = mg.template_from_graph(POSITIONAL)
    try:
        missing = await adapter.send_template(CUSTOMER, template, {"1": "Ann"})
        assert missing.success is False
        assert "{{header}}" in (missing.error or "") and "{{button_1}}" in (missing.error or "")
        result = await adapter.send_template(
            CUSTOMER, template, {"header": "#9", "1": "Ann", "2": "Mon\nday", "button_1": "A1"}
        )
        assert result.success, result.error
        sent = graph.sent[-1]
        assert sent["type"] == "template"
        assert sent["template"]["name"] == "order_update"
        assert sent["template"]["language"] == {"code": "en_US"}
        body = sent["template"]["components"][1]
        assert body["parameters"][1] == {"type": "text", "text": "Mon day"}, "no line breaks"
        assert result.raw_response["rendered"].startswith("Order #9\nHi Ann")
        record = adapter.message_record(result.message_id)
        assert record["template"] == "order_update" and record["category"] == "MARKETING"
    finally:
        await adapter.disconnect()


async def test_a_media_header_needs_an_https_link(monkeypatch):
    graph = FakeGraph()
    adapter = await _connect(monkeypatch, graph)
    try:
        result = await adapter.send_template(
            CUSTOMER, mg.template_from_graph(IMAGE_HEADER), {"header": "not a link"}
        )
        assert result.success is False and "https link" in (result.error or "")
    finally:
        await adapter.disconnect()


# ── delivery statuses ────────────────────────────────────────────────────────


def _status(wamid: str, status: str, **extra: Any) -> dict[str, Any]:
    return {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": BUSINESS_NUMBER, "phone_number_id": PHONE_ID},
        "statuses": [
            {
                "id": wamid,
                "status": status,
                "timestamp": str(int(time.time())),
                "recipient_id": CUSTOMER[1:],
                **extra,
            }
        ],
    }


async def test_statuses_are_recorded_with_billing_and_never_go_backwards(monkeypatch):
    graph = FakeGraph()
    adapter = await _connect(monkeypatch, graph)
    try:
        pricing = {
            "billable": False,
            "pricing_model": "PMP",
            "type": "free_customer_service",
            "category": "utility",
        }
        await adapter.handle_webhook_value(_status("wamid.OUT7", "sent", pricing=pricing))
        await adapter.handle_webhook_value(_status("wamid.OUT7", "read"))
        await adapter.handle_webhook_value(_status("wamid.OUT7", "delivered"))
        assert await adapter.message_status(CUSTOMER, "wamid.OUT7") == ("read", "")
        record = adapter.message_record("wamid.OUT7")
        assert record["billing"]["type"] == "free_customer_service"

        errors = [
            {"code": 131049, "title": "not delivered", "error_data": {"details": "ecosystem"}}
        ]
        await adapter.handle_webhook_value(_status("wamid.OUT8", "failed", errors=errors))
        status, detail = await adapter.message_status(CUSTOMER, "wamid.OUT8")
        assert status == "failed" and "marketing message" in detail

        assert (await adapter.message_status(CUSTOMER, "wamid.NEVER"))[0] == ""
    finally:
        await adapter.disconnect()


async def test_the_window_and_the_statuses_survive_a_restart(monkeypatch):
    """The Cloud API has no message history to recover a window from."""
    graph = FakeGraph()
    adapter = await _connect(monkeypatch, graph)
    _collect(monkeypatch, adapter)
    await adapter.handle_webhook_value(_inbound(_text("remember me")))
    await adapter.handle_webhook_value(_status("wamid.OUT3", "delivered"))
    await adapter.disconnect()

    again = _adapter(monkeypatch, graph)
    assert again._window_is_open(CUSTOMER)
    assert again.message_record("wamid.OUT3")["status"] == "delivered"


def test_an_old_window_is_not_reloaded(monkeypatch):
    graph = FakeGraph()
    adapter = _adapter(monkeypatch, graph)
    adapter._last_inbound[CUSTOMER] = time.time() - SESSION_WINDOW_SECONDS - 5
    adapter._save_state()
    assert not _adapter(monkeypatch, graph)._window_is_open(CUSTOMER)


# ── the Graph client ─────────────────────────────────────────────────────────


async def test_the_client_pages_templates_and_caches_them(monkeypatch):
    graph = FakeGraph()
    graph.templates = [POSITIONAL, NAMED]
    mg.invalidate_templates()
    client = graph.client()
    first = await client.list_templates()
    second = await client.list_templates()
    assert [t.name for t in first] == ["order_update", "welcome"] == [t.name for t in second]
    assert graph.paths().count(f"{WABA}/message_templates") == 1, "cached for a minute"
    await client.list_templates(fresh=True)
    assert graph.paths().count(f"{WABA}/message_templates") == 2


async def test_profile_verticals_travel_as_metas_enums():
    graph = FakeGraph()
    client = graph.client()
    await client.update_business_profile({"vertical": "Shopping/Retail", "about": "Birds"})
    body = json.loads(graph.requests[-1].content)
    assert body == {"messaging_product": "whatsapp", "vertical": "RETAIL", "about": "Birds"}
    profile = await client.business_profile()
    assert profile == {"about": "Birds", "vertical": "Shopping/Retail"}


def test_owner_numbers_are_admitted_even_when_the_allowlist_omits_them(monkeypatch) -> None:
    adapter = _adapter(
        monkeypatch,
        FakeGraph(),
        allowed_users=["+6590000001"],
        owner_users=["+65 9000 0099"],
    )
    assert adapter._is_allowed("+6590000001")
    assert adapter._is_allowed("+6590000099")
    assert not adapter._is_allowed("+6590000002")
