from __future__ import annotations

import signal

import pytest

from olano.runtime import daemon


def test_lionclaw_serve_command_matcher_ignores_text_searches():
    assert daemon._is_lionclaw_serve_command("/repo/.venv/bin/python3 -m olano serve")
    assert daemon._is_lionclaw_serve_command("/repo/.venv/bin/olano serve")
    assert not daemon._is_lionclaw_serve_command("zsh -c ps aux | rg 'olano serve'")


def test_lionclaw_serve_command_matcher_accepts_console_script_via_interpreter():
    # A venv console script is a shebang file: ps reports interpreter + script.
    assert daemon._is_lionclaw_serve_command(
        "/repo/.venv/bin/python3 /repo/.venv/bin/olano serve -a merli"
    )


def test_lionclaw_serve_command_matcher_rejects_server_management_commands():
    # Regression: "olano serve" is a substring of "olano server <sub>",
    # so `server restart` used to be discovered as a runtime daemon and
    # stop_daemon would SIGTERM the restart command's own process group —
    # the restart died with exit code -15 and the runtime never came back.
    assert not daemon._is_lionclaw_serve_command(
        "/home/ubuntu/tech_devel/olano/.venv/bin/olano server restart -a comodin"
    )
    assert not daemon._is_lionclaw_serve_command(
        "/home/ubuntu/tech_devel/olano/.venv/bin/python3 "
        "/home/ubuntu/tech_devel/olano/.venv/bin/olano server restart -a comodin"
    )
    assert not daemon._is_lionclaw_serve_command("/repo/.venv/bin/olano server status")
    assert not daemon._is_lionclaw_serve_command("/repo/.venv/bin/olano server logs -f")
    assert not daemon._is_lionclaw_serve_command("python3 -m olano server stop")
    assert not daemon._is_lionclaw_serve_command("")


def test_signal_process_group_uses_killpg_for_foreign_group(monkeypatch):
    sent: list[tuple[int, int]] = []

    monkeypatch.setattr(daemon.os, "getpgid", lambda pid: 999 if pid == 0 else 100)
    monkeypatch.setattr(daemon.os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))

    daemon._signal_process_group(1234, signal.SIGTERM)

    assert sent == [(100, signal.SIGTERM)]


def test_signal_process_group_never_kills_own_group(monkeypatch):
    # Regression: `server restart` executed inside the runtime's process
    # group (agent shell command, or a runtime backgrounded from the same
    # shell session) killed itself via killpg before it could start the new
    # daemon. The group signal must fall back to a tree kill that spares the
    # caller.
    tree_calls: list[tuple[int, int]] = []

    monkeypatch.setattr(daemon.os, "getpgid", lambda pid: 4242)  # same group for everyone

    def forbidden_killpg(pgid: int, sig: int) -> None:
        raise AssertionError("killpg on own process group would SIGTERM the caller")

    monkeypatch.setattr(daemon.os, "killpg", forbidden_killpg)
    monkeypatch.setattr(
        daemon, "_signal_process_tree", lambda pid, sig: tree_calls.append((pid, sig))
    )

    daemon._signal_process_group(1234, signal.SIGTERM)

    assert tree_calls == [(1234, signal.SIGTERM)]


def test_signal_process_tree_spares_caller_and_its_shell(monkeypatch):
    # runtime 100 → worker 110 → grandchild 111
    #             → shell 200 → this very process 300 (running the restart)
    parents = {100: 1, 110: 100, 111: 110, 200: 100, 300: 200}
    killed: list[int] = []

    monkeypatch.setattr(daemon, "_pid_parent_map", lambda: parents)
    monkeypatch.setattr(daemon.os, "getpid", lambda: 300)
    monkeypatch.setattr(daemon.os, "kill", lambda pid, sig: killed.append(pid))

    daemon._signal_process_tree(100, signal.SIGTERM)

    # The runtime and its workers die; the caller (300) and the shell that
    # connects it to the runtime (200) survive to finish the restart.
    assert set(killed) == {100, 110, 111}


def test_restart_blast_radius_detects_runtime_ancestor(monkeypatch):
    parents = {100: 1, 200: 100, 300: 200}
    monkeypatch.setattr(daemon, "_pid_parent_map", lambda: parents)
    monkeypatch.setattr(daemon.os, "getpid", lambda: 300)
    monkeypatch.setattr(daemon.os, "getpgid", lambda pid: 999 if pid == 0 else 100)

    assert daemon.restart_blast_radius_pid({100}) == 100


def test_restart_blast_radius_detects_shared_process_group(monkeypatch):
    monkeypatch.setattr(daemon, "_pid_parent_map", dict)
    monkeypatch.setattr(daemon.os, "getpid", lambda: 300)
    monkeypatch.setattr(daemon.os, "getpgid", lambda pid: 777)  # caller and runtime share

    assert daemon.restart_blast_radius_pid({4242}) == 4242


def test_restart_blast_radius_clear_for_detached_runtime(monkeypatch):
    parents = {100: 1, 300: 50, 50: 1}
    monkeypatch.setattr(daemon, "_pid_parent_map", lambda: parents)
    monkeypatch.setattr(daemon.os, "getpid", lambda: 300)
    monkeypatch.setattr(daemon.os, "getpgid", lambda pid: 999 if pid == 0 else 100)

    assert daemon.restart_blast_radius_pid({100}) is None
    assert daemon.restart_blast_radius_pid(set()) is None


