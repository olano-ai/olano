"""Daemon-mode helpers for ``olano server start | stop | status | logs``.

The foreground command (``olano serve``) keeps running in the terminal.
The daemon path here spawns the *same* serve code in a detached child
process, writes a PID file under ``~/.olano/run/olano.pid``, and
redirects stdout/stderr to ``~/.olano/logs/runtime.log`` so
``olano server logs`` can tail them.

We deliberately do NOT do a classic double-fork.  ``subprocess.Popen``
with ``start_new_session=True`` is enough to detach from the controlling
terminal on macOS / Linux, and it keeps the implementation small and
testable.  Windows isn't supported by daemon mode — callers should fall
back to ``serve`` (or run inside a service wrapper).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from olano.config.home import (
    ensure_olano_home,
    get_olano_paths,
    load_home_config,
)

# Set in the environment of the detached helper `server restart` spawns when
# it detects it would die with the runtime it is stopping (see
# `restart_blast_radius_pid`). The helper skips detection and runs inline.
RESTART_DETACHED_ENV = "LIONCLAW_RESTART_DETACHED"

# ─────────────────────────────────── PID file ────────────────────────────────


def _pid_path() -> Path:
    return get_olano_paths().runtime_pid_path


def _log_path() -> Path:
    return get_olano_paths().runtime_log_path


def read_pid() -> int | None:
    """Return the PID stored in the runtime pidfile, or ``None`` if absent / invalid."""
    path = _pid_path()
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def write_pid(pid: int) -> None:
    path = _pid_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n", encoding="utf-8")


def clear_pid() -> None:
    try:
        _pid_path().unlink()
    except FileNotFoundError:
        pass


def is_pid_alive(pid: int) -> bool:
    """Best-effort liveness check using signal 0 (no-op kill)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we don't own it — still "alive" for our purposes.
        return True
    return True


def _runtime_api_port() -> int:
    return int(os.environ.get("LIONCLAW_RUNTIME_PORT", str(load_home_config().runtime_api_port)))


def _pid_command(pid: int) -> str:
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception:
        return ""
    return proc.stdout.strip()


def _is_lionclaw_serve_command(command: str) -> bool:
    """True only for an actual runtime process (``olano serve …``).

    Deliberately token-based, NOT a substring test: ``"olano serve"`` is a
    substring of ``"olano server restart"`` (and every other ``olano
    server <sub>`` management command), so a substring match classified the
    very CLI invocation that was stopping the daemon as a daemon itself —
    ``stop_daemon`` would then signal that command's process group and kill
    it mid-flight (observed as ``server restart`` dying with exit code -15
    before it could start the runtime back up).

    Recognised forms, exactly as ``ps`` reports them:
    - ``…/olano serve …``
    - ``…/python[3.x] -m olano serve …``
    - ``…/python[3.x] …/bin/olano serve …`` (console script via shebang)
    """
    tokens = command.split()
    if not tokens:
        return False
    executable = Path(tokens[0]).name
    args = tokens[1:]
    if executable == "olano":
        return args[:1] == ["serve"]
    if executable.startswith("python"):
        if args[:2] == ["-m", "olano"]:
            return args[2:3] == ["serve"]
        if args and Path(args[0]).name == "olano":
            return args[1:2] == ["serve"]
    return False


def _discover_lionclaw_serve_pids() -> set[int]:
    """Return live-looking ``olano serve`` PIDs visible to this user.

    This intentionally catches stale daemon processes whose pidfile was
    overwritten by a later ``server start``. Those orphaned runtimes can keep
    the API port, platform tokens, or session locks, so ``server stop`` must
    clean them even when they are no longer the pidfile PID.
    """
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception:
        return set()

    current = os.getpid()
    pids: set[int] = set()
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_raw, _, command = stripped.partition(" ")
        try:
            pid = int(pid_raw)
        except ValueError:
            continue
        if pid == current:
            continue
        if _is_lionclaw_serve_command(command):
            pids.add(pid)
    return pids


def _listening_pids(port: int) -> set[int]:
    """Return PIDs listening on the given TCP port, using lsof when available."""
    try:
        proc = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception:
        return set()

    pids: set[int] = set()
    for raw in proc.stdout.splitlines():
        try:
            pids.add(int(raw.strip()))
        except ValueError:
            continue
    return pids


