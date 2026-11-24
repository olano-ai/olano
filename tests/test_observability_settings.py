"""Langfuse tracing settings, the per-agent override, credentials and the policy.

The property under test: a turn is traced only when the resolved switch is on
AND a key pair resolves, the agent's opinion wins over the deployment's, and
anything malformed reads as "not traced" or as the safer value, never as an
error and never as more export than was asked for.
"""

from __future__ import annotations

import pytest
import yaml

from olano.config.agent_config import AgentConfig, AgentLangfuseConfig
from olano.config.home import get_olano_paths
from olano.config.system_settings import (
    LangfuseSettings,
    load_observability_settings,
    normalize_langfuse_host,
    set_langfuse_settings,
)
from olano.observability import credentials
from olano.observability.policy import (
    DEFAULT_LANGFUSE_HOST,
    effective_trace_policy,
    trace_identity,
)
from olano.vault.resolver import set_vault
from olano.vault.store import VaultStore

PK = "pk-lf-1111aaaa"
SK = "sk-lf-2222bbbb"


def _write_block(block: dict) -> None:
    path = get_olano_paths().config_path
    raw = yaml.safe_load(path.read_text()) if path.exists() else {}
    raw = raw or {}
    raw["observability"] = block
    path.write_text(yaml.safe_dump(raw))


def _agent(name: str = "merli", **langfuse) -> AgentConfig:
    return AgentConfig(
        name=name,
        model="anthropic:claude-haiku-4-5-20251001",
        observability={"langfuse": langfuse},
    )


@pytest.fixture
def vault(lionclaw_home):
    store = VaultStore.open(
        passphrase="test-passphrase", db_path=lionclaw_home / "vault-obs.db", use_keyring=False
    )
    set_vault(store)
    credentials._reset_for_tests()
    yield store
    set_vault(None)
    credentials._reset_for_tests()


@pytest.fixture
def env_keys(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", PK)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", SK)
    credentials._reset_for_tests()


# ── settings ────────────────────────────────────────────────────────────────


def test_defaults_are_off_and_metadata():
    s = LangfuseSettings()
    assert s.enabled is False
    assert s.capture == "metadata"
    assert s.sample_rate == 1.0
    assert s.trace_background is True
    assert load_observability_settings().langfuse == s


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://jp.cloud.langfuse.com/", "https://jp.cloud.langfuse.com"),
        ("https://lf.example.com:8443/some/path", "https://lf.example.com:8443"),
        ("http://localhost:3000", "http://localhost:3000"),
        ("http://127.0.0.1:3000", "http://127.0.0.1:3000"),
        ("http://lf.example.com", ""),  # credentials ride every export: no plain http
        ("https://user:pw@lf.example.com", ""),
        ("ftp://lf.example.com", ""),
        ("not a url", ""),
        ("", ""),
    ],
)
def test_host_normalization(raw, expected):
    assert normalize_langfuse_host(raw) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("full", "full"),
        ("FULL", "full"),
        ("metadata", "metadata"),
        ("fulll", "metadata"),
        (None, "metadata"),
    ],
)
def test_unknown_capture_reads_as_metadata(value, expected):
    assert LangfuseSettings(capture=value).capture == expected


def test_sample_rate_clamped_and_nan_safe():
    assert LangfuseSettings(sample_rate=5).sample_rate == 1.0
    assert LangfuseSettings(sample_rate=-1).sample_rate == 0.0
    assert LangfuseSettings(sample_rate=float("nan")).sample_rate == 1.0
    assert LangfuseSettings(sample_rate="0.25").sample_rate == 0.25


def test_environment_and_project_id_charset():
    assert LangfuseSettings(environment="Production").environment == "production"
    assert LangfuseSettings(environment="langfuse-x").environment == ""
    assert LangfuseSettings(environment="has space").environment == ""
    assert LangfuseSettings(project_id="cmvdimqo2000lad0jcd39a66s").project_id
    assert LangfuseSettings(project_id="../etc").project_id == ""


