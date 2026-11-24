"""WhatsApp templates over Twilio: the Content API client, the adapter's
template sends, and the two toolkits that split who may do what.

What these pin, because each failure would be silent or unsafe:

* **No variable ever carries a line break.** WhatsApp refuses one after Twilio
  has accepted the message, so the send would read as delivered.
* **A missing variable is refused, never sent**, since Twilio would fill it with
  the template's sample text, to a real customer.
* **Meta's template rules are checked before a review is spent.**
* **The access split holds:** the send pair is implicit only with the
  connector and can be switched off; the admin toolkit is never implicit, no
  agent can grant it to itself, granting a member from chat needs an
  administrator, and every tool is owner-only.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from olano.config.agent_config import AgentConfig
from olano.platforms import twilio_content as tc
from olano.platforms.config import PlatformConfig
from olano.platforms.twilio_content import (
    Template,
    TwilioContentClient,
    TwilioContentError,
    clean_variable,
    describe_error_code,
    find_template,
    normalize_template_name,
    placeholders_in,
    template_from_api,
    template_problems,
)
from olano.platforms.whatsapp_twilio import WhatsAppTwilioAdapter
from olano.tools import whatsapp_business_tools as wa

ACCOUNT = "AC" + "0" * 32
TOKEN = "test-token"
SENDER = "whatsapp:+12025550111"
CUSTOMER = "whatsapp:+15551234567"
#: A free number from WhatsApp (area code 555), which Twilio does not support.
FREE_555 = "whatsapp:+15550001234"
CONVERSATION = "CH" + "1" * 32
SERVICE = "IS" + "2" * 32
SEND_TOOLS = (
    "whatsapp_template_list",
    "whatsapp_template_send",
    "whatsapp_message_status",
    "whatsapp_window_status",
)
ADMIN_TOOLS = (
    "whatsapp_template_create",
    "whatsapp_template_submit",
    "whatsapp_template_status",
    "whatsapp_template_delete",
    "whatsapp_sender_status",
    "whatsapp_profile_update",
    "twilio_account_balance",
    "twilio_usage_summary",
)


def _row(
    sid: str,
    name: str,
    body: str,
    *,
    status: str = "approved",
    category: str = "UTILITY",
    language: str = "en",
    samples: dict[str, str] | None = None,
    rejection: str = "",
) -> dict[str, Any]:
    return {
        "sid": sid,
        "friendly_name": name,
        "language": language,
        "variables": samples or {},
        "types": {"twilio/text": {"body": body}},
        "approval_requests": {
            "name": name,
            "category": category,
            "status": status,
            "rejection_reason": rejection,
        },
    }


ORDER_READY = _row(
    "HX" + "a" * 32,
    "order_ready",
    "Hi {{1}}, your order {{2}} is ready for pickup at our store.",
    samples={"1": "Maria", "2": "#1042"},
)
SPRING_SALE = _row(
    "HX" + "b" * 32,
    "spring_sale",
    "Our spring sale starts {{1}} with great deals in every store.",
    status="pending",
    category="MARKETING",
    samples={"1": "Monday"},
)
OLD_NOTICE = _row(
    "HX" + "c" * 32,
    "old_notice",
    "Your appointment on {{1}} is confirmed, see you then.",
    status="rejected",
    rejection="INVALID_FORMAT",
    samples={"1": "May 3"},
)


class _Twilio:
    """A small in-memory Content API that records every request."""

    def __init__(self, rows: list[dict[str, Any]] | None = None):
        self.rows = [json.loads(json.dumps(r)) for r in rows or []]
        self.requests: list[httpx.Request] = []
        # The account side: messaging.twilio.com senders, api.twilio.com balance.
        self.senders: list[dict[str, Any]] = []
        self.balance = {"balance": "12.29", "currency": "USD", "account_sid": ACCOUNT}
        self.usage_records: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        parts = path.split("/")
        if request.url.host == "messaging.twilio.com" and path == "/v2/Channels/Senders":
            assert request.url.params.get("Channel") == "whatsapp"
            return httpx.Response(200, json={"senders": self.senders, "meta": {}})
        if request.url.host == "messaging.twilio.com" and method == "POST":
            body = json.loads(request.content)
            for sender in self.senders:
                if sender["sid"] == parts[-1]:
                    sender["profile"] = body["profile"]
                    return httpx.Response(200, json=sender)
            return httpx.Response(404, json={"message": "Not found", "code": 20404})
        if request.url.host == "api.twilio.com" and "/Usage/Records/" in path:
            assert path.startswith(f"/2010-04-01/Accounts/{ACCOUNT}/Usage/Records/")
            return httpx.Response(
                200, json={"usage_records": self.usage_records, "next_page_uri": None}
            )
        if request.url.host == "api.twilio.com":
            assert path == f"/2010-04-01/Accounts/{ACCOUNT}/Balance.json"
            return httpx.Response(200, json=self.balance)
        if method == "GET" and path == "/v1/ContentAndApprovals":
            return httpx.Response(200, json={"contents": self.rows, "meta": {}})
        if method == "POST" and path == "/v1/Content":
            body = json.loads(request.content)
            row = {
                "sid": "HX" + "d" * 32,
                "friendly_name": body["friendly_name"],
                "language": body["language"],
                "variables": body["variables"],
                "types": body["types"],
            }
            self.rows.append({**row, "approval_requests": {"status": "unsubmitted"}})
            return httpx.Response(201, json=row)
        if method == "POST" and path.endswith("/ApprovalRequests/whatsapp"):
            body = json.loads(request.content)
            for row in self.rows:
                if row["sid"] == parts[3]:
                    row["approval_requests"] = {**body, "status": "received"}
            return httpx.Response(201, json={**body, "status": "received"})
        if method == "GET" and path.endswith("/ApprovalRequests"):
            row = next(r for r in self.rows if r["sid"] == parts[3])
            return httpx.Response(200, json={"sid": parts[3], "whatsapp": row["approval_requests"]})
        if method == "GET" and path.startswith("/v1/Content/"):
            found = next((r for r in self.rows if r["sid"] == parts[3]), None)
            if found is None:
                return httpx.Response(404, json={"message": "Not found", "code": 20404})
            return httpx.Response(
                200, json={k: v for k, v in found.items() if k != "approval_requests"}
            )
        if method == "DELETE" and path.startswith("/v1/Content/"):
            self.rows = [r for r in self.rows if r["sid"] != parts[3]]
            return httpx.Response(204)
        return httpx.Response(404, json={"message": f"unrouted {method} {path}"})

    def client(self) -> TwilioContentClient:
        return TwilioContentClient(ACCOUNT, TOKEN, transport=httpx.MockTransport(self.handler))

    def calls(self, method: str, fragment: str = "") -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and fragment in r.url.path]


@pytest.fixture(autouse=True)
def _fresh_cache():
    tc.invalidate_templates()
    yield
    tc.invalidate_templates()


# ── the rules, as pure functions ─────────────────────────────────────────────


def test_variables_lose_line_breaks_tabs_and_runs_of_spaces():
    assert (
        clean_variable("Line one\nLine two\r\n\tthree    four ") == "Line one Line two three four"
    )
    assert clean_variable(None) == ""


def test_placeholders_are_read_in_order_once_each():
    assert placeholders_in("Hi {{1}}, {{2}} and {{ 1 }} again {{3}}") == ["1", "2", "3"]


def test_a_well_formed_template_has_no_problems():
    body = "Hi {{1}}, your order {{2}} is ready for pickup at our store."
    assert template_problems(body, {"1": "Maria", "2": "#1042"}) == []


@pytest.mark.parametrize(
    "body,samples,needle",
    [
        ("Hi {{1}}, and then {{3}} arrives at the store today.", {"1": "a", "3": "b"}, "no gaps"),
        ("Hello friend, {{1}} {{2}} is what we have ready.", {"1": "a", "2": "b"}, "side by side"),
        ("{{1}} is ready for pickup at our Main Street store.", {"1": "Order"}, "starts with"),
        ("Your order is ready for pickup, dear {{1}}.", {"1": "Maria"}, "ends with"),
        ("Hi {{1}} and {{2}}", {"1": "a", "2": "b"}, "Too many placeholders"),
        ("Hi {{1}}, your order is ready for pickup today.", {}, "sample value for {{1}}"),
        ("Hi {{1}}, your order is ready for pickup today.", {"1": "Ma\nria"}, "line break"),
        (
            "Hi {{1}}, your order {{order}} is ready for pickup today.",
            {"1": "a", "order": "b"},
            "not both",
        ),
        ("Hi {{Name}}, your order is ready for pickup today.", {"Name": "x"}, "lowercase"),
        ("x" * 1025, {}, "1024"),
    ],
)
def test_each_meta_rule_is_named_when_broken(body, samples, needle):
    problems = template_problems(body, samples)
    assert any(needle in p for p in problems), problems


def test_named_placeholders_are_a_valid_template():
    """WhatsApp takes {{first_name}} as well as {{1}}; the first check refused it."""
    body = "Hi {{first_name}}, your order {{order_number}} is ready for pickup at our store."
    assert template_problems(body, {"first_name": "Maria", "order_number": "#1042"}) == []


def test_named_placeholders_longer_than_twilios_limit_are_refused():
    """Twilio caps a variable key at 16 characters on every channel."""
    body = "Hi {{customer_first_name}}, your order is ready for pickup at our store today."
    problems = template_problems(body, {"customer_first_name": "Maria"})
    assert any("at most 16 characters" in p and "customer_first_name" in p for p in problems)
    ok_body = "Hi {{carereceiver}}, it has been {{hours_ago}} hours since your last check in today."
    assert template_problems(ok_body, {"carereceiver": "Ann", "hours_ago": "12"}) == []


def test_63027_names_the_usual_cause_not_just_the_language():
    """The reported sends failed 63027 from the sandbox number and then from a
    free 555 number, never over a language."""
    text = describe_error_code(63027)
    assert "sandbox" in text and "555" in text and "WhatsApp Business Account" in text


@pytest.mark.parametrize(
    "value, expected",
    [
        ("whatsapp:+15550001234", True),
        ("+1 555 000 1234", True),
        ("15550001234", True),
        ("whatsapp:+14155550123", False),  # 555 as the exchange of a real area code
        ("whatsapp:+14155238886", False),  # the sandbox
        ("whatsapp:+5551234567", False),  # Brazil, +55
        ("whatsapp:+155500012345", False),  # too long for a US number
        ("", False),
    ],
)
def test_free_555_numbers_are_recognized_by_their_area_code(value, expected):
    assert tc.is_whatsapp_provided_number(value) is expected


def test_categories_this_tool_cannot_create_are_refused():
    body = "Your code arrives shortly, thanks for waiting."
    assert any("one-time-code" in p for p in template_problems(body, {}, category="AUTHENTICATION"))
    assert any("UTILITY" in p for p in template_problems(body, {}, category="PROMO"))
    assert template_problems(body, {}, category="MARKETING") == []


def test_template_names_are_meta_shaped():
    assert normalize_template_name("Order Ready!  v2") == "order_ready_v2"


def test_both_approval_shapes_are_read():
    flat = template_from_api(ORDER_READY)
    assert flat.approved and flat.whatsapp_name == "order_ready" and flat.category == "UTILITY"
    keyed = template_from_api(
        {**ORDER_READY, "approval_requests": {"whatsapp": {"name": "x", "status": "Pending"}}}
    )
    assert keyed.status == "pending" and keyed.whatsapp_name == "x"


def test_templates_resolve_by_sid_or_name_and_refuse_ambiguity():
    templates = [template_from_api(r) for r in (ORDER_READY, SPRING_SALE)]
    assert find_template(templates, ORDER_READY["sid"]).name == "order_ready"
    assert find_template(templates, "ORDER_READY").sid == ORDER_READY["sid"]
    spanish = template_from_api({**ORDER_READY, "sid": "HX" + "f" * 32, "language": "es"})
    both = [template_from_api(ORDER_READY), spanish]
    assert find_template(both, "order_ready") is None, "two approved languages: ask which"


def test_delivery_error_codes_read_as_sentences():
    text = describe_error_code(63015)
    assert "joined" in text and "https://www.twilio.com/docs/api/errors/63015" in text
    assert describe_error_code("99999").startswith("Twilio error 99999")


# ── the client ───────────────────────────────────────────────────────────────


async def test_the_list_is_cached_briefly_and_refetched_on_demand():
    twilio = _Twilio([ORDER_READY])
    client = twilio.client()
    assert [t.name for t in await client.list_templates()] == ["order_ready"]
    await client.list_templates()
    assert len(twilio.calls("GET", "ContentAndApprovals")) == 1, "second read is cached"
    await client.list_templates(fresh=True)
    assert len(twilio.calls("GET", "ContentAndApprovals")) == 2


async def test_create_submit_and_delete_speak_the_content_api():
    twilio = _Twilio()
    client = twilio.client()
    created = await client.create_template(
        friendly_name="order_ready",
        language="en",
        body="Hi {{1}}, your order is ready for pickup today.",
        samples={"1": "Maria"},
    )
    posted = json.loads(twilio.calls("POST", "/v1/Content")[0].content)
    assert posted["types"] == {
        "twilio/text": {"body": "Hi {{1}}, your order is ready for pickup today."}
    }
    assert posted["variables"] == {"1": "Maria"}

    approval = await client.submit_for_whatsapp(created.sid, name="order_ready", category="UTILITY")
    assert approval["status"] == "received"
    assert (await client.get_template(created.sid)).status == "received"

    await client.delete_template(created.sid)
    assert twilio.calls("DELETE", created.sid)
    with pytest.raises(TwilioContentError) as err:
        await client.get_template(created.sid)
    assert err.value.status == 404 and "Not found (error 20404)" in str(err.value)


# ── the adapter ──────────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, status: int = 200, payload: Any = None):
        self.status = status
        self._payload = payload if payload is not None else {}

    async def json(self) -> Any:
        return self._payload

    async def text(self) -> str:
        return json.dumps(self._payload)

    async def __aenter__(self) -> _Resp:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _Session:
    def __init__(self, route):
        self.calls: list[tuple[str, str, dict]] = []
        self._route = route
        self.closed = False

    def _call(self, method: str, url: str, kw: dict) -> _Resp:
        self.calls.append((method, url, kw))
        return self._route(method, url, kw)

    def get(self, url: str, **kw: Any) -> _Resp:
        return self._call("GET", url, kw)

    def post(self, url: str, **kw: Any) -> _Resp:
        return self._call("POST", url, kw)

    def delete(self, url: str, **kw: Any) -> _Resp:
        return self._call("DELETE", url, kw)

    def messages(self) -> list[dict]:
        return [kw["data"] for m, u, kw in self.calls if m == "POST" and u.endswith("/Messages")]


def _conversations_route(*, receipt: dict | None = None, sender: str = SENDER):
    def route(method: str, url: str, kw: dict) -> _Resp:
        if method == "GET" and url.endswith("/ParticipantConversations"):
            binding = {"proxy_address": sender}
            return _Resp(
                200,
                {
                    "conversations": [
                        {
                            "conversation_sid": CONVERSATION,
                            "chat_service_sid": SERVICE,
                            "participant_messaging_binding": binding,
                        }
                    ]
                },
            )
        if method == "POST" and url.endswith("/Messages"):
            return _Resp(201, {"sid": "IM" + "9" * 32})
        if method == "GET" and url.endswith("/Receipts"):
            return _Resp(200, {"delivery_receipts": [receipt] if receipt else []})
        return _Resp(200, {})

    return route


def _adapter(monkeypatch, extra: dict[str, Any] | None = None, route=None) -> WhatsAppTwilioAdapter:
    for name in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_TEMPLATE_SID"):
        monkeypatch.delenv(name, raising=False)
    cfg = {"account_sid": ACCOUNT, "auth_token": TOKEN, "from_number": SENDER, **(extra or {})}
    adapter = WhatsAppTwilioAdapter(PlatformConfig(enabled=True, extra=cfg), agent_name="shop")
    adapter._http_session = _Session(route or _conversations_route())  # type: ignore[assignment]
    adapter._running = True
    return adapter


async def test_a_template_send_fills_every_variable_with_clean_text(monkeypatch):
    adapter = _adapter(monkeypatch)
    template = template_from_api(ORDER_READY)

    result = await adapter.send_template(
        CUSTOMER, template, {"1": "Maria\nGarcia", "2": "#1042", "9": "ignored"}
    )

    assert result.success is True
    (sent,) = adapter._http_session.messages()  # type: ignore[union-attr]
    assert sent["ContentSid"] == template.sid
    assert json.loads(sent["ContentVariables"]) == {"1": "Maria Garcia", "2": "#1042"}
    assert "Body" not in sent
    assert result.raw_response["conversation_sid"] == CONVERSATION
    assert result.raw_response["rendered"].startswith("Hi Maria Garcia, your order #1042")


async def test_a_missing_variable_is_refused_not_sent_with_sample_text(monkeypatch):
    adapter = _adapter(monkeypatch)
    result = await adapter.send_template(CUSTOMER, template_from_api(ORDER_READY), {"1": "Maria"})
    assert result.success is False
    assert "{{2}}" in (result.error or "")
    assert adapter._http_session.messages() == []  # type: ignore[union-attr]


async def test_an_unapproved_template_is_never_sent(monkeypatch):
    adapter = _adapter(monkeypatch)
    result = await adapter.send_template(CUSTOMER, template_from_api(SPRING_SALE), {"1": "Mon"})
    assert result.success is False and "pending" in (result.error or "")
    assert adapter._http_session.messages() == []  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "receipt,expected",
    [
        ({"status": "failed", "error_code": 63015}, ("failed", "63015")),
        ({"status": "delivered"}, ("delivered", "")),
        (None, ("", "")),
    ],
)
async def test_delivery_receipts_report_whatsapps_verdict(monkeypatch, receipt, expected):
    adapter = _adapter(monkeypatch, route=_conversations_route(receipt=receipt))
    status, detail = await adapter.delivery_receipt(CONVERSATION, "IM1")
    assert status == expected[0]
    assert expected[1] in detail


# ── the send toolkit ─────────────────────────────────────────────────────────


def _as_agent(monkeypatch, adapter: Any = None, twilio: _Twilio | None = None) -> None:
    monkeypatch.setattr(wa, "whatsapp_adapter", lambda agent="": adapter)
    if twilio is not None:
        monkeypatch.setattr(wa, "content_client", lambda: twilio.client())
        if adapter is not None:
            monkeypatch.setattr(adapter, "content_client", lambda: twilio.client())
    monkeypatch.setattr(wa, "RECEIPT_POLL_SECONDS", 0.0)


async def test_the_list_shows_only_what_can_be_sent(monkeypatch):
    _as_agent(monkeypatch, twilio=_Twilio([ORDER_READY, SPRING_SALE, OLD_NOTICE]))
    out = json.loads(await wa.whatsapp_template_list.ainvoke({}))
    assert [row["name"] for row in out["approved_templates"]] == ["order_ready"]
    assert out["approved_templates"][0]["variables"] == {"1": "Maria", "2": "#1042"}

    none = json.loads(await wa.whatsapp_template_list.ainvoke({"search": "sale"}))
    assert none["count"] == 0 and "still in WhatsApp's review" in none["note"]


async def test_the_list_says_what_is_missing_without_an_account(monkeypatch):
    monkeypatch.setattr(wa, "content_client", lambda: None)
    out = json.loads(await wa.whatsapp_template_list.ainvoke({}))
    assert "TWILIO_ACCOUNT_SID" in out["error"]


async def test_send_reports_delivery(monkeypatch):
    adapter = _adapter(monkeypatch, route=_conversations_route(receipt={"status": "delivered"}))
    _as_agent(monkeypatch, adapter, _Twilio([ORDER_READY]))

    out = json.loads(
        await wa.whatsapp_template_send.ainvoke(
            {
                "to": "+1 555 123 4567",
                "template": "order_ready",
                "variables": {"1": "Maria", "2": "#7"},
            }
        )
    )

    assert out["sent"] is True and out["status"] == "delivered"
    assert out["text"] == "Hi Maria, your order #7 is ready for pickup at our store."


async def test_send_reports_whatsapps_refusal_instead_of_success(monkeypatch):
    receipt = {"status": "failed", "error_code": 63015}
    adapter = _adapter(monkeypatch, route=_conversations_route(receipt=receipt))
    _as_agent(monkeypatch, adapter, _Twilio([ORDER_READY]))

    out = json.loads(
        await wa.whatsapp_template_send.ainvoke(
            {"to": CUSTOMER, "template": "order_ready", "variables": {"1": "M", "2": "#7"}}
        )
    )

    assert "did not deliver" in out["error"] and "63015" in out["error"]


async def test_send_refuses_pending_unknown_and_connectorless(monkeypatch):
    adapter = _adapter(monkeypatch)
    _as_agent(monkeypatch, adapter, _Twilio([ORDER_READY, SPRING_SALE]))
    pending = json.loads(
        await wa.whatsapp_template_send.ainvoke(
            {"to": CUSTOMER, "template": "spring_sale", "variables": {"1": "Mon"}}
        )
    )
    assert "pending" in pending["error"]
    unknown = json.loads(
        await wa.whatsapp_template_send.ainvoke({"to": CUSTOMER, "template": "nope"})
    )
    assert "No template named" in unknown["error"]
    assert adapter._http_session.messages() == []  # type: ignore[union-attr]

    adapter._running = False
    stopped = json.loads(
        await wa.whatsapp_template_send.ainvoke(
            {"to": CUSTOMER, "template": "order_ready", "variables": {"1": "M", "2": "#7"}}
        )
    )
    assert "not running" in stopped["error"]

    _as_agent(monkeypatch, None, _Twilio([ORDER_READY]))
    none = json.loads(
        await wa.whatsapp_template_send.ainvoke({"to": CUSTOMER, "template": "order_ready"})
    )
    assert "no WhatsApp business connector" in none["error"]


CARE_ALERT = _row(
    "HX" + "d" * 32,
    "carereceiver_alert",
    "Hi {{carereceiver}}, there has been no movement for {{hours_ago}} hours. We are here "
    "if you need us.",
    category="MARKETING",
    samples={"carereceiver": "Irene", "hours_ago": "12"},
)


async def test_named_placeholders_take_named_values(monkeypatch):
    """The reported template is written with {{carereceiver}}, not {{1}}.

    The tool's own description said its values are "keyed by its number", so the
    model's first try was {"1": "Sam", "2": "24"}, refused for missing values.
    """
    adapter = _adapter(monkeypatch, route=_conversations_route(receipt={"status": "delivered"}))
    _as_agent(monkeypatch, adapter, _Twilio([CARE_ALERT]))
    assert "first_name" in json.dumps(wa.whatsapp_template_send.args)

    numbered = json.loads(
        await wa.whatsapp_template_send.ainvoke(
            {"to": CUSTOMER, "template": "carereceiver_alert", "variables": {"1": "A", "2": "2"}}
        )
    )
    assert "{{carereceiver}}" in numbered["error"]
    assert adapter._http_session.messages() == []  # type: ignore[union-attr]

    out = json.loads(
        await wa.whatsapp_template_send.ainvoke(
            {
                "to": CUSTOMER,
                "template": "carereceiver_alert",
                "variables": {"carereceiver": "Sam", "hours_ago": "24"},
            }
        )
    )
    assert out["sent"] is True
    (sent,) = adapter._http_session.messages()  # type: ignore[union-attr]
    assert json.loads(sent["ContentVariables"]) == {"carereceiver": "Sam", "hours_ago": "24"}
    assert out["text"].startswith("Hi Sam, there has been no movement for 24 hours.")


async def test_the_sandbox_number_never_tries_an_account_template(monkeypatch):
    """The reported 63027: Twilio's shared sandbox sends only Twilio's own
    sample templates, so an account template from it fails after Twilio has
    accepted it. The tools say so up front instead."""
    adapter = _adapter(
        monkeypatch,
        {"from_number": "+14155238886"},
        route=_conversations_route(receipt={"status": "delivered"}),
    )
    assert adapter.is_sandbox
    _as_agent(monkeypatch, adapter, _Twilio([ORDER_READY]))

    listed = json.loads(await wa.whatsapp_template_list.ainvoke({}))
    assert listed["count"] == 1, "still listed: the person can see what the account has"
    assert "sandbox" in listed["cannot_send_from_this_number"]

    out = json.loads(
        await wa.whatsapp_template_send.ainvoke(
            {"to": CUSTOMER, "template": "order_ready", "variables": {"1": "M", "2": "#7"}}
        )
    )
    assert "sandbox" in out["error"] and "63027" in out["error"]
    assert "WhatsApp senders" in out["agent_guidance"]
    assert adapter._http_session.messages() == []  # type: ignore[union-attr]


async def test_an_own_number_gets_no_sandbox_note(monkeypatch):
    _as_agent(monkeypatch, _adapter(monkeypatch), _Twilio([ORDER_READY]))
    listed = json.loads(await wa.whatsapp_template_list.ainvoke({}))
    assert "cannot_send_from_this_number" not in listed


# ── the admin toolkit ────────────────────────────────────────────────────────


async def test_create_refuses_a_draft_meta_would_reject_without_calling_twilio(monkeypatch):
    twilio = _Twilio()
    _as_agent(monkeypatch, twilio=twilio)
    out = json.loads(
        await wa.whatsapp_template_create.ainvoke(
            {"name": "promo", "body": "{{1}} {{2}}", "category": "MARKETING"}
        )
    )
    assert "was not created" in out["error"] and len(out["problems"]) >= 3
    assert twilio.requests == []


async def test_create_submits_for_approval(monkeypatch):
    twilio = _Twilio()
    _as_agent(monkeypatch, twilio=twilio)
    out = json.loads(
        await wa.whatsapp_template_create.ainvoke(
            {
                "name": "Order Ready",
                "body": "Hi {{1}}, your order {{2}} is ready for pickup at our store.",
                "samples": {"1": "Maria", "2": "#1042"},
            }
        )
    )
    assert out["submitted"] is True and out["status"] == "received"
    assert out["created"]["name"] == "order_ready"
    submitted = json.loads(twilio.calls("POST", "ApprovalRequests/whatsapp")[0].content)
    assert submitted == {"name": "order_ready", "category": "UTILITY"}


async def test_create_refuses_a_name_already_taken(monkeypatch):
    twilio = _Twilio([ORDER_READY])
    _as_agent(monkeypatch, twilio=twilio)
    out = json.loads(
        await wa.whatsapp_template_create.ainvoke(
            {
                "name": "order_ready",
                "body": "Hi {{1}}, your parcel {{2}} is waiting at the front desk.",
                "samples": {"1": "Maria", "2": "#1"},
            }
        )
    )
    assert "already exists" in out["error"]
    assert twilio.calls("POST") == []


async def test_create_without_submit_then_submit(monkeypatch):
    twilio = _Twilio()
    _as_agent(monkeypatch, twilio=twilio)
    created = json.loads(
        await wa.whatsapp_template_create.ainvoke(
            {
                "name": "pickup_reminder",
                "body": "Hi {{1}}, a reminder that your order is ready for pickup.",
                "samples": {"1": "Maria"},
                "submit": False,
            }
        )
    )
    assert created["submitted"] is False
    out = json.loads(await wa.whatsapp_template_submit.ainvoke({"template": "pickup_reminder"}))
    assert out["submitted"] is True and out["whatsapp_name"] == "pickup_reminder"
    again = json.loads(await wa.whatsapp_template_submit.ainvoke({"template": "pickup_reminder"}))
    assert "already submitted" in again["error"]


async def test_a_rejected_template_points_at_a_new_copy(monkeypatch):
    _as_agent(monkeypatch, twilio=_Twilio([OLD_NOTICE]))
    out = json.loads(await wa.whatsapp_template_submit.ainvoke({"template": "old_notice"}))
    assert "create a corrected copy" in out["agent_guidance"]


async def test_status_lists_every_state_with_reasons(monkeypatch):
    _as_agent(monkeypatch, twilio=_Twilio([ORDER_READY, SPRING_SALE, OLD_NOTICE]))
    out = json.loads(await wa.whatsapp_template_status.ainvoke({}))
    assert out["count_by_status"] == {"approved": 1, "pending": 1, "rejected": 1}
    rejected = next(t for t in out["templates"] if t["name"] == "old_notice")
    assert rejected["rejection_reason"] == "INVALID_FORMAT"
    one = json.loads(await wa.whatsapp_template_status.ainvoke({"template": "spring_sale"}))
    assert one["template"]["status"] == "pending"


async def test_delete_needs_the_name_typed_back(monkeypatch):
    twilio = _Twilio([ORDER_READY])
    _as_agent(monkeypatch, twilio=twilio)
    refused = json.loads(
        await wa.whatsapp_template_delete.ainvoke(
            {"template": "order_ready", "confirm_name": "yes"}
        )
    )
    assert "Not deleted" in refused["error"] and twilio.calls("DELETE") == []
    done = json.loads(
        await wa.whatsapp_template_delete.ainvoke(
            {"template": "order_ready", "confirm_name": "order_ready"}
        )
    )
    assert done["deleted"] is True and twilio.rows == []


async def test_rejected_credentials_say_so(monkeypatch):
    class _Refusing(_Twilio):
        def handler(self, request):
            return httpx.Response(401, json={"message": "Authenticate", "code": 20003})

    _as_agent(monkeypatch, twilio=_Refusing())
    out = json.loads(await wa.whatsapp_template_status.ainvoke({}))
    assert "refused this account's credentials" in out["error"]


def test_the_account_comes_from_the_connector_else_the_vault(monkeypatch):
    class _Adapter:
        _account_sid = "AC-connector"
        _auth_token = "tok-connector"

    monkeypatch.setattr(wa, "whatsapp_adapter", lambda agent="": _Adapter())
    assert wa.twilio_account() == ("AC-connector", "tok-connector")

    monkeypatch.setattr(wa, "whatsapp_adapter", lambda agent="": None)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-vault")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok-vault")
    assert wa.twilio_account() == ("AC-vault", "tok-vault")


# ── who may hold and use them ────────────────────────────────────────────────


def _tool(name: str) -> Any:
    module = wa
    return module.get_tool(name)


@pytest.mark.parametrize("name", SEND_TOOLS + ADMIN_TOOLS)
def test_every_template_tool_is_owner_only(name):
    """A customer writing to the agent can never drive any of them."""
    from olano.security.pipeline import evaluate_tool_access
    from olano.security.request_context import push_request_context
    from olano.security.tool_metadata import get_tool_metadata

    meta = get_tool_metadata(_tool(name))
    assert meta.owner_only is True
    cfg = AgentConfig(name="shop", model="fake:instant")
    with push_request_context(source="channel", channel="whatsapp_twilio", is_owner=False):
        denied = evaluate_tool_access(name, meta, cfg)
    assert denied.allowed is False and denied.layer == 5
    with push_request_context(source="api", channel="api", is_owner=True, is_admin=True):
        assert evaluate_tool_access(name, meta, cfg).allowed is True


def test_writes_ask_for_approval_under_the_stricter_profiles():
    from olano.security.hitl_policy import tool_category

    for name in (
        "whatsapp_template_send",
        "whatsapp_template_create",
        "whatsapp_template_submit",
        "whatsapp_template_delete",
    ):
        assert tool_category(name) == "write_external", name
    for name in ("whatsapp_template_list", "whatsapp_template_status"):
        assert tool_category(name) is None, name


@pytest.mark.parametrize("toolkit", ["whatsapp_business"])
def test_no_agent_can_add_either_toolkit_by_name(toolkit):
    from olano.admin.integrations import expand_toolkit, toolkit_members

    with pytest.raises(ValueError, match="cannot be granted"):
        expand_toolkit(toolkit)
    assert toolkit_members()[toolkit], "the static index knows the members"


def test_the_two_toolkits_hold_exactly_their_own_tools():
    from olano.admin.integrations import toolkit_members

    members = toolkit_members()
    assert members["whatsapp_business"] == sorted(SEND_TOOLS + ADMIN_TOOLS)
    # The catalog is what splits one toolkit into the two kinds: every member
    # is either bound with the connector or operator-granted, never neither.
    from olano.config.internal_tools import WHATSAPP_BUSINESS_TOOL_NAMES

    assert set(WHATSAPP_BUSINESS_TOOL_NAMES) == set(SEND_TOOLS)


def test_granting_an_admin_tool_from_chat_needs_an_administrator(tmp_path, monkeypatch):
    from olano.admin.integrations import operator_granted_tool_names, privileged_tool_names
    from olano.security.request_context import push_request_context
    from olano.tools import olano_admin

    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    assert operator_granted_tool_names() == frozenset(ADMIN_TOOLS)
    # Not "fleet-wide": the Tools tab badge and the doctor audit stay olano_admin's.
    assert not (operator_granted_tool_names() & privileged_tool_names())

    with push_request_context(source="peer", channel="peer"):
        denial = olano_admin._privileged_tools_denial(
            "shop", {"tools": ["whatsapp_template_send", "whatsapp_template_create"]}
        )
        assert (
            olano_admin._privileged_tools_denial("shop", {"tools": ["whatsapp_template_send"]})
            is None
        ), "the send side is not operator-gated"
    payload = json.loads(denial)
    assert payload["refused_tools"] == ["whatsapp_template_create"]
    assert "Twilio" in payload["why"]


def test_the_send_side_is_implicit_only_with_the_connector():
    from olano.config.internal_tools import (
        available_whatsapp_business_tool_names,
        effective_internal_tools,
    )
    from olano.config.ops_defaults import resolve_effective_tool_names

    plain = AgentConfig(name="plain", model="fake:instant")
    assert available_whatsapp_business_tool_names(plain) == []
    rows = {r["name"]: r for r in effective_internal_tools(plain)}
    assert rows["whatsapp_template_send"]["on"] is False
    assert "WhatsApp (Twilio Business API)" in rows["whatsapp_template_send"]["note"]

    shop = AgentConfig(
        name="shop",
        model="fake:instant",
        channels=[{"type": "platform:whatsapp_twilio", "extra": {"from_number": "+12025550111"}}],
    )
    assert available_whatsapp_business_tool_names(shop) == list(SEND_TOOLS)
    effective = set(resolve_effective_tool_names(shop.tools, shop.disabled_tools, shop))
    assert set(SEND_TOOLS) <= effective
    assert not (set(ADMIN_TOOLS) & effective), "the admin side is never implicit"

    # Switched off per agent (the Tools tab writes disabled_tools).
    off = AgentConfig.model_validate(
        {**shop.model_dump(), "disabled_tools": ["whatsapp_template_send"]}
    )
    effective = set(resolve_effective_tool_names(off.tools, off.disabled_tools, off))
    assert "whatsapp_template_send" not in effective and "whatsapp_template_list" in effective
    none = AgentConfig.model_validate({**shop.model_dump(), "default_internal_tools": False})
    assert available_whatsapp_business_tool_names(none) == []


def test_an_admin_agent_gets_the_admin_tools_only_by_name():
    from olano.config.ops_defaults import resolve_effective_tool_names
    from olano.tools.registry import resolve_tools

    admin = AgentConfig(name="twilio_admin", model="fake:instant", tools=list(ADMIN_TOOLS))
    effective = set(resolve_effective_tool_names(admin.tools, admin.disabled_tools, admin))
    assert set(ADMIN_TOOLS) <= effective
    assert not (set(SEND_TOOLS) & effective), "no connector, so no implicit send side"
    assert {t.name for t in resolve_tools(list(ADMIN_TOOLS))} == set(ADMIN_TOOLS)


def test_template_is_a_frozen_value():
    template = Template(sid="HX1", body="Hi {{1}}, thanks for your order today.")
    assert template.placeholders == ["1"] and template.name == "HX1"


# ── buttons, message status, the account ─────────────────────────────────────

ORDER_TRACK = _row(
    "HX" + "e" * 32,
    "order_track",
    "Hi {{1}}, your order has shipped and is on its way to you.",
    samples={"1": "Maria", "2": "A1B2"},
)
ORDER_TRACK["types"] = {
    "twilio/call-to-action": {
        "body": "Hi {{1}}, your order has shipped and is on its way to you.",
        "actions": [
            {"type": "URL", "title": "Track order", "url": "https://shop.example/t/{{2}}"},
            {"type": "PHONE_NUMBER", "title": "Call us", "phone": "+15551234567"},
        ],
    }
}
CONFIRM = _row("HX" + "f" * 32, "confirm_visit", "Your visit is booked for tomorrow at noon.")
CONFIRM["types"] = {
    "twilio/quick-reply": {
        "body": "Your visit is booked for tomorrow at noon.",
        "actions": [
            {"title": "Confirm", "id": "confirm"},
            {"title": "Reschedule", "id": "resched"},
        ],
    }
}


def test_buttons_are_read_and_their_placeholders_count():
    """A placeholder in a website button needs a value like one in the body:
    unfilled, Twilio would send the template's sample to a real customer."""
    track = template_from_api(ORDER_TRACK)
    assert track.content_type == "twilio/call-to-action"
    assert track.placeholders == ["1", "2"]
    assert track.describe()["buttons"] == [
        {"type": "url", "title": "Track order", "url": "https://shop.example/t/{{2}}"},
        {"type": "phone", "title": "Call us", "phone": "+15551234567"},
    ]
    confirm = template_from_api(CONFIRM)
    assert [b.title for b in confirm.buttons] == ["Confirm", "Reschedule"]
    assert confirm.placeholders == []


