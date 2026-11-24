"""Balance client (`deployment/credits_client.py`) + its runtime API proxy.

The property under test: the number on the dashboard card is CLOUD truth with
honest freshness — cached ~60s, served stale (and labeled stale) through
outages, and absent (not zero) on unmanaged installs.
"""

from __future__ import annotations

import httpx
import pytest

from olano.deployment import credits_client as cc


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    cc.invalidate_cache()
    from olano.deployment import sources

    monkeypatch.setattr(sources, "deployment_token", lambda: "olano_dep_tok")
    monkeypatch.setattr(sources, "cloud_api_url", lambda: "https://cloud.example.test")
    yield
    cc.invalidate_cache()


def _fake_get(monkeypatch, responses: list):
    """Queue of responses; a callable raises, a dict returns 200 JSON."""
    calls: list[str] = []

    def get(url, headers=None, timeout=None):
        calls.append(url)
        item = responses.pop(0) if responses else responses_default
        if isinstance(item, Exception):
            raise item
        return httpx.Response(200, json=item, request=httpx.Request("GET", url))

    responses_default = {"balance": 0}
    monkeypatch.setattr(httpx, "get", get)
    return calls


BALANCE = {
    "balance": 4321.5,
    "included_remaining": 3321.5,
    "purchased_remaining": 1000,
    "monthly_included": 5000,
    "monthly_used": 1678.5,
    "period_end": "2026-09-01T00:00:00Z",
    "credits_per_usd": 100,
}


def test_balance_is_fetched_once_then_cached(monkeypatch) -> None:
    calls = _fake_get(monkeypatch, [dict(BALANCE)])
    first = cc.get_balance()
    assert first is not None and first["balance"] == 4321.5
    assert first["stale"] is False
    second = cc.get_balance()
    assert second is not None and second["balance"] == 4321.5
    assert calls == ["https://cloud.example.test/api/v1/credits/balance"]  # one fetch


def test_stale_on_error_keeps_the_last_value(monkeypatch) -> None:
    _fake_get(monkeypatch, [dict(BALANCE), httpx.ConnectError("down")])
    assert cc.get_balance() is not None
    stale = cc.get_balance(force=True)  # force past TTL into the failing fetch
    assert stale is not None
    assert stale["balance"] == 4321.5
    assert stale["stale"] is True


def test_unmanaged_returns_none(monkeypatch) -> None:
    from olano.deployment import sources

    monkeypatch.setattr(sources, "deployment_token", lambda: None)
    assert cc.get_balance() is None


def test_never_fetched_and_failing_returns_none(monkeypatch) -> None:
    _fake_get(monkeypatch, [httpx.ConnectError("down")])
    assert cc.get_balance() is None