def _discover_runtime_pids(include_port_listener: bool = True) -> set[int]:
    pids = _discover_lionclaw_serve_pids()
    if include_port_listener:
        for pid in _listening_pids(_runtime_api_port()):
            if _is_lionclaw_serve_command(_pid_command(pid)):
                pids.add(pid)
    return pids


# ─────────────────────────────────── start ───────────────────────────────────


@dataclass
class StartResult:
    pid: int
    log_path: Path
    pid_path: Path
    already_running: bool = False


def _inject_vault_key_from_keyring(env: dict[str, str]) -> None:
    """Bridge the system keyring's vault master key into the *daemon's* env.

    On macOS a detached background process (the daemon) typically cannot read
    the login Keychain, so keyring auto-unlock fails for it even though it works
    in this foreground ``server start`` process. Reading the key here and handing
    it to the child via ``OLANO_VAULT_KEY`` lets the daemon unlock the vault
    via the env path — no Keychain access required at runtime.

    Best-effort and silent: if the key is absent, the keyring is unavailable, or
    the key doesn't actually unlock the vault, the env is left untouched and the
    daemon logs its own actionable locked-vault warning.
    """
    from olano.vault.store import set_vault_key_env, vault_key_from_env

    if vault_key_from_env(env):
        return  # an explicit env key was provided — respect it
    try:
        from olano.vault.keyring_backend import secure_keyring_available

        if not secure_keyring_available():
            return
        import keyring

        key = keyring.get_password("olano", "vault_master_key")
        if not key:
            return
        # Only inject a key that ACTUALLY unlocks — a drifted keyring key would
        # otherwise just relocate the failure into the daemon (mode=env).
        from olano.vault.runtime import unlock_vault_for_session

        if unlock_vault_for_session(passphrase=key, register=False).vault is not None:
            set_vault_key_env(key, env)
    except Exception:
        return


def start_daemon(serve_args: list[str]) -> StartResult:
    """Spawn ``olano serve <serve_args...>`` detached in the background.

    ``serve_args`` is forwarded verbatim to the foreground command so flag
    parity is automatic — anything ``serve`` accepts, ``server start``
    accepts.

    Raises ``RuntimeError`` if the daemon is already running, or if the
    child process dies within the first 2 seconds (clear startup error).
    """
    ensure_olano_home()

    existing = read_pid()
    if existing and is_pid_alive(existing):
        return StartResult(
            pid=existing,
            log_path=_log_path(),
            pid_path=_pid_path(),
            already_running=True,
        )
    if existing:
        # Stale pidfile from a crashed process — clean it up first.
        clear_pid()

    # A previous start may have overwritten the pidfile while an older daemon
    # was still alive. Refuse to create a duplicate runtime; point the pidfile
    # back at the discovered daemon so `server stop` can clean it.
    runtime_pids = _discover_runtime_pids()
    if runtime_pids:
        pid = min(runtime_pids)
        write_pid(pid)
        return StartResult(
            pid=pid,
            log_path=_log_path(),
            pid_path=_pid_path(),
            already_running=True,
        )

    api_port = _runtime_api_port()
    port_pids = _listening_pids(api_port)
    if port_pids:
        owners = ", ".join(str(pid) for pid in sorted(port_pids))
        raise RuntimeError(
            f"Runtime API port {api_port} is already in use by PID(s): {owners}. "
            "Stop that process or set LIONCLAW_RUNTIME_PORT before starting Olano."
        )

    log_path = _log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(log_path, "ab", buffering=0)  # noqa: SIM115 — handed to child

    cmd = [sys.executable, "-m", "olano", "serve", *serve_args]
    env = dict(os.environ)
    # Tell the child it's running detached so its log formatter can drop
    # ANSI colour and the friendly Rich banner ``serve`` prints today.
    env["LIONCLAW_DAEMON"] = "1"
    # Never let the restart-helper marker leak into the daemon (and from
    # there into agent shell commands): it would disable blast-radius
    # detection for every future `server restart` run inside the runtime.
    env.pop(RESTART_DETACHED_ENV, None)
    # macOS daemons can't read the Keychain; bridge the keyring's vault key into
    # the child env (from this foreground process, which CAN) so the daemon can
    # unlock the vault headlessly. No-op if OLANO_VAULT_KEY is already set.
    _inject_vault_key_from_keyring(env)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
            close_fds=True,
        )
    finally:
        # The child inherits its own copy of the file descriptor; we close
        # the parent's so subprocess.Popen's start_new_session is the only
        # owner left.
        log_fh.close()

    write_pid(proc.pid)

    # Sanity-check that the child didn't die immediately.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            clear_pid()
            # Read the last ~20 lines of the daemon log so the user gets
            # the real error instead of "tail the log yourself".
            tail = _tail_log(log_path, lines=20)
            raise RuntimeError(
                f"Daemon exited with code {proc.returncode} during startup.\n"
                f"--- last lines of {log_path} ---\n{tail}"
            )
        time.sleep(0.1)

    return StartResult(
        pid=proc.pid,
        log_path=log_path,
        pid_path=_pid_path(),
        already_running=False,
    )


