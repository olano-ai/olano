"""`usage_budgets:` — the config block, its API surface, and what it may move.

The deployment's token ceilings used to be invisible and immovable: the only
place they were rendered was a dashboard card deliberately left unmounted, on
the grounds that the numbers were "practically unlimited". When one did bite,
nothing on the box named it, warned about it, or could raise it. These tests
pin the seam that fixed that — and, just as importantly, the half that must
NOT move: the cost budgets bound olano's own upstream bill and are signed, not
local.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from olano.deployment import resolver
from olano.deployment.envelope import sign_license

NOW = 1_780_000_000
KEY_ID = "settings-test-2026-07"
DEP_ID = "dep_budget_api"

_PRIVATE = ed25519.Ed25519PrivateKey.generate()
_KEYS = {KEY_ID: _PRIVATE.public_key()}


def _mint(settings: dict) -> str:
    return sign_license(
        {
            "schema": "olano.deployment-settings",
            "schema_version": 1,
            "issuer": "cloud.olano.ai",
            "audience": "olano-deployment-settings",
            "channel": "local",
            "deployment_id": DEP_ID,
            "plan": "pro",
            "policy_revision": 1,
            "issued_at": NOW - 1000,
            "expires_at": NOW + 10 * 365 * 86400,
            "settings": settings,
        },
        _PRIVATE,
        key_id=KEY_ID,
    )


@pytest.fixture
def managed_home(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LIONCLAW_INSTALLATION_FILE", str(tmp_path / "installation.json"))
    monkeypatch.setattr("olano.deployment.envelope.load_verify_keys", lambda: _KEYS)
    (tmp_path / "installation.json").write_text(
        json.dumps({"managed": True, "mode": "local", "deployment_id": DEP_ID})
    )
    from olano.config.home import get_olano_paths

    paths = get_olano_paths()
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.deployment_license_path.write_text(
        _mint(
            {
                "usage": {
                    "input_tokens_per_agent": {"value": 10_000_000_000, "window": "monthly"},
                    "cost_total": {"value": 100_000_000, "window": "monthly"},
                }
            }
        )
        + "\n"
    )
    resolver.invalidate()
    yield tmp_path
    resolver.invalidate()


@pytest.fixture
def client(managed_home, tmp_workspace, monkeypatch):
    from fastapi.testclient import TestClient

    from olano.admin.memory import MemoryMonitor
    from olano.runtime.api import create_api

    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_workspace))
    # A managed posture defaults multi-user mode ON, which 401s the anonymous
    # calls below with `setup_required`. These tests are about the budget
    # payload, not the login gate, so pin single-owner mode.
    monkeypatch.setattr("olano.runtime.auth.multi_user_enabled", lambda: False)
    runner = MagicMock()
    runner.ainvoke = AsyncMock(return_value="hi")
    rt = MagicMock()
    rt._runners = {}
    rt._channels = []
    rt._memory_monitor = MemoryMonitor()
    return TestClient(create_api(rt))


def _budgets(client) -> dict:
    resp = client.get("/api/system/config")
    assert resp.status_code == 200, resp.text[:800]
    return resp.json()["usage_budgets"]


def _row(client, key: str) -> dict:
    return next(r for r in _budgets(client)["limits"] if r["key"] == key)


class TestConfigSurface:
    def test_get_lists_every_budget_with_its_live_ceiling(self, client):
        block = _budgets(client)
        assert block["enforced"] is True
        assert {r["key"] for r in block["limits"]} == {
            "input_tokens_per_agent",
            "input_tokens_total",
            "output_tokens_per_agent",
            "output_tokens_total",
            "cost_per_agent",
            "cost_total",
        }
        row = _row(client, "input_tokens_per_agent")
        assert row["limit"] == 10_000_000_000
        assert row["source"] == "deployment"
        assert row["override"] is None
        assert row["overridable"] is True

    def test_cost_rows_are_marked_not_overridable(self, client):
        assert _row(client, "cost_total")["overridable"] is False
        assert _row(client, "cost_total")["unit"] == "micro_usd"

    def test_default_alert_thresholds_are_the_documented_ladder(self, client):
        assert _budgets(client)["alert_percents"] == [50, 60, 70, 80, 90, 95, 99]

    def test_put_raises_a_token_ceiling(self, client):
        resp = client.put(
            "/api/system/config",
            json={"usage_budgets": {"input_tokens_per_agent": 50_000_000_000}},
        )
        assert resp.status_code == 200
        row = _row(client, "input_tokens_per_agent")
        assert row["limit"] == 50_000_000_000
        assert row["source"] == "config"
        assert row["override"] == 50_000_000_000
        # What it replaced stays visible, so the card can show both.
        assert row["deployment_limit"] == 10_000_000_000

    def test_put_lifts_a_ceiling_entirely(self, client):
        client.put(
            "/api/system/config",
            json={"usage_budgets": {"input_tokens_per_agent": "unlimited"}},
        )
        row = _row(client, "input_tokens_per_agent")
        assert row["limit"] is None
        assert row["override"] == "unlimited"

    def test_empty_field_clears_the_override(self, client):
        client.put("/api/system/config", json={"usage_budgets": {"input_tokens_per_agent": 5_000}})
        assert _row(client, "input_tokens_per_agent")["source"] == "config"

        client.put("/api/system/config", json={"usage_budgets": {"input_tokens_per_agent": ""}})
        row = _row(client, "input_tokens_per_agent")
        assert row["source"] == "deployment"
        assert row["limit"] == 10_000_000_000

    def test_put_refuses_a_cost_budget_and_says_why(self, client):
        resp = client.put("/api/system/config", json={"usage_budgets": {"cost_total": 999_000_000}})
        assert resp.status_code == 400
        assert "signed settings" in resp.json()["detail"]
        assert _row(client, "cost_total")["limit"] == 100_000_000  # untouched

    def test_put_rejects_junk_rather_than_coercing_it(self, client):
        resp = client.put(
            "/api/system/config", json={"usage_budgets": {"input_tokens_total": "lots"}}
        )
        assert resp.status_code == 400
        assert "unlimited" in resp.json()["detail"]

    def test_put_rejects_a_negative_ceiling(self, client):
        resp = client.put("/api/system/config", json={"usage_budgets": {"input_tokens_total": -5}})
        assert resp.status_code == 400

    def test_put_sets_alert_thresholds(self, client):
        client.put(
            "/api/system/config",
            json={"usage_budgets": {"alert_percents": [25, 75], "alerts_enabled": True}},
        )
        assert _budgets(client)["alert_percents"] == [25, 75]

    def test_put_rejects_a_threshold_of_100(self, client):
        """100 always alerts — listing it would just duplicate that row."""
        resp = client.put("/api/system/config", json={"usage_budgets": {"alert_percents": [100]}})
        assert resp.status_code == 400
        assert "always alerted" in resp.json()["detail"]

    def test_alerts_can_be_switched_off(self, client):
        client.put("/api/system/config", json={"usage_budgets": {"alerts_enabled": False}})
        assert _budgets(client)["alerts_enabled"] is False

    def test_written_config_holds_only_what_was_set(self, client):
        """An unset budget must not persist as a null — a file full of nulls
        reads like six configured "unlimited"s to the next person."""
        client.put("/api/system/config", json={"usage_budgets": {"input_tokens_total": 1_000_000}})
        import yaml

        from olano.config.home import get_olano_paths

        raw = yaml.safe_load(get_olano_paths().config_path.read_text())
        assert set(raw["usage_budgets"]) == {
            "input_tokens_total",
            "alerts_enabled",
            "alert_percents",
        }


class TestDeploymentCard:
    def test_usage_rows_quote_the_enforced_ceiling(self, client):
        client.put(
            "/api/system/config",
            json={"usage_budgets": {"input_tokens_per_agent": 50_000_000_000}},
        )
        rows = {r["key"]: r for r in client.get("/api/system/deployment").json()["usage"]}
        row = rows["input_tokens_per_agent"]
        assert row["limit"] == 50_000_000_000
        assert row["source"] == "config"
        assert row["deployment_limit"] == 10_000_000_000
        assert rows["cost_total"]["source"] == "deployment"
        assert rows["cost_total"]["overridable"] is False


class TestUnmanaged:
    def test_open_posture_reports_nothing_enforced(self, tmp_workspace, monkeypatch, tmp_path):
        """No installation manifest = no counting and no ceilings, and the card
        has to say that rather than render six limits that do nothing."""
        from fastapi.testclient import TestClient

        from olano.admin.memory import MemoryMonitor
        from olano.runtime.api import create_api

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("LIONCLAW_INSTALLATION_FILE", str(tmp_path / "none.json"))
        monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_workspace))
        resolver.invalidate()
        rt = MagicMock()
        rt._runners = {}
        rt._channels = []
        rt._memory_monitor = MemoryMonitor()
        api = TestClient(create_api(rt))

        block = api.get("/api/system/config").json()["usage_budgets"]
        assert block["enforced"] is False
        assert block["posture"] == "open"
        assert all(row["current"] is None for row in block["limits"])
        resolver.invalidate()
