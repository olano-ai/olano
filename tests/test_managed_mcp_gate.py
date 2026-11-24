"""Managed posture: stdio MCP never launches on the host.

Open posture launches the command as-is (byte-identical to the pre-sandbox
behaviour). Managed posture either rewrites the launch into an ephemeral
hardened sandbox container (``docker run --rm -i …``) or blocks it fail-closed
— there is no third path, and the runtime's ``os.environ`` never crosses into
a sandboxed launch.
"""

from __future__ import annotations

import os

import pytest

import olano.core.mcp_setup as mcp_setup


class _Srv:
    def __init__(self, **kw):
        self.__dict__.update(
            {
                "transport": "stdio",
                "command": "bash",
                "args": ["-c", "id"],
                "env": {},
                "name": "evil",
                "url": "",
            }
        )
        self.__dict__.update(kw)


def _force_mode(monkeypatch, mode: str, reason: str | None = None) -> None:
    monkeypatch.setattr(mcp_setup, "_stdio_launch_decision", lambda: (mode, reason))


@pytest.fixture
def sandboxed(monkeypatch, tmp_path):
    """Force sandboxed mode with every external dependency stubbed hermetic."""
    _force_mode(monkeypatch, "sandboxed")
    monkeypatch.setattr(mcp_setup.shutil, "which", lambda cmd: "/usr/local/bin/docker")
    monkeypatch.setattr(mcp_setup, "_stdio_sandbox_image", lambda: "olano-sandbox:test")
    monkeypatch.setattr(mcp_setup, "_agent_workspace_root", lambda agent: tmp_path)
    monkeypatch.setattr(mcp_setup, "_prepare_stdio_sandbox_paths", lambda agent, ws: None)
    # The sandboxed launch forwards only what the server DECLARES (resolved
    # through get_credential) — never the agent's whole vault, which is what
    # `agent_secret_env` hands a host launch on the operator's own box.
    monkeypatch.setattr(
        mcp_setup, "agent_declared_env", lambda env, agent: {"MY_TOKEN": "s3cret-value"}
    )
    return tmp_path


# ── blocked: managed with no sandbox route ─────────────────────────────────


def test_build_connection_params_skips_stdio_when_blocked(monkeypatch):
    _force_mode(monkeypatch, "blocked", "sandboxed execution is not enabled")
    assert mcp_setup.build_connection_params(_Srv(), "agent") is None


def test_build_connection_params_allows_remote_http_when_blocked(monkeypatch):
    _force_mode(monkeypatch, "blocked", "whatever")
    params = mcp_setup.build_connection_params(
        _Srv(transport="http", command=None, url="https://mcp.example.com"), "agent"
    )
    assert params is not None and params["url"] == "https://mcp.example.com"


def test_stdio_unavailable_reason_names_the_blocker(monkeypatch):
    _force_mode(monkeypatch, "blocked", "the docker CLI is not available in the runtime")
    reason = mcp_setup.stdio_unavailable_reason(_Srv())
    assert reason is not None
    assert "docker CLI" in reason
    assert "remote (http/sse)" in reason
    # Remote entries never get a stdio reason.
    assert (
        mcp_setup.stdio_unavailable_reason(
            _Srv(transport="http", command=None, url="https://x.example")
        )
        is None
    )


async def test_open_transport_stdio_raises_when_blocked(monkeypatch):
    _force_mode(monkeypatch, "blocked", "no sandbox")
    with pytest.raises(RuntimeError, match="disabled on this managed deployment"):
        await mcp_setup._open_transport("x", "stdio", command="bash", args=["-c", "id"])


async def test_open_transport_backstop_refuses_unrewritten_stdio_when_sandboxed(monkeypatch):
    """Even with the sandbox available, a raw (unplanned) stdio launch on a
    managed box must be refused — the flag, not the mode, is the permission."""
    _force_mode(monkeypatch, "sandboxed")
    with pytest.raises(RuntimeError, match="disabled on this managed deployment"):
        await mcp_setup._open_transport("x", "stdio", command="bash", args=["-c", "id"])


# ── host: open posture stays byte-identical ────────────────────────────────


def test_open_posture_params_are_the_historical_shape(monkeypatch):
    _force_mode(monkeypatch, "host")
    monkeypatch.setattr(mcp_setup, "agent_secret_env", lambda env, agent: {"FOO": "bar"})
    monkeypatch.setenv("GATE_CANARY", "still-here")
    params = mcp_setup.build_connection_params(_Srv(), "agent")
    assert params is not None
    assert params["command"] == "bash"
    assert params["args"] == ["-c", "id"]
    assert params["transport"] == "stdio"
    assert params["env"] == {**os.environ, "FOO": "bar"}


async def test_open_transport_allows_host_stdio(monkeypatch):
    _force_mode(monkeypatch, "host")
    cm = await mcp_setup._open_transport("x", "stdio", command="true", args=[])
    assert cm is not None  # context manager built; nothing spawned until entered


# ── sandboxed: managed rewrite into an ephemeral container ─────────────────


