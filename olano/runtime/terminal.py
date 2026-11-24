"""Interactive PTY ↔ WebSocket bridge for the dashboard's Terminal tab.

The dashboard's left-nav Terminal renders an `xterm.js` instance per tab and
opens one WebSocket per tab to ``/api/terminal/ws`` (wired in ``api.py``). This
module is the server half: it forks the operator's login shell onto a real
pseudo-terminal and pumps bytes both directions, so all the "real terminal"
behaviour — arrow-key history, tab-completion, line editing, colour — comes
for free from the actual shell, not from any client-side emulation.

Wire protocol (one socket, two frame kinds):
  * client → server: raw stdin as **binary** frames → ``os.write(master_fd)``;
    control as **text** JSON ``{"type":"resize","rows":R,"cols":C}``.
  * server → client: PTY output as **binary** frames (``ws.send_bytes``).
    Bytes are forwarded raw so xterm's own UTF-8 decoder stitches multibyte
    sequences that fall across ``os.read`` boundaries.

Security note: on an OPEN/unmanaged install this hands a browser a real host
shell. On a MANAGED deployment the host shell in the runtime container (which
holds /app + secrets) is never exposed — instead the PTY is opened INTO the
agent's isolated, source-/secret-free Docker sandbox via ``docker exec`` (see
``run_pty_bridge``'s ``container`` argument). The route gates it on
``terminal_enabled()`` (on by default on open boxes; managed boxes require the
sandbox to be enabled) plus admin + origin + token checks; this module assumes
the caller has already authorised the connection and only owns the process/PTY
lifecycle.
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import json
import logging
import os
import pty
import shlex
import signal
import struct
import sys
import termios
from pathlib import Path
from typing import Any

from olano.backends.interpreter_shims import CONTAINER_SHIM_PATH, TERMINAL_RC_NAME

logger = logging.getLogger(__name__)

# How much to read from the PTY master per wake-up, and how many pending output
# chunks to buffer before we pause reading (kernel PTY buffer then back-pressures
# the child) so a slow client can't make us buffer unboundedly.
_READ_CHUNK = 65536
_QUEUE_MAX = 256

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def terminal_enabled() -> bool:
    """Whether the terminal/shell surface is available. **Enabled by default.**

    On an open box this is a real host shell over a runtime that binds 0.0.0.0,
    so the route layer still enforces admin (multi-user), origin and bearer-token
    checks on top of this. Disable it entirely via the environment:

      * ``LIONCLAW_ENABLE_TERMINAL=0`` (or false/no/off) → force OFF
      * ``LIONCLAW_DISABLE_TERMINAL=1`` (or true/yes/on)  → force OFF (kill switch)

    Managed deployments can additionally turn it off via the signed
    deployment settings (``features.terminal`` / ``features.shell``) — checked
    per call so a remote change bites live connections' reconnects immediately.
    """
    if os.environ.get("LIONCLAW_ENABLE_TERMINAL", "").strip().lower() in _FALSY:
        return False
    if os.environ.get("LIONCLAW_DISABLE_TERMINAL", "").strip().lower() in _TRUTHY:
        return False
    try:
        from olano.deployment import enforcement as dep_enforcement

        if not dep_enforcement.feature_enabled("terminal"):
            return False
        if not dep_enforcement.feature_enabled("shell"):
            # No shell entitlement ⇒ no PTY either.
            return False
        if dep_enforcement.require_sandboxed_exec():
            # Managed posture: the host shell in the runtime container (holds
            # /app + secrets) is NEVER exposed. The terminal instead opens INTO
            # the agent's isolated sandbox (see run_pty_bridge / api.py), which
            # holds neither source nor secrets — but only when sandbox exec is
            # enabled+validated. With no sandbox there is no safe shell to offer.
            return dep_enforcement.sandbox_exec_enabled()
    except Exception:  # never let the entitlements layer 500 the status path
        logger.exception("deployment terminal gate failed; keeping terminal off")
        return False
    return True


def terminal_uses_sandbox() -> bool:
    """True when the PTY must open into the agent's sandbox, not a host shell.

    Managed posture ⇒ True (the host shell in the runtime container is never
    exposed). Open/unmanaged ⇒ False (the ordinary host login shell).

    **Fails closed**, matching ``require_sandboxed_exec``: if the posture can't
    be resolved we demand the sandbox. The caller then refuses the connection
    when no sandbox container is up (api.py closes 4503) rather than opening a
    host PTY — an unresolvable posture must never downgrade to a host shell.
    """
    try:
        from olano.deployment import enforcement as dep_enforcement

        return dep_enforcement.require_sandboxed_exec()
    except Exception:
        logger.exception("sandbox terminal gate failed; requiring sandbox (fail closed)")
        return True


def sandbox_backend_for(agent: str | None) -> Any | None:
    """The live ``OlanoDockerBackend`` for an agent, or None.

    Reads the per-agent sandbox registry populated at agent-build time
    (``core.builder._SANDBOX_BACKENDS``). Callers that only need somewhere to
    ``docker exec`` use :func:`sandbox_container_ref`; callers that want to run
    a one-shot command (the chat ``!`` lane in ``api.py``) use the backend
    itself, so the command goes through the same hardened exec path as the
    agent's own tools.
    """
    if not agent:
        return None
    try:
        from olano.core.builder import get_sandbox_backend

        return get_sandbox_backend(agent)
    except Exception:
        logger.exception("sandbox backend resolution failed for agent %s", agent)
        return None


def sandbox_container_ref(agent: str | None) -> str | None:
    """Resolve the live sandbox container for an agent, or None.

    Returns the docker container name/id to ``docker exec`` into, or None when
    the agent has no live sandbox (caller must then refuse to open a terminal
    under managed posture — never fall back to a host shell).
    """
    backend = sandbox_backend_for(agent)
    if backend is None:
        return None
    try:
        return backend.container_ref() or None
    except Exception:
        logger.exception("sandbox container resolution failed for agent %s", agent)
        return None


# A WebSocket close reason is capped at 123 bytes of UTF-8 (RFC 6455 §5.5.1);
# a longer one makes the close frame itself invalid.
_CLOSE_REASON_MAX_BYTES = 123


def sandbox_unavailable_reason(agent: str | None) -> str:
    """Why this agent has no live sandbox, sized for a WebSocket close reason.

    The 4503 close used to carry the bare "sandbox not running", so the tab
    could only ever say "start the agent, then reconnect" — which was wrong
    whenever the container had failed to start (or the agent was never
    booted). The builder records the outcome of every sandbox decision
    (``core.builder.sandbox_status``); relaying it lets the tab say what
    actually happened. Never raises.
    """
    text = "sandbox not running"
    try:
        from olano.core.builder import sandbox_status

        status = sandbox_status(agent) if agent else None
        reason = str((status or {}).get("reason") or "").strip()
        if reason:
            text = reason
    except Exception:
        logger.debug("sandbox status lookup failed for %r", agent, exc_info=True)
    raw = text.encode("utf-8")
    if len(raw) > _CLOSE_REASON_MAX_BYTES:
        text = raw[: _CLOSE_REASON_MAX_BYTES - 1].decode("utf-8", errors="ignore") + "…"
        while len(text.encode("utf-8")) > _CLOSE_REASON_MAX_BYTES:
            text = text[:-2] + "…"
    return text


def _default_shell() -> str:
    return os.environ.get("SHELL") or "/bin/bash"


def _venv_bin() -> Path | None:
    """The bin/ dir of the virtualenv the daemon runs under, if any.

    Uses ``sys.executable`` (the daemon's own python) so it's correct even when
    ``VIRTUAL_ENV`` isn't exported. Returns None for a non-venv (system) python —
    no ``activate`` script to source.

    NB: do NOT ``resolve()`` — ``.venv/bin/python`` is a symlink to the base
    interpreter (e.g. under miniconda/homebrew), and resolving it would point at
    the base install's bin instead of the venv's. The venv bin is simply the
    directory ``sys.executable`` lives in.
    """
    bin_dir = Path(sys.executable).parent
    return bin_dir if (bin_dir / "activate").exists() else None


def _child_env() -> dict[str, str]:
    """Environment for the interactive shell child.

    Inherits the runtime daemon's FULL environment so the terminal matches the
    chat "!" shell (`/api/shell/run`) and the agent's own tools — most visibly
    the virtualenv the daemon was launched from. (This is the operator's own
    host shell, gated admin-only; it intentionally mirrors `!`.)

    The one adjustment: start the runtime venv at the front of ``PATH`` and set
    ``VIRTUAL_ENV`` from ``sys.executable``. The zsh startup wrapper below then
    re-activates that venv after user rc files run, without writing commands into
    the live PTY where they could land inside full-screen programs such as vim.
    """
    # Strip platform secrets (vault key, relay secret, deployment/dashboard
    # tokens, and — under a managed posture — provider keys) so an interactive
    # terminal can't `printenv` them out. Mirrors the agent `!`/execute shell.
    from olano.security.shell_guard import sanitized_shell_env

    env = sanitized_shell_env()
    for k in ("NO_COLOR", "CLICOLOR"):  # undo the quiet-output defaults for a real TTY
        env.pop(k, None)
    env["TERM"] = "xterm-256color"
    env["LIONCLAW_TERMINAL"] = "1"  # let shell rc detect the context if it wants
    env["CONDA_AUTO_ACTIVATE_BASE"] = "false"
    vbin = _venv_bin()
    if vbin:
        venv_bin = str(vbin)
        env["VIRTUAL_ENV"] = str(vbin.parent)
        env.pop("PYTHONHOME", None)
        parts = [p for p in env.get("PATH", "").split(os.pathsep) if p and p != venv_bin]
        env["PATH"] = os.pathsep.join([venv_bin, *parts])
    return env


def _source_if_exists(path: Path) -> str:
    quoted = shlex.quote(str(path))
    return f"[ -r {quoted} ] && source {quoted}\n"


def _venv_activate_snippet(vbin: Path | None) -> str:
    if not vbin:
        return ""
    activate = vbin / "activate"
    quoted_activate = shlex.quote(str(activate))
    quoted_venv = shlex.quote(str(vbin.parent))
    quoted_bin = shlex.quote(str(vbin))
    path_case = shlex.quote(f":{vbin}:")
    return (
        f"if [ -r {quoted_activate} ]; then\n"
        f"  source {quoted_activate}\n"
        "else\n"
        f"  export VIRTUAL_ENV={quoted_venv}\n"
        f'  case ":$PATH:" in *{path_case}*) ;; *) export PATH={quoted_bin}:$PATH ;; esac\n'
        "fi\n"
    )


def _prepare_zsh_startup(env: dict[str, str], vbin: Path | None) -> None:
    """Install a temporary ZDOTDIR that sources user zsh files, then the venv.

    Writing ``source .venv/bin/activate`` into the PTY after startup races with
    fast typists and full-screen programs. A ZDOTDIR wrapper lets zsh complete
    normal startup first, then activate the runtime venv before the first prompt.
    """
    from olano.config.home import get_olano_home

    home = Path(env.get("HOME") or str(Path.home()))
    # Resolve through the shared resolver rather than re-implementing the
    # env-var + default chain here: a second hardcoded "~/.olano" fallback
    # silently disagrees with the real home the moment either name changes.
    env_home = env.get("OLANO_HOME") or env.get("LIONCLAW_HOME")
    zdir = (
        (Path(env_home) if env_home else get_olano_home())
        / "run"
        / f"terminal-zdotdir-{os.getpid()}"
    )
    zdir.mkdir(parents=True, exist_ok=True)

    (zdir / ".zshenv").write_text(_source_if_exists(home / ".zshenv"), encoding="utf-8")
    (zdir / ".zprofile").write_text(_source_if_exists(home / ".zprofile"), encoding="utf-8")
    (zdir / ".zshrc").write_text(_source_if_exists(home / ".zshrc"), encoding="utf-8")
    (zdir / ".zlogin").write_text(
        _source_if_exists(home / ".zlogin") + _venv_activate_snippet(vbin),
        encoding="utf-8",
    )
    env["ZDOTDIR"] = str(zdir)


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    try:
        winsize = struct.pack("HHHH", max(int(rows), 1), max(int(cols), 1), 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except OSError:
        pass


def _spawn_shell(cwd: Path) -> tuple[int, int]:
    """Fork the login shell attached to a fresh PTY. Returns (pid, master_fd).

    ``pty.fork`` atomically does openpty + fork + setsid + dup2, so the child is
    a new session leader with the slave as its controlling terminal.
    """
    pid, master_fd = pty.fork()
    if pid == 0:  # child
        try:
            os.chdir(str(cwd))
        except Exception:
            pass
        shell = _default_shell()
        env = _child_env()
        if Path(shell).name == "zsh":
            _prepare_zsh_startup(env, _venv_bin())
        try:
            os.execvpe(shell, [shell, "-l"], env)
        except Exception:
            os._exit(127)
    return pid, master_fd


def _docker_client_env() -> dict[str, str]:
    """Minimal env for the ``docker exec`` client child (managed sandbox PTY).

    The interactive environment is the CONTAINER's own login shell (its PATH,
    incl. ``/opt/agent-tools/bin``, and the ``sandbox`` user), so this only needs
    what the docker CLI itself uses to reach the daemon — no secrets, no venv,
    no host rc wiring. DOCKER_HOST/context vars are passed through when set so a
    non-default socket still resolves.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "TERM": "xterm-256color",
        "HOME": os.environ.get("HOME", "/root"),
    }
    for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        val = os.environ.get(key)
        if val:
            env[key] = val
    return env


