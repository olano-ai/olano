"""Enforcement decisions — caps, shrink tolerance, quarantine, concurrency."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from olano.deployment import enforcement, resolver
from olano.deployment.envelope import sign_license

NOW = 1_780_000_000
KEY_ID = "settings-test-2026-07"
DEP_ID = "dep_enforcement_test"

_PRIVATE = ed25519.Ed25519PrivateKey.generate()
_KEYS = {KEY_ID: _PRIVATE.public_key()}


def mint(settings: dict, **overrides) -> str:
    payload: dict = {
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
    }
    payload.update(overrides)
    return sign_license(payload, _PRIVATE, key_id=KEY_ID)


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    """Isolated, unmanaged home; cwd moved off the repo so the project-local
    agents/ dir can't leak into resource counts."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LIONCLAW_INSTALLATION_FILE", str(tmp_path / "installation.json"))
    monkeypatch.delenv("LIONCLAW_LICENSE", raising=False)
    monkeypatch.delenv("LIONCLAW_LICENSE_PATH", raising=False)
    monkeypatch.setattr("olano.deployment.envelope.load_verify_keys", lambda: _KEYS)
    resolver.invalidate()
    enforcement.invalidate_counts()
    yield tmp_path
    resolver.invalidate()
    enforcement.invalidate_counts()


def manage(home: Path, settings: dict) -> None:
    """Flip the home into managed-local with a license carrying *settings*."""
    (home / "installation.json").write_text(
        json.dumps({"managed": True, "mode": "local", "deployment_id": DEP_ID})
    )
    from olano.config.home import get_olano_paths

    paths = get_olano_paths()
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.deployment_license_path.write_text(mint(settings) + "\n")
    resolver.invalidate()
    enforcement.invalidate_counts()


def quarantine(home: Path) -> None:
    """Managed manifest with no license ⇒ quarantine."""
    (home / "installation.json").write_text(
        json.dumps({"managed": True, "mode": "local", "deployment_id": DEP_ID})
    )
    resolver.invalidate()
    enforcement.invalidate_counts()


def add_agent(name: str, **fields) -> Path:
    from olano.config.home import get_olano_paths

    agent_dir = get_olano_paths().agents_dir / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / "agent.json5"
    path.write_text(json.dumps({"name": name, **fields}))
    enforcement.invalidate_counts()
    return path


class TestOpenPosture:
    def test_everything_allowed_unmanaged(self, home):
        for name in ("a", "b", "c", "d", "e"):
            add_agent(name, channels=[{"type": "telegram"}] * 3)
        assert enforcement.can_create_agent().allowed
        assert enforcement.can_add_connector("a").allowed
        assert enforcement.can_add_mcp().allowed
        assert enforcement.can_add_webhook().allowed
        assert enforcement.can_create_user(999).allowed
        assert enforcement.agent_turn_allowed("anything").allowed
        assert enforcement.feature_enabled("shell")
        assert enforcement.validate_config_write("a", None, {"name": "z"}).allowed


class TestAgentCap:
    def test_under_cap_allowed(self, home):
        manage(home, {"resources": {"max_agents": 3}})
        add_agent("a")
        assert enforcement.can_create_agent().allowed

    def test_at_cap_denied_with_structure(self, home):
        manage(home, {"resources": {"max_agents": 2}})
        add_agent("a")
        add_agent("b")
        decision = enforcement.can_create_agent()
        assert not decision.allowed
        assert decision.code == "deployment_limit_max_agents"
        assert decision.limit == 2
        assert decision.current == 2
        assert decision.remediation

    def test_unlimited_when_none(self, home):
        manage(home, {"resources": {}})
        for i in range(10):
            add_agent(f"agent{i}")
        assert enforcement.can_create_agent().allowed