@pytest.mark.parametrize(
    "buttons,needle",
    [
        ([{"type": "reply", "title": "x" * 21}], "at most 20"),
        ([{"type": "url", "title": "Go", "url": "http://shop.example"}], "https://"),
        ([{"type": "url", "title": "Go", "url": "https://shop.example/{{1}}/x"}], "very end"),
        ([{"type": "phone", "title": "Call", "phone": "555-1234"}], "international format"),
        (
            [
                {"type": "reply", "title": "Yes"},
                {"type": "url", "title": "Go", "url": "https://a.b"},
            ],
            "different templates",
        ),
        ([{"type": "reply", "title": f"Option {n}"} for n in range(11)], "At most 10"),
        (
            [{"type": "url", "title": f"Site {n}", "url": "https://a.b"} for n in range(3)],
            "At most 2",
        ),
        ([{"type": "reply", "title": "Yes"}, {"type": "reply", "title": "yes"}], "share a title"),
        ([{"type": "carousel", "title": "Hm"}], "reply, url or phone"),
    ],
)
def test_each_button_rule_is_named_when_broken(buttons, needle):
    _, problems = tc.parse_buttons(buttons)
    assert any(needle in p for p in problems), problems


def test_button_content_types_match_twilios_documented_shapes():
    replies, _ = tc.parse_buttons([{"type": "reply", "title": "Yes, confirm"}])
    assert tc.content_types_for("Body", replies) == {
        "twilio/quick-reply": {
            "body": "Body",
            "actions": [{"title": "Yes, confirm", "id": "yes_confirm"}],
        }
    }
    links, _ = tc.parse_buttons(
        [
            {"type": "url", "title": "Track", "url": "https://a.b/{{1}}"},
            {"type": "phone", "title": "Call", "phone": "+1 (555) 123-4567"},
        ]
    )
    assert tc.content_types_for("Body", links) == {
        "twilio/call-to-action": {
            "body": "Body",
            "actions": [
                {"type": "URL", "title": "Track", "url": "https://a.b/{{1}}"},
                {"type": "PHONE_NUMBER", "title": "Call", "phone": "+15551234567"},
            ],
        }
    }
    assert tc.content_types_for("Body", []) == {"twilio/text": {"body": "Body"}}


