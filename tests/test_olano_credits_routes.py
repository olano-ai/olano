"""``/api/olano/credits-key`` and the provider surfaces that read the account key.

Property under test: a self-hosted (open) box can connect, inspect and remove an
Olano account API key through one admin-only route, the key is verified against
cloud.olano.ai before it is stored, and every provider surface (`/api/catalog/
providers`, `admin.models.provider_connection`, the gateway diagnostics) says
so on an open box while a managed deployment's payload stays byte-identical.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from olano.deployment import gateway_key as gk

ACCOUNT = "sk-olano-" + "a" * 40


class _Vault:
    def __init__(self) -> None:
        self.rows: dict[str, str] = {}

    def get(self, name: str, touch: bool = False):
        return self.rows.get(name)

    def set(self, name: str, value: str, **kwargs) -> None:
        self.rows[name] = value

    def delete(self, name: str, agent: str | None = None) -> bool:
        return self.rows.pop(name, None) is not None


@pytest.fixture
def vault(monkeypatch) -> _Vault:
    v = _Vault()
    monkeypatch.setattr(gk, "_writable_vault", lambda: v)
    monkeypatch.setattr(gk, "_vault_get", lambda name: v.rows.get(name))
    monkeypatch.setattr(gk, "_memory_key", None)
    monkeypatch.setattr(gk, "_dead_env_loaded", True)
    monkeypatch.setattr(gk, "_dead_env_digest", None)
    monkeypatch.delenv(gk.GATEWAY_KEY_ENV, raising=False)
    monkeypatch.delenv(gk.LEGACY_GATEWAY_KEY_ENV, raising=False)
    gk.reset_account_key_memo()
    return v


def _posture(monkeypatch, posture: str) -> None:
    from olano.deployment import resolver, sources

    status = SimpleNamespace(
        posture=posture, settings=SimpleNamespace(feature_enabled=lambda n: True)
    )
    monkeypatch.setattr(resolver, "deployment_status", lambda now=None: status)
    monkeypatch.setattr(sources, "deployment_token", lambda: None)
    monkeypatch.setattr(sources, "cloud_api_url", lambda: "https://cloud.example.test")
    # A real managed box always carries provisioning evidence (the manifest, the
    # env flag); `gateway_key()` reads that evidence, never the resolver, to
    # decide whether the account key may be served, so the stub carries it too.
    if posture == "open":
        monkeypatch.delenv("LIONCLAW_DEPLOYMENT_MANAGED", raising=False)
    else:
        monkeypatch.setenv("LIONCLAW_DEPLOYMENT_MANAGED", "1")
    # A managed posture defaults multi-user mode ON (login gate), which would
    # 401 the anonymous test client; these tests are about the key, not auth.
    monkeypatch.setattr("olano.runtime.auth.multi_user_enabled", lambda: False)


def _verify(monkeypatch, status: int = 200, body: dict | None = None, *, boom: bool = False):
    calls: list[dict] = []

    def get(url, headers=None, timeout=None, params=None):
        calls.append({"url": url, "headers": headers})
        if boom:
            raise httpx.ConnectError("no route")
        payload = (
            body
            if body is not None
            else {
                "schemaVersion": 1,
                "org_name": "Acme",
                "balance": 500,
                "lifetime_used": 20,
                "credits_per_usd": 1000,
                "services": ["intelligence", "image"],
                "key": {"id": "key_1", "name": "office", "prefix": "sk-olano-aaaaaa"},
            }
        )
        return httpx.Response(status, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    return calls


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from olano.runtime.api import create_api

    runtime = MagicMock()
    runtime._runners = {}
    runtime.registry = MagicMock()
    runtime.registry.get.return_value = None
    tc = TestClient(create_api(runtime))
    tc.runtime = runtime  # the handlers close over it; tests reach it here
    return tc


# ── access ───────────────────────────────────────────────────────────────────


def test_the_key_route_is_admin_only_and_the_credits_reads_are_not() -> None:
    from olano.runtime.auth import _ADMIN_ONLY_PREFIXES

    assert "/api/olano/credits-key" in _ADMIN_ONLY_PREFIXES
    assert not any("/api/olano/credits".startswith(p) for p in _ADMIN_ONLY_PREFIXES)
    assert not any("/api/olano/services/usage".startswith(p) for p in _ADMIN_ONLY_PREFIXES)


# ── the route ────────────────────────────────────────────────────────────────


def test_status_on_an_open_box_without_a_key(client, vault, monkeypatch) -> None:
    _posture(monkeypatch, "open")
    body = client.get("/api/olano/credits-key").json()
    assert body["manageable"] is True
    assert body["connected"] is False
    assert body["source"] is None
    assert body["rejected"] is False
    assert body["key_url"] == "https://cloud.example.test/app/api-keys"


def test_connect_verifies_stores_and_swaps(client, vault, monkeypatch) -> None:
    from olano.models import olano_gateway as og

    _posture(monkeypatch, "open")
    calls = _verify(monkeypatch)
    swapped: list[str] = []
    monkeypatch.setattr(og, "swap_live_models", lambda key: swapped.append(key) or 2)

    res = client.put("/api/olano/credits-key", json={"key": f" {ACCOUNT} "})
    assert res.status_code == 200, res.text
    body = res.json()
    assert calls[0]["url"] == "https://cloud.example.test/api/v1/account/verify"
    assert calls[0]["headers"]["Authorization"] == f"Bearer {ACCOUNT}"
    assert vault.rows[gk.ACCOUNT_KEY_ENV] == ACCOUNT
    assert swapped == [ACCOUNT]
    assert body["connected"] is True
    assert body["source"] == "account"
    assert body["key_prefix"] == "sk-olano-aaaaaa"
    assert body["live_models_updated"] == 2
    assert body["verified"]["org_name"] == "Acme"
    assert body["verified"]["balance"] == 500
    assert body["verified"]["services"] == ["intelligence", "image"]
    # The key itself never rides back.
    assert ACCOUNT not in res.text


def test_connect_refuses_a_malformed_key_without_a_round_trip(client, vault, monkeypatch) -> None:
    _posture(monkeypatch, "open")
    calls = _verify(monkeypatch)
    for bad in ("", "olano_dep_abc", "sk-olano-short", "sk-olano-" + "a b" * 20):
        res = client.put("/api/olano/credits-key", json={"key": bad})
        assert res.status_code == 400, bad
        assert res.json()["detail"]["code"] == "invalid_key"
    assert calls == []
    assert gk.ACCOUNT_KEY_ENV not in vault.rows


def test_connect_never_stores_a_key_the_cloud_refuses(client, vault, monkeypatch) -> None:
    _posture(monkeypatch, "open")
    _verify(monkeypatch, status=401, body={"error": "unauthorized"})
    res = client.put("/api/olano/credits-key", json={"key": ACCOUNT})
    assert res.status_code == 400
    assert res.json()["detail"]["code"] == "key_rejected"
    assert gk.ACCOUNT_KEY_ENV not in vault.rows


def test_connect_reports_an_unreachable_cloud(client, vault, monkeypatch) -> None:
    _posture(monkeypatch, "open")
    _verify(monkeypatch, boom=True)
    res = client.put("/api/olano/credits-key", json={"key": ACCOUNT})
    assert res.status_code == 502
    assert res.json()["detail"]["code"] == "cloud_unreachable"
    assert gk.ACCOUNT_KEY_ENV not in vault.rows


def test_connect_with_a_locked_vault_is_a_503(client, vault, monkeypatch) -> None:
    _posture(monkeypatch, "open")
    _verify(monkeypatch)
    monkeypatch.setattr(gk, "_writable_vault", lambda: None)
    res = client.put("/api/olano/credits-key", json={"key": ACCOUNT})
    assert res.status_code == 503
    assert res.json()["detail"]["code"] == "vault_locked"


@pytest.mark.parametrize("posture", ["enforced", "quarantine"])
def test_a_managed_box_refuses_the_whole_route(client, vault, monkeypatch, posture) -> None:
    """Rule 4: a managed deployment bills its own wallet; the second key family
    is never offered there, and the status says the card may not manage one."""
    _posture(monkeypatch, posture)
    calls = _verify(monkeypatch)
    assert client.get("/api/olano/credits-key").json()["manageable"] is False
    res = client.put("/api/olano/credits-key", json={"key": ACCOUNT})
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "managed_box"
    assert client.delete("/api/olano/credits-key").status_code == 409
    assert calls == []
    assert gk.ACCOUNT_KEY_ENV not in vault.rows


def test_forget_removes_the_key_and_retires_it_from_live_models(client, vault, monkeypatch) -> None:
    from olano.models import olano_gateway as og

    _posture(monkeypatch, "open")
    vault.rows[gk.ACCOUNT_KEY_ENV] = ACCOUNT
    swapped: list[str] = []
    monkeypatch.setattr(og, "swap_live_models", lambda key: swapped.append(key) or 0)

    res = client.delete("/api/olano/credits-key")
    assert res.status_code == 200
    assert res.json()["removed"] is True
    assert res.json()["connected"] is False
    assert gk.ACCOUNT_KEY_ENV not in vault.rows
    assert swapped == [gk.UNENROLLED_SENTINEL]
    # Idempotent.
    assert client.delete("/api/olano/credits-key").json()["removed"] is False


# ── provider surfaces ────────────────────────────────────────────────────────


def test_catalog_adds_source_and_manageable_on_an_open_box_only(client, vault, monkeypatch) -> None:
    _posture(monkeypatch, "open")
    olano = {
        p["key"]: p for p in client.get("/api/catalog/providers?fields=basic").json()["providers"]
    }["olano"]
    assert olano["auth"]["type"] == "deployment_token"
    assert olano["auth"]["manageable"] is True
    assert olano["auth"]["source"] is None
    assert olano["auth"]["status"] == "not_connected"
    assert "API key" in olano["auth"]["error"]
    # The card's note comes from the synced catalog on a managed box and still
    # says "needs a deployment"; here it must describe the pasted key instead.
    assert "Olano API key" in olano["notes"]

    vault.rows[gk.ACCOUNT_KEY_ENV] = ACCOUNT
    gk.reset_account_key_memo()
    olano = {
        p["key"]: p for p in client.get("/api/catalog/providers?fields=basic").json()["providers"]
    }["olano"]
    assert olano["auth"]["connected"] is True
    assert olano["auth"]["status"] == "connected"
    assert olano["auth"]["source"] == "account"

    gk.note_account_key_rejected()
    olano = {
        p["key"]: p for p in client.get("/api/catalog/providers?fields=basic").json()["providers"]
    }["olano"]
    assert olano["auth"]["status"] == "degraded"
    assert "refused" in olano["auth"]["error"]


def test_catalog_payload_is_unchanged_on_a_managed_box(client, vault, monkeypatch) -> None:
    """The production invariant: a managed deployment's olano `auth` object has
    exactly the keys it had before this family existed."""
    _posture(monkeypatch, "enforced")
    vault.rows[gk.ACCOUNT_KEY_ENV] = ACCOUNT  # present, and must change nothing
    olano = {
        p["key"]: p for p in client.get("/api/catalog/providers?fields=basic").json()["providers"]
    }["olano"]
    assert set(olano["auth"]) == {"type", "connected", "status", "error"}
    assert olano["auth"] == {
        "type": "deployment_token",
        "connected": False,
        "status": "not_connected",
        "error": "Olano models need a deployment attached to cloud.olano.ai",
    }


def test_admin_models_provider_connection_mirrors_the_catalog(vault, monkeypatch) -> None:
    from olano.admin import models as admin_models

    _posture(monkeypatch, "enforced")
    managed = admin_models.provider_connection("olano", set())
    assert set(managed) == {"connected", "state", "auth", "env_var", "detail"}

    _posture(monkeypatch, "open")
    row = admin_models.provider_connection("olano", set())
    assert row["manageable"] is True and row["source"] is None
    assert row["connected"] is False and "API key" in row["detail"]

    vault.rows[gk.ACCOUNT_KEY_ENV] = ACCOUNT
    gk.reset_account_key_memo()
    row = admin_models.provider_connection("olano", set())
    assert row["connected"] is True and row["state"] == "enrolled"
    assert row["source"] == "account" and "account API key" in row["detail"]


def test_gateway_diagnostics_name_the_account_key(client, vault, monkeypatch) -> None:
    from olano.deployment import gateway_probe

    _posture(monkeypatch, "open")
    monkeypatch.setattr(gateway_probe, "cached", lambda: None)
    body = client.get("/api/diagnostics/gateway").json()
    assert body["state"] == "unattached"
    assert "Olano API key" in body["remediation"]

    vault.rows[gk.ACCOUNT_KEY_ENV] = ACCOUNT
    gk.reset_account_key_memo()
    body = client.get("/api/diagnostics/gateway").json()
    assert body["state"] == "enrolled" and body["source"] == "account"
    assert body["remediation"] is None
    gk.note_account_key_rejected()
    body = client.get("/api/diagnostics/gateway").json()
    assert body["account_key_rejected"] is True
    assert "refused" in body["remediation"]


# ── the effective key, not the pasted one ────────────────────────────────────


def test_an_env_gateway_key_shadows_the_pasted_key(client, vault, monkeypatch) -> None:
    """An operator's OLANO_GATEWAY_KEY outranks a pasted key in gateway_key();
    the route must push THAT key into the live models and say the pasted one is
    stored but not in use, instead of handing the models a key the box's own
    resolution says is not in charge."""
    from olano.models import olano_gateway as og

    _posture(monkeypatch, "open")
    _verify(monkeypatch)
    monkeypatch.setenv(gk.GATEWAY_KEY_ENV, "sk-operator-env-key")
    swapped: list[str] = []
    monkeypatch.setattr(og, "swap_live_models", lambda key: swapped.append(key) or 1)

    res = client.put("/api/olano/credits-key", json={"key": ACCOUNT})
    assert res.status_code == 200, res.text
    body = res.json()
    assert swapped == ["sk-operator-env-key"]
    assert body["connected"] is True
    assert body["active"] is False
    assert body["shadowed_by"] == "env"
    assert body["source"] == "env"
    # The status read says the same.
    status = client.get("/api/olano/credits-key").json()
    assert status["active"] is False and status["shadowed_by"] == "env"


def test_a_key_from_the_environment_is_reported_as_such(client, vault, monkeypatch) -> None:
    _posture(monkeypatch, "open")
    monkeypatch.setenv(gk.ACCOUNT_KEY_ENV, ACCOUNT)
    status = client.get("/api/olano/credits-key").json()
    assert status["connected"] is True
    assert status["from_env"] is True
    assert status["active"] is True
    assert status["shadowed_by"] is None


def test_removal_swaps_the_placeholder_and_rebuilds_the_tier_agents(
    client, vault, monkeypatch
) -> None:
    """Agents on olano:* or default:* keep a model pointed at the placeholder
    after a removal and would 401 on every turn until a restart; the route
    restarts exactly those, and leaves a BYOK agent alone."""
    from unittest.mock import AsyncMock

    from olano.models import olano_gateway as og

    _posture(monkeypatch, "open")
    vault.rows[gk.ACCOUNT_KEY_ENV] = ACCOUNT
    swapped: list[str] = []
    monkeypatch.setattr(og, "swap_live_models", lambda key: swapped.append(key) or 0)
    runtime = client.runtime
    runtime._runners = {
        "tiered": SimpleNamespace(config=SimpleNamespace(model="olano:turbo")),
        "defaulted": SimpleNamespace(config=SimpleNamespace(model="default:fast")),
        "byok": SimpleNamespace(config=SimpleNamespace(model="anthropic:claude-sonnet-4-6")),
    }
    runtime._stop_agent = AsyncMock()
    runtime._boot_agent = AsyncMock()

    res = client.delete("/api/olano/credits-key")
    assert res.status_code == 200, res.text
    assert swapped == [gk.UNENROLLED_SENTINEL]
    assert res.json()["agents_restarted"] == ["defaulted"]
    assert [c.args[0] for c in runtime._stop_agent.await_args_list] == ["defaulted"]
    assert [c.args[0] for c in runtime._boot_agent.await_args_list] == ["defaulted"]


def test_a_first_connect_rebuilds_the_default_agents_too(client, vault, monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from olano.models import olano_gateway as og

    _posture(monkeypatch, "open")
    _verify(monkeypatch)
    monkeypatch.setattr(og, "swap_live_models", lambda key: 0)
    runtime = client.runtime
    runtime._runners = {
        "defaulted": SimpleNamespace(config=SimpleNamespace(model="default:fast")),
        "tiered": SimpleNamespace(config=SimpleNamespace(model="olano:turbo")),
        "byok": SimpleNamespace(config=SimpleNamespace(model="anthropic:claude-sonnet-4-6")),
    }
    runtime._stop_agent = AsyncMock()
    runtime._boot_agent = AsyncMock()
    # A tier agent that could not boot without a key is not running; a connect
    # boots it from its config on disk.
    monkeypatch.setattr(
        "olano.config.loader.list_agents", lambda **kw: ["defaulted", "tiered", "byok", "down"]
    )
    monkeypatch.setattr(
        "olano.config.loader.load_agent_config",
        lambda name, **kw: SimpleNamespace(model={"down": "olano:pro"}.get(name, "fake:instant")),
    )

    first = client.put("/api/olano/credits-key", json={"key": ACCOUNT})
    assert first.status_code == 200, first.text
    assert sorted(first.json()["agents_restarted"]) == ["defaulted", "down", "tiered"]
    assert sorted(c.args[0] for c in runtime._stop_agent.await_args_list) == ["defaulted", "tiered"]
    assert sorted(c.args[0] for c in runtime._boot_agent.await_args_list) == [
        "defaulted",
        "down",
        "tiered",
    ]
    # A replacement while a key is already in use swaps the models in place.
    again = client.put("/api/olano/credits-key", json={"key": "sk-olano-" + "b" * 40})
    assert again.status_code == 200, again.text
    assert again.json()["agents_restarted"] == []


# ── what the cloud answers ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "code", "http"),
    [
        (401, "key_rejected", 400),
        (403, "key_rejected", 400),
        (404, "account_credits_unavailable", 502),
        (429, "cloud_busy", 503),
        (500, "cloud_error", 502),
    ],
)
def test_verify_answers_are_mapped_to_codes(client, vault, monkeypatch, status, code, http) -> None:
    _posture(monkeypatch, "open")
    _verify(monkeypatch, status=status, body={"error": "x"})
    res = client.put("/api/olano/credits-key", json={"key": ACCOUNT})
    assert res.status_code == http, res.text
    assert res.json()["detail"]["code"] == code
    assert gk.ACCOUNT_KEY_ENV not in vault.rows


def test_a_200_that_is_not_a_verification_is_refused(client, vault, monkeypatch) -> None:
    """A proxy's HTML page or an older cloud's JSON must not read as a live key."""
    _posture(monkeypatch, "open")
    _verify(monkeypatch, status=200, body={"ok": True})
    res = client.put("/api/olano/credits-key", json={"key": ACCOUNT})
    assert res.status_code == 502
    assert res.json()["detail"]["code"] == "cloud_error"
    assert gk.ACCOUNT_KEY_ENV not in vault.rows


def test_an_unnamed_org_still_verifies(client, vault, monkeypatch) -> None:
    """`org_name` is null for an org that never named itself (the cloud hides
    an id-shaped name); the key block is what proves the answer."""
    from olano.models import olano_gateway as og

    _posture(monkeypatch, "open")
    monkeypatch.setattr(og, "swap_live_models", lambda key: 0)
    _verify(
        monkeypatch,
        status=200,
        body={
            "schemaVersion": 1,
            "org_name": None,
            "balance": 0,
            "services": [],
            "key": {"id": "k", "prefix": "sk-olano-aaaaaa"},
        },
    )
    res = client.put("/api/olano/credits-key", json={"key": ACCOUNT})
    assert res.status_code == 200, res.text
    assert res.json()["verified"]["org_name"] is None


@pytest.mark.parametrize(
    "bad",
    [
        "sk-olano-short",
        "sk-olano-" + "a" * 20 + " b",
        "sk-olano-" + "é" * 30,
        "olano_dep_" + "a" * 40,
    ],
)
def test_the_key_shape_is_checked_before_any_round_trip(client, vault, monkeypatch, bad) -> None:
    _posture(monkeypatch, "open")
    calls = _verify(monkeypatch)
    res = client.put("/api/olano/credits-key", json={"key": bad})
    assert res.status_code == 400
    assert res.json()["detail"]["code"] == "invalid_key"
    assert calls == []
