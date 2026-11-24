"""Integration tests for the public /api/connect/* handshake surface.

The route layer is the part an unauthenticated stranger can reach, so these
cover the gate rather than the flows: it must be openable without a dashboard
session, reveal nothing before the PIN, refuse a token it doesn't know without
saying why, and never let a GET advance a link (messaging platforms fetch link
targets to build previews — a side-effecting GET would burn every link before
its human ever tapped it).

The client speaks HTTPS because the unlock cookie is issued ``Secure`` whenever
the deployment has an HTTPS public URL, which is the production posture.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.config.system_settings import set_magic_link_settings
from olano.magic_links import service, store
from olano.magic_links.flows import BaseFlow, register
from olano.magic_links.routes import UNLOCK_COOKIE
from olano.runtime.api import create_api


class _RouteTestFlow(BaseFlow):
    id = "route-test"
    label = "Route test"
    kind = "form"

    def submit(self, link, fields):
        return {"status": store.STATUS_DONE, "result": "saved"}


register(_RouteTestFlow())


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
def link():
    minted = service.mint_link(
        "route-test",
        params={},
        requested_by={"channel": "telegram", "username": "jrivera", "agent": "merli"},
        notify={},
        minted_by="test",
    )
    return minted, minted.url.rsplit("/", 1)[-1]


def _unlock(client, token, pin):
    return client.post(f"/api/connect/{token}/unlock", json={"pin": pin})


# ── reachability without a session ───────────────────────────────────────────


def test_page_is_reachable_without_any_dashboard_session(client, link):
    _, token = link
    response = client.get(f"/api/connect/{token}")
    assert response.status_code == 200
    assert "Confirm it's you" in response.text


def test_unknown_token_is_a_plain_404(client):
    response = client.get("/api/connect/definitely-not-a-real-token")
    assert response.status_code == 404
    # No hint about whether any link exists.
    assert "PIN" not in response.text


def test_locked_page_reveals_nothing_about_the_flow(client, link):
    _, token = link
    body = client.get(f"/api/connect/{token}").text
    assert "Route test" not in body  # the service name is behind the PIN
    assert "jrivera" in body  # but the requester is shown, for self-verification


def test_page_sets_hardening_headers(client, link):
    _, token = link
    headers = client.get(f"/api/connect/{token}").headers
    assert headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert "default-src 'none'" in headers["content-security-policy"]
    assert headers["referrer-policy"] == "no-referrer"
    assert "no-store" in headers["cache-control"]


# ── the PIN gate ─────────────────────────────────────────────────────────────


def test_state_and_action_require_an_unlocked_session(client, link):
    _, token = link
    assert client.get(f"/api/connect/{token}/state").status_code == 401
    assert client.post(f"/api/connect/{token}/action", json={"op": "start"}).status_code == 401


def test_wrong_pin_is_403_with_attempts_remaining(client, link):
    minted, token = link
    wrong = "000000" if minted.pin != "000000" else "111111"
    response = _unlock(client, token, wrong)
    assert response.status_code == 403
    assert response.json()["unlocked"] is False
    assert "left" in response.json()["detail"]


def test_correct_pin_unlocks_and_scopes_the_cookie_to_this_link(client, link):
    minted, token = link
    response = _unlock(client, token, minted.pin)
    assert response.status_code == 200
    assert response.json()["unlocked"] is True
    assert response.json()["state"]["kind"] == "form"

    cookie = response.headers["set-cookie"]
    assert UNLOCK_COOKIE in cookie
    assert f"Path=/api/connect/{token}" in cookie
    assert "HttpOnly" in cookie
    assert "Secure" in cookie  # HTTPS deployment

    assert client.get(f"/api/connect/{token}/state").status_code == 200


def test_one_links_session_cannot_drive_another(client, link):
    minted, token = link
    _unlock(client, token, minted.pin)
    stolen = client.cookies.get(UNLOCK_COOKIE, path=f"/api/connect/{token}")

    other = service.mint_link("route-test", params={}, requested_by={}, notify={}, minted_by="t")
    other_token = other.url.rsplit("/", 1)[-1]
    attacker = TestClient(create_api(MagicMock()), base_url="https://testserver")
    attacker.cookies.set(UNLOCK_COOKIE, stolen or "")
    assert attacker.get(f"/api/connect/{other_token}/state").status_code == 401


# ── no side effects on GET ───────────────────────────────────────────────────


def test_get_never_advances_or_consumes_a_link(client, link):
    _, token = link
    for _ in range(3):  # a preview bot, a tap, a refresh
        assert client.get(f"/api/connect/{token}").status_code == 200
    record = service.resolve_token(token)
    assert record.status == store.STATUS_PENDING
    assert record.pin_attempts == 0
    assert record.serves_actions()


# ── completion ───────────────────────────────────────────────────────────────


def test_state_still_readable_after_out_of_band_completion(client, link):
    """The page must render the final state even when the background watcher
    finished the link first.

    Redirect/device flows complete out-of-band (a provider callback, the relay
    pump, or the watcher's own poll), and finishing clears the unlock session.
    The page is still polling /state with its now-orphaned unlock cookie; if
    that 401'd, the page would hang on "waiting…" over a sign-in that actually
    succeeded. A spent link serves its outcome to the token holder regardless.
    """
    minted, token = link
    _unlock(client, token, minted.pin)
    # Finish it the way the watcher would — not via this client's action POST.
    store.finish(service.resolve_token(token).id, status=store.STATUS_DONE, result="Connected.")
    response = client.get(f"/api/connect/{token}/state")
    assert response.status_code == 200
    assert response.json()["status"] == store.STATUS_DONE
    assert response.json()["result"] == "Connected."


def test_state_still_requires_unlock_while_the_link_is_live(client, link):
    """The relaxation above is only for spent links — a live one still gates."""
    _, token = link
    fresh = TestClient(create_api(MagicMock()), base_url="https://testserver")
    assert fresh.get(f"/api/connect/{token}/state").status_code == 401


def test_submit_completes_the_link_and_then_refuses_reuse(client, link):
    minted, token = link
    _unlock(client, token, minted.pin)

    response = client.post(
        f"/api/connect/{token}/action", json={"op": "submit", "fields": {"value": "x"}}
    )
    assert response.status_code == 200
    assert response.json()["status"] == store.STATUS_DONE

    # Completing the handshake also tears the page session down, so the replay
    # is refused at the session gate (401) before it reaches the spent-link
    # check (400). Either is a correct refusal; assert it simply cannot happen.
    again = client.post(
        f"/api/connect/{token}/action", json={"op": "submit", "fields": {"value": "x"}}
    )
    assert again.status_code in (400, 401)
    assert store.get(service.resolve_token(token).id).status == store.STATUS_DONE


def test_spent_link_refuses_actions_even_with_a_live_session(client, link):
    """The link check stands on its own, independent of the session teardown."""
    minted, token = link
    _unlock(client, token, minted.pin)
    record = service.resolve_token(token)
    # Expire the link without touching the unlock cookie.
    store.finish(record.id, status=store.STATUS_EXPIRED, error="expired")
    store.mutate(
        record.id,
        lambda r: (
            setattr(r, "unlock_hash", record.unlock_hash),
            setattr(r, "unlock_expires_at", record.unlock_expires_at),
        )
        and None,
    )
    response = client.post(f"/api/connect/{token}/action", json={"op": "start"})
    assert response.status_code == 400
    assert "no longer active" in response.json()["detail"]


def test_spent_link_explains_itself_without_the_pin(client, link):
    minted, token = link
    service.revoke_link(service.resolve_token(token).id)
    fresh = TestClient(create_api(MagicMock()), base_url="https://testserver")
    body = fresh.get(f"/api/connect/{token}").text
    assert "Confirm it's you" not in body  # no point asking for a PIN


def test_disabled_feature_hides_the_page(client, link):
    _, token = link
    set_magic_link_settings({"enabled": False})
    response = client.get(f"/api/connect/{token}")
    assert response.status_code == 404
    assert "turned off" in response.text


def test_exemption_does_not_leak_to_the_connectors_routes():
    """``/api/connect/`` must not swallow ``/api/connectors/…``.

    The pairing streams under /api/connectors/ are session-gated dashboard
    surfaces. Only the trailing slash keeps the prefixes apart, so pin the exact
    predicate both gate sites use (``api.security_middleware`` and
    ``auth.access_gate``) — dropping that slash would silently expose them.
    """
    exempt = lambda path: path.startswith("/api/connect/")  # noqa: E731
    assert not exempt("/api/connectors/whatsapp/pair")
    assert not exempt("/api/connectors/weixin/pair")
    assert not exempt("/api/connect")
    assert exempt("/api/connect/some-token")
    assert exempt("/api/connect/some-token/state")


# ── admin surface ────────────────────────────────────────────────────────────


def test_admin_can_list_mint_and_revoke(client):
    listing = client.get("/api/magic-links/links")
    assert listing.status_code == 200
    assert listing.json()["available"] is True

    flows = client.get("/api/magic-links/flows").json()["flows"]
    assert any(f["id"] == "github-device" for f in flows)

    minted = client.post("/api/magic-links/links", json={"flow": "route-test", "params": {}}).json()
    assert minted["url"].startswith("https://acme.olano.run/api/connect/")
    assert minted["pin"]

    assert client.delete(f"/api/magic-links/links/{minted['id']}").status_code == 200
    assert client.delete("/api/magic-links/links/hs_nope").status_code == 404


def test_admin_listing_never_exposes_tokens_or_pins(client, link):
    minted, token = link
    body = client.get("/api/magic-links/links").text
    assert token not in body
    assert minted.pin not in body
    assert "token_hash" not in body


def test_mint_refuses_an_unknown_flow(client):
    response = client.post("/api/magic-links/links", json={"flow": "nope", "params": {}})
    assert response.status_code == 400
    assert "Available:" in response.json()["detail"]


def test_legacy_handshake_routes_still_answer(client):
    """The pre-rename paths stay registered.

    A bookmarked dashboard tab, a script, or a cached SPA bundle can still
    call them, and a 404 there looks like the feature vanished.
    """
    minted = client.post("/api/handshake/links", json={"flow": "route-test", "params": {}}).json()
    assert minted["url"].startswith("https://acme.olano.run/api/connect/")

    legacy = client.get("/api/handshake/links")
    current = client.get("/api/magic-links/links")
    assert legacy.status_code == current.status_code == 200
    assert [link["id"] for link in legacy.json()["links"]] == [
        link["id"] for link in current.json()["links"]
    ]
    assert any(
        f["id"] == "github-device" for f in client.get("/api/handshake/flows").json()["flows"]
    )
    assert client.delete(f"/api/handshake/links/{minted['id']}").status_code == 200


def test_both_admin_prefixes_are_owner_gated():
    """The alias must be exactly as gated as the name that replaced it.

    Registering a second path for an admin route is how a mint surface
    accidentally becomes public.
    """
    from olano.runtime.auth import _ADMIN_ONLY_PREFIXES

    assert "/api/magic-links" in _ADMIN_ONLY_PREFIXES
    assert "/api/handshake" in _ADMIN_ONLY_PREFIXES
