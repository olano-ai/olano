"""A stray OLANO_CREDITS_KEY on a MANAGED box changes nothing.

Every gate that turns Olano-managed defaults on for a self-hosted box reads
``gateway_key.account_key_if_unmanaged()``; the raw ``account_key()`` slot
answers on a managed box too, and before this pin six gates read it directly,
so an operator exporting the variable on a quarantined or enforced box flipped
cognition defaults, media, the web services and the credits widgets while the
gateway itself (``gateway_key()``) correctly ignored the key. The managed
evidence here is REAL (the env marker and an installation manifest in a tmp
home), not a patched resolver, because that evidence is what the predicate
reads.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from olano.config import system_settings as ss
from olano.deployment import gateway_key as gk

ACCOUNT = "sk-olano-" + "m" * 40


@pytest.fixture
def managed_box(monkeypatch, tmp_path: Path):
    manifest = tmp_path / "installation.json"
    manifest.write_text(
        json.dumps({"deployment_id": "dep_test", "managed": True, "provider": "digitalocean"})
    )
    monkeypatch.setenv("LIONCLAW_INSTALLATION_FILE", str(manifest))
    monkeypatch.setenv("LIONCLAW_DEPLOYMENT_MANAGED", "1")
    monkeypatch.delenv("LIONCLAW_DEPLOYMENT_TOKEN", raising=False)
    monkeypatch.delenv(gk.GATEWAY_KEY_ENV, raising=False)
    monkeypatch.delenv(gk.LEGACY_GATEWAY_KEY_ENV, raising=False)
    monkeypatch.setattr(gk, "_memory_key", None)
    monkeypatch.setattr(gk, "_dead_env_loaded", True)
    monkeypatch.setattr(gk, "_vault_get", lambda name: None)
    gk.reset_account_key_memo()
    # The stray variable.
    monkeypatch.setenv(gk.ACCOUNT_KEY_ENV, ACCOUNT)
    from olano.deployment import resolver, sources

    status = SimpleNamespace(
        posture="quarantine", settings=SimpleNamespace(feature_enabled=lambda n: True)
    )
    monkeypatch.setattr(resolver, "deployment_status", lambda now=None: status)
    monkeypatch.setattr(sources, "deployment_token", lambda: None)
    yield
    gk.reset_account_key_memo()


def test_the_raw_slot_sees_it_but_the_predicate_does_not(managed_box) -> None:
    assert gk.account_key() == ACCOUNT  # the slot is just a value
    assert gk.account_key_if_unmanaged() is None
    assert gk.gateway_key() is None
    assert gk.gateway_key_source() is None


def test_no_gate_flips(managed_box, monkeypatch) -> None:
    """Every gate answers exactly what it answers WITHOUT the stray variable.
    (Some are True on a managed box by posture alone, which is the managed
    default and not a flip; the invariant is equality, not falsehood.)"""
    from olano.deployment import linkedin_client, services_client
    from olano.tools import image_tools

    monkeypatch.setattr(linkedin_client, "_byok", lambda: False)
    gates = {
        "olano_credits_available": ss.olano_credits_available,
        "services_available": services_client.services_available,
        "service_credential": services_client.service_credential,
        "service_credential_pair": services_client.service_credential_pair,
        "gateway_media_reachable": image_tools.gateway_media_reachable,
        "linkedin_service_available": linkedin_client.linkedin_service_available,
    }
    with_key = {name: gate() for name, gate in gates.items()}
    monkeypatch.delenv(gk.ACCOUNT_KEY_ENV)
    monkeypatch.delenv("LIONCLAW_CREDITS_KEY", raising=False)
    gk.reset_account_key_memo()
    without_key = {name: gate() for name, gate in gates.items()}
    assert with_key == without_key
    # And none of them took the account branch: no token means no credential.
    assert with_key["service_credential"] is None
    assert with_key["service_credential_pair"] is None
    assert with_key["services_available"] is False


def test_cost_display_is_what_it_is_without_the_key(
    managed_box, tmp_path: Path, monkeypatch
) -> None:
    """A managed box's cost display is decided by posture (locked to credits
    off the open posture); the stray key must not be what decides it."""
    monkeypatch.delenv("LIONCLAW_COST_DISPLAY", raising=False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("")
    with_key = ss.cost_display_mode(cfg)
    monkeypatch.delenv(gk.ACCOUNT_KEY_ENV)
    monkeypatch.delenv("LIONCLAW_CREDITS_KEY", raising=False)
    gk.reset_account_key_memo()
    assert ss.cost_display_mode(cfg) == with_key


def test_the_credits_route_predicate_stays_off(managed_box, monkeypatch) -> None:
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    from olano.runtime.api import create_api

    # A managed posture defaults multi-user mode ON (login gate), which would
    # 401 the anonymous test client; this test is about the key, not auth.
    monkeypatch.setattr("olano.runtime.auth.multi_user_enabled", lambda: False)
    runtime = MagicMock()
    runtime._runners = {}
    body = TestClient(create_api(runtime)).get("/api/credits").json()
    assert body.get("credits_enabled") is False