# Prefer an interactive **bash** inside the sandbox, falling back to whatever
# /bin/sh is. This is not a style preference: Debian's /bin/sh is dash, which has
# no readline — so Tab did nothing, ↑/↓ printed `^[[A`, and the dashboard
# terminal felt broken in exactly the ways a shell is not supposed to. bash
# brings filename/command completion and history editing for free (and, with
# bash-completion in the image, git-subcommand completion too). Evaluated inside
# the container because only it knows what it has; `command -v` keeps it POSIX,
# so the fallback runs under dash itself.
#
# `bash --rcfile <tool layer>/shims/terminal.bashrc -i` when the runtime has
# written that file (interpreter_shims.py), else the plain `bash -l`. A login
# shell sources /etc/profile, which on Debian RESETS PATH — so nothing under
# /opt/agent-tools (pip --user scripts, npm globals, seeded CLIs, the
# `python` shim) was reachable from the Terminal even though the agent's
# `execute` had all of it. The rc file sources /etc/profile itself first
# (completion, prompt, history still load) and then applies the exact env +
# PATH the exec lane uses.
_SANDBOX_TERMINAL_RC = f"{CONTAINER_SHIM_PATH}/{TERMINAL_RC_NAME}"
_SANDBOX_SHELL_BOOTSTRAP = (
    f"f={_SANDBOX_TERMINAL_RC}; "
    "if command -v bash >/dev/null 2>&1; then "
    'if [ -r "$f" ]; then exec bash --rcfile "$f" -i; fi; '
    "exec bash -l; fi; exec sh -l"
)