# ─────────────────────────────────── stop ────────────────────────────────────


@dataclass
class StopResult:
    pid: int | None
    stopped: bool
    forced: bool
    detail: str


def _tail_log(path: Path, lines: int = 20) -> str:
    """Return the last *lines* of *path*, or an empty string on failure.
    Used to make startup-failure messages actionable instead of pointing
    the user at a log file they have to fish through manually."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = 4096
            data = b""
            while size > 0 and data.count(b"\n") <= lines:
                read = min(block, size)
                size -= read
                f.seek(size)
                data = f.read(read) + data
            text = data.decode("utf-8", errors="replace")
            return "\n".join(text.splitlines()[-lines:])
    except Exception:
        return ""


def _pid_parent_map() -> dict[int, int]:
    """Map of pid → ppid for every process visible to ``ps`` (empty on failure)."""
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,ppid="],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception:
        return {}
    parents: dict[int, int] = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            parents[int(parts[0])] = int(parts[1])
        except ValueError:
            continue
    return parents


def _self_and_ancestors_below(root: int, parents: dict[int, int]) -> set[int]:
    """This process plus its ancestor chain, stopping before *root*.

    These pids must never be signalled when tearing down *root*'s tree:
    killing ourselves would abort the very stop/restart we are executing,
    and killing the shells between us and the runtime breaks our stdio and
    wait chain the same way. *root* itself is excluded from the result —
    it IS the process being stopped."""
    protected = {os.getpid()}
    pid = parents.get(os.getpid(), os.getppid())
    while pid > 1 and pid != root and pid not in protected:
        protected.add(pid)
        pid = parents.get(pid, 1)
    return protected


def _signal_process_tree(root: int, sig: int) -> None:
    """Deliver *sig* to *root* and all its descendants, sparing this process.

    Used instead of ``os.killpg`` when the runtime shares a process group
    with the caller — e.g. ``server restart`` executed by an agent's shell
    tool inside the runtime, or a runtime that was backgrounded from the
    same automation shell session. ``killpg`` would SIGTERM the stop/restart
    command itself mid-flight (exit code -15, runtime left down)."""
    parents = _pid_parent_map()
    children: dict[int, list[int]] = {}
    for child, parent in parents.items():
        children.setdefault(parent, []).append(child)

    targets: list[int] = []
    stack = [root]
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        targets.append(pid)
        stack.extend(children.get(pid, []))

    protected = _self_and_ancestors_below(root, parents)
    for pid in targets:
        if pid in protected:
            continue
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            continue


def _signal_process_group(pid: int, sig: int) -> None:
    """Deliver *sig* to the daemon AND every child it spawned.

    The daemon was started with ``start_new_session=True`` so its PID is
    also the leader of its own process group. ``killpg`` therefore reaches
    the dashboard's node subprocess, the uvicorn workers, anything the
    Telegram SDK forked off, etc. ``os.kill(pid, sig)`` only delivered to
    the leader, leaving children running and frequently holding ports —
    the next ``server start`` would then fail to bind.

    Exception: when the *caller* is inside that same process group (an agent
    shell command running ``server restart`` inside the runtime, or a runtime
    started from the caller's own shell session), ``killpg`` would terminate
    the caller too. Signal the runtime's process *tree* instead, excluding
    this process and its ancestors."""
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        # Fall back to the single PID — caller already verified it was
        # alive a moment ago.
        os.kill(pid, sig)
        return
    try:
        own_pgid = os.getpgid(0)
    except OSError:
        own_pgid = -1
    if pgid == own_pgid:
        _signal_process_tree(pid, sig)
        return
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return


def stop_daemon(timeout: float = 10.0, force: bool = False) -> StopResult:
    """Send SIGTERM (or SIGKILL with ``force=True``) and wait for exit.

    Returns ``StopResult.stopped=False`` with a detail message if the
    pidfile is missing or the process was already gone.
    """
    pid = read_pid()
    runtime_pids = _discover_runtime_pids()
    if pid is not None:
        runtime_pids.add(pid)
    if pid is None:
        if not runtime_pids:
            return StopResult(pid=None, stopped=False, forced=False, detail="no pid file")
        target_pids = {p for p in runtime_pids if is_pid_alive(p)}
    elif not is_pid_alive(pid):
        clear_pid()
        target_pids = {p for p in runtime_pids if p != pid and is_pid_alive(p)}
        if not target_pids:
            return StopResult(
                pid=pid,
                stopped=False,
                forced=False,
                detail="process not running (pidfile cleared)",
            )
    else:
        target_pids = {p for p in runtime_pids if is_pid_alive(p)}

    sig = signal.SIGKILL if force else signal.SIGTERM
    for target_pid in sorted(target_pids):
        try:
            _signal_process_group(target_pid, sig)
        except ProcessLookupError:
            pass

    if not target_pids:
        clear_pid()
        return StopResult(pid=pid, stopped=False, forced=False, detail="process disappeared")

    deadline = time.monotonic() + timeout
    alive: set[int] | list[int]
    while time.monotonic() < deadline:
        alive = {target_pid for target_pid in target_pids if is_pid_alive(target_pid)}
        if not alive:
            clear_pid()
            extra = sorted(target_pids - ({pid} if pid is not None else set()))
            suffix = f"; also cleaned stale runtime pid(s): {extra}" if extra else ""
            return StopResult(
                pid=pid,
                stopped=True,
                forced=force,
                detail=f"stopped after {sig.name}{suffix}",
            )
        time.sleep(0.1)

    if force:
        alive = sorted(target_pid for target_pid in target_pids if is_pid_alive(target_pid))
        return StopResult(
            pid=pid,
            stopped=False,
            forced=True,
            detail=f"still running after SIGKILL + {timeout:.0f}s: {alive}",
        )

    # Escalate to SIGKILL automatically — the user can opt out with --no-force
    # in a future revision. Kill the whole group so orphaned children
    # (dashboard node proc, telegram updater threads) go down with the
    # parent and don't hold ports the next start needs.
    for target_pid in sorted(target_pids):
        try:
            _signal_process_group(target_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        alive = {target_pid for target_pid in target_pids if is_pid_alive(target_pid)}
        if not alive:
            clear_pid()
            extra = sorted(target_pids - ({pid} if pid is not None else set()))
            suffix = f"; also cleaned stale runtime pid(s): {extra}" if extra else ""
            return StopResult(
                pid=pid,
                stopped=True,
                forced=True,
                detail=f"escalated to SIGKILL after {timeout:.0f}s{suffix}",
            )
        time.sleep(0.1)

    alive = sorted(target_pid for target_pid in target_pids if is_pid_alive(target_pid))
    return StopResult(
        pid=pid,
        stopped=False,
        forced=True,
        detail=f"process refused to die — manual intervention needed: {alive}",
    )


# ─────────────────────── restart-from-inside-the-runtime ─────────────────────


def stop_targets() -> set[int]:
    """The live pids ``stop_daemon`` would signal right now."""
    pids = _discover_runtime_pids()
    pid = read_pid()
    if pid is not None:
        pids.add(pid)
    return {p for p in pids if p > 0 and p != os.getpid() and is_pid_alive(p)}


def restart_blast_radius_pid(targets: set[int] | None = None) -> int | None:
    """Return a runtime pid whose stop would take this process down with it.

    Two ways the caller dies with the runtime it is restarting:

    - the runtime is an *ancestor* of this process — ``server restart`` was
      executed by an agent's shell tool inside the runtime, so this process's
      stdio and wait chain disappear when the runtime exits, killing the
      restart between its stop and start phases;
    - the runtime shares this process's *process group* — it was backgrounded
      from the same (job-control-less) shell session, so a group-wide signal
      aimed at the runtime reaches this process too.

    ``server restart`` uses this to re-exec itself detached first, so the
    start half always runs. Returns ``None`` when a plain inline restart is
    safe.
    """
    if targets is None:
        targets = stop_targets()
    if not targets:
        return None
    try:
        own_pgid = os.getpgid(0)
    except OSError:
        own_pgid = -1
    parents = _pid_parent_map()
    ancestors: set[int] = set()
    pid = parents.get(os.getpid(), os.getppid())
    while pid > 1 and pid not in ancestors:
        ancestors.add(pid)
        pid = parents.get(pid, 1)
    for target in sorted(targets):
        if target in ancestors:
            return target
        try:
            if os.getpgid(target) == own_pgid:
                return target
        except (ProcessLookupError, PermissionError):
            continue
    return None


def spawn_detached_restart(restart_args: list[str]) -> int:
    """Re-exec ``olano server restart`` in its own session and return its pid.

    The helper's output goes to the runtime log — the caller's stdout is a
    pipe into the runtime being stopped, so it dies with the old daemon.
    ``RESTART_DETACHED_ENV`` tells the helper to skip blast-radius detection
    and restart inline.
    """
    log_path = _log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(log_path, "ab", buffering=0)  # noqa: SIM115 — handed to child
    env = dict(os.environ)
    env[RESTART_DETACHED_ENV] = "1"
    cmd = [sys.executable, "-m", "olano", "server", "restart", *restart_args]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
            close_fds=True,
        )
    finally:
        log_fh.close()
    return proc.pid


# ─────────────────────────────────── status ──────────────────────────────────


@dataclass
class StatusResult:
    running: bool
    pid: int | None
    api_reachable: bool
    api_port: int
    # Structured payloads from the runtime API.  ``status_payload`` is the
    # ``/api/status`` snapshot (uptime, channel count, scheduler jobs).
    # ``agents_payload`` is ``/api/agents`` — one dict per loaded agent
    # with ``name``, ``emoji``, ``model``, ``description``, ``channels``.
    status_payload: dict[str, Any] | None
    agents_payload: list[dict[str, Any]] | None
    pid_path: Path
    log_path: Path
    detail: str


def status() -> StatusResult:
    pid = read_pid()
    api_port = _runtime_api_port()

    running = bool(pid and is_pid_alive(pid))
    status_payload: dict[str, Any] | None = None
    agents_payload: list[dict[str, Any]] | None = None
    api_reachable = False
    detail = ""

    if running:
        status_payload, agents_payload, api_reachable = _probe_api(api_port)
        if not api_reachable:
            detail = "process running, but runtime API didn't answer (still booting?)"
    elif pid is not None:
        detail = f"stale pidfile (pid {pid} not alive) — run `olano server stop` to clean up"
    else:
        detail = "not running"

    return StatusResult(
        running=running,
        pid=pid,
        api_reachable=api_reachable,
        api_port=api_port,
        status_payload=status_payload,
        agents_payload=agents_payload,
        pid_path=_pid_path(),
        log_path=_log_path(),
        detail=detail,
    )


def _http_get_json(port: int, path: str, timeout: float = 1.5) -> Any | None:
    """GET ``http://127.0.0.1:<port><path>`` and return parsed JSON or ``None``.

    Returns ``None`` on connection refused, timeout, non-200 status, or
    bodies that don't parse as JSON — callers treat that as "the API isn't
    answering yet" rather than "broken".
    """
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}{path}",
            timeout=timeout,
        ) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return None


def _probe_api(
    port: int,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]] | None, bool]:
    """Probe the runtime API for status + agent listing.

    Returns ``(status_payload, agents_payload, reachable)`` where
    ``reachable`` is True if either endpoint answered with JSON.  Both
    payloads are structured dicts/lists from ``/api/status`` and
    ``/api/agents`` respectively.
    """
    status_payload = _http_get_json(port, "/api/status")
    agents_raw = _http_get_json(port, "/api/agents")

    agents_payload: list[dict[str, Any]] | None = None
    if isinstance(agents_raw, list):
        agents_payload = [a for a in agents_raw if isinstance(a, dict)]

    reachable = status_payload is not None or agents_payload is not None
    if isinstance(status_payload, dict):
        return status_payload, agents_payload, reachable
    return None, agents_payload, reachable