class TestHiddenAgentsAreNotCounted:
    """The image ships per-provider test agents (`hidden: true`). They never
    boot, so charging a customer's plan for them refused every install on a box
    whose real roster was empty."""

    def test_hidden_agents_do_not_count_toward_the_cap(self, home):
        manage(home, {"resources": {"max_agents": 3}})
        for i in range(7):
            add_agent(f"test-agent-{i}", hidden=True)
        add_agent("merli")
        assert enforcement.count_deployment_objects(fresh=True).agents == 1
        assert enforcement.can_create_agent().allowed

    def test_hidden_agent_resources_are_excluded(self, home):
        manage(home, {"resources": {"max_connectors_total": 2}})
        add_agent(
            "test-agent-fake",
            hidden=True,
            channels=[{"type": "telegram"}] * 3,
            mcp_servers=[{"name": "x"}],
            webhooks=[{"name": "w"}],
        )
        counts = enforcement.count_deployment_objects(fresh=True)
        assert counts.connectors_total == 0
        assert counts.mcp_total == 0
        assert counts.webhooks_total == 0

    def test_only_an_explicit_true_hides(self, home):
        manage(home, {"resources": {"max_agents": 5}})
        add_agent("truthy", hidden="yes")
        add_agent("falsy", hidden=False)
        assert enforcement.count_deployment_objects(fresh=True).agents == 2

    def test_unparseable_config_still_counts(self, home):
        manage(home, {"resources": {"max_agents": 5}})
        path = add_agent("broken")
        path.write_text("{ this is not json5")
        enforcement.invalidate_counts()
        assert enforcement.count_deployment_objects(fresh=True).agents == 1

    def test_new_agent_allowed_at_cap_when_existing_are_hidden(self, home):
        manage(home, {"resources": {"max_agents": 1}})
        for i in range(7):
            add_agent(f"test-agent-{i}", hidden=True)
        assert enforcement.validate_config_write("newbie", None, {"name": "newbie"}).allowed


class TestConnectorCaps:
    def test_per_agent_cap(self, home):
        manage(home, {"resources": {"max_connectors_per_agent": 2}})
        add_agent("a", channels=[{"type": "telegram"}, {"type": "slack"}])
        add_agent("b", channels=[])
        denied = enforcement.can_add_connector("a")
        assert not denied.allowed
        assert denied.code == "deployment_limit_max_connectors_per_agent"
        assert enforcement.can_add_connector("b").allowed

    def test_total_cap(self, home):
        manage(home, {"resources": {"max_connectors_total": 3}})
        add_agent("a", channels=[{"type": "telegram"}, {"type": "slack"}])
        add_agent("b", channels=[{"type": "discord"}])
        denied = enforcement.can_add_connector("b")
        assert not denied.allowed
        assert denied.code == "deployment_limit_max_connectors_total"


class TestMcpWebhookUserCaps:
    def test_mcp_total(self, home):
        manage(home, {"resources": {"max_mcp_servers_total": 1}})
        add_agent("a", mcp_servers=[{"name": "x"}])
        denied = enforcement.can_add_mcp()
        assert not denied.allowed
        assert denied.code == "deployment_limit_max_mcp_servers_total"

    def test_webhook_total(self, home):
        manage(home, {"resources": {"max_webhooks_total": 0}})
        add_agent("a")
        denied = enforcement.can_add_webhook()
        assert not denied.allowed
        assert denied.code == "deployment_limit_max_webhooks_total"

    def test_user_cap(self, home):
        manage(home, {"resources": {"max_users": 2}})
        assert enforcement.can_create_user(1).allowed
        denied = enforcement.can_create_user(2)
        assert not denied.allowed
        assert denied.code == "deployment_limit_max_users"