def test_malformed_block_reads_as_off(lionclaw_home):
    _write_block("yes please")
    assert load_observability_settings().langfuse.enabled is False
    _write_block({"langfuse": ["on"]})
    assert load_observability_settings().langfuse.enabled is False


def test_set_langfuse_settings_round_trips_and_preserves_other_keys(lionclaw_home):
    path = get_olano_paths().config_path
    path.write_text(yaml.safe_dump({"browser": {"mode": "off"}, "observability": {"other": 1}}))
    set_langfuse_settings({"enabled": True, "capture": "bogus", "host": "https://x.example.com/"})
    raw = yaml.safe_load(path.read_text())
    assert raw["browser"] == {"mode": "off"}
    assert raw["observability"]["other"] == 1
    lf = raw["observability"]["langfuse"]
    assert lf["enabled"] is True
    assert lf["capture"] == "metadata"
    assert lf["host"] == "https://x.example.com"
    set_langfuse_settings({"project_id": "p123"})
    assert load_observability_settings().langfuse.project_id == "p123"
    assert load_observability_settings().langfuse.enabled is True


def test_agent_override_is_three_state():
    assert AgentLangfuseConfig().model_dump() == {
        "enabled": None,
        "capture": None,
        "sample_rate": None,
    }
    o = AgentLangfuseConfig(enabled=False, capture="weird", sample_rate="2")
    assert o.enabled is False
    assert o.capture == "metadata"
    assert o.sample_rate == 1.0
    assert AgentLangfuseConfig(capture="", sample_rate="").capture is None
    assert AgentLangfuseConfig(sample_rate="x").sample_rate is None


def test_agent_config_carries_the_block_and_defaults_inherit():
    cfg = AgentConfig(name="a", model="anthropic:claude-haiku-4-5-20251001")
    assert cfg.observability.langfuse.enabled is None
    dumped = cfg.model_dump()
    assert dumped["observability"] == {
        "langfuse": {"enabled": None, "capture": None, "sample_rate": None}
    }


# ── policy ────────────────────────────────────────────────────────────────────


def test_off_by_default_even_with_keys(lionclaw_home, env_keys):
    assert effective_trace_policy(_agent()) is None


def test_on_without_keys_is_not_traced(lionclaw_home):
    _write_block({"langfuse": {"enabled": True}})
    assert effective_trace_policy(_agent()) is None


def test_on_with_env_keys(lionclaw_home, env_keys):
    _write_block({"langfuse": {"enabled": True}})
    policy = effective_trace_policy(_agent())
    assert policy is not None
    assert policy.public_key == PK and policy.secret_key == SK
    assert policy.host == DEFAULT_LANGFUSE_HOST
    assert policy.capture == "metadata"
    assert policy.credential_source == "env"


def test_base_url_then_legacy_host(lionclaw_home, env_keys, monkeypatch):
    _write_block({"langfuse": {"enabled": True}})
    monkeypatch.setenv("LANGFUSE_HOST", "https://us.cloud.langfuse.com")
    credentials._reset_for_tests()
    assert effective_trace_policy(_agent()).host == "https://us.cloud.langfuse.com"
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://jp.cloud.langfuse.com")
    credentials._reset_for_tests()
    assert effective_trace_policy(_agent()).host == "https://jp.cloud.langfuse.com"
    # The config's host wins over the credentials' host.
    _write_block({"langfuse": {"enabled": True, "host": "https://lf.example.com"}})
    assert effective_trace_policy(_agent()).host == "https://lf.example.com"