def test_a_website_button_template_has_the_shorter_body_limit_and_needs_its_samples():
    links, _ = tc.parse_buttons([{"type": "url", "title": "Track", "url": "https://a.b/{{2}}"}])
    body = "Hi {{1}}, your order has shipped and is on its way to you today."
    problems = template_problems(body, {"1": "Maria"}, buttons=links)
    assert any("sample value for {{2}}" in p for p in problems)
    assert template_problems(body, {"1": "Maria", "2": "A1"}, buttons=links) == []
    long_body = "Your order is ready. " * 40
    assert any("640" in p for p in template_problems(long_body, {}, buttons=links))
    assert template_problems(long_body.strip()[:1000], {}) == []


async def test_create_with_reply_buttons(monkeypatch):
    twilio = _Twilio()
    _as_agent(monkeypatch, twilio=twilio)
    out = json.loads(
        await wa.whatsapp_template_create.ainvoke(
            {
                "name": "visit_confirm",
                "body": "Hi {{1}}, your visit is booked for tomorrow at noon.",
                "samples": {"1": "Maria"},
                "buttons": [
                    {"type": "reply", "title": "Confirm"},
                    {"type": "reply", "title": "Reschedule"},
                ],
            }
        )
    )
    assert out["created"]["buttons"] == [
        {"type": "reply", "title": "Confirm"},
        {"type": "reply", "title": "Reschedule"},
    ]
    (create,) = [r for r in twilio.calls("POST") if r.url.path == "/v1/Content"]
    types = json.loads(create.content)["types"]
    assert list(types) == ["twilio/quick-reply"]


