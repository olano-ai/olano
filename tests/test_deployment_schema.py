"""Deployment Settings schema — defaults, coercion, forward compatibility."""

from __future__ import annotations

from olano.deployment.schema import (
    KNOWN_FEATURES,
    KNOWN_TABS,
    OPEN_SETTINGS,
    QUARANTINE_SETTINGS,
    DeploymentSettings,
    SignedSettings,
    UsageLimit,
)


class TestDefaultsArePermissive:
    def test_default_settings_are_open(self):
        s = DeploymentSettings()
        assert s.resources.max_agents is None
        assert s.resources.max_connectors_per_agent is None
        assert s.resources.max_connectors_total is None
        assert s.resources.max_mcp_servers_total is None
        assert s.resources.max_webhooks_total is None
        assert s.resources.max_users is None
        assert s.usage.input_tokens_total.value is None
        assert s.usage.cost_total.value is None

    def test_missing_feature_and_tab_keys_mean_enabled(self):
        s = DeploymentSettings()
        for name in KNOWN_FEATURES:
            assert s.feature_enabled(name) is True
        for name in KNOWN_TABS:
            assert s.tab_enabled(name) is True
        # Unknown names also default on — new toggles must be additive.
        assert s.feature_enabled("some_future_feature") is True
        assert s.tab_enabled("some_future_tab") is True

    def test_explicit_false_disables(self):
        s = DeploymentSettings(features={"terminal": False}, tabs={"cortex": False})
        assert s.feature_enabled("terminal") is False
        assert s.tab_enabled("cortex") is False
        assert s.feature_enabled("shell") is True

    def test_sandbox_exec_is_a_known_feature(self):
        # The control-plane vocabulary must carry sandbox_exec (mirrors cloud
        # KNOWN_FEATURES) so a signed setting can turn on managed exec. Strict
        # cloud schema rejects unknown names, so a missing entry here would make
        # sandbox_exec un-signable and leave managed boxes with no code exec.
        assert "sandbox_exec" in KNOWN_FEATURES

    def test_open_settings_constant_is_default(self):
        assert OPEN_SETTINGS == DeploymentSettings()


class TestWindowCoercion:
    def test_valid_windows_pass_through(self):
        for w in ("daily", "weekly", "monthly", "lifetime"):
            assert UsageLimit(value=1, window=w).window == w

    def test_unknown_window_coerces_to_monthly(self):
        assert UsageLimit(value=1, window="hourly").window == "monthly"
        assert UsageLimit(value=1, window="").window == "monthly"

    def test_non_string_window_coerces_to_monthly(self):
        assert UsageLimit.model_validate({"value": 1, "window": 7}).window == "monthly"

    def test_default_window_is_monthly(self):
        assert UsageLimit().window == "monthly"


class TestForwardCompatibility:
    def test_unknown_fields_survive_round_trip(self):
        raw = {
            "resources": {"max_agents": 3, "max_widgets": 9},
            "usage": {"cost_total": {"value": 100, "window": "monthly", "burst": 2}},
            "features": {"terminal": False},
            "brand_new_block": {"nested": True},
        }
        s = DeploymentSettings.model_validate(raw)
        dumped = s.model_dump()
        assert dumped["brand_new_block"] == {"nested": True}
        assert dumped["resources"]["max_widgets"] == 9
        assert dumped["usage"]["cost_total"]["burst"] == 2
        assert s.resources.max_agents == 3

    def test_signed_settings_tolerates_unknown_claims(self):
        payload = _minimal_payload()
        payload["brand_new_claim"] = "x"
        parsed = SignedSettings.model_validate(payload)
        assert parsed.deployment_id == "dep_test"
        assert parsed.model_dump()["brand_new_claim"] == "x"


class TestQuarantineShape:
    def test_quarantine_locks_down_resources(self):
        q = QUARANTINE_SETTINGS
        assert q.resources.max_agents == 1
        assert q.resources.max_connectors_total == 0
        assert q.resources.max_mcp_servers_total == 0
        assert q.resources.max_webhooks_total == 0
        assert q.resources.max_users == 1

    def test_quarantine_disables_sensitive_features(self):
        q = QUARANTINE_SETTINGS
        for name in (
            "shell",
            "terminal",
            "father_agent",
            "cortex",
            "agent_config_raw",
            "model_selection",
        ):
            assert q.feature_enabled(name) is False, name

    def test_quarantine_keeps_recovery_surface(self):
        q = QUARANTINE_SETTINGS
        # The customer must still be able to see what's wrong and fix it.
        for tab in ("overview", "chat", "agents", "config", "admin"):
            assert q.tab_enabled(tab) is True, tab
        # Small but non-zero recovery budget.
        assert (q.usage.input_tokens_total.value or 0) > 0
        assert (q.usage.cost_total.value or 0) > 0


def _minimal_payload() -> dict:
    return {
        "schema": "olano.deployment-settings",
        "schema_version": 1,
        "issuer": "cloud.olano.ai",
        "audience": "olano-deployment-settings",
        "channel": "local",
        "deployment_id": "dep_test",
        "policy_revision": 1,
        "issued_at": 1_700_000_000,
        "expires_at": 1_800_000_000,
        "settings": {},
    }
