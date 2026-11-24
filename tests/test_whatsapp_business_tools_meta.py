"""The WhatsApp Business tools on the Meta Cloud API connector.

The same tools, with the same names and arguments, serve both business
connectors; ``test_whatsapp_business_tools.py`` covers the Twilio side. Here
the agent's connector is Meta's, faked at the HTTP layer by the ``FakeGraph``
of the connector tests, and every tool must take the Meta path: templates from
the WhatsApp Business Account, sends through the Cloud API, statuses and
billing from the connector's record.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from olano.platforms import whatsapp_meta_graph as mg
from olano.tools import whatsapp_business_tools as wa
from tests.test_platform_whatsapp_meta_api import (
    CUSTOMER,
    NAMED,
    POSITIONAL,
    WABA,
    FakeGraph,
    _connect,
)

SPANISH = {**POSITIONAL, "id": "112", "language": "es"}
PENDING = {**NAMED, "id": "223", "name": "pending_one", "status": "PENDING"}


@pytest.fixture(autouse=True)
def _fresh_cache():
    mg.invalidate_templates()
    yield
    mg.invalidate_templates()


async def _as_agent(monkeypatch, graph: FakeGraph, **extra: Any):
    adapter = await _connect(monkeypatch, graph, **extra)
    monkeypatch.setattr(wa, "whatsapp_adapter", lambda agent="": adapter)
    monkeypatch.setattr(wa, "RECEIPT_POLL_SECONDS", 0.0)
    return adapter


async def test_the_meta_connector_wins_when_an_agent_has_both(monkeypatch):
    from olano.platforms import get_platform_registry
    from olano.platforms.config import Platform

    registry = get_platform_registry()
    twilio, meta = object(), object()
    registry.register("both", Platform.WHATSAPP_TWILIO, twilio)
    registry.register("both", Platform.WHATSAPP_META_API, meta)
    try:
        assert wa.whatsapp_adapter("both") is meta
    finally:
        registry.unregister_agent("both")


async def test_the_list_shows_approved_templates_with_their_billing_category(monkeypatch):
    graph = FakeGraph()
    graph.templates = [POSITIONAL, NAMED, PENDING]
    adapter = await _as_agent(monkeypatch, graph)
    try:
        out = json.loads(await wa.whatsapp_template_list.ainvoke({}))
        names = [row["name"] for row in out["approved_templates"]]
        assert names == ["order_update", "welcome"]
        first = out["approved_templates"][0]
        assert first["category"] == "MARKETING" and "billed as MARKETING" in first["category_note"]
        assert set(first["variables"]) == {"header", "1", "2", "button_1"}
        assert not any("appsecret" in json.dumps(row) for row in out["approved_templates"])
    finally:
        await adapter.disconnect()


async def test_a_send_goes_through_the_cloud_api_and_reports_what_meta_said(monkeypatch):
    graph = FakeGraph()
    graph.templates = [POSITIONAL, SPANISH]
    adapter = await _as_agent(monkeypatch, graph)
    try:
        values = {"header": "#9", "1": "Ann", "2": "Monday", "button_1": "A1"}
        ambiguous = json.loads(
            await wa.whatsapp_template_send.ainvoke(
                {"to": CUSTOMER, "template": "order_update", "variables": values}
            )
        )
        assert "Several templates" in ambiguous["error"]
        assert "language" in ambiguous["agent_guidance"]

        out = json.loads(
            await wa.whatsapp_template_send.ainvoke(
                {"to": CUSTOMER, "template": "order_update", "variables": values, "language": "es"}
            )
        )
        assert out["sent"] is True and out["status"] == "accepted"
        assert out["language"] == "es" and out["message_sid"].startswith("wamid.")
        assert graph.sent[-1]["template"]["language"] == {"code": "es"}
    finally:
        await adapter.disconnect()


async def test_a_failed_send_explains_a_missing_template(monkeypatch):
    graph = FakeGraph()
    graph.templates = [POSITIONAL]
    adapter = await _as_agent(monkeypatch, graph)

    async def _failed(to: str, message_id: str) -> tuple[str, str]:
        adapter._statuses[message_id] = {"status": "failed", "error_code": 132001}
        return "failed", mg.describe_meta_error(132001)

    monkeypatch.setattr(adapter, "message_status", _failed)
    try:
        values = {"header": "#9", "1": "Ann", "2": "Monday", "button_1": "A1"}
        out = json.loads(
            await wa.whatsapp_template_send.ainvoke(
                {"to": CUSTOMER, "template": "order_update", "variables": values}
            )
        )
        assert "132001" in out["error"]
        assert "WhatsApp Manager" in out["agent_guidance"] and WABA in out["agent_guidance"]
    finally:
        await adapter.disconnect()


async def test_message_status_carries_the_billing(monkeypatch):
    graph = FakeGraph()
    adapter = await _as_agent(monkeypatch, graph)
    try:
        adapter._statuses["wamid.X"] = {
            "status": "delivered",
            "template": "order_update",
            "billing": {"billable": True, "category": "marketing", "type": "regular"},
        }
        out = json.loads(
            await wa.whatsapp_message_status.ainvoke({"to": CUSTOMER, "message_sid": "wamid.X"})
        )
        assert out["status"] == "delivered" and out["template"] == "order_update"
        assert out["billing"]["category"] == "marketing"
        assert out["billing"]["means"] == "billed at the category's rate"
    finally:
        await adapter.disconnect()


async def test_window_status_reads_the_connectors_record(monkeypatch):
    graph = FakeGraph()
    adapter = await _as_agent(monkeypatch, graph)
    try:
        out = json.loads(await wa.whatsapp_window_status.ainvoke({"to": CUSTOMER}))
        assert out["window_open"] is False and "template" in out["next"]
    finally:
        await adapter.disconnect()


async def test_create_submits_at_once_and_refuses_a_draft(monkeypatch):
    graph = FakeGraph()
    adapter = await _as_agent(monkeypatch, graph)
    call = {
        "name": "Order Ready",
        "body": "Hi {{1}}, your order {{2}} is ready for pickup at our Main Street store.",
        "samples": {"1": "Maria", "2": "#1042"},
    }
    try:
        draft = json.loads(await wa.whatsapp_template_create.ainvoke({**call, "submit": False}))
        assert "no unsubmitted draft" in draft["error"]
        out = json.loads(await wa.whatsapp_template_create.ainvoke(call))
        assert out["submitted"] is True and out["status"] == "pending"
        assert out["created"]["name"] == "order_ready"
        posted = json.loads(
            next(
                r
                for r in graph.requests
                if r.method == "POST" and r.url.path.endswith("message_templates")
            ).content
        )
        assert posted["name"] == "order_ready" and posted["category"] == "UTILITY"
        again = json.loads(await wa.whatsapp_template_create.ainvoke(call))
        assert "already exists" in again["error"]
    finally:
        await adapter.disconnect()


async def test_submit_says_where_a_template_stands(monkeypatch):
    graph = FakeGraph()
    graph.templates = [PENDING]
    adapter = await _as_agent(monkeypatch, graph)
    try:
        out = json.loads(await wa.whatsapp_template_submit.ainvoke({"template": "pending_one"}))
        assert "submits a template when it is created" in out["error"]
        assert "whatsapp_template_status" in out["agent_guidance"]
    finally:
        await adapter.disconnect()


async def test_status_counts_and_notes_a_category_change(monkeypatch):
    graph = FakeGraph()
    graph.templates = [POSITIONAL, PENDING]
    adapter = await _as_agent(monkeypatch, graph)
    try:
        out = json.loads(await wa.whatsapp_template_status.ainvoke({}))
        assert out["count_by_status"] == {"approved": 1, "pending": 1}
        assert out["whatsapp_business_account"] == WABA
        one = json.loads(await wa.whatsapp_template_status.ainvoke({"template": "order_update"}))
        assert one["template"]["category_changed_from"] == "UTILITY"
    finally:
        await adapter.disconnect()


async def test_delete_removes_one_language_only(monkeypatch):
    graph = FakeGraph()
    graph.templates = [POSITIONAL]
    adapter = await _as_agent(monkeypatch, graph)
    try:
        wrong = json.loads(
            await wa.whatsapp_template_delete.ainvoke(
                {"template": "order_update", "confirm_name": "x"}
            )
        )
        assert "Not deleted" in wrong["error"]
        out = json.loads(
            await wa.whatsapp_template_delete.ainvoke(
                {"template": "order_update", "confirm_name": "order_update"}
            )
        )
        assert out["deleted"] is True
        delete = next(r for r in graph.requests if r.method == "DELETE")
        assert delete.url.params["name"] == "order_update" and delete.url.params["hsm_id"] == "111"
    finally:
        await adapter.disconnect()


async def test_sender_status_lists_the_business_accounts_numbers(monkeypatch):
    graph = FakeGraph()
    graph.waba_numbers = [
        {
            **graph.number,
            "id": graph.number["id"],
        }
    ]
    adapter = await _as_agent(monkeypatch, graph)
    try:
        out = json.loads(await wa.whatsapp_sender_status.ainvoke({}))
        (row,) = out["senders"]
        assert row["number"] == "+15550783881" and row["status"] == "CONNECTED"
        assert row["display_name"] == "Birdhouse" and row["messaging_limit"] == "TIER_2K"
        assert row["profile"] == {"about": "Birds", "vertical": "Shopping/Retail"}
        assert out["this_agent_sends_from"] == "+15550783881"
    finally:
        await adapter.disconnect()


async def test_profile_update_speaks_metas_vocabulary(monkeypatch):
    graph = FakeGraph()
    adapter = await _as_agent(monkeypatch, graph)
    try:
        out = json.loads(
            await wa.whatsapp_profile_update.ainvoke(
                {
                    "about": "Fresh seed daily",
                    "email": "hi@birdhouse.example",
                    "website": "https://birdhouse.example",
                    "vertical": "restaurant",
                }
            )
        )
        assert out["updated"] is True
        posted = json.loads(
            next(
                r
                for r in graph.requests
                if r.method == "POST" and r.url.path.endswith("whatsapp_business_profile")
            ).content
        )
        assert posted == {
            "messaging_product": "whatsapp",
            "about": "Fresh seed daily",
            "email": "hi@birdhouse.example",
            "websites": ["https://birdhouse.example"],
            "vertical": "RESTAURANT",
        }
    finally:
        await adapter.disconnect()


async def test_an_admin_agent_without_a_connector_uses_the_vault(monkeypatch):
    graph = FakeGraph()
    graph.templates = [POSITIONAL]
    monkeypatch.setattr(wa, "whatsapp_adapter", lambda agent="": None)
    creds = {"WHATSAPP_META_ACCESS_TOKEN": "EAAvault", "WHATSAPP_META_WABA_ID": WABA}
    monkeypatch.setattr(wa, "get_credential", lambda name, default="": creds.get(name, default))
    import olano.tools._whatsapp_meta as meta_side

    real = meta_side.MetaGraphClient

    def _client(token: str, **kw: Any):
        assert token == "EAAvault" and kw["waba_id"] == WABA
        return graph.client()

    monkeypatch.setattr(meta_side, "MetaGraphClient", _client)
    out = json.loads(await wa.whatsapp_template_status.ainvoke({}))
    assert out["count_by_status"] == {"approved": 1}
    assert real is mg.MetaGraphClient


async def test_the_twilio_only_tools_do_not_pretend_on_meta(monkeypatch):
    graph = FakeGraph()
    adapter = await _as_agent(monkeypatch, graph)
    monkeypatch.setattr(wa, "get_credential", lambda name, default="": "")
    try:
        out = json.loads(await wa.twilio_account_balance.ainvoke({}))
        assert "TWILIO_ACCOUNT_SID" in out["error"]
    finally:
        await adapter.disconnect()