async def test_create_refuses_broken_buttons_without_calling_twilio(monkeypatch):
    twilio = _Twilio()
    _as_agent(monkeypatch, twilio=twilio)
    out = json.loads(
        await wa.whatsapp_template_create.ainvoke(
            {
                "name": "track",
                "body": "Hi {{1}}, your order has shipped and is on its way to you.",
                "samples": {"1": "Maria"},
                "buttons": [{"type": "url", "title": "Track", "url": "http://shop.example"}],
            }
        )
    )
    assert "was not created" in out["error"] and any("https://" in p for p in out["problems"])
    assert twilio.requests == []


async def test_sending_a_template_with_a_website_button_fills_its_placeholder(monkeypatch):
    adapter = _adapter(monkeypatch, route=_conversations_route(receipt={"status": "delivered"}))
    _as_agent(monkeypatch, adapter, _Twilio([ORDER_TRACK]))

    short = json.loads(
        await wa.whatsapp_template_send.ainvoke(
            {"to": CUSTOMER, "template": "order_track", "variables": {"1": "Maria"}}
        )
    )
    assert "{{2}}" in short["error"] and "Track order" in short["error"]

    out = json.loads(
        await wa.whatsapp_template_send.ainvoke(
            {"to": CUSTOMER, "template": "order_track", "variables": {"1": "Maria", "2": "Z9"}}
        )
    )
    assert out["sent"] is True
    (sent,) = adapter._http_session.messages()  # type: ignore[union-attr]
    assert json.loads(sent["ContentVariables"]) == {"1": "Maria", "2": "Z9"}
    assert out["text"].endswith("[Track order] [Call us]")


