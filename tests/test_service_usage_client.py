"""Per-service usage client (`deployment/service_usage_client.py`).

Same contract as the balance client: CLOUD-authoritative per-service credit
usage, cached ~60s, served stale (and labeled) through outages, and absent (not
empty) on unmanaged installs — so the box card shows the same figures as the
cloud deployment view.
"""

from __future__ import annotations

import httpx
import pytest

from olano.deployment import service_usage_client as su


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    su.invalidate_cache()
    from olano.deployment import sources

    monkeypatch.setattr(sources, "deployment_token", lambda: "olano_dep_tok")
    monkeypatch.setattr(sources, "cloud_api_url", lambda: "https://cloud.example.test")
    yield
    su.invalidate_cache()


def _fake_get(monkeypatch, responses: list):
    def get(url, headers=None, timeout=None):
        item = responses.pop(0) if responses else {"services": []}
        if isinstance(item, Exception):
            raise item
        return httpx.Response(200, json=item, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)


USAGE = {
    "schemaVersion": 1,
    "window": {"since": "2026-07-13T00:00:00Z", "days": 30},
    "total_credits": 900,
    "services": [
        {"service": "pro", "label": "Olano Pro", "calls": 3, "credits": 500},
        {"service": "vision", "label": "Olano Vision", "calls": 2, "credits": 240},
        {"service": "image_gen", "label": "Olano Image", "calls": 1, "credits": 160},
    ],
    "credits_per_usd": 1000,
}


def test_days_param_reaches_the_cloud_and_caches_per_window(monkeypatch) -> None:
    """The Insights overview asks for its own window (`?days=N`); each window
    caches independently so the services card's default fetch and a 7-day
    overview fetch can't serve each other stale figures."""
    calls: list[tuple[str, dict | None]] = []

    def get(url, headers=None, timeout=None, params=None):
        calls.append((url, params))
        payload = dict(USAGE)
        payload["total_credits"] = 100 if params else 900
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    default = su.get_usage()
    windowed = su.get_usage(days=7)
    assert default is not None and default["total_credits"] == 900
    assert windowed is not None and windowed["total_credits"] == 100
    # The default request carries no params (byte-identical to the old client);
    # the windowed one names its window.
    assert calls[0][1] is None
    assert calls[1][1] == {"days": 7}
    # Both windows serve from their own cache entry now.
    assert su.get_usage()["total_credits"] == 900  # type: ignore[index]
    assert su.get_usage(days=7)["total_credits"] == 100  # type: ignore[index]
    assert len(calls) == 2
    su.invalidate_cache()
    su.get_usage(days=7)
    assert len(calls) == 3


def test_usage_is_fetched_once_then_cached(monkeypatch) -> None:
    calls: list[str] = []

    def get(url, headers=None, timeout=None):
        calls.append(url)
        return httpx.Response(200, json=dict(USAGE), request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    first = su.get_usage()
    assert first is not None and first["total_credits"] == 900
    assert first["services"][0]["label"] == "Olano Pro"
    assert first["stale"] is False
    su.get_usage()
    assert calls == ["https://cloud.example.test/api/v1/usage/services"]  # one fetch


def test_stale_on_error_keeps_the_last_value(monkeypatch) -> None:
    _fake_get(monkeypatch, [dict(USAGE), httpx.ConnectError("down")])
    assert su.get_usage() is not None
    stale = su.get_usage(force=True)
    assert stale is not None and stale["total_credits"] == 900
    assert stale["stale"] is True


def test_unmanaged_returns_none(monkeypatch) -> None:
    from olano.deployment import sources

    monkeypatch.setattr(sources, "deployment_token", lambda: None)
    assert su.get_usage() is None


def test_missing_services_key_is_rejected(monkeypatch) -> None:
    _fake_get(monkeypatch, [{"total_credits": 5}])  # no "services"
    assert su.get_usage() is None
