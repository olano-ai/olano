"""Per-platform setup / pairing CLI commands.

These are registered under ``olano connect <platform>`` (and aliased as
``olano setup <platform>``).  Each handler is responsible for driving the
platform's pairing / bootstrap flow in the foreground so the user can scan a
QR code, complete an OAuth dance, etc., **without having to stop the main
``olano serve`` process** running in another terminal.

The runtime adapter detects an already-running bridge via ``GET /health`` and
reuses it (see ``olano/platforms/whatsapp.py``), so this works as a
side-by-side pairing helper.

Adding a new platform is a one-liner: write ``async def connect_<name>(...)``
and add it to ``CONNECT_HANDLERS``.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

from rich.console import Console

console = Console()


# ── Helpers ──────────────────────────────────────────────────────────────────


def _check_node() -> bool:
    return shutil.which("node") is not None and shutil.which("npm") is not None


async def _http_get_json(url: str, timeout: float = 2.0) -> dict | None:
    try:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status == 200:
                    return cast("dict | None", await resp.json())
    except Exception:
        return None
    return None


# ── WhatsApp ────────────────────────────────────────────────────────────────


def _resolve_agent_whatsapp_layout(agent_name: str) -> tuple[int, Path] | None:
    """Load ``agent.json5`` and return that agent's bridge_port + session_path.

    Returns ``None`` when the agent has no ``platform:whatsapp`` connector
    configured.  Mirrors ``WhatsAppAdapter.resolve_runtime_layout`` so the
    CLI talks to the exact same port + writes pairing creds to the exact
    same directory as the live runtime.
    """
    from olano.config.loader import load_agent_config
    from olano.platforms.whatsapp import WhatsAppAdapter

    try:
        cfg = load_agent_config(agent_name)
    except Exception as exc:
        console.print(f"[red]Could not load agent '{agent_name}':[/red] {exc}")
        return None
    extra: dict[str, Any] = {}
    for plat in getattr(cfg, "platforms", None) or []:
        ptype = getattr(plat, "type", None) or (
            plat.get("type") if isinstance(plat, dict) else None
        )
        if ptype == "platform:whatsapp":
            raw_extra = (
                getattr(plat, "extra", None) if not isinstance(plat, dict) else plat.get("extra")
            )
            if isinstance(raw_extra, dict):
                extra = raw_extra
            break
    else:
        console.print(
            f"[yellow]Agent '{agent_name}' has no `platform:whatsapp` connector.[/yellow]"
        )
        return None
    return WhatsAppAdapter.resolve_runtime_layout(agent_name, extra)


def _prompt_yes(question: str, default_yes: bool = True) -> bool:
    """Render a ``[Y/n]`` (or ``[y/N]``) prompt that re-asks on garbage input.

    Returns ``default_yes`` when stdin is not a tty so non-interactive runs
    (e.g. CI) don't hang forever.
    """
    if not sys.stdin.isatty():
        return default_yes
    suffix = "[Y/n]" if default_yes else "[y/N]"
    while True:
        try:
            answer = input(f"{question} {suffix} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if not answer:
            return default_yes
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False


def _kill_bridge_subprocess(proc: subprocess.Popen | None) -> None:
    """Best-effort SIGTERM+SIGKILL of a bridge subprocess group."""
    if proc is None or proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            proc.terminate()
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            if sys.platform == "win32":
                proc.kill()
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except Exception:
                pass


def _reset_session_dir(session_path: Path) -> bool:
    """Wipe and recreate the session credential dir.  Returns True on success."""
    try:
        if session_path.exists():
            shutil.rmtree(session_path, ignore_errors=True)
        session_path.mkdir(parents=True, exist_ok=True)
        return True
    except Exception as exc:
        console.print(f"[red]Could not reset session dir:[/red] {exc}")
        return False


async def connect_whatsapp(
    *,
    port: int,
    session: str | None,
    bridge_script: str | None,
    exit_after_pair: bool,
    timeout: int,
    agent: str | None = None,
) -> int:
    """Start the bundled Baileys bridge and walk the user through pairing.

    Streams the bridge's stdout/stderr (which contains the ASCII QR) directly
    to the user's terminal.  Polls ``/health`` to surface a clear status line
    and exits 0 once status==connected (when ``--exit-after-pair``) or stays
    running until Ctrl+C otherwise.

    When the bridge crashes or WhatsApp reports the session as logged-out,
    the user is offered an interactive "Reset session and try again?" prompt
    so they don't have to ``rm -rf`` the credential directory by hand.
    """
    from olano.config.home import get_olano_home
    from olano.platforms.whatsapp import WhatsAppAdapter

    if not _check_node():
        console.print("[red]Node.js + npm are required.[/red]  Run:  ./install.sh")
        return 2

    script = Path(bridge_script or WhatsAppAdapter._default_bridge_script())
    if not script.exists():
        console.print(f"[red]Bridge script not found:[/red] {script}")
        console.print(
            "[dim]Expected the bundled bridge at[/dim] "
            "[cyan]whatsapp-bridge/index.js[/cyan] "
            "[dim]in your project root, or set --bridge-script / "
            "LIONCLAW_WHATSAPP_BRIDGE.[/dim]"
        )
        return 2

    bridge_dir = script.parent
    if not (bridge_dir / "node_modules").exists():
        console.print(f"[dim]Installing bridge npm deps in[/dim] {bridge_dir} …")
        try:
            subprocess.run(
                ["npm", "install", "--no-audit", "--no-fund", "--silent"],
                cwd=str(bridge_dir),
                check=True,
                timeout=120,
            )
        except subprocess.CalledProcessError as exc:
            console.print(f"[red]npm install failed (exit {exc.returncode}).[/red]")
            return 2
        except subprocess.TimeoutExpired:
            console.print("[red]npm install timed out.[/red]")
            return 2

    # Agent-aware path resolution.  When `--agent <name>` is given we mirror
    # the live adapter's per-agent bridge_port + session_path so the pairing
    # credentials land where the running runtime actually reads them.
    # Explicit `--port` / `--session` still override agent defaults.
    agent_layout: tuple[int, Path] | None = None
    if agent:
        agent_layout = _resolve_agent_whatsapp_layout(agent)
        if agent_layout is None:
            return 2
        agent_port, agent_session_path = agent_layout
        session_path = agent_session_path if session is None else Path(session)
        # Only override the default port (3000) — let the user pin a
        # different port explicitly via --port if they really want to.
        if port == 3000:
            port = agent_port
        console.print(
            f"[dim]Agent '{agent}' resolves to port={agent_port} session={agent_session_path}[/dim]"
        )
    else:
        session_path = Path(
            session
            or os.environ.get("WHATSAPP_SESSION_PATH")
            or (get_olano_home() / "platforms" / "whatsapp" / "session")
        )
    session_path.mkdir(parents=True, exist_ok=True)

    health_url = f"http://127.0.0.1:{port}/health"

    # Outer retry loop: lets us wipe the session and respawn the bridge
    # when WhatsApp invalidates the credentials, without forcing the user
    # to ``rm -rf`` anything manually.  ``resets_remaining`` is the budget
    # of reset prompts we'll offer before giving up.
    resets_remaining = 1
    rc = 0

    while True:
        existing = await _http_get_json(health_url)
        if existing and existing.get("status") == "connected":
            console.print(
                f"[green]✓ WhatsApp bridge already paired on port {port}[/green] "
                f"(jid={existing.get('jid')}). Nothing to do."
            )
            return 0

        if existing is not None:
            console.print(
                f"[yellow]A bridge is already running on port {port}[/yellow] "
                f"(status={existing.get('status')}). I'll talk to that one — "
                "scan the QR shown by the other terminal."
            )

        console.print(
            f"[bold cyan]Starting WhatsApp bridge[/bold cyan]  "
            f"[dim]port={port} session={session_path}[/dim]"
        )
        console.print(
            "[dim]Open WhatsApp on your phone → Settings → Linked Devices → "
            "Link a device, then point the camera at the QR code below.[/dim]\n"
        )

        proc: subprocess.Popen | None = None
        if existing is None:
            bridge_env = os.environ.copy()
            bridge_env.setdefault("WHATSAPP_DEBUG", "0")
            proc = subprocess.Popen(
                [
                    "node",
                    str(script),
                    "--port",
                    str(port),
                    "--session",
                    str(session_path),
                    "--mode",
                    "pairing",
                ],
                cwd=str(bridge_dir),
                env=bridge_env,
                preexec_fn=None if sys.platform == "win32" else os.setsid,
                stdout=None,
                stderr=None,
            )
            console.print(f"[dim]bridge pid={proc.pid}[/dim]")

        stop = asyncio.Event()

        def _sigint(_sig=None, _frm=None, _stop=stop):
            _stop.set()

        if sys.platform != "win32":
            signal.signal(signal.SIGINT, _sigint)
            signal.signal(signal.SIGTERM, _sigint)

        # Sentinels for "should we offer to reset and loop again?".
        bridge_logged_out = False
        bridge_died_unexpectedly = False
        finished = False

        started = time.monotonic()
        last_status = ""
        paired_announced = False
        try:
            while not stop.is_set():
                if proc is not None and proc.poll() is not None:
                    console.print(
                        f"\n[red]Bridge exited (code {proc.returncode}).[/red]  "
                        "Check the output above for the cause."
                    )
                    rc = proc.returncode or 1
                    bridge_died_unexpectedly = True
                    break

                health = await _http_get_json(health_url)
                status = (health or {}).get("status", "starting")
                if status != last_status:
                    if status == "qr":
                        console.print(
                            "[yellow]Waiting for QR scan…[/yellow]  "
                            "[dim](rescan if it expires; bridge auto-rotates QRs)[/dim]"
                        )
                    elif status == "connecting":
                        console.print("[dim]Connecting to WhatsApp…[/dim]")
                    elif status == "connected" and not paired_announced:
                        paired_announced = True
                        jid = (health or {}).get("jid") or "(unknown jid)"
                        console.print(f"\n[bold green]✓ Paired and connected as {jid}[/bold green]")
                        if exit_after_pair:
                            console.print(
                                "[dim]Auth state saved to[/dim] "
                                f"[cyan]{session_path}[/cyan]\n"
                                "[dim]Run[/dim] [cyan]olano serve[/cyan] "
                                "[dim]— the runtime will start its own bridge that "
                                "auto-authenticates from this session.[/dim]"
                            )
                            rc = 0
                            finished = True
                            break
                        console.print(
                            "[dim]Bridge is running. Press Ctrl+C to stop. "
                            "If `olano serve` is running, it will detect this "
                            "bridge and route through it.[/dim]"
                        )
                    elif status == "logged_out":
                        console.print("[red]WhatsApp logged this session out.[/red]")
                        rc = 3
                        bridge_logged_out = True
                        break
                    last_status = status

                if (
                    status in ("starting", "connecting", "qr")
                    and (time.monotonic() - started) > timeout
                ):
                    console.print(f"\n[red]Timed out after {timeout}s without pairing.[/red]")
                    rc = 4
                    finished = True
                    break

                try:
                    await asyncio.wait_for(stop.wait(), timeout=1.5)
                except TimeoutError:
                    pass

            if stop.is_set() and not finished:
                # Ctrl+C — leave the loop entirely.
                finished = True
        finally:
            # Always stop the subprocess we own before deciding what to
            # do next — even a logged-out bridge holds the port open.
            _kill_bridge_subprocess(proc)

        if finished:
            return rc

        # Recoverable failure path — both ``bridge_logged_out`` and
        # ``bridge_died_unexpectedly`` typically mean WhatsApp invalidated
        # the saved credentials.  Offer to wipe + retry instead of asking
        # the user to mess with the filesystem.
        if (bridge_logged_out or bridge_died_unexpectedly) and resets_remaining > 0:
            resets_remaining -= 1
            console.print()
            console.print(
                "[yellow]This is almost always caused by stale WhatsApp credentials.[/yellow]"
            )
            if not _prompt_yes(
                f"Reset the session at [cyan]{session_path}[/cyan] and try again?",
                default_yes=True,
            ):
                return rc
            if not _reset_session_dir(session_path):
                return rc
            console.print(
                f"[green]✓ Cleared session at {session_path}[/green]  "
                "[dim]restarting bridge…[/dim]\n"
            )
            # Loop around → fresh spawn against the empty session dir.
            continue

        return rc


# ── Weixin ──────────────────────────────────────────────────────────────────


async def connect_weixin(timeout: int = 480, bot_type: str = "3") -> int:
    """Bind a personal WeChat account via the iLink QR login flow.

    There is no developer console for this connector: Tencent mints the
    ``account_id`` + ``bot_token`` when the user scans and confirms the QR.
    ``qr_login`` persists them under ``$LIONCLAW_HOME/weixin/accounts/`` where
    the runtime adapter picks them up automatically.
    """
    from olano.config.home import get_olano_home
    from olano.platforms.weixin import check_weixin_requirements, qr_login

    if not check_weixin_requirements():
        console.print(
            "[red]Weixin support needs the `aiohttp` and `cryptography` packages.[/red]\n"
            "Install them into the olano environment and re-run."
        )
        return 2

    console.print("[bold]Weixin / iLink QR login[/bold]")
    console.print(
        "[dim]Scan the QR below with the WeChat account the agent should use, "
        "then confirm in the app.[/dim]\n"
    )
    try:
        creds = await qr_login(str(get_olano_home()), bot_type=bot_type, timeout_seconds=timeout)
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/yellow]")
        return 130
    if not creds:
        console.print("[red]Weixin login did not complete (timed out or failed).[/red]")
        return 4

    console.print()
    console.print("[green]✓ WeChat account linked.[/green]")
    console.print(f"  account_id (bot ID): [cyan]{creds['account_id']}[/cyan]")
    console.print("  bot token: [dim]saved to $LIONCLAW_HOME/weixin/accounts/ (0600)[/dim]")
    console.print()
    console.print(
        "Next: add a [cyan]WeChat (Weixin / iLink Bot)[/cyan] connector to the agent in the "
        "dashboard. With a single linked account, the credential fields can stay "
        "blank — the adapter finds this login on its own. Then restart the agent."
    )
    return 0


# ── Registry ────────────────────────────────────────────────────────────────

CONNECT_HANDLERS: dict[str, Callable[..., Awaitable[int]]] = {
    "whatsapp": connect_whatsapp,
    "weixin": connect_weixin,
}


def supported_platforms() -> list[str]:
    return sorted(CONNECT_HANDLERS.keys())