@pytest.mark.parametrize(
    "receipt,expected",
    [
        ({"status": "read"}, ("read", "opened")),
        ({"status": "delivered"}, ("delivered", "phone")),
        (None, ("unknown", "not reported")),
    ],
)
async def test_message_status_reports_what_whatsapp_said(monkeypatch, receipt, expected):
    adapter = _adapter(monkeypatch, route=_conversations_route(receipt=receipt))
    _as_agent(monkeypatch, adapter, _Twilio())
    out = json.loads(
        await wa.whatsapp_message_status.ainvoke({"to": CUSTOMER, "message_sid": "IM1"})
    )
    assert out["status"] == expected[0] and expected[1] in out["status_means"]


async def test_message_status_explains_a_failure_and_never_opens_a_conversation(monkeypatch):
    receipt = {"status": "failed", "error_code": 63024}
    adapter = _adapter(monkeypatch, route=_conversations_route(receipt=receipt))
    _as_agent(monkeypatch, adapter, _Twilio())
    failed = json.loads(
        await wa.whatsapp_message_status.ainvoke({"to": CUSTOMER, "message_sid": "IM1"})
    )
    assert "did not deliver" in failed["error"] and "63024" in failed["error"]

    def no_conversation(method, url, kw):
        if url.endswith("/ParticipantConversations"):
            return _Resp(200, {"conversations": []})
        raise AssertionError(f"nothing else may be called: {method} {url}")

    lonely = _adapter(monkeypatch, route=no_conversation)
    _as_agent(monkeypatch, lonely, _Twilio())
    out = json.loads(
        await wa.whatsapp_message_status.ainvoke({"to": CUSTOMER, "message_sid": "IM1"})
    )
    assert out["status"] == "unknown" and "no conversation" in out["status_means"]