def _spawn_sandbox_shell(container: str) -> tuple[int, int]:
    """Fork ``docker exec -it <container> <login shell>`` attached to a fresh PTY.

    The PTY's slave is a real TTY, so docker's ``-t`` allocates a terminal inside
    the container and ``-i`` keeps stdin flowing; bytes pump through exactly like
    the host-shell path. The shell runs INSIDE the isolated sandbox (no product
    source, no secrets) as the image's non-root ``sandbox`` user — so, unlike the
    host path, there is no env to scrub and no venv/zsh wiring to graft on.
    """
    pid, master_fd = pty.fork()
    if pid == 0:  # child
        env = _docker_client_env()
        argv = ["docker", "exec", "-it", container, "sh", "-c", _SANDBOX_SHELL_BOOTSTRAP]
        try:
            os.execvpe(argv[0], argv, env)
        except Exception:
            os._exit(127)
    return pid, master_fd


async def _terminate(pid: int, master_fd: int, loop: asyncio.AbstractEventLoop) -> None:
    """Tear the child process group down (SIGHUP → grace → SIGKILL) and reap it."""
    try:
        loop.remove_reader(master_fd)
    except Exception:
        pass

    def _killpg(sig: int) -> None:
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, OSError):
            pass

    _killpg(signal.SIGHUP)
    await asyncio.sleep(0.2)
    _killpg(signal.SIGKILL)
    try:
        os.close(master_fd)
    except OSError:
        pass
    # Reap so we don't leave a zombie; bounded poll.
    for _ in range(50):
        try:
            wpid, _ = os.waitpid(pid, os.WNOHANG)
            if wpid != 0:
                break
        except ChildProcessError:
            break
        except OSError:
            break
        await asyncio.sleep(0.02)


