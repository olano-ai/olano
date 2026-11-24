"""Tests for per-customer credentials (each end-customer brings their own token).

The contract: secrets live encrypted under (agent, customer, name) — a
namespace separate from every operator table; only DECLARED names are
accepted; during a customer-bound turn a declared name resolves to the
customer's own value or NOTHING (fail closed — never the operator's token);
with no customer bound the normal chain is untouched; action runs get the
customer's values as env vars; forget_me erases them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from olano.config.agent_config import AgentConfig
from olano.memory.customer_memory import (
    active_customer_id,
    make_customer_memory_tools,
)
from olano.security.request_context import push_request_context
from olano.tools._credentials import get_credential
from olano.tools.customer_credentials import (
    customer_secret_env,
    make_customer_credential_tools,
    register_customer_credential_names,
)

AGENT = "supportbot"
NAME = "SHEETS_TOKEN"


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def _seed(platform: str, user: str) -> str:
    with push_request_context(source="channel", channel=platform, user_id=user):
        mem = make_customer_memory_tools(AGENT)
        _tool(mem, "remember").invoke({"fact": "seeded"})
        return str(active_customer_id())


def _config(**cs_extra) -> AgentConfig:
    return AgentConfig(
        name=AGENT,
        customer_service={
            "enabled": True,
            "user_credentials": True,
            "customer_credential_names": [NAME],
            **cs_extra,
        },
    )


@pytest.fixture
def cred_tools(lionclaw_home, tmp_path_factory, monkeypatch):
    # A real (test-keyed) vault registered the way the runtime does at boot.
    import olano.vault.resolver as resolver
    from olano.vault.store import VaultStore

    store = VaultStore.open(
        passphrase="test-vault-key-for-customer-credentials",
        db_path=tmp_path_factory.mktemp("vault") / "vault.db",
    )
    monkeypatch.setattr(resolver, "_vault_instance", store)
    tools = make_customer_credential_tools(AGENT, _config())
    yield tools
    register_customer_credential_names(AGENT, [])


def _as(user: str):
    # is_owner=False: how a real inbound customer turn is bound (the fallback
    # context and operator chats are owner=True and use the normal chain).
    return push_request_context(source="channel", channel="telegram", user_id=user, is_owner=False)


def test_store_list_delete_round_trip(cred_tools):
    _seed("telegram", "alice")
    with _as("alice"):
        out = _tool(cred_tools, "set_my_credential").invoke({"name": NAME, "value": "tok-a"})
        assert "Stored SHEETS_TOKEN" in out and "tok-a" not in out
        listing = _tool(cred_tools, "list_my_credentials").invoke({})
        assert NAME in listing and "tok-a" not in listing
        assert "Deleted" in _tool(cred_tools, "delete_my_credential").invoke({"name": NAME})
        assert "No credentials stored" in _tool(cred_tools, "list_my_credentials").invoke({})


def test_only_declared_names_are_accepted(cred_tools):
    _seed("telegram", "alice")
    with _as("alice"):
        out = _tool(cred_tools, "set_my_credential").invoke(
            {"name": "OPENAI_API_KEY", "value": "x"}
        )
    assert "not a credential this agent accepts" in out and NAME in out


def test_resolution_is_per_customer_and_fails_closed(cred_tools, monkeypatch):
    """The heart of the feature: during a customer turn a declared name is the
    customer's own value or nothing — the operator's env/vault token must
    never leak into another customer's work."""
    monkeypatch.setenv(NAME, "operators-own-token")
    from olano.tools import talk_to

    token = talk_to.current_agent.set(AGENT)
    try:
        _seed("telegram", "alice")
        _seed("telegram", "bob")
        with _as("alice"):
            _tool(cred_tools, "set_my_credential").invoke({"name": NAME, "value": "alice-token"})

        with _as("alice"):
            assert get_credential(NAME) == "alice-token"
            # An UNdeclared name still follows the normal chain during the turn.
            monkeypatch.setenv("OTHER_KEY", "env-value")
            assert get_credential("OTHER_KEY") == "env-value"
        with _as("bob"):
            # Bob stored nothing: fail closed to the default — NEVER the
            # operator's os.environ value.
            assert get_credential(NAME, default="") == ""
        # No customer bound (operator/CLI context): the normal chain applies.
        assert get_credential(NAME) == "operators-own-token"
    finally:
        talk_to.current_agent.reset(token)


def test_action_runs_get_the_customers_env(cred_tools, tmp_path: Path):
    from olano.tools.customer_actions import run_customer_action

    key = _seed("telegram", "alice")
    with _as("alice"):
        _tool(cred_tools, "set_my_credential").invoke({"name": NAME, "value": "alice-token"})

    env = customer_secret_env(AGENT, key)
    assert env == {NAME: "alice-token"}
    # And a customer with nothing stored injects nothing.
    assert customer_secret_env(AGENT, "telegram-bob") == {}

    script = tmp_path / "echo_env.py"
    script.write_text(
        f"import json, os, sys; json.load(sys.stdin); print(os.environ.get({NAME!r}, 'MISSING'))",
        encoding="utf-8",
    )
    tenant = tmp_path / key
    tenant.mkdir()
    proc = run_customer_action(
        script,
        {},
        customer_dir=tenant,
        platform="telegram",
        user_id="alice",
        customer_key=key,
        extra_env=env,
    )
    assert proc.stdout.strip() == "alice-token"


def test_forget_me_erases_stored_credentials(cred_tools):
    key = _seed("telegram", "alice")
    with _as("alice"):
        _tool(cred_tools, "set_my_credential").invoke({"name": NAME, "value": "alice-token"})
        assert customer_secret_env(AGENT, key) != {}
        mem = make_customer_memory_tools(AGENT)
        _tool(mem, "forget_me").invoke({})
    assert customer_secret_env(AGENT, key) == {}


def test_builder_binds_credential_tools_only_when_opted_in(lionclaw_home, tmp_path: Path):
    from olano.core.builder import _build_extra_tools

    names_on = {t.name for t in _build_extra_tools(_config(), tmp_path)}
    assert {"set_my_credential", "list_my_credentials", "delete_my_credential"} <= names_on

    off = AgentConfig(name=AGENT, customer_service={"enabled": True})
    names_off = {t.name for t in _build_extra_tools(off, tmp_path)}
    assert not ({"set_my_credential", "list_my_credentials"} & names_off)


def test_catalog_reports_credential_rows(lionclaw_home):
    from olano.config.internal_tools import effective_internal_tools

    rows = {r["name"]: r for r in effective_internal_tools(_config())}
    assert rows["set_my_credential"]["on"] is True
    plain = AgentConfig(name=AGENT, customer_service={"enabled": True})
    rows = {r["name"]: r for r in effective_internal_tools(plain)}
    assert rows["set_my_credential"]["on"] is False
    assert "user_credentials" in rows["set_my_credential"]["note"]


def test_user_credentials_requires_multi_user_mode():
    with pytest.raises(ValueError, match="user_credentials requires multi-user"):
        AgentConfig(name=AGENT, customer_service={"user_credentials": True})