async def test_sender_status_lists_the_numbers_and_flags_the_sandbox(monkeypatch):
    twilio = _Twilio()
    twilio.senders = [
        {
            "sid": "XE" + "1" * 32,
            "sender_id": SENDER,
            "status": "ONLINE",
            "profile": {"name": "Birdhouse"},
            "properties": {"quality_rating": "HIGH", "messaging_limit": "TIER_1K"},
        },
        {
            "sid": "XE" + "2" * 32,
            "sender_id": "whatsapp:+12025550222",
            "status": "OFFLINE",
            "offline_reasons": [{"code": 30503, "message": "Display name rejected"}],
        },
    ]
    adapter = _adapter(monkeypatch)
    _as_agent(monkeypatch, adapter, twilio)
    out = json.loads(await wa.whatsapp_sender_status.ainvoke({}))
    online, offline = out["senders"]
    assert online["status"] == "ONLINE" and online["messaging_limit"] == "TIER_1K"
    assert online["display_name"] == "Birdhouse" and "active" in online["status_means"]
    assert offline["offline_reasons"] == ["Display name rejected"]
    assert out["this_agent_sends_from"] == SENDER and "note" not in out

    sandbox = _adapter(monkeypatch, {"from_number": "+14155238886"})
    _as_agent(monkeypatch, sandbox, twilio)
    out = json.loads(await wa.whatsapp_sender_status.ainvoke({}))
    assert "sandbox" in out["note"]

    _as_agent(monkeypatch, None, _Twilio())
    none = json.loads(await wa.whatsapp_sender_status.ainvoke({}))
    assert none["count"] == 0 and "No WhatsApp number is registered" in none["note"]