# Active PTY WebSockets, so a live deployment-settings change (terminal/shell
# disabled) can close open sessions instead of only blocking new ones.
_ACTIVE_SESSIONS: set[Any] = set()


async def close_all_sessions() -> int:
    """Close every active terminal session (policy code 4403). Returns count."""
    closed = 0
    for ws in list(_ACTIVE_SESSIONS):
        try:
            await ws.close(code=4403, reason="terminal disabled by deployment settings")
            closed += 1
        except Exception:  # already closing/closed
            pass
        _ACTIVE_SESSIONS.discard(ws)
    return closed


async def run_pty_bridge(websocket, *, cwd: Path, container: str | None = None) -> None:
    """Bridge an already-accepted WebSocket to a freshly-forked login shell.

    ``container`` None → a host login shell in ``cwd`` (open/unmanaged install).
    ``container`` set  → ``docker exec`` into that live sandbox container (managed
    posture); ``cwd`` is ignored (the container's own workdir applies). Runs until
    either side closes; always tears the child down on exit.
    """
    _ACTIVE_SESSIONS.add(websocket)
    loop = asyncio.get_running_loop()
    pid, master_fd = _spawn_sandbox_shell(container) if container else _spawn_shell(cwd)
    os.set_blocking(master_fd, False)

    queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=_QUEUE_MAX)
    paused = False  # reader removed because the output queue filled up

    def on_readable() -> None:
        nonlocal paused
        # Never read what we can't enqueue (would force us to drop PTY bytes and
        # corrupt the stream). Pause first; the consumer re-arms us.
        if queue.full():
            try:
                loop.remove_reader(master_fd)
            except Exception:
                pass
            paused = True
            return
        try:
            data = os.read(master_fd, _READ_CHUNK)
        except OSError as exc:
            if exc.errno == errno.EAGAIN:
                return
            data = b""  # EIO on macOS (and friends) means the child exited
        if not data:  # EOF
            try:
                loop.remove_reader(master_fd)
            except Exception:
                pass
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
            return
        queue.put_nowait(data)  # safe: we checked not-full above
        if queue.full():
            try:
                loop.remove_reader(master_fd)
            except Exception:
                pass
            paused = True

    async def pump_to_client() -> None:
        nonlocal paused
        while True:
            data = await queue.get()
            if data is None:  # EOF sentinel — child gone
                return
            await websocket.send_bytes(data)  # natural back-pressure to a slow client
            if paused and not queue.full():
                paused = False
                try:
                    loop.add_reader(master_fd, on_readable)
                except Exception:
                    pass

    async def pump_from_client() -> None:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                return
            data = msg.get("bytes")
            if data is not None:
                try:
                    os.write(master_fd, data)
                except OSError:
                    return
                continue
            text = msg.get("text")
            if text:
                try:
                    ctl = json.loads(text)
                except (ValueError, TypeError):
                    continue
                if isinstance(ctl, dict) and ctl.get("type") == "resize":
                    _set_winsize(master_fd, ctl.get("rows", 24), ctl.get("cols", 80))

    loop.add_reader(master_fd, on_readable)
    send_task = asyncio.create_task(pump_to_client())
    recv_task = asyncio.create_task(pump_from_client())
    try:
        await asyncio.wait({send_task, recv_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        _ACTIVE_SESSIONS.discard(websocket)
        for task in (send_task, recv_task):
            if not task.done():
                task.cancel()
        await _terminate(pid, master_fd, loop)