def test_sandboxed_rewrite_shape(monkeypatch, sandboxed):
    monkeypatch.setenv("GATE_CANARY", "must-not-leak")
    srv = _Srv(command="npx", args=["-y", "@scope/pkg", "--flag", "two words"])
    params = mcp_setup.build_connection_params(srv, "agent")
    assert params is not None
    # The stdio child is the docker CLI, by absolute path (the child env is a
    # minimal allowlist, so PATH is not trusted for the spawn itself).
    assert params["command"] == "/usr/local/bin/docker"
    args = params["args"]
    assert args[0] == "run"
    for flag in ("--rm", "-i", "--init"):
        assert flag in args
    assert "--label=olano.mcp=1" in args
    # Ephemeral container: image, then the sh -c launch line.
    from olano.backends.lionclaw_docker import mcp_stdio_shell_script

    script = mcp_stdio_shell_script("npx", ["-y", "@scope/pkg", "--flag", "two words"])
    assert args[-3:] == ["sh", "-c", script]
    assert args[-4] == "olano-sandbox:test"
    # Tool layer first, then the interpreter shims LAST (interpreter_shims.py:
    # a `python` the image lacks resolves, a real one still wins).
    assert script.startswith("export PATH=/opt/agent-tools/python/bin:")
    assert ":$PATH:/opt/agent-tools/shims; exec " in script
    assert "exec npx -y @scope/pkg --flag 'two words'" in script
    # Workspace mounted at /workspace AND its host path (one path namespace).
    joined = " ".join(args)
    assert f"{sandboxed}:/workspace:rw" in joined
    assert f"{sandboxed}:{sandboxed}:rw" in joined
    # Secrets: name-only on argv, value only in the CLI child's env.
    assert "MY_TOKEN" in args
    assert not any("s3cret-value" in a for a in args)
    assert params["env"]["MY_TOKEN"] == "s3cret-value"
    # The runtime env must NOT cross into the sandboxed launch.
    assert "GATE_CANARY" not in params["env"]


def test_sandboxed_launch_never_forwards_the_whole_vault(monkeypatch, sandboxed):
    """An arbitrary directory package gets the env block it declares — the
    agent's Stripe key must not ride along because it exists in the vault."""
    monkeypatch.setattr(
        mcp_setup,
        "agent_secret_env",
        lambda env, agent: {"STRIPE_API_KEY": "sk_live", "MY_TOKEN": "s3cret-value"},
    )
    monkeypatch.setattr(
        mcp_setup, "agent_declared_env", lambda env, agent: {k: "v" for k in (env or {})}
    )
    params = mcp_setup.build_connection_params(_Srv(env={"MY_TOKEN": "${vault:MY_TOKEN}"}), "agent")
    assert params is not None
    assert "MY_TOKEN" in params["args"]
    assert "STRIPE_API_KEY" not in params["args"]
    assert "STRIPE_API_KEY" not in params["env"]


def test_sandboxed_hostile_env_name_cannot_become_a_docker_flag(monkeypatch, sandboxed):
    monkeypatch.setattr(
        mcp_setup,
        "agent_declared_env",
        lambda env, agent: {"NAME --privileged": "x", "OK_NAME": "y"},
    )
    params = mcp_setup.build_connection_params(_Srv(), "agent")
    assert params is not None
    args = params["args"]
    # The hostile name rides as ONE argv element after -e; it is never split
    # into a standalone --privileged flag.
    assert "NAME --privileged" in args
    assert "--privileged" not in args
    assert args[args.index("NAME --privileged") - 1] == "-e"


def test_sandboxed_blocks_docker_command_entries(monkeypatch, sandboxed):
    """`command: docker` presets can't nest inside the sandbox image (no CLI,
    no socket — deliberate); they block with a pointer at the remote preset."""
    srv = _Srv(command="docker", args=["run", "-i", "ghcr.io/x/mcp"])
    assert mcp_setup.build_connection_params(srv, "agent") is None
    reason = mcp_setup.stdio_unavailable_reason(srv)
    assert reason is not None and "remote (http/sse)" in reason


def test_sandboxed_planning_failure_blocks_not_host_launches(monkeypatch, sandboxed):
    def _boom(agent):
        raise RuntimeError("workspace resolution exploded")

    monkeypatch.setattr(mcp_setup, "_agent_workspace_root", _boom)
    plan = mcp_setup.plan_stdio_launch("npx", ["-y", "pkg"], {}, "agent")
    assert plan.invocation is None
    assert plan.blocked_reason is not None


# ── the decision itself ────────────────────────────────────────────────────


def test_decision_open_posture_is_host(monkeypatch):
    monkeypatch.setattr(mcp_setup, "stdio_mcp_blocked", lambda: False)
    assert mcp_setup._stdio_launch_decision() == ("host", None)


def test_decision_matrix_on_managed(monkeypatch):
    monkeypatch.setattr(mcp_setup, "stdio_mcp_blocked", lambda: True)

    monkeypatch.setattr("olano.deployment.enforcement.sandbox_exec_enabled", lambda: False)
    mode, reason = mcp_setup._stdio_launch_decision()
    assert mode == "blocked" and "not enabled" in (reason or "")

    monkeypatch.setattr("olano.deployment.enforcement.sandbox_exec_enabled", lambda: True)
    monkeypatch.setattr(mcp_setup.shutil, "which", lambda cmd: None)
    mode, reason = mcp_setup._stdio_launch_decision()
    assert mode == "blocked" and "docker CLI" in (reason or "")

    monkeypatch.setattr(mcp_setup.shutil, "which", lambda cmd: "/usr/local/bin/docker")
    monkeypatch.setattr(mcp_setup, "_stdio_sandbox_image_present", lambda: False)
    mode, reason = mcp_setup._stdio_launch_decision()
    assert mode == "blocked" and "image" in (reason or "")

    monkeypatch.setattr(mcp_setup, "_stdio_sandbox_image_present", lambda: True)
    assert mcp_setup._stdio_launch_decision() == ("sandboxed", None)


def test_decision_fails_closed(monkeypatch):
    def _boom():
        raise RuntimeError("posture layer broken")

    monkeypatch.setattr(mcp_setup, "stdio_mcp_blocked", _boom)
    mode, reason = mcp_setup._stdio_launch_decision()
    assert mode == "blocked" and reason