async def test_sender_status_flags_a_free_555_number_that_reads_online(monkeypatch):
    """The reported account: its only sender a free 555 number from WhatsApp,
    ONLINE on the Business Account, and unsupported by Twilio. The row and the
    note say so, or the person reads the row as healthy."""
    twilio = _Twilio()
    twilio.senders = [
        {
            "sid": "XE" + "1" * 32,
            "sender_id": FREE_555,
            "status": "ONLINE",
            "configuration": {"waba_id": "1234567890"},
        }
    ]
    _as_agent(monkeypatch, _adapter(monkeypatch, {"from_number": FREE_555}), twilio)
    out = json.loads(await wa.whatsapp_sender_status.ainvoke({}))
    (row,) = out["senders"]
    assert row["status"] == "ONLINE"
    assert "Twilio does not support these numbers" in row["problem"]
    assert "Twilio does not support these numbers" in out["note"]

    twilio.senders[0]["sender_id"] = SENDER
    _as_agent(monkeypatch, _adapter(monkeypatch), twilio)
    out = json.loads(await wa.whatsapp_sender_status.ainvoke({}))
    assert "problem" not in out["senders"][0] and "note" not in out


async def test_account_balance(monkeypatch):
    _as_agent(monkeypatch, twilio=_Twilio())
    out = json.loads(await wa.twilio_account_balance.ainvoke({}))
    assert out == {"account_sid": ACCOUNT, "balance": "12.29", "currency": "USD"}

    monkeypatch.setattr(wa, "content_client", lambda: None)
    missing = json.loads(await wa.twilio_account_balance.ainvoke({}))
    assert "TWILIO_ACCOUNT_SID" in missing["error"]


# ── the window, the profile, the usage ───────────────────────────────────────


async def test_window_status_says_whether_an_ordinary_message_would_arrive(monkeypatch):
    from datetime import datetime, timedelta, timezone

    recent = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")

    def route(method, url, kw):
        if method == "GET" and url.endswith("/ParticipantConversations"):
            return _Resp(
                200,
                {
                    "conversations": [
                        {
                            "conversation_sid": CONVERSATION,
                            "chat_service_sid": SERVICE,
                            "participant_messaging_binding": {"proxy_address": SENDER},
                        }
                    ]
                },
            )
        if method == "GET" and url.endswith(f"/Conversations/{CONVERSATION}/Messages"):
            return _Resp(200, {"messages": [{"author": CUSTOMER, "date_created": recent}]})
        return _Resp(200, {})

    adapter = _adapter(monkeypatch, route=route)
    _as_agent(monkeypatch, adapter, _Twilio())
    out = json.loads(await wa.whatsapp_window_status.ainvoke({"to": "+1 555 123 4567"}))
    assert out["window_open"] is True and out["conversation_exists"] is True
    assert out["last_message_from_them"][:13] == recent[:13]
    assert out["window_closes_at"] > out["last_message_from_them"]
    assert "ordinary message reaches" in out["next"]

    lonely = _adapter(monkeypatch, route=lambda m, u, kw: _Resp(200, {"conversations": []}))
    _as_agent(monkeypatch, lonely, _Twilio())
    out = json.loads(await wa.whatsapp_window_status.ainvoke({"to": CUSTOMER}))
    assert out["window_open"] is False and out["conversation_exists"] is False
    assert out["last_message_from_them"] is None
    assert "whatsapp_template_send" in out["next"]


async def test_profile_update_merges_the_changes_into_the_current_profile(monkeypatch):
    twilio = _Twilio()
    twilio.senders = [
        {
            "sid": "XE" + "1" * 32,
            "sender_id": SENDER,
            "status": "ONLINE",
            "profile": {"name": "Birdhouse", "about": "Old about", "vertical": "Other"},
        }
    ]
    _as_agent(monkeypatch, _adapter(monkeypatch), twilio)
    out = json.loads(
        await wa.whatsapp_profile_update.ainvoke(
            {
                "about": "We check in on the people you love",
                "website": "https://birdhouse.example",
                "vertical": "medical/health",
            }
        )
    )
    assert out["updated"] is True and out["number"] == SENDER
    (post,) = [r for r in twilio.requests if r.method == "POST" and "/Senders/" in r.url.path]
    sent = json.loads(post.content)["profile"]
    assert sent["name"] == "Birdhouse", "the current name rides along: Twilio requires it"
    assert sent["about"] == "We check in on the people you love"
    assert sent["vertical"] == "Medical/Health"
    assert sent["websites"] == [{"website": "https://birdhouse.example", "label": "Website"}]
    assert out["profile"]["about"] == "We check in on the people you love"