class TestConfigWriteShrinkTolerance:
    """The `new > limit and new > old` matrix: growth beyond cap is blocked,
    shrinking and re-saving over-limit configs must always succeed."""

    def test_growth_beyond_cap_blocked(self, home):
        manage(home, {"resources": {"max_connectors_per_agent": 1}})
        add_agent("a", channels=[{"type": "telegram"}])
        old = {"name": "a", "channels": [{"type": "telegram"}]}
        new = {"name": "a", "channels": [{"type": "telegram"}, {"type": "slack"}]}
        decision = enforcement.validate_config_write("a", old, new)
        assert not decision.allowed
        assert decision.code == "deployment_limit_max_connectors_per_agent"

    def test_resave_over_limit_config_allowed(self, home):
        # Caps shrank below current usage: re-saving unchanged must succeed.
        manage(home, {"resources": {"max_connectors_per_agent": 1}})
        over = {"name": "a", "channels": [{"type": "telegram"}, {"type": "slack"}]}
        add_agent("a", channels=over["channels"])
        assert enforcement.validate_config_write("a", over, dict(over)).allowed

    def test_shrinking_over_limit_config_allowed(self, home):
        manage(home, {"resources": {"max_connectors_per_agent": 1}})
        old = {"name": "a", "channels": [{"type": "t"}, {"type": "s"}, {"type": "d"}]}
        add_agent("a", channels=old["channels"])
        new = {"name": "a", "channels": [{"type": "t"}, {"type": "s"}]}
        # Still over the cap of 1, but strictly shrinking → allowed.
        assert enforcement.validate_config_write("a", old, new).allowed

    def test_new_agent_over_agent_cap_blocked(self, home):
        manage(home, {"resources": {"max_agents": 1}})
        add_agent("a")
        decision = enforcement.validate_config_write("b", None, {"name": "b"})
        assert not decision.allowed
        assert decision.code == "deployment_limit_max_agents"

    def test_total_delta_uses_other_agents(self, home):
        manage(home, {"resources": {"max_mcp_servers_total": 2}})
        add_agent("a", mcp_servers=[{"name": "x"}, {"name": "y"}])
        add_agent("b", mcp_servers=[])
        old = {"name": "b", "mcp_servers": []}
        new = {"name": "b", "mcp_servers": [{"name": "z"}]}
        decision = enforcement.validate_config_write("b", old, new)
        assert not decision.allowed
        assert decision.code == "deployment_limit_max_mcp_servers_total"


class TestQuarantine:
    def test_creations_blocked(self, home):
        quarantine(home)
        for decision in (
            enforcement.can_create_agent(),
            enforcement.can_add_connector("a"),
            enforcement.can_add_mcp(),
            enforcement.can_add_webhook(),
            enforcement.can_create_user(0),
        ):
            assert not decision.allowed
            assert decision.code == "deployment_quarantined"

    def test_only_default_agent_takes_turns(self, home, monkeypatch):
        quarantine(home)
        from olano.config.home import update_home_config

        update_home_config({"default_agent": "merli"})
        assert enforcement.agent_turn_allowed("merli").allowed
        denied = enforcement.agent_turn_allowed("other")
        assert not denied.allowed
        assert denied.code == "deployment_quarantined"

    def test_resave_allowed_growth_blocked(self, home):
        quarantine(home)
        old = {"name": "a", "channels": [{"type": "t"}]}
        assert enforcement.validate_config_write("a", old, dict(old)).allowed
        grown = {"name": "a", "channels": [{"type": "t"}, {"type": "s"}]}
        assert not enforcement.validate_config_write("a", old, grown).allowed

    def test_sensitive_features_off(self, home):
        quarantine(home)
        for f in ("shell", "terminal", "father_agent", "cortex", "agent_config_raw"):
            assert not enforcement.feature_enabled(f), f


class TestPolicyUnavailable:
    def test_count_failure_rejects_not_allows(self, home, monkeypatch):
        manage(home, {"resources": {"max_agents": 5}})
        monkeypatch.setattr(
            enforcement,
            "count_deployment_objects",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        decision = enforcement.can_create_agent()
        assert not decision.allowed
        assert decision.code == "policy_unavailable"


class TestConcurrentCreates:
    def test_parallel_creates_cannot_exceed_cap(self, home):
        """N threads race save_agent_config for distinct new agents; with
        max_agents=3 exactly 3 may win regardless of interleaving."""
        manage(home, {"resources": {"max_agents": 3}})

        from olano.config.agent_config import AgentConfig
        from olano.config.loader import list_agents, save_agent_config

        results: list[bool] = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def create(i: int) -> None:
            barrier.wait()
            try:
                save_agent_config(AgentConfig(name=f"racer{i}"))
                ok = True
            except enforcement.DeploymentLimitExceeded:
                ok = False
            with lock:
                results.append(ok)

        threads = [threading.Thread(target=create, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(results) == 3, results
        assert len(list_agents()) == 3


class TestFeatureDecisions:
    def test_disabled_feature_decision(self, home):
        manage(home, {"features": {"terminal": False, "father_agent": False}})
        assert enforcement.feature_enabled("shell")
        decision = enforcement.feature_decision("terminal")
        assert not decision.allowed
        assert decision.code == "deployment_feature_terminal_disabled"
        assert not enforcement.feature_decision("father_agent", label="FatherAgent").allowed

    def test_tab_toggle(self, home):
        manage(home, {"tabs": {"insights": False}})
        assert not enforcement.tab_enabled("insights")
        assert enforcement.tab_enabled("overview")
