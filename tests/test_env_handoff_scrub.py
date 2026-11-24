"""The runtime's own child processes never inherit the platform secrets.

Three places hand ``os.environ`` wholesale to a child: a HOST-mode stdio MCP
server (two entry points in ``core/mcp_setup.py``) and the WhatsApp bridge
(``platforms/whatsapp.py``). ``shell_guard._ALWAYS_STRIP_ENV`` listed the
account credits key, but these sites never went through the shell scrubber, so
a pasted ``OLANO_CREDITS_KEY`` (and the gateway key, the vault key, the
deployment token) reached every stdio server and the bridge. Each site now
goes through ``shell_guard.strip_platform_secrets``; these pins keep it so.
"""

from __future__ import annotations

import pytest

from olano.security import shell_guard

SECRETS = {
    "OLANO_CREDITS_KEY": "sk-olano-" + "a" * 40,
    "LIONCLAW_CREDITS_KEY": "sk-olano-" + "a" * 40,
    "OLANO_GATEWAY_KEY": "sk-gw",
    "LIONCLAW_DEPLOYMENT_TOKEN": "olano_dep_x",
    "OLANO_VAULT_KEY": "vault",
}


@pytest.fixture
def secret_env(monkeypatch):
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("WHATSAPP_DEBUG", "1")
    monkeypatch.setenv("MY_SERVER_SETTING", "kept")


def _assert_scrubbed(env: dict[str, str]) -> None:
    for name in SECRETS:
        assert name not in env, name
    assert "PATH" in env
    assert env["MY_SERVER_SETTING"] == "kept"
    assert env["WHATSAPP_DEBUG"] == "1"


def test_strip_platform_secrets_removes_both_spellings_and_nothing_else(secret_env) -> None:
    _assert_scrubbed(shell_guard.strip_platform_secrets())
    # Explicit base, same rule; the shell defaults are NOT layered on (this is
    # not an agent shell).
    out = shell_guard.strip_platform_secrets(
        {
            "OLANO_CREDITS_KEY": "x",
            "PATH": "/bin",
            "MY_SERVER_SETTING": "kept",
            "WHATSAPP_DEBUG": "1",
        }
    )
    _assert_scrubbed(out)
    assert set(out) == {"PATH", "MY_SERVER_SETTING", "WHATSAPP_DEBUG"}


def test_a_host_mode_stdio_server_does_not_inherit_the_secrets(secret_env, monkeypatch) -> None:
    from olano.core import mcp_setup

    monkeypatch.setattr(mcp_setup, "_stdio_launch_decision", lambda: ("host", None))
    plan = mcp_setup.plan_stdio_launch("npx", ["-y", "some-server"], {"SERVER_TOKEN": "s"}, "agent")
    assert plan.invocation is not None
    env = plan.invocation.env
    _assert_scrubbed(env)
    assert env["SERVER_TOKEN"] == "s"  # the server's own secret still travels
    # The unplanned-caller branch of the transport opener reads the same helper.
    _assert_scrubbed(mcp_setup._host_env())


def test_the_whatsapp_bridge_does_not_inherit_the_secrets(secret_env) -> None:
    from olano.platforms import whatsapp

    env = whatsapp.bridge_environment()
    _assert_scrubbed(env)
    assert "WHATSAPP_REPLY_PREFIX" not in env
    assert whatsapp.bridge_environment("bot: ")["WHATSAPP_REPLY_PREFIX"] == "bot: "