async def test_profile_update_refuses_bad_values_the_sandbox_and_unknown_numbers(monkeypatch):
    twilio = _Twilio()
    _as_agent(monkeypatch, _adapter(monkeypatch), twilio)
    bad = json.loads(
        await wa.whatsapp_profile_update.ainvoke(
            {"about": "x" * 140, "website": "http://a.b", "vertical": "Crypto"}
        )
    )
    assert len(bad["problems"]) == 3 and twilio.requests == []
    nothing = json.loads(await wa.whatsapp_profile_update.ainvoke({}))
    assert "Nothing to change" in nothing["error"]
    unknown = json.loads(await wa.whatsapp_profile_update.ainvoke({"about": "Hi there"}))
    assert "not a WhatsApp sender" in unknown["error"]

    sandbox = _adapter(monkeypatch, {"from_number": "+14155238886"})
    _as_agent(monkeypatch, sandbox, twilio)
    out = json.loads(await wa.whatsapp_profile_update.ainvoke({"about": "Hi there"}))
    assert "sandbox" in out["error"]


async def test_usage_summary_reports_whatsapp_categories_and_the_total(monkeypatch):
    def record(category: str, description: str, count: str, price: str) -> dict[str, Any]:
        return {
            "category": category,
            "description": description,
            "count": count,
            "count_unit": "messages",
            "usage": count,
            "usage_unit": "messages",
            "price": price,
            "price_unit": "usd",
            "start_date": "2026-09-01",
            "end_date": "2026-09-28",
        }

    twilio = _Twilio()
    twilio.usage_records = [
        record("channels-whatsapp-template-utility", "WhatsApp Utility Templates", "12", "0.48"),
        record("channels-whatsapp-conversation-free", "WhatsApp Free Conversations", "3", "0"),
        record("sms-outbound", "Outbound SMS", "5", "0.04"),
        record("totalprice", "Total Price", "", "0.52"),
    ]
    _as_agent(monkeypatch, twilio=twilio)
    out = json.loads(await wa.twilio_usage_summary.ainvoke({"period": "This Month"}))
    assert [r["category"] for r in out["whatsapp"]] == [
        "channels-whatsapp-template-utility",
        "channels-whatsapp-conversation-free",
    ]
    assert out["whatsapp_total"] == {"price": "0.4800", "price_unit": "usd"}
    assert out["account_total"]["price"] == "0.52"
    (req,) = twilio.calls("GET", "/Usage/Records/")
    assert req.url.path.endswith("/Usage/Records/ThisMonth.json")

    bad = json.loads(await wa.twilio_usage_summary.ainvoke({"period": "next_year"}))
    assert "period must be" in bad["error"]


# ── 63027 from the account's own number ──────────────────────────────────────


async def test_63027_from_an_own_number_names_the_sender_and_its_business_account(monkeypatch):
    """The sandbox is refused before a send; a live number that still gets
    63027 is not a sender, not online, or missing the template on its
    WhatsApp Business Account. The refusal says which, and for the last one
    where to look rather than a cause nobody here has seen."""
    receipt = {"status": "failed", "error_code": 63027}
    twilio = _Twilio([ORDER_READY])
    twilio.senders = [
        {
            "sid": "XE" + "1" * 32,
            "sender_id": SENDER,
            "status": "ONLINE",
            "configuration": {"waba_id": "1234567890"},
        }
    ]
    adapter = _adapter(monkeypatch, route=_conversations_route(receipt=receipt))
    _as_agent(monkeypatch, adapter, twilio)
    call = {"to": CUSTOMER, "template": "order_ready", "variables": {"1": "M", "2": "#7"}}

    out = json.loads(await wa.whatsapp_template_send.ainvoke(call))
    assert "63027" in out["error"]
    assert out["sender"] == {"status": "ONLINE", "waba_id": "1234567890", "offline_reasons": []}
    assert out["template_name"] == "order_ready" and out["template_language"] == "en"
    assert "1234567890" in out["agent_guidance"]
    assert "WhatsApp Manager" in out["agent_guidance"]
    assert "555" not in out["agent_guidance"]

    twilio.senders[0]["status"] = "OFFLINE"
    twilio.senders[0]["offline_reasons"] = [{"code": 30503, "message": "Display name rejected"}]
    out = json.loads(await wa.whatsapp_template_send.ainvoke(call))
    assert "OFFLINE" in out["agent_guidance"] and "Display name rejected" in out["agent_guidance"]

    twilio.senders = [
        {"sid": "XE" + "2" * 32, "sender_id": "whatsapp:+12025550222", "status": "ONLINE"}
    ]
    out = json.loads(await wa.whatsapp_template_send.ainvoke(call))
    assert "not a WhatsApp sender on this Twilio account" in out["agent_guidance"]
    assert out["account_senders"] == ["whatsapp:+12025550222"]


async def test_63027_from_a_free_555_number_blames_the_number_not_the_template(monkeypatch):
    """The reported case: the template approved and the number ONLINE on the
    same Business Account, so the explanation above told the person to
    recreate a template that was fine. A free 555 number from WhatsApp is
    named first, from the number alone."""
    receipt = {"status": "failed", "error_code": 63027}
    twilio = _Twilio([ORDER_READY])
    twilio.senders = [
        {
            "sid": "XE" + "1" * 32,
            "sender_id": FREE_555,
            "status": "ONLINE",
            "configuration": {"waba_id": "1234567890"},
        }
    ]
    adapter = _adapter(
        monkeypatch,
        {"from_number": FREE_555},
        route=_conversations_route(receipt=receipt, sender=FREE_555),
    )
    _as_agent(monkeypatch, adapter, twilio)
    out = json.loads(
        await wa.whatsapp_template_send.ainvoke(
            {"to": CUSTOMER, "template": "order_ready", "variables": {"1": "M", "2": "#7"}}
        )
    )
    assert "63027" in out["error"]
    assert out["sends_from"] == FREE_555
    guidance = out["agent_guidance"]
    assert "Twilio does not support these numbers" in guidance
    assert "needs no change" in guidance
    assert "whatsapp_template_create" not in guidance, "the template is not the problem"


async def test_other_refusals_keep_the_plain_guidance(monkeypatch):
    receipt = {"status": "failed", "error_code": 63016}
    adapter = _adapter(monkeypatch, route=_conversations_route(receipt=receipt))
    _as_agent(monkeypatch, adapter, _Twilio([ORDER_READY]))
    out = json.loads(
        await wa.whatsapp_template_send.ainvoke(
            {"to": CUSTOMER, "template": "order_ready", "variables": {"1": "M", "2": "#7"}}
        )
    )
    assert "63016" in out["error"] and "sender" not in out
    assert out["agent_guidance"].startswith("Tell the person why")


def test_sender_rows_carry_the_business_account():
    row = tc._sender_row(
        {"sid": "XE1", "sender_id": SENDER, "status": "ONLINE", "configuration": {"waba_id": "98"}}
    )
    assert row["waba_id"] == "98"
    assert tc._sender_row({"sid": "XE2", "sender_id": SENDER, "status": "DRAFT"})["waba_id"] == ""


async def test_send_refuses_up_front_when_the_number_is_not_a_sender(monkeypatch):
    """The connector learned at connect that its number is not one of the
    account's; the tool says so before listing templates or sending."""
    adapter = _adapter(monkeypatch)
    adapter._sender_problem = (
        "whatsapp:+12025550199 is not a WhatsApp sender on this Twilio account, so WhatsApp "
        "delivers nothing from it."
    )
    _as_agent(monkeypatch, adapter, _Twilio([ORDER_READY]))
    out = json.loads(
        await wa.whatsapp_template_send.ainvoke(
            {"to": CUSTOMER, "template": "order_ready", "variables": {"1": "M", "2": "#7"}}
        )
    )
    assert "not a WhatsApp sender" in out["error"]
    assert "Connectors tab" in out["agent_guidance"]
    assert adapter._http_session.messages() == []  # type: ignore[union-attr]
