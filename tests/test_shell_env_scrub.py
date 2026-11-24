"""Platform-secret scrubbing for agent-run shells (source/secret exfil defense).

Guards the invariant that a shell tool / terminal can never `printenv` the vault
master key, relay secret, or deployment tokens out of the runtime environment.
"""

from __future__ import annotations

import olano.security.shell_guard as sg


def test_always_strips_infra_secrets(monkeypatch):
    monkeypatch.setenv("LIONCLAW_VAULT_KEY", "supersecret")
    monkeypatch.setenv("LIONCLAW_MCP_RELAY_SECRET", "relay")
    monkeypatch.setenv("LIONCLAW_DEPLOYMENT_TOKEN", "olano_dep_x")
    monkeypatch.setenv("LIONCLAW_DASHBOARD_TOKEN", "dash")
    monkeypatch.setenv("OLANO_SETTINGS_SIGNING_KEY", "signkey")
    # The account API key a self-hosted box spends from: the one secret that
    # matters most on exactly the open posture this test pins.
    monkeypatch.setenv("OLANO_CREDITS_KEY", "sk-olano-account")
    monkeypatch.setenv("LIONCLAW_CREDITS_KEY", "sk-olano-account")
    monkeypatch.setenv("PATH", "/usr/bin")
    # open posture → provider keys pass, infra secrets still stripped
    monkeypatch.setattr(sg, "_managed_posture", lambda: False)

    env = sg.sanitized_shell_env()
    for k in (
        "LIONCLAW_VAULT_KEY",
        "LIONCLAW_MCP_RELAY_SECRET",
        "LIONCLAW_DEPLOYMENT_TOKEN",
        "LIONCLAW_DASHBOARD_TOKEN",
        "OLANO_SETTINGS_SIGNING_KEY",
        "OLANO_CREDITS_KEY",
        "LIONCLAW_CREDITS_KEY",
    ):
        assert k not in env, f"{k} must be stripped from an agent shell env"
    assert env["PATH"] == "/usr/bin"
    assert env["NO_COLOR"] == "1"  # SHELL_ENV_DEFAULTS layered on


def test_open_posture_keeps_provider_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setenv("MY_DB_PASSWORD", "pw")
    monkeypatch.setattr(sg, "_managed_posture", lambda: False)
    env = sg.sanitized_shell_env()
    # operator's own (unmanaged) box: broad secrets left intact
    assert env["ANTHROPIC_API_KEY"] == "sk-ant"
    assert env["MY_DB_PASSWORD"] == "pw"


def test_managed_posture_strips_secret_shaped_vars(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setenv("STRIPE_SECRET", "sk_live")
    monkeypatch.setenv("SOME_TOKEN", "t")
    monkeypatch.setenv("AWS_ACCESS_KEY", "ak")
    monkeypatch.setenv("HOME", "/root")  # not secret-shaped → kept
    monkeypatch.setattr(sg, "_managed_posture", lambda: True)
    env = sg.sanitized_shell_env()
    for k in ("ANTHROPIC_API_KEY", "STRIPE_SECRET", "SOME_TOKEN", "AWS_ACCESS_KEY"):
        assert k not in env, f"{k} must be stripped under managed posture"
    assert env["HOME"] == "/root"


def test_managed_posture_resolution_failure_is_safe(monkeypatch):
    # If posture resolution raises, we treat as open (no broad strip) but the
    # always-strip set still applies — never crash the shell.
    monkeypatch.setenv("LIONCLAW_VAULT_KEY", "x")
    env = sg.sanitized_shell_env()
    assert "LIONCLAW_VAULT_KEY" not in env


def test_run_local_appends_the_interpreter_shims_to_path(monkeypatch, lionclaw_home):
    """The host lane of `execute`/`safe_shell` gets the runtime's `python` shim
    last on PATH, and `sanitized_shell_env()` itself stays pure (pinned above)."""
    import subprocess

    monkeypatch.setenv("PATH", "/usr/bin")
    captured = {}

    class _Res:
        stdout, stderr, returncode = "", "", 0

    def fake_run(*a, **kw):
        captured["env"] = kw["env"]
        return _Res()

    monkeypatch.setattr(subprocess, "run", fake_run)
    sg.run_local("echo hi")

    shim = str(lionclaw_home / "sandbox" / "tools" / "shims")
    assert captured["env"]["PATH"] == f"/usr/bin:{shim}"
    assert "NO_COLOR" in captured["env"]
    assert sg.sanitized_shell_env()["PATH"] == "/usr/bin"


def test_sandboxed_run_host_branch_never_shows_the_vault_key(monkeypatch):
    """`sandboxed_run`'s host lane (an in-process tool shelling out on an open
    box) used to hand the child `os.environ.copy()` — one `printenv` away from
    the vault master key. It now gets `sanitized_shell_env()` like every other
    shell lane."""
    from olano.tools._sandbox_exec import sandbox_active_for_current_agent, sandboxed_run
    from olano.tools.talk_to import current_agent

    monkeypatch.setenv("LIONCLAW_VAULT_KEY", "vault-master-key-must-not-leak")
    monkeypatch.setenv("LIONCLAW_DEPLOYMENT_TOKEN", "olano_dep_must_not_leak")
    monkeypatch.setenv("SHELL_ENV_SCRUB_CANARY", "still-here")
    tok = current_agent.set("no-such-agent-xyz")
    try:
        assert sandbox_active_for_current_agent() is False
        res = sandboxed_run(["printenv"], timeout=10)
    finally:
        current_agent.reset(tok)
    assert res.returncode == 0
    assert "vault-master-key-must-not-leak" not in res.stdout
    assert "LIONCLAW_VAULT_KEY" not in res.stdout
    assert "olano_dep_must_not_leak" not in res.stdout
    assert "SHELL_ENV_SCRUB_CANARY=still-here" in res.stdout  # ordinary vars still pass
    assert "NO_COLOR=1" in res.stdout  # SHELL_ENV_DEFAULTS layered on
