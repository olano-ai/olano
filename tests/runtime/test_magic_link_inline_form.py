"""The inline secret card's backend: ``GET …/form`` and ``POST …/submit``.

The dashboard chat and the mobile app render a ``secret-entry`` /
``provider-key`` link as a form under the agent's reply instead of a link to
open in a browser. These routes are what that card talks to, and the
properties that matter are the ones the public page already has: the same
link record and the same flow ``submit`` (one audit trail, single use), the
PIN still gating when the policy issued one, nothing but secret-shaped flows
served inline, and never a secret or a chat id in what the card is handed.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.magic_links import service, store
from olano.magic_links.adapters import secret_entry
from olano.magic_links.flows import BaseFlow, register
from olano.runtime.api import create_api


class _OtherFormFlow(BaseFlow):
    """A form flow that is NOT on the inline allowlist (an editor, say)."""

    id = "inline-test-other-form"
    label = "Other form"
    kind = "form"

    def submit(self, link, fields):
        return {"status": store.STATUS_DONE, "result": "saved"}


register(_OtherFormFlow())


@pytest.fixture(autouse=True)
def public_origin(monkeypatch):
    monkeypatch.setenv("LIONCLAW_PUBLIC_URL", "https://acme.olano.run")
    import olano.core.mcp_setup as mcp_setup

    monkeypatch.setattr(mcp_setup, "_LAST_SEEN_ORIGIN", None)


@pytest.fixture
def client():
    runtime = MagicMock()
    runtime._runners = {}
    return TestClient(create_api(runtime), base_url="https://testserver")


@pytest.fixture
def stored(monkeypatch):
    """Capture vault writes instead of needing an unlocked vault."""
    calls: list[dict] = []

    def fake_store(name, value, *, agent, description, category="general"):
        calls.append({"name": name, "value": value, "agent": agent, "category": category})

    monkeypatch.setattr(secret_entry, "store_secret", fake_store)
    return calls


def _mint(flow="secret-entry", **params):
    params = {"name": "STRIPE_API_KEY", "reason": "for invoices", "agent": "curtis", **params}
    if flow != "secret-entry":
        params = {k: v for k, v in params.items() if k != "name" and k != "reason"}
    return service.mint_link(
        flow,
        params=params,
        requested_by={"channel": "api", "username": "owner@example.com", "agent": "merli"},
        notify={"channel_type": "api", "target": "thread-1", "agent_name": "merli"},
        minted_by="agent:merli",
    )


# ── the form ─────────────────────────────────────────────────────────────────


def test_form_describes_the_secret_entry_without_leaking_anything(client):
    minted = _mint()
    response = client.get(f"/api/magic-links/links/{minted.id}/form")
    assert response.status_code == 200
    body = response.json()
    assert body["flow"] == "secret-entry"
    assert body["kind"] == "form"
    assert body["status"] == store.STATUS_PENDING
    assert body["title"] == "Add STRIPE_API_KEY"
    assert "for invoices" in body["instructions"]
    assert body["agent"] == "curtis"
    assert body["pin_required"] is True
    assert [f["name"] for f in body["fields"]] == ["value"]
    assert body["fields"][0]["secret"] is True
    assert body["fields"][0]["value"] == ""
    # Nothing the card does not need: no chat ids, no PIN, no token.
    assert "requested_by" not in body
    assert minted.pin not in response.text
    assert minted.url.rsplit("/", 1)[-1] not in response.text


def test_provider_key_form_offers_the_provider_picker_when_unpinned(client):
    minted = _mint("provider-key")
    body = client.get(f"/api/magic-links/links/{minted.id}/form").json()
    assert body["flow"] == "provider-key"
    names = [f["name"] for f in body["fields"]]
    assert names == ["provider", "value"]
    assert any(o["value"] == "anthropic" for o in body["fields"][0]["options"])


def test_form_refuses_a_flow_that_is_not_an_inline_secret(client):
    """An editor link's descriptor carries the file's contents; an OAuth link
    has nothing to type. Neither is served to the card."""
    minted = service.mint_link(
        "inline-test-other-form", params={}, requested_by={}, notify={}, minted_by="t"
    )
    response = client.get(f"/api/magic-links/links/{minted.id}/form")
    assert response.status_code == 400
    assert "not an inline form" in response.json()["detail"]


def test_form_404s_on_an_unknown_link(client):
    assert client.get("/api/magic-links/links/hs_nope/form").status_code == 404


def test_form_routes_sit_under_the_admin_prefix():
    from olano.runtime.auth import _ADMIN_ONLY_PREFIXES

    assert any("/api/magic-links/links/x/form".startswith(p) for p in _ADMIN_ONLY_PREFIXES)


# ── the submission ───────────────────────────────────────────────────────────


def test_submit_stores_the_secret_and_finishes_the_link(client, stored):
    minted = _mint()
    response = client.post(
        f"/api/magic-links/links/{minted.id}/submit",
        json={"pin": minted.pin, "fields": {"value": "sk_live_123"}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == store.STATUS_DONE
    assert "STRIPE_API_KEY saved" in body["result"]
    assert stored == [
        {"name": "STRIPE_API_KEY", "value": "sk_live_123", "agent": "curtis", "category": "general"}
    ]
    # The value went to the vault and nowhere else: not the record, not the reply.
    record = store.get(minted.id)
    assert record.status == store.STATUS_DONE
    assert "sk_live_123" not in response.text
    assert "sk_live_123" not in str(record.__dict__)


def test_submit_with_the_wrong_pin_is_refused_and_counted(client, stored):
    minted = _mint()
    wrong = "000000" if minted.pin != "000000" else "111111"
    response = client.post(
        f"/api/magic-links/links/{minted.id}/submit",
        json={"pin": wrong, "fields": {"value": "sk_live_123"}},
    )
    assert response.status_code == 403
    assert "left" in response.json()["detail"]
    assert stored == []
    assert store.get(minted.id).pin_attempts == 1


def test_submit_without_a_pin_is_refused_when_one_was_issued(client, stored):
    minted = _mint()
    response = client.post(
        f"/api/magic-links/links/{minted.id}/submit", json={"fields": {"value": "x"}}
    )
    assert response.status_code == 403
    assert stored == []


def test_submit_is_single_use(client, stored):
    minted = _mint()
    first = client.post(
        f"/api/magic-links/links/{minted.id}/submit",
        json={"pin": minted.pin, "fields": {"value": "one"}},
    )
    assert first.status_code == 200
    again = client.post(
        f"/api/magic-links/links/{minted.id}/submit",
        json={"pin": minted.pin, "fields": {"value": "two"}},
    )
    assert again.status_code == 400
    assert "no longer active" in again.json()["detail"]
    assert [c["value"] for c in stored] == ["one"]


def test_submit_refuses_a_non_inline_flow(client):
    minted = service.mint_link(
        "inline-test-other-form", params={}, requested_by={}, notify={}, minted_by="t"
    )
    response = client.post(
        f"/api/magic-links/links/{minted.id}/submit",
        json={"pin": minted.pin, "fields": {"value": "x"}},
    )
    assert response.status_code == 400
    assert store.get(minted.id).status == store.STATUS_PENDING


def test_an_empty_value_is_a_form_error_not_a_spent_link(client, monkeypatch):
    """A validation refusal from the flow leaves the link usable: the person
    corrects the field and saves again."""
    minted = _mint()
    response = client.post(
        f"/api/magic-links/links/{minted.id}/submit",
        json={"pin": minted.pin, "fields": {"value": "   "}},
    )
    assert response.status_code == 400
    assert "empty" in response.json()["detail"]
    assert store.get(minted.id).serves_actions()


def test_the_inline_set_is_exactly_the_two_secret_forms():
    """The allowlist is what keeps editor descriptors (file contents) and
    redirect flows out of the chat card; widen it deliberately or not at all."""
    assert service.INLINE_FORM_FLOWS == frozenset({"secret-entry", "provider-key"})
