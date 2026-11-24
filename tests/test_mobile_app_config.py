"""config.yaml → mobile_app: the mobile app layout an admin sets in the dashboard."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.config.system_settings import (
    MobileAppSettings,
    load_mobile_app_settings,
    set_mobile_app_settings,
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    from olano.runtime.api import create_api

    runtime = MagicMock()
    runtime._runners = {}
    return TestClient(create_api(runtime))


def test_defaults_show_everything_and_transcribe_on_the_deployment() -> None:
    s = MobileAppSettings()
    assert s.cortex_tab is True
    assert s.inbox.model_dump() == {
        "enabled": True,
        "approvals": True,
        "alerts": True,
        "issues": True,
    }
    assert s.settings_tab.model_dump() == {
        "files": True,
        "credits_usage": True,
        "transcribe_on_phone": True,
    }
    assert s.transcribe_on_phone_default is False


def test_nested_groups_merge_key_by_key() -> None:
    set_mobile_app_settings({"inbox": {"alerts": False}})
    set_mobile_app_settings({"inbox": {"issues": False}, "cortex_tab": False})
    s = load_mobile_app_settings()
    assert s.inbox.alerts is False
    assert s.inbox.issues is False
    assert s.inbox.approvals is True  # untouched by either partial update
    assert s.cortex_tab is False


def test_revision_moves_only_when_a_value_does() -> None:
    a = MobileAppSettings().revision()
    assert a == MobileAppSettings().revision()
    assert a != MobileAppSettings(cortex_tab=False).revision()


def test_phone_endpoint_serves_the_block_with_a_revision(client) -> None:
    body = client.get("/api/mobile/config").json()
    assert body["cortex_tab"] is True
    assert body["revision"] == MobileAppSettings().revision()

    saved = client.put(
        "/api/system/config",
        json={"mobile_app": {"cortex_tab": False, "settings_tab": {"files": False}}},
    )
    assert saved.status_code == 200
    after = client.get("/api/mobile/config").json()
    assert after["cortex_tab"] is False
    assert after["settings_tab"]["files"] is False
    assert after["settings_tab"]["credits_usage"] is True
    assert after["revision"] != body["revision"]
    # The dashboard's own read carries the same block.
    assert client.get("/api/system/config").json()["mobile_app"]["cortex_tab"] is False


@pytest.mark.parametrize(
    "bad",
    [
        ["on"],
        {"cortex_tab": "no"},
        {"bogus": True},
        {"inbox": {"alerts": 0}},
        {"inbox": {"nope": False}},
        {"settings_tab": True},
    ],
)
def test_put_rejects_anything_but_known_booleans(client, bad) -> None:
    resp = client.put("/api/system/config", json={"mobile_app": bad})
    assert resp.status_code == 400
    assert client.get("/api/mobile/config").json()["cortex_tab"] is True