def test_server_restart_detaches_when_inside_runtime(monkeypatch):
    from click.testing import CliRunner

    from olano.cli.main import cli

    spawned: list[list[str]] = []
    monkeypatch.delenv("LIONCLAW_RESTART_DETACHED", raising=False)
    monkeypatch.setattr(daemon, "restart_blast_radius_pid", lambda targets=None: 4242)
    monkeypatch.setattr(daemon, "spawn_detached_restart", lambda args: spawned.append(args) or 555)

    def forbidden_stop(**_kwargs):
        raise AssertionError("inline stop would kill the caller — must detach instead")

    monkeypatch.setattr(daemon, "stop_daemon", forbidden_stop)

    runner = CliRunner()
    result = runner.invoke(cli, ["server", "restart", "-a", "comodin"], catch_exceptions=False)

    assert result.exit_code == 0
    assert spawned == [["-a", "comodin", "--timeout", "10.0"]]
    assert "Detached a helper" in result.output


def test_server_restart_detached_child_skips_detection_and_restarts_inline(monkeypatch):
    from click.testing import CliRunner

    import olano.cli.main as cli_main
    from olano.cli.main import cli

    events: list[str] = []
    monkeypatch.setenv("LIONCLAW_RESTART_DETACHED", "1")
    monkeypatch.setattr(cli_main.time, "sleep", lambda s: events.append(f"sleep:{s}"))
    monkeypatch.setattr(
        daemon,
        "restart_blast_radius_pid",
        lambda targets=None: pytest.fail("detached child must not re-detect"),
    )
    monkeypatch.setattr(
        daemon,
        "stop_daemon",
        lambda timeout, force: events.append("stop")
        or daemon.StopResult(pid=None, stopped=False, forced=False, detail="no pid file"),
    )

    def fake_start(agents, no_housekeeping, verbose, log_level):
        events.append("start")

    monkeypatch.setattr(cli_main, "server_start", fake_start)

    runner = CliRunner()
    result = runner.invoke(cli, ["server", "restart"], catch_exceptions=False)

    assert result.exit_code == 0
    assert events == ["sleep:2.0", "stop", "start"]


def test_start_daemon_reuses_discovered_runtime(monkeypatch, tmp_path):
    written: list[int] = []

    monkeypatch.setattr(daemon, "ensure_olano_home", lambda: None)
    monkeypatch.setattr(daemon, "read_pid", lambda: None)
    monkeypatch.setattr(daemon, "_discover_runtime_pids", lambda: {4321})
    monkeypatch.setattr(daemon, "write_pid", written.append)
    monkeypatch.setattr(daemon, "_log_path", lambda: tmp_path / "runtime.log")
    monkeypatch.setattr(daemon, "_pid_path", lambda: tmp_path / "olano.pid")

    result = daemon.start_daemon([])

    assert result.already_running is True
    assert result.pid == 4321
    assert written == [4321]


def test_start_daemon_refuses_occupied_api_port(monkeypatch):
    monkeypatch.setattr(daemon, "ensure_olano_home", lambda: None)
    monkeypatch.setattr(daemon, "read_pid", lambda: None)
    monkeypatch.setattr(daemon, "_discover_runtime_pids", lambda: set())
    monkeypatch.setattr(daemon, "_runtime_api_port", lambda: 8765)
    monkeypatch.setattr(daemon, "_listening_pids", lambda port: {9999})

    with pytest.raises(RuntimeError, match="port 8765 is already in use"):
        daemon.start_daemon([])


def test_stop_daemon_cleans_discovered_stale_runtimes(monkeypatch):
    alive = {1234, 5678}
    signaled: list[tuple[int, int]] = []

    def fake_signal(pid: int, sig) -> None:
        signaled.append((pid, sig))
        alive.discard(pid)

    monkeypatch.setattr(daemon, "read_pid", lambda: 1234)
    monkeypatch.setattr(daemon, "_discover_runtime_pids", lambda: {5678})
    monkeypatch.setattr(daemon, "is_pid_alive", lambda pid: pid in alive)
    monkeypatch.setattr(daemon, "_signal_process_group", fake_signal)
    monkeypatch.setattr(daemon, "clear_pid", lambda: None)

    result = daemon.stop_daemon()

    assert result.stopped is True
    assert result.pid == 1234
    assert "5678" in result.detail
    assert {pid for pid, _sig in signaled} == {1234, 5678}


def test_stop_daemon_without_pidfile_stops_discovered_runtime(monkeypatch):
    alive = {5678}

    def fake_signal(pid: int, _sig) -> None:
        alive.discard(pid)

    monkeypatch.setattr(daemon, "read_pid", lambda: None)
    monkeypatch.setattr(daemon, "_discover_runtime_pids", lambda: {5678})
    monkeypatch.setattr(daemon, "is_pid_alive", lambda pid: pid in alive)
    monkeypatch.setattr(daemon, "_signal_process_group", fake_signal)
    monkeypatch.setattr(daemon, "clear_pid", lambda: None)

    result = daemon.stop_daemon()

    assert result.stopped is True
    assert result.pid is None
    assert "5678" in result.detail
