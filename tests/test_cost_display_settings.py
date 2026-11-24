"""Cost display is signed deployment policy, not operator preference.

Requirement under test: on a MANAGED box the display unit comes from the
signed deployment settings — default "credits" — and neither config.yaml nor
the env var can override it (the operator and the agent can both write
config.yaml, and the number they'd be flipping back to is Olano's COGS, not
their price). Unmanaged OSS installs keep the local controls and default to
honest dollars.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from olano.config import system_settings
from olano.deployment.schema import DeploymentSettings


def _managed(monkeypatch, cost_display: str | None) -> None:
    status = SimpleNamespace(
        posture="enforced",
        settings=DeploymentSettings(cost_display=cost_display),
    )
    from olano.deployment import resolver

    monkeypatch.setattr(resolver, "deployment_status", lambda now=None: status)


def _unmanaged(monkeypatch) -> None:
    status = SimpleNamespace(posture="open", settings=DeploymentSettings())
    from olano.deployment import resolver

    monkeypatch.setattr(resolver, "deployment_status", lambda now=None: status)


def test_managed_defaults_to_credits(monkeypatch) -> None:
    _managed(monkeypatch, None)
    monkeypatch.delenv("LIONCLAW_COST_DISPLAY", raising=False)
    assert system_settings.cost_display_mode() == "credits"
    assert system_settings.cost_display_locked() is True


def test_managed_signed_value_wins(monkeypatch) -> None:
    _managed(monkeypatch, "usd")
    assert system_settings.cost_display_mode() == "usd"


def test_managed_ignores_env_and_config(monkeypatch) -> None:
    """The whole point: local overrides are dead on a managed box."""
    _managed(monkeypatch, None)
    monkeypatch.setenv("LIONCLAW_COST_DISPLAY", "usd")
    assert system_settings.cost_display_mode() == "credits"
    # Even a config.yaml write (the agent can do this!) changes nothing.
    system_settings.set_cost_display_mode("usd")
    assert system_settings.cost_display_mode() == "credits"


def test_managed_garbage_signed_value_degrades_to_credits(monkeypatch) -> None:
    _managed(monkeypatch, "pesos")
    assert system_settings.cost_display_mode() == "credits"


def test_unmanaged_env_beats_config_beats_usd_default(monkeypatch) -> None:
    _unmanaged(monkeypatch)
    monkeypatch.delenv("LIONCLAW_COST_DISPLAY", raising=False)
    assert system_settings.cost_display_mode() == "usd"
    system_settings.set_cost_display_mode("credits")
    assert system_settings.cost_display_mode() == "credits"
    monkeypatch.setenv("LIONCLAW_COST_DISPLAY", "usd")
    assert system_settings.cost_display_mode() == "usd"
    assert system_settings.cost_display_locked() is False


def test_broken_resolver_never_blanks_costs(monkeypatch) -> None:
    """Resolver trouble must degrade to the unmanaged chain, not raise."""
    from olano.deployment import resolver

    def boom(now=None):
        raise RuntimeError("resolver down")

    monkeypatch.setattr(resolver, "deployment_status", boom)
    monkeypatch.delenv("LIONCLAW_COST_DISPLAY", raising=False)
    assert system_settings.cost_display_mode() == "usd"
    assert system_settings.cost_display_locked() is False


# ── cross-repo contract: the cloud's artifact shape parses ───────────────────


def test_cloud_shaped_settings_payload_parses() -> None:
    """Mirror of the cloud's deploymentSettingsSchema output (schema.ts) with
    the new field — the two repos' schemas must not drift. An old runtime
    ignores the field (extra=allow); this runtime types it."""
    payload = {
        "resources": {"max_agents": 3},
        "usage": {},
        "features": {"olano_models": True},
        "tabs": {},
        "credits_left": 1234.5,
        "olano_model_credits": {},
        "cost_display": "credits",
        "extensions": {},
    }
    settings = DeploymentSettings.model_validate(payload)
    assert settings.cost_display == "credits"
    assert settings.credits_left == 1234.5

    # Field absent (old cloud) and null (explicit no-opinion) both → None,
    # which the resolution layer treats as the managed default (credits).
    assert DeploymentSettings.model_validate({}).cost_display is None
    assert DeploymentSettings.model_validate({"cost_display": None}).cost_display is None


@pytest.mark.parametrize("mode", ["usd", "credits"])
def test_signed_roundtrip_carries_cost_display(mode) -> None:
    """Full envelope round-trip: mint, verify, and read the field off the
    parsed settings — the instance-side stand-in for the cloud golden vector
    (wire format + payload-byte signing are shared with the Node signer)."""
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.asymmetric import ed25519

    from olano.deployment.envelope import sign_license, verify_license

    now = 1_780_000_000
    private = ed25519.Ed25519PrivateKey.generate()
    keys = {"settings-test": private.public_key()}
    payload = {
        "schema": "olano.deployment-settings",
        "schema_version": 1,
        "issuer": "cloud.olano.ai",
        "audience": "olano-deployment-settings",
        "channel": "local",
        "license_id": "lic_test",
        "deployment_id": "dep_test",
        "plan": "pro",
        "policy_revision": 4_000_001,
        "issued_at": now - 1000,
        "not_before": now - 1000,
        "refresh_after": None,
        "expires_at": now + 86400,
        "settings": {"cost_display": mode},
    }
    text = sign_license(payload, private, key_id="settings-test")
    parsed = verify_license(text, expected_channel="local", keys=keys, now=now)
    assert parsed.settings.cost_display == mode