def test_agent_override_wins_both_ways(lionclaw_home, env_keys):
    _write_block({"langfuse": {"enabled": True, "capture": "metadata", "sample_rate": 0.5}})
    assert effective_trace_policy(_agent(enabled=False)) is None
    p = effective_trace_policy(_agent(capture="full", sample_rate=1.0))
    assert p.capture == "full" and p.sample_rate == 1.0
    p = effective_trace_policy(_agent())
    assert p.capture == "metadata" and p.sample_rate == 0.5

    _write_block({"langfuse": {"enabled": False}})
    assert effective_trace_policy(_agent()) is None
    assert effective_trace_policy(_agent(enabled=True)) is not None


def test_worker_is_traced_as_its_parent():
    cfg = AgentConfig(name="merli__cog__x__ab12", model="anthropic:claude-haiku-4-5-20251001")
    cfg.credential_scope = "merli"
    assert trace_identity(cfg) == "merli"


# ── credentials ───────────────────────────────────────────────────────────────


def test_vault_beats_env_and_agent_override_beats_vault(lionclaw_home, vault, env_keys):
    _write_block({"langfuse": {"enabled": True}})
    credentials.store_credentials("pk-lf-vault", "sk-lf-vault", "https://jp.cloud.langfuse.com")
    p = effective_trace_policy(_agent())
    assert (p.public_key, p.credential_source, p.host) == (
        "pk-lf-vault",
        "vault",
        "https://jp.cloud.langfuse.com",
    )
    credentials.store_credentials("pk-lf-merli", "sk-lf-merli", agent="merli")
    p = effective_trace_policy(_agent())
    assert (p.public_key, p.secret_key, p.credential_source) == (
        "pk-lf-merli",
        "sk-lf-merli",
        "agent vault",
    )
    # Another agent still uses the deployment's row.
    assert effective_trace_policy(_agent("other")).public_key == "pk-lf-vault"
    credentials.clear_credentials(agent="merli")
    assert effective_trace_policy(_agent()).public_key == "pk-lf-vault"
    credentials.clear_credentials()
    assert effective_trace_policy(_agent()).credential_source == "env"


def test_half_a_key_pair_never_counts(lionclaw_home, vault, monkeypatch):
    _write_block({"langfuse": {"enabled": True}})
    vault.set(credentials.PUBLIC_KEY_NAME, "pk-lf-only", category="system")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", SK)
    credentials._reset_for_tests()
    assert effective_trace_policy(_agent()) is None


def test_keys_are_reserved_and_never_resolve_for_an_agent(lionclaw_home, vault):
    from olano.tools._credentials import agent_runtime_context, get_credential
    from olano.vault.reserved import is_runtime_secret

    credentials.store_credentials(PK, SK)
    for name in (
        credentials.PUBLIC_KEY_NAME,
        credentials.SECRET_KEY_NAME,
        credentials.HOST_NAME,
        credentials.USER_SALT_NAME,
    ):
        assert is_runtime_secret(name)
    with agent_runtime_context("merli", {}):
        assert get_credential(credentials.SECRET_KEY_NAME) == ""
        assert get_credential(credentials.PUBLIC_KEY_NAME) == ""


def test_store_refuses_without_a_vault(lionclaw_home, monkeypatch):
    monkeypatch.setattr(credentials, "_vault", lambda: None)
    with pytest.raises(RuntimeError, match="vault is locked"):
        credentials.store_credentials(PK, SK)


def test_user_salt_is_stable(lionclaw_home, vault):
    first = credentials.user_salt()
    credentials._reset_for_tests()
    assert credentials.user_salt() == first
    assert len(first) == 64


def test_secret_key_is_stripped_from_every_shell(monkeypatch):
    from olano.security.shell_guard import sanitized_shell_env

    env = sanitized_shell_env(
        {"LANGFUSE_SECRET_KEY": SK, "OLANO_LANGFUSE_SECRET_KEY": SK, "PATH": "/bin"}
    )
    assert "LANGFUSE_SECRET_KEY" not in env
    assert "OLANO_LANGFUSE_SECRET_KEY" not in env
    assert env["PATH"] == "/bin"
