"""Salesforce toolkit: a refused token is repaired or explained, never relayed.

The incident: the Connections card read "Connected" while every ``sf_*`` call
returned ``401 Unauthorized`` and the agent said only that the connection
"needs to be reauthorized". These pin the two halves of the fix, a 401 first
gets a forced token refresh and one retry, and a 401 that survives it comes
back as a ``[connector-auth]`` result that marks the connection and names the
reconnect link, plus the hand-set-token variant, where there is nothing to
refresh and the fix is a different one.

The HTTP seam is ``httpx.request`` (every Salesforce call goes through
``_request``); the broker is exercised for real against a throwaway provider
record in the autouse tmp home, with ``base._post_token`` stubbed.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest

from olano.config.home import get_olano_paths
from olano.core.connectors import base, registry
from olano.core.connectors.base import connector_auth_status
from olano.tools import salesforce_tools as sf
from olano.tools._connector_auth import CONNECTOR_AUTH_MARKER


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Broker state pristine per test; env never decides the outcome."""
    from olano.vault import resolver

    saved_providers = dict(registry._PROVIDERS)
    saved_exposed = dict(registry._EXPOSED)
    saved_names = registry._EXPOSED_NAMES
    resolver.set_vault(None)
    base._RECORD_CACHE.clear()
    monkeypatch.delenv("SALESFORCE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("SALESFORCE_INSTANCE_URL", raising=False)
    yield
    base._RECORD_CACHE.clear()
    registry._PROVIDERS.clear()
    registry._PROVIDERS.update(saved_providers)
    registry._EXPOSED.clear()
    registry._EXPOSED.update(saved_exposed)
    registry._EXPOSED_NAMES = saved_names
    resolver.set_vault(None)


def _provider():
    provider = registry.get_provider("salesforce")
    assert provider is not None
    return provider


def _connect(access_token: str = "old-tok", *, refresh_token: str | None = "rt") -> Any:
    """A connected Salesforce record at system scope, as the OAuth flow saves it."""
    path = get_olano_paths().connector_token_path("salesforce")
    record: dict[str, Any] = {
        "service": "salesforce",
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_at": time.time() + 3600,  # the file says: an hour left
        "scopes": ["api", "refresh_token"],
        "client_id": "cid",
        "client_secret": "cs",
        "params": {"domain": "login"},
        "extras": {"instance_url": "https://org.my.salesforce.com"},
        "obtained_at": time.time(),
    }
    if refresh_token:
        record["refresh_token"] = refresh_token
    base._save_record(record, path)
    return path


def _response(
    status: int, body: Any, url: str = "https://org.my.salesforce.com/x"
) -> httpx.Response:
    req = httpx.Request("GET", url)
    return httpx.Response(status, json=body, request=req)


class _Salesforce:
    """Stand-in for ``httpx.request``: refuses every token but the ones in ``valid``."""

    def __init__(self, valid: set[str]):
        self.valid = valid
        self.calls: list[dict[str, Any]] = []

    def __call__(self, method: str, url: str, headers=None, timeout=None, **kwargs):
        token = str((headers or {}).get("Authorization", "")).removeprefix("Bearer ")
        self.calls.append({"url": url, "token": token, **kwargs})
        if token not in self.valid:
            return _response(
                401, [{"message": "Session expired or invalid", "errorCode": "INVALID_SESSION_ID"}]
            )
        return _response(
            200, {"totalSize": 1, "records": [{"attributes": {}, "Id": "001", "Name": "Acme"}]}
        )


def _stub_refresh(monkeypatch, payload: dict[str, Any] | None):
    calls: list[dict[str, Any]] = []

    def fake_post(url, data, *, headers=None, auth=None):
        calls.append({"url": url, "data": dict(data)})
        if payload is None:
            raise RuntimeError('Token endpoint returned 400: {"error":"invalid_grant"}')
        return dict(payload)

    monkeypatch.setattr(base, "_post_token", fake_post)
    return calls


def test_a_401_is_repaired_by_a_forced_refresh_and_one_retry(monkeypatch):
    """The common case ends with nobody involved: refresh, retry, answer."""
    _connect("old-tok")
    refreshes = _stub_refresh(monkeypatch, {"access_token": "new-tok"})
    api = _Salesforce(valid={"new-tok"})
    monkeypatch.setattr(sf.httpx, "request", api)

    out = sf.sf_query.invoke({"soql": "SELECT Id FROM Account LIMIT 1"})

    assert "Acme" in out and CONNECTOR_AUTH_MARKER not in out
    assert [c["token"] for c in api.calls] == ["old-tok", "new-tok"]
    assert len(refreshes) == 1 and refreshes[0]["data"]["grant_type"] == "refresh_token"
    # The refreshed token was persisted, so the NEXT call never sees the 401.
    assert connector_auth_status(_provider())["connected"] is True
    api.calls.clear()
    sf.sf_query.invoke({"soql": "SELECT Id FROM Account LIMIT 1"})
    assert [c["token"] for c in api.calls] == ["new-tok"]


def test_a_401_that_survives_the_refresh_marks_the_connection_and_names_the_fix(monkeypatch):
    """The card was lying ("Connected"); after this it says "Needs reconnecting"."""
    _connect("old-tok")
    _stub_refresh(monkeypatch, {"access_token": "still-dead"})
    api = _Salesforce(valid=set())
    monkeypatch.setattr(sf.httpx, "request", api)

    out = sf.sf_query.invoke({"soql": "SELECT Id FROM Account LIMIT 1"})

    assert out.startswith(CONNECTOR_AUTH_MARKER)
    # Salesforce's own words, not httpx's "Client error '401 Unauthorized' for url".
    assert "INVALID_SESSION_ID" in out and "Session expired or invalid" in out
    # The exact fix the model must OFFER, not just report.
    assert 'magic_link_connect_service("salesforce")' in out
    assert "Disconnect, then Connect" in out
    assert "retrying it unchanged will fail" in out.lower() or "fail the same way" in out
    assert "Do not stop at reporting the error" in out
    # One retry, never a loop.
    assert [c["token"] for c in api.calls] == ["old-tok", "still-dead"]
    status = connector_auth_status(_provider())
    assert status["connected"] is False
    assert status["needs_reauth"] is True
    assert "sf_query" in status["auth_error"] and "INVALID_SESSION_ID" in status["auth_error"]


def test_a_refused_refresh_grant_is_definitive(monkeypatch):
    """``invalid_grant`` from the token endpoint: nothing but a new sign-in follows."""
    _connect("old-tok")
    _stub_refresh(monkeypatch, payload=None)  # 400 invalid_grant
    api = _Salesforce(valid=set())
    monkeypatch.setattr(sf.httpx, "request", api)

    out = sf.sf_get_record.invoke({"object_type": "Account", "record_id": "001"})

    assert out.startswith(CONNECTOR_AUTH_MARKER)
    assert "could not be renewed" in out
    assert "invalid_grant" in out
    assert [c["token"] for c in api.calls] == ["old-tok"]  # no retry with nothing fresh
    assert connector_auth_status(_provider())["needs_reauth"] is True


def test_a_retry_loop_does_not_hammer_the_token_endpoint(monkeypatch):
    """Once marked, a second 401 within the cooldown skips the refresh round-trip."""
    _connect("old-tok")
    refreshes = _stub_refresh(monkeypatch, {"access_token": "still-dead"})
    api = _Salesforce(valid=set())
    monkeypatch.setattr(sf.httpx, "request", api)

    sf.sf_query.invoke({"soql": "SELECT Id FROM Account"})
    sf.sf_query.invoke({"soql": "SELECT Id FROM Account"})
    sf.sf_query.invoke({"soql": "SELECT Id FROM Account"})

    assert len(refreshes) == 1


def test_a_hand_set_token_gets_the_vault_fix_not_the_reconnect_story(monkeypatch):
    """No connector record: the value came from the environment/vault."""
    monkeypatch.setenv("SALESFORCE_ACCESS_TOKEN", "pasted")
    monkeypatch.setenv("SALESFORCE_INSTANCE_URL", "https://org.my.salesforce.com")
    api = _Salesforce(valid=set())
    monkeypatch.setattr(sf.httpx, "request", api)

    def no_refresh(*a, **k):  # pragma: no cover - the assertion is that it is never reached
        raise AssertionError("nothing to refresh for a hand-set token")

    monkeypatch.setattr(base, "_post_token", no_refresh)

    out = sf.sf_query.invoke({"soql": "SELECT Id FROM Account"})

    assert out.startswith(CONNECTOR_AUTH_MARKER)
    assert "hand-set token" in out
    assert 'magic_link_connect_service("salesforce")' in out
    assert 'magic_link_request_secret("SALESFORCE_ACCESS_TOKEN")' in out
    assert "Needs reconnecting" not in out  # nothing on the Connections tab to mark
    assert [c["token"] for c in api.calls] == ["pasted"]
    # And nothing was marked: there is no record.
    assert connector_auth_status(_provider())["connected"] is False
    assert "needs_reauth" not in connector_auth_status(_provider())


def test_a_reconnect_clears_the_mark(monkeypatch):
    """A fresh sign-in overwrites the record, so the card turns green again on its own."""
    _connect("old-tok")
    _stub_refresh(monkeypatch, {"access_token": "still-dead"})
    monkeypatch.setattr(sf.httpx, "request", _Salesforce(valid=set()))
    sf.sf_query.invoke({"soql": "SELECT Id FROM Account"})
    assert connector_auth_status(_provider())["needs_reauth"] is True

    _connect("brand-new")  # what the OAuth callback / relay pump saves

    status = connector_auth_status(_provider())
    assert status["connected"] is True and "needs_reauth" not in status


def test_other_http_errors_keep_salesforces_words(monkeypatch):
    """A 400 is not an auth problem; the body's message still reaches the model."""
    _connect("old-tok")

    def bad_request(method, url, headers=None, timeout=None, **kwargs):
        return _response(
            400, [{"message": "unexpected token: FROM", "errorCode": "MALFORMED_QUERY"}]
        )

    monkeypatch.setattr(sf.httpx, "request", bad_request)
    out = sf.sf_query.invoke({"soql": "SELEC Id FROM Account"})
    assert out.startswith("[tool-error] salesforce:")
    assert "MALFORMED_QUERY" in out and "unexpected token" in out
    assert connector_auth_status(_provider())["connected"] is True


def test_the_error_text_survives_a_non_json_body():
    r = httpx.Response(
        502, text="<html>bad gateway</html>", request=httpx.Request("GET", "https://x")
    )
    assert sf._error_text(r) == "502 Bad Gateway"
    r2 = _response(401, {"error": "invalid_token", "error_description": "expired"})
    assert sf._error_text(r2) == "401 Unauthorized: expired (invalid_token)"
    assert json.loads(r2.text)["error"] == "invalid_token"
