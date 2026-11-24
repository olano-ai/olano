"""
Olano CLI entry point.

Usage:
    olano --agent <name> --message <text>         # send a message to an agent
    olano --agent <name> --message <text> --stream # stream the response
    olano serve                                     # start the runtime
    olano agents                                    # list available agents
    olano agents create --name <name>               # scaffold a new agent
    olano setup                                     # init workspace
    olano acp --agent <name>                        # start ACP stdio server
    olano schedule list                             # list all scheduled jobs
"""

from __future__ import annotations

import asyncio
import select
import sys
import time
from pathlib import Path
from typing import Any, Literal, cast

from olano.cli._quiet import run_quietly, run_quietly_async
from olano.config.home import (
    HomeConfig,
    ensure_olano_home,
    get_olano_home,
    get_olano_paths,
    is_default_home,
    load_home_config,
    save_home_config,
    set_olano_home,
)


# Auto-load .env from cwd or project root before anything else
def _load_env(home: str | Path | None = None) -> None:
    try:
        from dotenv import load_dotenv

        search_roots = [Path.cwd(), *Path.cwd().parents[:3], get_olano_home(home)]
        for p in search_roots:
            env_file = p / ".env"
            if env_file.exists():
                load_dotenv(env_file, override=True)
    except ImportError:
        pass
    # ``OLANO_*`` / ``LIONCLAW_*`` are mirrored at ``import olano``, before the
    # files above and below exist to the process; re-apply after each overlay
    # so a variable they add under either spelling reaches every reader (and
    # a .env ``OLANO_X`` fills ``LIONCLAW_X`` before the bootstrap file can).
    from olano.env_aliases import apply_env_aliases

    apply_env_aliases()
    # Machine-level bootstrap env (/opt/olano/olano.env), written once by the
    # provisioner (olano-cloud cloud-init / deploy scripts). Lowest precedence —
    # only fills vars nothing else set — so every launch path (CLI, serve,
    # docker, systemd) inherits deployment wiring like the MCP OAuth relay.
    from olano.config.bootstrap_env import load_bootstrap_env

    load_bootstrap_env()
    apply_env_aliases()


_load_env()

import logging
import os

_ACTIVE_HOME_LOG: Path | None = None


def _setup_logging() -> None:
    level_name = os.environ.get("LIONCLAW_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    console_level = level if level <= logging.DEBUG else logging.WARNING
    console_handler = next(
        (
            handler
            for handler in root_logger.handlers
            if getattr(handler, "_lionclaw_console_handler", False)
        ),
        None,
    )
    if console_handler is None:
        console_handler = logging.StreamHandler()
        console_handler._lionclaw_console_handler = True  # type: ignore[attr-defined]
        console_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        root_logger.addHandler(console_handler)
    console_handler.setLevel(console_level)

    # Quiet noisy third-party loggers unless debug mode
    if level > logging.DEBUG:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("telegram").setLevel(logging.WARNING)
        logging.getLogger("apscheduler").setLevel(logging.WARNING)
        logging.getLogger("chromadb").setLevel(logging.WARNING)


def _configure_home_logging(home: Path) -> None:
    global _ACTIVE_HOME_LOG

    log_path = get_olano_paths(home).logs_dir / "olano.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if log_path == _ACTIVE_HOME_LOG:
        return

    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        if getattr(handler, "_lionclaw_home_handler", False):
            root_logger.removeHandler(handler)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler._lionclaw_home_handler = True  # type: ignore[attr-defined]
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root_logger.addHandler(file_handler)
    _ACTIVE_HOME_LOG = log_path


_setup_logging()

import click
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

console = Console()


# ─────────────────────────────────────── root group ───────────────────────────


@click.group(
    name="olano",
    invoke_without_command=True,
    epilog=(
        "Examples:\n"
        "  olano setup\n"
        "  olano --home ~/.lionclaw doctor --agent merli   # a pre-rename home\n"
        "  olano --home ~/client-a-olano dashboard\n"
        "  olano agents create --from-template researcher --name scout\n"
        "  olano agents install lionDev --as coder"
    ),
)
@click.option(
    "--home",
    default=None,
    help=(
        "Olano home directory (default: $OLANO_HOME, $LIONCLAW_HOME, ~/.olano, "
        "or an existing ~/olano or ~/.lionclaw)"
    ),
)
@click.option("--agent", "-a", default=None, help="Agent name to send a message to")
@click.option("--message", "-m", default=None, help="Message to send to the agent")
@click.option(
    "--non-interactive",
    "-n",
    "non_interactive",
    default=None,
    help="Run a single task non-interactively and exit",
)
@click.option(
    "--quiet", "-q", is_flag=True, help="Pipe-safe non-interactive output; diagnostics go to stderr"
)
@click.option(
    "--no-stream", is_flag=True, help="Buffer non-interactive output and flush once at the end"
)
@click.option(
    "--shell-allow-list",
    default=None,
    help="Typed shell allow-list entries for non-interactive mode",
)
@click.option("--thread", "-t", default=None, help="Thread ID for session continuity")
@click.option("--stream", "-s", is_flag=True, help="Stream the response token by token")
@click.option("--model", default=None, help="Override agent model (provider:model)")
@click.version_option()
@click.pass_context
def cli(
    ctx: click.Context,
    home: str | None,
    agent: str | None,
    message: str | None,
    non_interactive: str | None,
    quiet: bool,
    no_stream: bool,
    shell_allow_list: str | None,
    thread: str | None,
    stream: bool,
    model: str | None,
) -> None:
    """Olano — multi-agent runtime powered by deepagents."""
    active_home = set_olano_home(home or get_olano_home())
    ensure_olano_home(active_home)
    _load_env(active_home)
    _configure_home_logging(active_home)
    ctx.ensure_object(dict)
    ctx.obj["home"] = str(active_home)

    if ctx.invoked_subcommand is not None:
        return

    if message and non_interactive:
        raise click.UsageError("--message and --non-interactive cannot be used together.")
    message, non_interactive = _merge_prompt_inputs(message, non_interactive)

    if (quiet or no_stream) and not non_interactive:
        raise click.UsageError(
            "--quiet and --no-stream require --non-interactive (-n) or piped stdin."
        )

    selected_agent = agent or _default_agent_name()
    if non_interactive:
        from olano.cli.non_interactive import run_non_interactive

        config_source = "auto" if agent else "home"
        exit_code = asyncio.run(
            run_non_interactive(
                selected_agent,
                non_interactive,
                thread_id=thread,
                model_override=model,
                quiet=quiet,
                stream=not no_stream,
                shell_allow_list=shell_allow_list,
                config_source=config_source,
            )
        )
        sys.exit(exit_code)
    elif message:
        asyncio.run(
            _send_message(
                selected_agent,
                message,
                thread_id=thread,
                stream=stream,
                model_override=model,
            )
        )
    else:
        click.echo(ctx.get_help())


# ─────────────────────────────────────── send message ─────────────────────────


async def _send_message(
    agent_name: str,
    message: str,
    thread_id: str | None = None,
    stream: bool = False,
    model_override: str | None = None,
) -> None:
    from olano.config.loader import load_agent_config
    from olano.core.builder import build_agent_async, build_agent_with_mcp, stop_rag_watchers
    from olano.core.checkpoints import aclose_checkpointers
    from olano.core.runner import AgentRunner
    from olano.core.stores import aclose_stores

    try:
        config = load_agent_config(agent_name)
    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    if model_override:
        config.model = model_override

    console.print(
        f"[dim]Agent:[/dim] [bold]{config.emoji} {config.name}[/bold]  [dim]model={config.model}[/dim]"
    )

    try:
        try:
            builder = build_agent_with_mcp if config.mcp_servers else build_agent_async
            agent = await _run_quietly_async(builder, config)
        except Exception as e:
            console.print(f"[red]Failed to build agent:[/red] {e}")
            sys.exit(1)

        runner = AgentRunner(agent, config)

        from olano.core.actionable_errors import remediation_lines
        from olano.core.runner import AgentError

        if stream:
            console.print(Panel(f"[bold blue]{message}[/bold blue]", title="You", expand=False))
            console.print("[bold green]Assistant:[/bold green] ", end="")
            try:
                async for chunk in runner.astream(message, thread_id=thread_id):
                    console.print(chunk, end="", highlight=False)
                console.print()
            except AgentError as e:
                console.print(f"\n[red bold]Error:[/red bold] {e}")
                for line in remediation_lines(e):
                    console.print(f"[dim]{line}[/dim]")
        else:
            try:
                with console.status("[bold]Thinking...[/bold]"):
                    response = await runner.ainvoke(message, thread_id=thread_id)
                console.print(Panel(f"[bold blue]{message}[/bold blue]", title="You", expand=False))
                console.print(
                    Panel(Markdown(response), title=f"{config.emoji} {config.name}", expand=False)
                )
            except AgentError as e:
                console.print(f"[red bold]Error:[/red bold] {e}")
                for line in remediation_lines(e):
                    console.print(f"[dim]{line}[/dim]")
    finally:
        stop_rag_watchers()
        await aclose_checkpointers()
        await aclose_stores()


# ─────────────────────────────────────── serve ────────────────────────────────


@cli.command(name="serve")
@click.option("--agents", "-a", multiple=True, help="Specific agent names to load (default: all)")
@click.option(
    "--no-housekeeping", "no_housekeeping", is_flag=True, help="Skip startup housekeeping"
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Enable DEBUG-level logging (equivalent to LIONCLAW_LOG_LEVEL=DEBUG).",
)
@click.option(
    "--log-level",
    "log_level",
    default=None,
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], case_sensitive=False),
    help="Explicit log level (overrides --verbose and LIONCLAW_LOG_LEVEL).",
)
def serve(
    agents: tuple[str, ...],
    no_housekeeping: bool,
    verbose: bool,
    log_level: str | None,
) -> None:
    """Start the full runtime (all agents, channels, scheduler, self-learning)."""
    import os as _os

    from olano.runtime.server import RuntimeServer

    if log_level:
        _os.environ["LIONCLAW_LOG_LEVEL"] = log_level.upper()
    elif verbose:
        _os.environ["LIONCLAW_LOG_LEVEL"] = "DEBUG"
    _setup_logging()

    if _os.environ.get("LIONCLAW_TRACEMALLOC") == "1":
        # Start allocation tracing before agents boot so `olano mem` can
        # attribute allocations to source lines. Opt-in — it has overhead.
        import tracemalloc

        tracemalloc.start()

    if no_housekeeping:
        _os.environ["LIONCLAW_NO_HOUSEKEEPING"] = "1"
    agent_list = _expand_agent_names(agents) if agents else None
    active_level = _os.environ.get("LIONCLAW_LOG_LEVEL", "INFO").upper()
    console.print(
        f"[bold green]Starting Olano Runtime...[/bold green] [dim](log level: {active_level})[/dim]"
    )
    try:
        asyncio.run(RuntimeServer(agent_names=agent_list).start())
    except KeyboardInterrupt:
        # Graceful path is the in-loop SIGINT handler in RuntimeServer.
        # If KeyboardInterrupt still bubbles up here (e.g. a Ctrl+C lands
        # before signal handlers are installed), exit quietly with the
        # conventional 130 status instead of dumping a traceback.
        console.print("\n[dim]Interrupted.[/dim]")
        raise SystemExit(130)


# ─────────────────────────────────────── connect / setup ─────────────────────


@cli.group(
    name="connect",
    invoke_without_command=True,
    epilog=(
        "Examples:\n"
        "  olano connect whatsapp                  # pair WhatsApp via QR\n"
        "  olano connect whatsapp --exit-after-pair\n"
        "  olano connect whatsapp --port 3001 --session /tmp/wa\n"
        "  olano connect weixin                    # bind personal WeChat via QR\n"
    ),
)
@click.pass_context
def connect_group(ctx: click.Context) -> None:
    """Run a per-platform pairing/setup flow without stopping `olano serve`."""
    if ctx.invoked_subcommand is None:
        from olano.cli.connect_cmd import supported_platforms

        click.echo("Usage: olano connect <platform> [OPTIONS]")
        click.echo("")
        click.echo("Supported platforms:")
        for plat in supported_platforms():
            click.echo(f"  • {plat}")
        click.echo("")
        click.echo("Run `olano connect <platform> --help` for per-platform options.")


@connect_group.command(name="whatsapp")
@click.option(
    "--agent",
    "-a",
    default=None,
    help=(
        "Agent name. Resolves bridge_port + session_path the same way the "
        "running adapter does, so pairing credentials land where the live "
        "runtime reads them. Without this flag, the legacy global "
        "$LIONCLAW_HOME/platforms/whatsapp/session path is used (which the "
        "per-agent runtime will NOT pick up)."
    ),
)
@click.option("--port", default=3000, type=int, show_default=True, help="HTTP port for the bridge.")
@click.option(
    "--session",
    default=None,
    help="Auth/session directory (default: $LIONCLAW_HOME/platforms/whatsapp/session).",
)
@click.option(
    "--bridge-script",
    default=None,
    help="Override the Node bridge entry point (default: bundled whatsapp-bridge/index.js).",
)
@click.option(
    "--exit-after-pair",
    is_flag=True,
    help="Exit immediately once paired; otherwise stay running until Ctrl+C.",
)
@click.option(
    "--timeout",
    default=180,
    type=int,
    show_default=True,
    help="Seconds to wait in the QR/connecting phases before giving up.",
)
def connect_whatsapp_cmd(
    agent: str | None,
    port: int,
    session: str | None,
    bridge_script: str | None,
    exit_after_pair: bool,
    timeout: int,
) -> None:
    """Pair WhatsApp by scanning a QR code (no need to stop `olano serve`)."""
    from olano.cli.connect_cmd import connect_whatsapp

    rc = asyncio.run(
        connect_whatsapp(
            agent=agent,
            port=port,
            session=session,
            bridge_script=bridge_script,
            exit_after_pair=exit_after_pair,
            timeout=timeout,
        )
    )
    sys.exit(rc)


@connect_group.command(name="weixin")
@click.option(
    "--timeout",
    default=480,
    type=int,
    show_default=True,
    help="Seconds to wait for the QR scan before giving up.",
)
@click.option(
    "--bot-type",
    default="3",
    show_default=True,
    help="iLink bot type passed to the QR endpoint (leave at the default unless told otherwise).",
)
def connect_weixin_cmd(timeout: int, bot_type: str) -> None:
    """Bind a personal WeChat account by scanning an iLink QR code.

    No developer console or pre-issued key is involved: Tencent returns the
    account ID + bot token once the scan is confirmed, and they are saved
    under $LIONCLAW_HOME/weixin/accounts/ where the runtime adapter (and the
    dashboard connector, with its credential fields left blank) picks them up.
    """
    from olano.cli.connect_cmd import connect_weixin

    try:
        rc = asyncio.run(connect_weixin(timeout=timeout, bot_type=bot_type))
    except KeyboardInterrupt:
        click.echo("\nCancelled.")
        rc = 130
    sys.exit(rc)


@connect_group.command(name="link")
@click.argument("what")
@click.argument("extra", required=False)
@click.option("--agent", "-a", default="", help="Scope the connection to one agent.")
@click.option("--no-pin", is_flag=True, help="Mint without a PIN (not recommended).")
def connect_link_cmd(what: str, extra: str | None, agent: str, no_pin: bool) -> None:
    """Mint a magic link to hand to someone who can't reach this server.

    The link opens a page on this deployment's public address that drives the
    real sign-in (device code, OAuth hand-off, or a secure form straight into
    the vault). Use it when the person who owns the account is on a phone and
    the server has no browser.

    \b
      olano connect link github
      olano connect link google --agent merli
      olano connect link secret STRIPE_API_KEY
      olano connect link edit config/settings.yaml --agent merli
      olano connect link provider anthropic
      olano connect link https://mcp.example.com/mcp --agent merli
    """
    from olano.config.system_settings import load_magic_link_settings
    from olano.magic_links import service

    target = (what or "").strip()
    lowered = target.lower()
    if lowered in ("edit", "file"):
        if not extra:
            raise click.UsageError("Give the file path, e.g. `edit config/settings.yaml`.")
        flow, params = "edit-file", {"path": extra, "agent": agent}
    elif lowered in ("secret", "key"):
        if not extra:
            raise click.UsageError("Give the credential name, e.g. `secret STRIPE_API_KEY`.")
        flow, params = "secret-entry", {"name": extra, "agent": agent}
    elif lowered in ("provider", "model"):
        if not extra:
            raise click.UsageError("Give the provider, e.g. `provider anthropic`.")
        flow, params = "provider-key", {"provider": extra, "agent": agent}
    elif lowered.startswith(("http://", "https://")):
        mcp_agent = agent or (extra or "")
        if not mcp_agent:
            raise click.UsageError("An MCP server is authorized per agent — pass --agent.")
        flow, params = "mcp-oauth", {"url": target, "agent": mcp_agent}
    else:
        from olano.magic_links.routing import resolve_connect_flow

        try:
            flow, params = resolve_connect_flow(target, agent=agent)
        except ValueError as exc:
            raise click.UsageError(str(exc)) from exc

    settings = load_magic_link_settings()
    if no_pin:
        settings = settings.model_copy(update={"pin": "off"})
    try:
        minted = service.mint_link(
            flow,
            params=params,
            requested_by={"channel": "cli", "username": "operator"},
            notify={},  # nothing to notify: the operator is watching this terminal
            minted_by="cli",
            settings=settings,
        )
    except service.MagicLinkError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo("")
    click.echo(f"  {minted.label}")
    click.echo(f"  {minted.url}")
    if minted.pin:
        click.echo(f"  PIN: {minted.pin}")
    click.echo(f"  Expires in {minted.ttl_minutes} minutes. Single use.")
    click.echo("")
    # No watcher is spawned here — this process exits. It isn't needed: the page
    # is served by the running daemon, and its poll is what drives the flow to
    # completion and stores the token.
    click.echo("Send both the link and the PIN — the page asks for the PIN first.")


# ─────────────────────────────────────── mcp group ────────────────────────────


def _resolve_mcp_agent(agent_name: str | None) -> str:
    """Resolve the target agent for an ``mcp`` subcommand and verify it exists.

    Falls back to the configured default agent when ``--agent`` is omitted, so a
    bare ``olano mcp add <url>`` scopes to the default agent while
    ``--agent <name>`` targets a specific one.
    """
    from olano.config.loader import load_agent_config

    name = agent_name or _default_agent_name()
    try:
        load_agent_config(name, source="home")
    except FileNotFoundError as exc:
        raise click.ClickException(
            f"Agent '{name}' not found. List agents with `olano agents list`."
        ) from exc
    return name


def _render_mcp_servers(agent_name: str, servers: list[dict[str, Any]]) -> None:
    from olano.core.mcp_setup import oauth_token_state

    table = Table(title=f"MCP servers — {agent_name}", show_header=True)
    table.add_column("URL / name", style="bold", overflow="fold")
    table.add_column("Transport")
    table.add_column("Auth")
    table.add_column("Enabled")
    table.add_column("Disabled tools")
    for srv in servers:
        auth = (srv.get("auth") or "none").lower()
        url = srv.get("url", "")
        if auth == "oauth":
            state = oauth_token_state(agent_name, url)["state"]
            if state == "expired":
                auth_cell = "oauth [red]✗ token expired — re-authorize[/red]"
            elif state == "none":
                auth_cell = "oauth [yellow]needs auth[/yellow]"
            else:
                auth_cell = "oauth [green]✓ authorized[/green]"
        else:
            auth_cell = auth
        disabled = ", ".join(srv.get("disabled_tools") or []) or "—"
        enabled_cell = "yes" if srv.get("enabled", True) else "[dim]no[/dim]"
        table.add_row(url, srv.get("transport", ""), auth_cell, enabled_cell, disabled)
    console.print(table)


def _render_mcp_directory(entries: list[dict[str, Any]], source: str, title: str) -> None:
    """Render cloud-directory entries (presets + examples) as a table."""
    table = Table(title=title, show_header=True)
    table.add_column("Slug", style="bold", overflow="fold")
    table.add_column("Name")
    table.add_column("Kind")
    table.add_column("Transport")
    table.add_column("Auth")
    table.add_column("Category / endpoint", overflow="fold")
    for e in entries:
        if e.get("command"):
            endpoint = " ".join([e.get("command", ""), *(e.get("args") or [])]).strip()
        else:
            endpoint = e.get("url") or ""
        table.add_row(
            e.get("slug", ""),
            e.get("name", ""),
            e.get("kind") or "preset",
            e.get("transport") or "—",
            e.get("auth") or "none",
            e.get("category") or endpoint or "—",
        )
    console.print(table)
    plural = "entry" if len(entries) == 1 else "entries"
    console.print(f"[dim]{len(entries)} {plural} · source: {source}[/dim]")


def _render_mcp_probe(result: dict[str, Any]) -> None:
    status = result.get("status", "unknown")
    message = result.get("message", "")
    colour = {"connected": "green", "auth_required": "yellow"}.get(status, "red")
    console.print(f"[{colour}]{status}[/{colour}]: {message}")
    tools = result.get("tools") or []
    if tools:
        table = Table(title=f"Tools ({len(tools)})", show_header=True)
        table.add_column("Name", style="bold")
        table.add_column("Enabled")
        table.add_column("Description", overflow="fold")
        for tool in tools:
            table.add_row(
                tool.get("name", ""),
                "yes" if tool.get("enabled", True) else "[dim]no[/dim]",
                (tool.get("description") or "")[:80],
            )
        console.print(table)
    if status == "auth_required":
        url = result.get("url") or ""
        console.print(
            f"[dim]Run[/dim] [bold]olano mcp authorize {url}[/bold] [dim]to sign in.[/dim]"
        )


def _render_mcp_oauth_result(result: dict[str, Any]) -> None:
    status = result.get("status", "unknown")
    if status == "authorized":
        console.print(f"[green]✓ Authorized[/green] — {result.get('message', '')}")
    else:
        error = result.get("error") or result.get("message") or "authorization did not complete"
        console.print(f"[red]✗ {status}[/red]: {error}")
        if result.get("error_code") == "dcr_unsupported":
            console.print(
                "[dim]This server needs a pre-registered OAuth app. Re-add it with "
                "--oauth-client-id / --oauth-client-secret, then authorize again.[/dim]"
            )


@cli.group(
    name="mcp",
    epilog=(
        "Examples:\n"
        "  olano mcp browse                  # the shared Olano directory\n"
        "  olano mcp search stripe\n"
        "  olano mcp install notion -a merli --authorize\n"
        "  olano mcp list --agent merli\n"
        "  olano mcp add https://mcp.example.com/sse --agent merli --transport sse\n"
        "  olano mcp add https://mcp.notion.com/mcp -a merli --auth oauth --authorize\n"
        "  olano mcp add fs -a merli --transport stdio --command npx "
        "--arg -y --arg @modelcontextprotocol/server-filesystem\n"
        "  olano mcp authorize https://mcp.example.com/mcp --agent merli\n"
        "  olano mcp test https://mcp.example.com/sse --agent merli\n"
        "  olano mcp remove https://mcp.example.com/sse --agent merli"
    ),
)
def mcp() -> None:
    """Browse the Olano MCP directory and manage an agent's MCP servers.

    ``browse``/``search``/``install`` work against the **shared cloud directory**
    (the catalog of add-able servers, maintained centrally on the cloud side; falls back to
    a bundled snapshot offline). ``list``/``add``/``remove``/``authorize``/``test``
    are scoped to a single agent (``--agent``, defaulting to the configured default
    agent) — each agent owns its own server list in its agent.json5.
    """


@mcp.command(name="list")
@click.option(
    "--agent", "-a", "agent_name", default=None, help="Agent name (default: default agent)"
)
def mcp_list(agent_name: str | None) -> None:
    """List the MCP servers configured on an agent."""
    from olano import admin

    name = _resolve_mcp_agent(agent_name)
    servers = admin.list_mcp_servers(name)
    if not servers:
        console.print(f"[dim]No MCP servers configured on[/dim] [bold]{name}[/bold].")
        return
    _render_mcp_servers(name, servers)


@mcp.command(name="add")
@click.argument("url")
@click.option(
    "--agent", "-a", "agent_name", default=None, help="Agent name (default: default agent)"
)
@click.option(
    "--transport",
    default="sse",
    show_default=True,
    help="sse | http | stdio | websocket | auto",
)
@click.option(
    "--auth",
    type=click.Choice(["none", "oauth"]),
    default="none",
    show_default=True,
    help="Authentication mode for remote transports.",
)
@click.option("--oauth-scope", default=None, help="Optional OAuth scope to request.")
@click.option(
    "--oauth-client-id",
    default=None,
    help="Pre-registered OAuth client id (servers without Dynamic Client Registration).",
)
@click.option(
    "--oauth-client-secret",
    default=None,
    help="Pre-registered OAuth client secret (used with --oauth-client-id).",
)
@click.option("--command", default=None, help="Executable to launch for stdio transport.")
@click.option("--arg", "args", multiple=True, help="Argument for the stdio command (repeatable).")
@click.option("--env", "env_pairs", multiple=True, help="KEY=VALUE env var for stdio (repeatable).")
@click.option(
    "--header",
    "header_pairs",
    multiple=True,
    help="KEY=VALUE request header for http/sse (repeatable).",
)
@click.option(
    "--disable-tool",
    "disabled_tools",
    multiple=True,
    help="Disable a specific tool from this server (repeatable).",
)
@click.option("--disabled", is_flag=True, help="Add the server but leave it disabled.")
@click.option(
    "--authorize",
    is_flag=True,
    help="Run the interactive OAuth flow immediately after adding (implies --auth oauth).",
)
@click.option("--test", "do_test", is_flag=True, help="Probe the server after adding.")
@click.option("--no-browser", is_flag=True, help="Do not open a browser during OAuth.")
def mcp_add(
    url: str,
    agent_name: str | None,
    transport: str,
    auth: str,
    oauth_scope: str | None,
    oauth_client_id: str | None,
    oauth_client_secret: str | None,
    command: str | None,
    args: tuple[str, ...],
    env_pairs: tuple[str, ...],
    header_pairs: tuple[str, ...],
    disabled_tools: tuple[str, ...],
    disabled: bool,
    authorize: bool,
    do_test: bool,
    no_browser: bool,
) -> None:
    """Add (or replace, by URL) an MCP server on an agent.

    For stdio transport, URL is a local label/name and --command is required.
    For http/sse, URL is the server endpoint.
    """
    from olano import admin
    from olano.cli.mcp_cmd import parse_kv_pairs

    name = _resolve_mcp_agent(agent_name)

    if authorize:
        auth = "oauth"
    if transport == "stdio" and not command:
        raise click.ClickException("stdio transport requires --command.")

    env = parse_kv_pairs(env_pairs, label="--env")
    headers = parse_kv_pairs(header_pairs, label="--header")

    result = admin.add_mcp_server(
        name,
        url,
        transport=transport,
        auth=auth,
        oauth_scope=oauth_scope,
        oauth_client_id=oauth_client_id,
        oauth_client_secret=oauth_client_secret,
        command=command,
        args=list(args),
        env=env,
        headers=headers,
        disabled_tools=list(disabled_tools),
        enabled=not disabled,
    )
    verb = "Updated" if result.get("replaced") else "Added"
    console.print(
        f"[green]✓ {verb}[/green] MCP server [bold]{url}[/bold] "
        f"({transport}, auth={auth}) on [bold]{name}[/bold]."
    )

    if authorize:
        if transport == "stdio":
            raise click.ClickException("OAuth is only supported for http/sse transports.")
        from olano.cli.mcp_cmd import authorize_server

        oauth_result = authorize_server(name, url, open_browser=not no_browser)
        _render_mcp_oauth_result(oauth_result)
    elif do_test:
        result = asyncio.run(admin.test_mcp_server(name, url))
        _render_mcp_probe(result)


@mcp.command(name="remove")
@click.argument("url")
@click.option(
    "--agent", "-a", "agent_name", default=None, help="Agent name (default: default agent)"
)
@click.option("--keep-tokens", is_flag=True, help="Keep any stored OAuth tokens for this server.")
def mcp_remove(url: str, agent_name: str | None, keep_tokens: bool) -> None:
    """Remove an MCP server from an agent (and its stored OAuth tokens)."""
    from olano import admin
    from olano.core.mcp_setup import clear_oauth_tokens

    name = _resolve_mcp_agent(agent_name)
    result = admin.remove_mcp_server(name, url)
    if not result.get("removed"):
        raise click.ClickException(f"Agent '{name}' has no MCP server with url '{url}'.")
    cleared = False
    if not keep_tokens:
        cleared = clear_oauth_tokens(name, url)
    suffix = " [dim](cleared stored OAuth tokens)[/dim]" if cleared else ""
    console.print(
        f"[green]✓ Removed[/green] MCP server [bold]{url}[/bold] from [bold]{name}[/bold].{suffix}"
    )


@mcp.command(name="test")
@click.argument("url")
@click.option(
    "--agent", "-a", "agent_name", default=None, help="Agent name (default: default agent)"
)
def mcp_test(url: str, agent_name: str | None) -> None:
    """Probe a configured MCP server (connect, list tools, detect OAuth)."""
    from olano import admin

    # Best-effort vault unlock (OLANO_VAULT_KEY / keyring, never a prompt):
    # the probe resolves ${vault:NAME} header refs like the runtime does, and
    # with no vault they go verbatim — a 401 here for a server that works live.
    try:
        from olano.vault.runtime import unlock_vault_for_session

        unlock_vault_for_session(allow_prompt=False)
    except Exception:  # a locked vault degrades the probe, never blocks it
        pass

    name = _resolve_mcp_agent(agent_name)
    result = asyncio.run(admin.test_mcp_server(name, url))
    _render_mcp_probe(result)


@mcp.command(name="health")
@click.option(
    "--agent", "-a", "agent_name", default=None, help="Agent name (default: default agent)"
)
@click.option(
    "--refresh/--no-refresh",
    default=True,
    show_default=True,
    help="Renew OAuth access tokens that are close to expiry.",
)
def mcp_health(agent_name: str | None, refresh: bool) -> None:
    """Show (and maintain) an agent's MCP connection + token health.

    Reports each configured server's stored-token state and what the runtime's
    last build made of it, and — unless ``--no-refresh`` — renews any access
    token close to expiry right now. Renewal talks to the OAuth token endpoint
    directly, so it works even while the MCP server itself is unreachable.
    """
    from olano.config.loader import load_agent_config
    from olano.core import mcp_health as health
    from olano.core.builder import get_mcp_summary

    name = _resolve_mcp_agent(agent_name)
    config = load_agent_config(name, source="home")
    if not config.mcp_servers:
        console.print(f"[dim]No MCP servers configured on[/dim] [bold]{name}[/bold].")
        return

    entries = [srv.model_dump() for srv in config.mcp_servers]
    # A CLI run is a separate process from the runtime, so there is no live
    # build summary to read — the connection columns come from this process's
    # (empty) view and the token columns from disk, which is the part a CLI can
    # actually maintain. `recover=False` for the same reason: only the runtime
    # can reconnect a running agent.
    report = asyncio.run(
        health.check_agent(
            name, entries, get_mcp_summary(name), refresh_tokens=refresh, recover=False
        )
    )

    table = Table(title=f"MCP health — {name}", show_header=True)
    table.add_column("URL", style="bold", overflow="fold")
    table.add_column("Transport")
    table.add_column("Token")
    table.add_column("Expires in")
    table.add_column("This check", overflow="fold")
    now = time.time()
    for server in report.servers:
        if not server.enabled:
            token_cell, expiry_cell = "[dim]—[/dim]", "[dim]—[/dim]"
        else:
            token_cell = {
                "live": "[green]live[/green]",
                "refreshable": "[yellow]refreshable[/yellow]",
                "expired": "[red]expired[/red]",
                "none": "[dim]none[/dim]",
            }.get(server.token_state, server.token_state)
            if server.token_expires_at is None:
                expiry_cell = "[dim]unknown[/dim]"
            else:
                left = server.token_expires_at - now
                expiry_cell = f"{left / 60:.0f} min" if left > 0 else "[red]expired[/red]"
        action = {
            "refreshed": "[green]renewed[/green]",
            "refresh_failed": "[red]renewal failed[/red]",
            "needs_authorization": "[yellow]needs authorization[/yellow]",
            "none": "[dim]nothing to do[/dim]",
        }.get(server.action, server.action)
        detail = f" [dim]{server.detail}[/dim]" if server.detail else ""
        table.add_row(
            server.url,
            server.transport,
            token_cell,
            expiry_cell,
            f"{action}{detail}" if server.enabled else "[dim]disabled[/dim]",
        )
    console.print(table)
    console.print(
        "[dim]The runtime maintains these automatically "
        "(config.yaml → mcp_health); GET /api/diagnostics/mcp shows its last sweep.[/dim]"
    )


@mcp.command(name="authorize")
@click.argument("url")
@click.option(
    "--agent", "-a", "agent_name", default=None, help="Agent name (default: default agent)"
)
@click.option("--no-browser", is_flag=True, help="Do not open a browser automatically.")
def mcp_authorize(url: str, agent_name: str | None, no_browser: bool) -> None:
    """Run the interactive OAuth flow for a configured MCP server.

    Hosts a localhost redirect, prints the authorization URL to open, captures
    the callback, and stores the tokens under <LIONCLAW_HOME>/mcp_oauth/.
    """
    from olano.cli.mcp_cmd import authorize_server

    name = _resolve_mcp_agent(agent_name)
    result = authorize_server(name, url, open_browser=not no_browser)
    _render_mcp_oauth_result(result)


@mcp.command(name="search")
@click.argument("query")
@click.option(
    "--kind", type=click.Choice(["preset", "example"]), default=None, help="Filter by kind."
)
@click.option("--category", default=None, help="Filter by category (presets).")
@click.option("--limit", default=50, show_default=True, help="Max results.")
def mcp_search(query: str, kind: str | None, category: str | None, limit: int) -> None:
    """Search the Olano MCP directory — the shared catalog of add-able servers.

    Backed by the cloud registry (falls back to a bundled snapshot offline).
    Install a result on an agent with ``olano mcp install <slug>``.
    """
    from olano.core import mcp_registry

    results = mcp_registry.search(query, kind=kind, category=category, limit=limit)
    if not results:
        console.print(f"[dim]No MCP servers match[/dim] [bold]{query}[/bold].")
        return
    source = mcp_registry.get_directory().get("source", "?")
    _render_mcp_directory(results, source, f"MCP directory — search: {query}")
    console.print("[dim]Add one with[/dim] olano mcp install <slug> -a <agent>")


@mcp.command(name="browse")
@click.option(
    "--kind", type=click.Choice(["preset", "example"]), default=None, help="Filter by kind."
)
@click.option("--category", default=None, help="Filter by category (presets).")
@click.option("--refresh", is_flag=True, help="Bypass the local cache and refetch from the cloud.")
def mcp_browse(kind: str | None, category: str | None, refresh: bool) -> None:
    """Browse the full Olano MCP directory (presets + example configs)."""
    from olano.core import mcp_registry

    directory = mcp_registry.get_directory(force=refresh)
    entries = list(directory["presets"]) + list(directory["examples"])
    if kind:
        entries = [e for e in entries if e.get("kind") == kind]
    if category:
        entries = [e for e in entries if (e.get("category") or "") == category]
    if not entries:
        console.print("[dim]No directory entries.[/dim]")
        return
    _render_mcp_directory(entries, directory.get("source", "?"), "MCP directory")
    console.print("[dim]Add one with[/dim] olano mcp install <slug> -a <agent>")


@mcp.command(name="install")
@click.argument("slug")
@click.option(
    "--agent", "-a", "agent_name", default=None, help="Agent name (default: default agent)"
)
@click.option(
    "--authorize", is_flag=True, help="Run OAuth immediately after adding (implies --auth oauth)."
)
@click.option("--no-browser", is_flag=True, help="Do not open a browser during OAuth.")
@click.option("--test", "do_test", is_flag=True, help="Probe the server after adding.")
def mcp_install(
    slug: str, agent_name: str | None, authorize: bool, no_browser: bool, do_test: bool
) -> None:
    """Add a server from the directory by slug (find slugs with ``mcp browse``).

    Pulls the endpoint/transport/auth (or stdio command/args) from the directory
    entry, so you don't have to retype them. For providers that need a
    pre-registered OAuth app (e.g. HubSpot), supply credentials via ``mcp add``.
    """
    from olano import admin
    from olano.core import mcp_registry

    entry = mcp_registry.get_server(slug)
    if not entry:
        raise click.ClickException(
            f"No directory entry with slug '{slug}'. Try: olano mcp search {slug}"
        )

    name = _resolve_mcp_agent(agent_name)
    transport = entry.get("transport") or "auto"
    auth = "oauth" if authorize else (entry.get("auth") or "none")
    url = entry.get("url") or slug
    command = entry.get("command")
    args = list(entry.get("args") or [])
    if transport == "stdio" and not command:
        raise click.ClickException(f"'{slug}' is a stdio server but the directory has no command.")

    result = admin.add_mcp_server(
        name,
        url,
        transport=transport,
        auth=auth,
        command=command,
        args=args,
        enabled=True,
    )
    verb = "Updated" if result.get("replaced") else "Added"
    console.print(
        f"[green]✓ {verb}[/green] [bold]{entry.get('name') or slug}[/bold] "
        f"[dim]({url}, {transport})[/dim] on [bold]{name}[/bold]."
    )

    if entry.get("oauthClient") == "preregistered":
        console.print(
            "[yellow]This provider needs a pre-registered OAuth app.[/yellow] "
            "Set --oauth-client-id/--oauth-client-secret via `olano mcp add`, "
            "or enter them in the dashboard."
        )

    if authorize and transport != "stdio":
        from olano.cli.mcp_cmd import authorize_server

        _render_mcp_oauth_result(authorize_server(name, url, open_browser=not no_browser))
    elif do_test:
        probe = asyncio.run(admin.test_mcp_server(name, url))
        _render_mcp_probe(probe)


@mcp.group(name="token")
def mcp_token() -> None:
    """Bearer tokens for the **inbound** MCP server (agents as tools for the outside).

    The endpoint is switched on home-wide in ``config.yaml`` (``mcp_server:
    external_access: {mcp: {enabled: true}}``) and each agent opts in with
    ``external_access.mcp: true``; a token
    minted here is what an external MCP client presents as ``Authorization:
    Bearer …``. The raw token is printed once and stored only as a hash.
    """


@mcp_token.command(name="create")
@click.option("--label", "-l", required=True, help="Which integration this token is for.")
@click.option(
    "--agent",
    "-a",
    "agents",
    multiple=True,
    required=True,
    help="Agent the token may address (repeatable); '*' for every exposed agent.",
)
@click.option(
    "--ttl-days",
    type=int,
    default=None,
    help="Lifetime in days (default: config.yaml external_access.token_ttl_days, 90; 0 = never).",
)
def mcp_token_create(label: str, agents: tuple[str, ...], ttl_days: int | None) -> None:
    """Mint a token and print it (once)."""
    from olano.config.system_settings import load_external_access_settings
    from olano.mcp_server.tokens import get_token_store

    settings = load_external_access_settings()
    ttl = settings.token_ttl_days if ttl_days is None else ttl_days
    try:
        raw, record = get_token_store().mint(
            label=label, agents=list(agents), ttl_days=ttl, created_by="cli"
        )
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from exc
    console.print(f"[green]✓[/green] token [bold]{record.id}[/bold] for {', '.join(record.agents)}")
    console.print(f"  expires: {record.expires_at or 'never'}")
    console.print("  Copy it now — it is not stored and cannot be shown again:\n")
    console.print(f"    {raw}\n")
    if not settings.mcp.enabled and not settings.a2a.enabled:
        console.print(
            "[yellow]![/yellow] Both inbound surfaces are off: turn on Config → External access "
            "(config.yaml → external_access.mcp / .a2a) before this token can be used."
        )


@mcp_token.command(name="list")
def mcp_token_list() -> None:
    """List minted tokens (never the secrets)."""
    from olano.mcp_server.tokens import get_token_store

    tokens = get_token_store().list()
    if not tokens:
        console.print("[dim]No MCP tokens minted yet.[/dim]")
        return
    table = Table(title="MCP bearer tokens")
    for col in ("id", "label", "agents", "state", "expires", "last used"):
        table.add_column(col)
    for t in tokens:
        state = "revoked" if t.revoked else ("expired" if t.expired else "active")
        table.add_row(
            t.id,
            t.label,
            ", ".join(t.agents),
            state,
            (t.expires_at or "never")[:19],
            (t.last_used_at or "-")[:19],
        )
    console.print(table)


@mcp_token.command(name="revoke")
@click.argument("token_id")
def mcp_token_revoke(token_id: str) -> None:
    """Revoke a token by id (takes effect on its next use)."""
    from olano.mcp_server.tokens import get_token_store

    if get_token_store().revoke(token_id):
        console.print(f"[green]✓[/green] revoked {token_id}")
    else:
        console.print(f"[red]✗[/red] no active token with id {token_id}")
        sys.exit(1)


@mcp.command(name="serve-status")
def mcp_serve_status() -> None:
    """Show the external-access configuration: switches, endpoints, exposed agents."""
    import os

    from olano.config.loader import list_agents, load_agent_config
    from olano.config.system_settings import load_external_access_settings
    from olano.mcp_server.server import MCP_PATH, METADATA_ALIAS_PATH, OAUTH_ISSUER_PATH
    from olano.mcp_server.tokens import get_token_store

    settings = load_external_access_settings()
    origin = (os.environ.get("LIONCLAW_PUBLIC_URL") or "").strip().rstrip("/")
    mcp_agents, a2a_agents = [], []
    for name in list_agents():
        try:
            ext = load_agent_config(name).external_access
        except Exception:
            continue
        if ext.mcp:
            mcp_agents.append(name)
        if ext.a2a:
            a2a_agents.append(name)
    tokens = get_token_store().list()
    unknown = "<public URL unknown — set LIONCLAW_PUBLIC_URL>"
    oauth = origin + OAUTH_ISSUER_PATH if origin and settings.oauth.enabled else "off"
    console.print(
        f"mcp enabled:    {'yes' if settings.mcp.enabled else 'no'}   (Config → External access)"
    )
    console.print(f"a2a enabled:    {'yes' if settings.a2a.enabled else 'no'}")
    console.print(f"mcp endpoint:   {origin + MCP_PATH if origin else unknown}")
    console.print(f"mcp metadata:   {origin + METADATA_ALIAS_PATH if origin else '-'}")
    console.print(f"a2a endpoints:  {origin + '/api/a2a/agents/<agent>' if origin else unknown}")
    console.print(f"oauth issuer:   {oauth}")
    console.print(f"require_https:  {settings.require_https}")
    console.print(f"mcp agents:     {', '.join(mcp_agents) or '(none)'}")
    console.print(f"a2a agents:     {', '.join(a2a_agents) or '(none)'}")
    console.print(
        f"tokens:         {sum(1 for t in tokens if t.active)} active / {len(tokens)} total"
    )
    if settings.jwt.configured:
        console.print(f"jwt issuer:     {settings.jwt.issuer}")


# ─────────────────────────────────────── server group ─────────────────────────


@cli.group(
    name="server",
    epilog=(
        "Examples:\n"
        "  olano server start -a merli       # daemonised runtime\n"
        "  olano server status               # is it running, what's loaded?\n"
        "  olano server logs -f              # tail the daemon log\n"
        "  olano server stop                 # graceful SIGTERM (escalates to SIGKILL)\n"
        "  olano server restart -a merli     # stop + start, preserves flags"
    ),
)
def server() -> None:
    """Manage the olano runtime in daemon mode (background process)."""


def _server_print(*args: Any, **kwargs: Any) -> None:
    """``console.print`` that survives stdout dying mid-command.

    ``server stop`` / ``server restart`` can be executed from *inside* the
    runtime (an agent shell command); once the old daemon exits, stdout is a
    pipe with no reader and every print raises — which would abort a restart
    between its stop and start phases."""
    try:
        console.print(*args, **kwargs)
    except OSError:
        pass


def _expand_agent_names(agents: tuple[str, ...]) -> list[str]:
    """Accept both `-a foo -a bar` and `-a foo,bar` styles. Trim and dedupe."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in agents:
        for piece in raw.split(","):
            name = piece.strip()
            if name and name not in seen:
                seen.add(name)
                out.append(name)
    return out


def _serve_passthrough_args(
    agents: tuple[str, ...],
    no_housekeeping: bool,
    verbose: bool,
    log_level: str | None,
) -> list[str]:
    """Build the argv list to forward to ``olano serve`` from a server cmd."""
    forwarded: list[str] = []
    for name in _expand_agent_names(agents):
        forwarded.extend(["-a", name])
    if no_housekeeping:
        forwarded.append("--no-housekeeping")
    if verbose:
        forwarded.append("--verbose")
    if log_level:
        forwarded.extend(["--log-level", log_level])
    return forwarded


@server.command(name="start")
@click.option(
    "--agents",
    "-a",
    multiple=True,
    help="Specific agent names to load (default: all). Repeat the flag for multiple agents.",
)
@click.option(
    "--no-housekeeping", "no_housekeeping", is_flag=True, help="Skip startup housekeeping"
)
@click.option("--verbose", "-v", is_flag=True, help="DEBUG-level logging in the daemon")
@click.option(
    "--log-level",
    "log_level",
    default=None,
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], case_sensitive=False),
    help="Explicit log level (overrides --verbose).",
)
def server_start(
    agents: tuple[str, ...],
    no_housekeeping: bool,
    verbose: bool,
    log_level: str | None,
) -> None:
    """Start the runtime detached, write a pidfile, and return immediately."""
    from olano.runtime.daemon import start_daemon

    forwarded = _serve_passthrough_args(agents, no_housekeeping, verbose, log_level)
    try:
        result = start_daemon(forwarded)
    except RuntimeError as exc:
        console.print(f"[bold red]✗[/bold red] {exc}")
        raise SystemExit(1) from exc

    if result.already_running:
        console.print(
            f"[yellow]Already running[/yellow] (pid {result.pid}). "
            f"Use [bold]olano server status[/bold] for details."
        )
        return

    console.print(
        f"[bold green]✓ Olano started[/bold green] "
        f"(pid [bold]{result.pid}[/bold])\n"
        f"  log:  {result.log_path}\n"
        f"  pid:  {result.pid_path}\n"
        f"  hint: [dim]olano server logs -f[/dim] to tail, "
        f"[dim]olano server status[/dim] to inspect."
    )

    # Verify the runtime API is reachable.  Without this check the daemon
    # can silently boot port-less (e.g. when the API port is still in
    # TIME_WAIT after the previous SIGKILL).  In that broken state, the
    # daemon keeps running but the dashboard, `talk_to`, and every other
    # consumer of the HTTP API fail mysteriously.  Detecting it now lets
    # us print an actionable hint instead of leaving the user to debug.
    import os as _os

    from olano.config.home import load_home_config as _load_home_config
    from olano.runtime.daemon import (
        _http_get_json as _probe,
    )
    from olano.runtime.daemon import (
        is_pid_alive as _is_alive,
    )

    api_port = int(
        _os.environ.get("LIONCLAW_RUNTIME_PORT", str(_load_home_config().runtime_api_port))
    )
    api_up = False
    for _ in range(40):
        if not _is_alive(result.pid):
            break
        if _probe(api_port, "/api/status") is not None:
            api_up = True
            break
        time.sleep(0.5)

    if not api_up:
        console.print(
            f"[bold yellow]⚠ Runtime API on port {api_port} is not responding yet.[/bold yellow] "
            f"[dim]The daemon (pid {result.pid}) is alive but its HTTP API failed to bind. "
            f"Common cause: port still in TIME_WAIT after a SIGKILL restart.[/dim]\n"
            f"  [dim]tail the log:[/dim]  [bold]olano server logs -f[/bold]\n"
            f"  [dim]or restart:[/dim]    [bold]olano server stop && olano server start[/bold]"
        )


@server.command(name="stop")
@click.option(
    "--timeout",
    default=10.0,
    type=float,
    help="Seconds to wait for graceful exit before escalating to SIGKILL (default: 10).",
)
@click.option("--force", "-f", is_flag=True, help="Skip SIGTERM and send SIGKILL immediately.")
def server_stop(timeout: float, force: bool) -> None:
    """Stop the running daemon (SIGTERM with SIGKILL escalation)."""
    from olano.runtime.daemon import stop_daemon

    result = stop_daemon(timeout=timeout, force=force)
    if result.stopped:
        if result.pid is None:
            _server_print(f"[bold green]✓ Stopped[/bold green]. [dim]{result.detail}.[/dim]")
            return
        _server_print(
            f"[bold green]✓ Stopped[/bold green] (pid {result.pid}). [dim]{result.detail}.[/dim]"
        )
        return
    if result.pid is None:
        _server_print("[dim]Not running (no pid file).[/dim]")
        return
    if "not running" in result.detail:
        _server_print(f"[dim]{result.detail.capitalize()}.[/dim]")
        return
    _server_print(f"[bold red]✗[/bold red] {result.detail}")
    raise SystemExit(1)


@server.command(name="status")
@click.option("--json", "as_json", is_flag=True, help="Emit a JSON snapshot for scripts.")
def server_status(as_json: bool) -> None:
    """Show whether the daemon is running and what it has loaded."""
    import json as _json

    from olano.runtime.daemon import status as _status

    snap = _status()

    if as_json:
        payload = {
            "running": snap.running,
            "pid": snap.pid,
            "api_port": snap.api_port,
            "api_reachable": snap.api_reachable,
            "status": snap.status_payload,
            "agents": snap.agents_payload,
            "pid_path": str(snap.pid_path),
            "log_path": str(snap.log_path),
            "detail": snap.detail,
        }
        click.echo(_json.dumps(payload, indent=2))
        return

    if not snap.running:
        console.print(f"[yellow]Not running.[/yellow] [dim]{snap.detail}[/dim]")
        console.print(f"  pid:  {snap.pid_path}")
        console.print(f"  log:  {snap.log_path}")
        return

    api_state = "[green]up[/green]" if snap.api_reachable else "[yellow]warming up[/yellow]"
    console.print(f"[bold green]Running[/bold green] (pid [bold]{snap.pid}[/bold])")
    console.print(f"  api:  http://127.0.0.1:{snap.api_port} ({api_state})")
    console.print(f"  pid:  {snap.pid_path}")
    console.print(f"  log:  {snap.log_path}")

    # Channel + scheduler counts from /api/status (cheap summary line).
    if snap.status_payload:
        channels = snap.status_payload.get("channels") or []
        jobs = snap.status_payload.get("scheduler_jobs", 0)
        if channels or jobs:
            channel_str = f"{len(channels)} ({', '.join(channels)})" if channels else "0"
            console.print(f"  channels: {channel_str}    scheduler jobs: {jobs}")

    # The big one — every loaded agent with model + channels.
    agents = snap.agents_payload or []
    if agents:
        console.print(f"\n  agents ({len(agents)}):")
        for agent in agents:
            name = agent.get("name", "?")
            emoji = agent.get("emoji", "") or ""
            model = agent.get("model", "")
            description = (agent.get("description") or "").strip()
            ch_list = agent.get("channels") or []
            channel_names: list[str] = []
            for ch in ch_list:
                if isinstance(ch, dict):
                    channel_names.append(str(ch.get("type") or ch.get("name") or "?"))
                else:
                    channel_names.append(str(ch))
            channel_str = ", ".join(channel_names) if channel_names else "—"

            header = f"    {emoji + ' ' if emoji else ''}[bold]{name}[/bold]"
            console.print(header)
            console.print(f"        model:    [dim]{model}[/dim]")
            console.print(f"        channels: [dim]{channel_str}[/dim]")
            if description:
                # One-line description, trimmed for terminal width.
                short = description.splitlines()[0][:120]
                console.print(f"        about:    [dim]{short}[/dim]")
    else:
        console.print("\n  [dim]No agents loaded yet (still booting?).[/dim]")

    if snap.detail:
        console.print(f"\n[dim]{snap.detail}[/dim]")


@server.command(name="logs")
@click.option("-f", "follow", is_flag=True, help="Follow the log file (like tail -f).")
@click.option("-n", "lines", default=200, type=int, help="Number of recent lines to show first.")
def server_logs(follow: bool, lines: int) -> None:
    """Print the daemon log file (optionally follow new output)."""
    import time as _time

    from olano.runtime.daemon import _log_path

    path = _log_path()
    if not path.exists():
        console.print(f"[yellow]No log file yet at[/yellow] {path}")
        return

    # Print the last ``lines`` lines first.
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-lines:]
        for line in tail:
            click.echo(line.rstrip("\n"))
    except OSError as exc:
        console.print(f"[bold red]✗[/bold red] Could not read {path}: {exc}")
        raise SystemExit(1) from exc

    if not follow:
        return

    # Simple tail -f loop.
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)  # end of file
            while True:
                chunk = f.readline()
                if chunk:
                    click.echo(chunk.rstrip("\n"))
                else:
                    _time.sleep(0.25)
    except KeyboardInterrupt:
        console.print("[dim]— stopped tailing —[/dim]")


@server.command(name="restart")
@click.option(
    "--agents",
    "-a",
    multiple=True,
    help="Specific agent names to load when restarting (default: all).",
)
@click.option("--no-housekeeping", "no_housekeeping", is_flag=True)
@click.option("--verbose", "-v", is_flag=True)
@click.option(
    "--log-level",
    "log_level",
    default=None,
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], case_sensitive=False),
)
@click.option("--timeout", default=10.0, type=float, help="Stop timeout (default: 10s).")
@click.pass_context
def server_restart(
    ctx: click.Context,
    agents: tuple[str, ...],
    no_housekeeping: bool,
    verbose: bool,
    log_level: str | None,
    timeout: float,
) -> None:
    """Stop the running daemon (if any) then start it back up with the given flags."""
    from olano.runtime.daemon import (
        RESTART_DETACHED_ENV,
        _log_path,
        restart_blast_radius_pid,
        spawn_detached_restart,
        stop_daemon,
    )

    if os.environ.get(RESTART_DETACHED_ENV) == "1":
        # We are the detached helper: give the parent's "detaching…" reply a
        # beat to flush through the runtime we are about to stop, then
        # restart inline (our new session is outside the blast radius).
        time.sleep(2.0)
    else:
        runtime_pid = restart_blast_radius_pid()
        if runtime_pid is not None:
            # Stopping the runtime would kill this very command (it runs
            # inside the runtime's process tree/group — e.g. an agent shell
            # command). Historically that meant the stop half fired, the
            # process died with SIGTERM (exit code -15), and the start half
            # never ran — leaving the server down. Hand the whole restart to
            # a detached helper that survives the stop instead.
            forwarded = _serve_passthrough_args(agents, no_housekeeping, verbose, log_level)
            helper_pid = spawn_detached_restart([*forwarded, "--timeout", str(timeout)])
            _server_print(
                f"[yellow]⚠ Restart requested from inside the running runtime[/yellow] "
                f"[dim](runtime pid {runtime_pid})[/dim].\n"
                f"Detached a helper (pid {helper_pid}) so the restart survives the stop — "
                f"the runtime will go down and come back within ~10s.\n"
                f"  check: [bold]olano server status[/bold]\n"
                f"  log:   {_log_path()}"
            )
            return

    stop_result = stop_daemon(timeout=timeout, force=False)
    if stop_result.pid is not None and stop_result.stopped:
        _server_print(
            f"[dim]Stopped previous daemon (pid {stop_result.pid}, {stop_result.detail}).[/dim]"
        )
    elif stop_result.pid is not None and not stop_result.stopped:
        _server_print(
            f"[bold red]✗[/bold red] Could not stop existing daemon: {stop_result.detail}"
        )
        raise SystemExit(1)

    ctx.invoke(
        server_start,
        agents=agents,
        no_housekeeping=no_housekeeping,
        verbose=verbose,
        log_level=log_level,
    )


# ─────────────────────────────────────── agents group ─────────────────────────


@cli.group(
    epilog=(
        "Examples:\n"
        "  olano agents list\n"
        "  olano agents create --from-template researcher --name scout\n"
        "  olano agents install lionDev --as coder"
    ),
)
def agents() -> None:
    """Manage agents."""


@cli.group(
    epilog=(
        "Examples:\n"
        "  olano share list --agent merli\n"
        "  olano share add ~/Documents/specs --agent merli --mode ro --as specs\n"
        "  olano share remove 123abc --agent merli"
    ),
)
def share() -> None:
    """Manage shared path grants."""


@share.command(name="list")
@click.option("--agent", "agent_name", required=True, help="Agent name")
@click.option("--thread", default=None, help="Optional thread ID to include session shares")
def share_list(agent_name: str, thread: str | None) -> None:
    """List effective shared paths for an agent."""
    from olano.config.agent_config import SharedPathConfig
    from olano.config.loader import load_agent_config
    from olano.core.runner import AgentRunner
    from olano.security.sharing import resolve_shared_mounts

    cfg = load_agent_config(agent_name)
    session_grants: list[SharedPathConfig] = []
    if thread:
        runner = AgentRunner(agent=object(), config=cfg)
        state = runner.load_thread_state(thread)
        session_grants = [
            SharedPathConfig.model_validate(item) for item in state.get("session_shared_paths", [])
        ]
    mounts = resolve_shared_mounts(cfg, session_grants)
    table = Table(title=f"Shared Paths — {cfg.name}", show_header=True)
    table.add_column("ID", style="bold")
    table.add_column("Mode")
    table.add_column("Persistence")
    table.add_column("Target")
    table.add_column("Source")
    for mount in mounts:
        table.add_row(mount.id, mount.mode, mount.persistence, mount.target, str(mount.source))
    console.print(table)


@share.command(name="add")
@click.argument("path")
@click.option("--agent", "agent_name", required=True, help="Agent name")
@click.option("--mode", type=click.Choice(["ro", "rw"]), default="ro", show_default=True)
@click.option("--as", "mount_name", default=None, help="Sandbox mount name under /shared/")
@click.option("--session", "session_only", is_flag=True, help="Store as a thread-scoped grant")
@click.option("--thread", default=None, help="Thread ID for session-scoped grants")
@click.option("--subagent", default=None, help="Limit grant to one subagent")
@click.option("--all-subagents", is_flag=True, help="Grant to all subagents")
def share_add(
    path: str,
    agent_name: str,
    mode: str,
    mount_name: str | None,
    session_only: bool,
    thread: str | None,
    subagent: str | None,
    all_subagents: bool,
) -> None:
    """Add a persistent or session-scoped shared path grant."""
    from olano.config.agent_config import SharedPathConfig, ShareSubjectConfig
    from olano.config.loader import find_agent_config_path, load_agent_config, save_agent_config
    from olano.core.runner import AgentRunner
    from olano.security.sharing import (
        canonicalize_share_source,
        normalize_share_target,
        sanitize_mount_name,
        validate_share_source_path,
    )

    cfg = load_agent_config(agent_name)
    source = canonicalize_share_source(path)
    path_error = validate_share_source_path(source)
    if path_error:
        raise click.ClickException(path_error)

    if all_subagents:
        subjects = [ShareSubjectConfig(type="all_subagents", name="*")]
    elif subagent:
        subjects = [ShareSubjectConfig(type="subagent", name=subagent)]
    else:
        subjects = [ShareSubjectConfig(type="agent", name="self")]

    mount_hint = sanitize_mount_name(mount_name) if mount_name else None
    grant = SharedPathConfig(
        source=str(source),
        target=normalize_share_target(mount_hint, source),
        mode=cast('Literal["ro", "rw"]', mode),
        persistence="session" if session_only else "persistent",
        subjects=subjects,
    )

    if session_only:
        if not thread:
            raise click.ClickException("--thread is required for --session grants.")
        runner = AgentRunner(agent=object(), config=cfg)
        state = runner.load_thread_state(thread)
        grants = [
            SharedPathConfig.model_validate(item)
            for item in state.get("session_shared_paths", [])
            if item.get("id") != grant.id and item.get("target") != grant.target
        ]
        grants.append(grant)
        runner.update_thread_state(thread, session_shared_paths=[g.model_dump() for g in grants])
        console.print(
            f"[green]✓[/green] Shared {source} as {grant.target} for thread [bold]{thread}[/bold]."
        )
        return

    cfg.shared_paths = [
        g for g in cfg.shared_paths if g.id != grant.id and g.target != grant.target
    ]
    cfg.shared_paths.append(grant)
    existing_path = find_agent_config_path(cfg.name)
    base = existing_path.parent.parent if existing_path else None
    save_agent_config(cfg, base=base)
    console.print(
        f"[green]✓[/green] Shared {source} as {grant.target} for agent [bold]{cfg.name}[/bold]."
    )


@share.command(name="remove")
@click.argument("share_id")
@click.option("--agent", "agent_name", required=True, help="Agent name")
@click.option("--thread", default=None, help="Optional thread ID for session-scoped grants")
def share_remove(share_id: str, agent_name: str, thread: str | None) -> None:
    """Remove a persistent or thread-scoped shared path grant."""
    from olano.config.agent_config import SharedPathConfig
    from olano.config.loader import find_agent_config_path, load_agent_config, save_agent_config
    from olano.core.runner import AgentRunner

    cfg = load_agent_config(agent_name)
    removed = False

    if thread:
        runner = AgentRunner(agent=object(), config=cfg)
        state = runner.load_thread_state(thread)
        grants = [
            SharedPathConfig.model_validate(item) for item in state.get("session_shared_paths", [])
        ]
        filtered = [grant for grant in grants if grant.id != share_id]
        if len(filtered) != len(grants):
            runner.update_thread_state(
                thread, session_shared_paths=[g.model_dump() for g in filtered]
            )
            removed = True

    filtered_persistent = [grant for grant in cfg.shared_paths if grant.id != share_id]
    if len(filtered_persistent) != len(cfg.shared_paths):
        cfg.shared_paths = filtered_persistent
        existing_path = find_agent_config_path(cfg.name)
        base = existing_path.parent.parent if existing_path else None
        save_agent_config(cfg, base=base)
        removed = True

    if not removed:
        raise click.ClickException(f"No shared path found for id '{share_id}'.")
    console.print(f"[green]✓[/green] Removed shared path [bold]{share_id}[/bold].")


@agents.command(
    name="list",
    epilog=("Examples:\n  olano agents list\n  olano agents list --archetypes"),
)
@click.option(
    "--archetypes", is_flag=True, help="List installable archetypes instead of installed agents"
)
def agents_list(archetypes: bool) -> None:
    """List installed agents, or show available archetypes with --archetypes."""
    if archetypes:
        _render_archetype_list()
        return

    from olano.config.loader import list_agents

    # Operator debug listing: shows hidden agents too, tagged "(hidden)" below.
    names = list_agents(include_hidden=True)
    if not names:
        console.print(
            "[yellow]No agents found.[/yellow] Create one with: olano agents create --name <name>"
        )
        return

    table = Table(title="Available Agents", show_header=True)
    table.add_column("Name", style="bold")
    table.add_column("Model")
    table.add_column("Channels")
    table.add_column("Subagents")
    table.add_column("Schedule")

    from olano.config.loader import load_agent_config

    for name in names:
        try:
            cfg = load_agent_config(name)
            # Hidden/internal agents (e.g. provider test agents) are withheld
            # from the dashboard and customers, but the management CLI still
            # lists them — tagged so it's clear they're internal-only.
            label = f"{cfg.emoji} {cfg.name}"
            if cfg.hidden:
                label += " [dim](hidden)[/dim]"
            table.add_row(
                label,
                cfg.model,
                ", ".join(str(c) for c in cfg.channels) or "-",
                str(len(cfg.subagents)),
                str(len(cfg.schedule)),
            )
        except Exception:
            table.add_row(name, "[red]error[/red]", "-", "-", "-")

    console.print(table)


@agents.command(
    name="refresh",
    epilog=(
        "Examples:\n"
        "  olano agents refresh                 # re-sync every agent's system/ zone\n"
        "  olano agents refresh --check         # dry-run: show what would change\n"
        "  olano agents refresh --agent merli   # only this agent"
    ),
)
@click.option("--agent", default=None, help="Refresh only this agent (default: all)")
@click.option(
    "--check", "check_mode", is_flag=True, help="Dry-run: show what would change without writing"
)
def agents_refresh(agent: str | None, check_mode: bool) -> None:
    """Re-sync agents' system/ zone (prompts, knowledge, schemas) to the installed release.

    User-owned files (workspace root, extensions/) are never touched.
    """
    from olano.update.refresh import refresh_agent_system_zone, refresh_all_agents

    if agent:
        results = [refresh_agent_system_zone(agent, check=check_mode)]
    else:
        results = refresh_all_agents(source="home", check=check_mode)
    _render_agent_refresh_results(results, check=check_mode)


@agents.command(
    name="create",
    epilog=(
        "Examples:\n"
        "  olano agents create --name myagent --model anthropic:claude-sonnet-4-6\n"
        "  olano agents create --from-template developer --name coder\n"
        "  olano agents create --from-template minimal --yes"
    ),
)
@click.option("--name", default=None, help="Agent name (slug, no spaces)")
@click.option("--model", default=None, help="Model (provider:model)")
@click.option("--description", default=None, help="Short description (optional)")
@click.option("--emoji", default=None, help="Agent emoji")
@click.option(
    "--from-template",
    "template_name",
    default=None,
    help="Create from built-in template: personal|developer|researcher|minimal",
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts")
def agents_create(
    name: str | None,
    model: str | None,
    description: str | None,
    emoji: str | None,
    template_name: str | None,
    yes: bool,
) -> None:
    """Create and scaffold a new agent, optionally from a built-in template."""
    if template_name:
        from olano.archetypes.installer import install_agent_template, list_agent_templates

        if template_name not in list_agent_templates():
            console.print(
                f"[red]Unknown template:[/red] {template_name}. "
                f"Available: {', '.join(list_agent_templates())}"
            )
            sys.exit(1)

        target_name = (name or template_name).strip().lower().replace(" ", "_")
        try:
            config_path = install_agent_template(
                template_name,
                agent_name=target_name,
                overwrite=yes,
                model=model,
                description=description,
                emoji=emoji,
            )
        except FileExistsError as e:
            console.print(f"[yellow]{e}[/yellow]")
            return
        except Exception as e:
            from olano.deployment.enforcement import DeploymentLimitExceeded

            if isinstance(e, DeploymentLimitExceeded):
                console.print(f"[red]{e.decision.message}[/red]")
                console.print(f"[dim]{e.decision.remediation}[/dim]")
                sys.exit(1)
            raise

        workspace_root = config_path.parent / "workspace"
        console.print(
            f"[green]✓[/green]  Agent [bold]{target_name}[/bold] created from template [bold]{template_name}[/bold]."
        )
        console.print(f"  Config:    [dim]{config_path}[/dim]")
        console.print(f"  Workspace: [dim]{workspace_root}[/dim]")
        console.print("  Chat with it in the dashboard: [bold]olano dashboard[/bold]")
        return

    console.print()
    console.print("[bold #5f87ff]🦁  Create a new Olano agent[/bold #5f87ff]")
    console.print("[dim]Press Enter to accept defaults. Ctrl+C to cancel.[/dim]")
    console.print()

    # ── Gather info interactively if not provided via flags ──────────────────
    if not name:
        name = click.prompt("  Agent name (slug, e.g. myagent)", default="myagent")
    name = name.strip().lower().replace(" ", "_")

    agent_dir = get_olano_paths().agents_dir / name
    config_path = agent_dir / "agent.json5"
    if config_path.exists() and not yes:
        overwrite = click.confirm(f"  Agent '{name}' already exists. Overwrite?", default=False)
        if not overwrite:
            console.print("[yellow]Cancelled.[/yellow]")
            return

    if not description:
        description = click.prompt(
            "  Description (optional, press Enter to skip)",
            default="",
            show_default=False,
        )

    if not emoji:
        emoji = click.prompt("  Emoji", default="🤖")

    if not model:
        preferred_model = load_home_config().preferred_model or "anthropic:claude-sonnet-4-6"
        model = click.prompt(
            "  Model",
            default=preferred_model,
        )

    role_prompt = click.prompt(
        "  System prompt (brief role description, or press Enter to use default)",
        default="",
        show_default=False,
    )
    if not role_prompt:
        role_prompt = (f"You are {name}, a helpful AI assistant. {description or ''}").strip()

    console.print()

    # ── Build agent.json5 ─────────────────────────────────────────────────────
    agent_dir.mkdir(parents=True, exist_ok=True)

    workspace_root = agent_dir / "workspace"
    config = {
        "name": name,
        "description": description or f"{name} agent",
        "emoji": emoji,
        "model": model,
        "system_prompt": role_prompt,
        "workspace": str(workspace_root),
        "tools": ["safe_shell", "node_run"],
        "subagents": [],
        "skills": [],
        "mcp_servers": [],
        "channels": [],
        "schedule": [],
        "memory": {"markdown": True, "rag": False, "self_learning": True},
        "security": {"profile": "developer", "trust_level": 3},
        "acp_enabled": False,
    }

    from olano.archetypes.installer import _guarded_agent_config_write
    from olano.deployment.enforcement import DeploymentLimitExceeded

    try:
        _guarded_agent_config_write(config_path, config)
    except DeploymentLimitExceeded as exc:
        console.print(f"[red]{exc.decision.message}[/red]")
        console.print(f"[dim]{exc.decision.remediation}[/dim]")
        sys.exit(1)

    # ── Scaffold workspace ────────────────────────────────────────────────────
    from olano.archetypes.installer import scaffold_agent_workspace

    template_data = {
        "name": name,
        "emoji": emoji,
        "description": description or f"{name} agent",
        "system_prompt": role_prompt,
        "tools": config["tools"],
        "subagents": [],
    }
    scaffold_agent_workspace(name, template_data, workspace_root, overwrite=yes)

    (agent_dir / "skills").mkdir(exist_ok=True)

    # ── Summary ───────────────────────────────────────────────────────────────
    console.print(f"[green]✓[/green]  Agent [bold]{name}[/bold] created.")
    console.print()
    console.print(f"  Config:    [dim]{config_path}[/dim]")
    console.print(f"  Workspace: [dim]{workspace_root}[/dim]")
    console.print(f"  Skills:    [dim]{agent_dir / 'skills'}[/dim]")
    console.print()
    console.print(
        f"  When you first chat, [bold]{name}[/bold] will introduce itself and learn about you."
    )
    console.print()
    console.print("  Chat with it in the dashboard: [bold]olano dashboard[/bold]")
    console.print()


@agents.command(
    name="install",
    epilog=(
        "Examples:\n"
        "  olano agents install merli\n"
        "  olano agents install lionDev --as coder\n"
        "  olano agents install lionMarketing --overwrite"
    ),
)
@click.argument("archetype_name")
@click.option("--as", "as_name", default=None, help="Name for the installed agent")
@click.option("--model", default=None, help="Override model (provider:model)")
@click.option("--overwrite", is_flag=True, help="Overwrite existing agent config")
def agents_install(
    archetype_name: str,
    as_name: str | None,
    model: str | None,
    overwrite: bool,
) -> None:
    """Install an archetype as a new agent with full workspace scaffold."""
    preferred_model = model or load_home_config().preferred_model
    _install_archetype_and_report(archetype_name, as_name, overwrite, model=preferred_model)


@agents.command(name="info")
@click.argument("name")
def agents_info(name: str) -> None:
    """Show detailed info about an agent."""
    from olano.config.loader import load_agent_config

    try:
        cfg = load_agent_config(name)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)

    import yaml

    console.print(
        Panel(
            yaml.dump(cfg.model_dump(), sort_keys=False),
            title=f"{cfg.emoji} {cfg.name}",
        )
    )


# ──────────────────────────────────── archetypes ──────────────────────────────


@cli.group()
def archetypes() -> None:
    """Manage the archetype catalog (built-in + external packs)."""


@archetypes.command(name="list")
def archetypes_list() -> None:
    """List every archetype Olano can install (across all configured dirs)."""
    from olano.archetypes.installer import _archetype_search_dirs, list_archetypes

    names = list_archetypes()
    console.print(f"[bold]{len(names)}[/bold] archetypes available:")
    for name in names:
        console.print(f"  • {name}")
    console.print()
    console.print("[dim]Search paths (priority order):[/dim]")
    for d in _archetype_search_dirs():
        marker = "✓" if d.exists() else "·"
        console.print(f"  {marker} {d}")


@archetypes.command(name="update")
@click.option(
    "--url",
    default=None,
    help="Archetype repo URL (overrides LIONCLAW_AGENTS_REPO_URL env var)",
)
@click.option(
    "--no-pull",
    is_flag=True,
    help="Skip the update step if the pack is already installed",
)
def archetypes_update(url: str | None, no_pull: bool) -> None:
    """Refresh the canonical archetype pack (cloud catalog; git with --url)."""
    from olano.archetypes.sync import fetch_lionclaw_agents, lionclaw_agents_pack_dir

    ok, msg = fetch_lionclaw_agents(url=url, update_if_exists=not no_pull)
    color = "green" if ok else "yellow"
    console.print(f"[{color}]{msg}[/{color}]")
    if ok:
        console.print(f"[dim]Pack location: {lionclaw_agents_pack_dir()}[/dim]")


@archetypes.command(name="install")
@click.argument("name")
@click.option("--as-name", default=None, help="Install under a different agent name")
@click.option("--model", default=None, help="Override the archetype's default model")
@click.option("--overwrite", is_flag=True, help="Overwrite an existing agent of the same name")
def archetypes_install(
    name: str,
    as_name: str | None,
    model: str | None,
    overwrite: bool,
) -> None:
    """Install an archetype as a new agent. Wraps `agents install`."""
    preferred_model = model or load_home_config().preferred_model
    _install_archetype_and_report(name, as_name, overwrite, model=preferred_model)


# ──────────────────────────────────── skills ──────────────────────────────────


@cli.group()
def skills() -> None:
    """Manage the common skills in <home>/skills/ (package baseline + external catalog)."""


@skills.command(name="sync")
def skills_sync() -> None:
    """Copy/refresh the bundled skills into <home>/skills/ (idempotent)."""
    from olano.config.home import get_olano_paths
    from olano.skills.built_in_sync import sync_builtin_skills

    paths = get_olano_paths()
    report = sync_builtin_skills(paths.root)
    console.print(f"[green]{report}[/green]")
    console.print(f"[dim]Skills location: {paths.managed_skills_dir}[/dim]")


@skills.command(name="update")
@click.option(
    "--url",
    default=None,
    help="Skills repo URL (overrides LIONCLAW_SKILLS_REPO_URL env var)",
)
@click.option(
    "--no-pull",
    is_flag=True,
    help="Skip the git update step if the pack is already cloned",
)
def skills_update(url: str | None, no_pull: bool) -> None:
    """Refresh the canonical skills pack (olano-ai/skills) into <home>/skills/.

    On a managed box the pack comes from the cloud skills catalog (no GitHub
    credential needed); elsewhere, or with --url, it is a git clone / pull.
    Copies each skill from the pack into the common skills directory, on top of
    the package-bundled skills that `skills sync` installs.
    """
    from olano.config.home import get_olano_paths
    from olano.skills.sync import fetch_olano_skills, olano_skills_pack_dir

    paths = get_olano_paths()
    ok, msg = fetch_olano_skills(paths.root, url=url, update_if_exists=not no_pull)
    color = "green" if ok else "yellow"
    console.print(f"[{color}]{msg}[/{color}]")
    if ok:
        console.print(f"[dim]Pack cache: {olano_skills_pack_dir(paths.root)}[/dim]")
        console.print(f"[dim]Skills location: {paths.managed_skills_dir}[/dim]")


@skills.command(name="fix-names")
@click.option("--agent", default=None, help="Also scan every skill dir this agent loads from")
@click.option("--apply", is_flag=True, help="Perform the repairs (default: dry run)")
def skills_fix_names(agent: str | None, apply: bool) -> None:
    """Make every skill's name agree with the folder holding its SKILL.md.

    The Agent Skills spec requires a skill's `name` to match the folder holding
    its SKILL.md. Mismatched skills still load, but every agent boot logs a
    warning and a future deepagents release may skip them outright.

    A legal name keeps its name and the folder is renamed to match, because the
    name is what the system prompt and delegate_task refer to. An illegal name
    (spaces, capitals) is set to its folder's name instead. Dry run by default;
    pass --apply to perform it.
    """
    from olano.admin.skills import repair_skill_names

    report = repair_skill_names(agent, apply=apply)
    planned = report.get("repaired" if apply else "would_repair") or []
    skipped = report.get("skipped") or []

    if not planned and not skipped:
        console.print("[green]Every skill's name matches its folder.[/green]")
        return

    for entry in planned:
        verb = "" if apply else "would "
        if entry.get("rename_to"):
            console.print(f"[green]{verb}rename[/green] {entry['path']} → {entry['rename_to']}")
        if entry.get("new_name"):
            console.print(
                f"[green]{verb}set name[/green] {entry['name']!r} → {entry['new_name']!r}"
            )
    for entry in skipped:
        console.print(f"[yellow]skipped[/yellow] {entry['path']} — {entry['skipped']}")

    if planned and not apply:
        console.print(f"\n[dim]Dry run. Re-run with --apply to repair {len(planned)}.[/dim]")
    if apply and planned:
        console.print(
            f"\n[dim]{len(planned)} repaired. Restart the affected agents to pick them up "
            "(a running agent does not hot-reload skills).[/dim]"
        )


# ──────────────────────────────────── subagents ───────────────────────────────


@cli.group()
def subagents() -> None:
    """Discover and install ToolkitAgents — one specialist per integration."""


@subagents.command(name="list")
@click.option(
    "--category", default=None, help="Filter by category (e.g. Markets, Finance, Productivity)"
)
@click.option(
    "--all", "show_all", is_flag=True, help="Include catalog-hidden infrastructure agents"
)
def subagents_list(category: str | None, show_all: bool) -> None:
    """List available ToolkitAgents."""
    from olano.subagents.registry import list_toolkit_agents

    defs = list_toolkit_agents(catalog_only=not show_all)
    if category:
        defs = [d for d in defs if d.category.lower() == category.lower()]

    table = Table(title=f"ToolkitAgents ({len(defs)})", show_header=True)
    table.add_column("Name", style="bold")
    table.add_column("Category")
    table.add_column("Toolkit")
    table.add_column("Credentials")
    table.add_column("Description")
    for d in defs:
        creds = ", ".join(d.credential_env_vars) if d.credential_env_vars else "—"
        table.add_row(d.name, d.category, d.toolkit, creds, d.description[:60])
    console.print(table)


@subagents.command(name="info")
@click.argument("name")
def subagents_info(name: str) -> None:
    """Show prompt, tools, security profile, and credentials for a ToolkitAgent."""
    from olano.subagents.registry import (
        _expand_tools,
        effective_security,
        load_toolkit_agent,
    )

    try:
        defn = load_toolkit_agent(name)
    except KeyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from exc

    tools = _expand_tools(defn, None)
    sec = effective_security(defn)

    info = {
        "name": defn.name,
        "toolkit": defn.toolkit,
        "category": defn.category,
        "description": defn.description,
        "credential_env_vars": defn.credential_env_vars,
        "tools": tools,
        "security": {
            "profile": sec.profile,
            "hitl_enabled": sec.hitl_enabled,
            "hitl_write_tools": sec.hitl_write_tools,
            "pii_scan_input": sec.pii_scan_input,
            "pii_scan_output": sec.pii_scan_output,
        },
        "system_prompt": defn.system_prompt,
    }
    import yaml

    console.print(Panel(yaml.dump(info, sort_keys=False), title=f"ToolkitAgent: {defn.name}"))


@subagents.command(name="search")
@click.argument("query")
def subagents_search(query: str) -> None:
    """Search ToolkitAgents by name, description, toolkit, or category."""
    from olano.subagents.registry import list_toolkit_agents

    q = query.lower()
    results = [
        d
        for d in list_toolkit_agents(catalog_only=False)
        if q in d.name.lower()
        or q in d.description.lower()
        or q in d.toolkit.lower()
        or q in d.category.lower()
        or any(q in t.lower() for t in d.tags)
    ]
    if not results:
        console.print(f"[yellow]No ToolkitAgents matched '{query}'.[/yellow]")
        return

    table = Table(title=f"Search: {query}", show_header=True)
    table.add_column("Name", style="bold")
    table.add_column("Category")
    table.add_column("Description")
    for d in results:
        table.add_row(d.name, d.category, d.description[:80])
    console.print(table)


@subagents.command(name="install")
@click.argument("name")
@click.option("--name", "install_name", default=None, help="Install under a custom agent name")
@click.option(
    "--enable-tool",
    "enable_tools",
    multiple=True,
    help="Add a tool beyond the toolkit defaults (repeatable)",
)
@click.option(
    "--disable-tool",
    "disable_tools",
    multiple=True,
    help="Remove a tool from the toolkit defaults (repeatable)",
)
@click.option("--model", default=None, help="Override the model for this agent")
@click.option("--overwrite", is_flag=True, help="Replace an existing agent of the same name")
def subagents_install(
    name: str,
    install_name: str | None,
    enable_tools: tuple[str, ...],
    disable_tools: tuple[str, ...],
    model: str | None,
    overwrite: bool,
) -> None:
    """Install a ToolkitAgent as a top-level agent.

    Examples:
      olano subagents install StripeAgent
      olano subagents install AlpacaAgent --name AlpacaPaper --disable-tool alpaca_place_order
    """
    from olano.subagents.installer import install_toolkit_agent_as_top_level

    try:
        path = install_toolkit_agent_as_top_level(
            name,
            install_name=install_name,
            extra_tools=list(enable_tools),
            disabled_tools=list(disable_tools),
            model=model,
            overwrite=overwrite,
        )
    except KeyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from exc
    except FileExistsError as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        raise SystemExit(1) from exc

    console.print(f"[green]Installed[/green] {install_name or name} at {path}")


@agents.command(name="subagent")
@click.argument("action", type=click.Choice(["enable", "disable"]))
@click.argument("agent_name")
@click.argument("subagent_name")
def agents_subagent(action: str, agent_name: str, subagent_name: str) -> None:
    """Enable or disable a subagent for an agent (P6.11).

    Examples:
      olano agents subagent enable merli WalletAgent
      olano agents subagent disable merli TikTokAgent
    """
    from olano.config.loader import find_agent_config_path, load_agent_config

    try:
        cfg = load_agent_config(agent_name)
    except FileNotFoundError:
        console.print(f"[red]Agent '{agent_name}' not found.[/red]")
        raise SystemExit(1)

    enabled = action == "enable"
    found = False
    for sub in cfg.subagents:
        if sub.name.lower() == subagent_name.lower():
            sub.enabled = enabled
            found = True
            break

    if not found:
        console.print(f"[yellow]Subagent '{subagent_name}' not in agent config — adding.[/yellow]")
        from olano.subagents.registry import resolve_as_subagent

        try:
            sub_cfg = resolve_as_subagent(subagent_name)
        except KeyError:
            console.print(f"[red]Unknown subagent '{subagent_name}'.[/red]")
            raise SystemExit(1)
        sub_cfg.enabled = enabled
        cfg.subagents.append(sub_cfg)

    # Persist to agent config file
    config_path = find_agent_config_path(agent_name)
    if config_path and config_path.exists():
        import yaml

        with config_path.open() as f:
            raw = yaml.safe_load(f) or {}
        raw_subagents = raw.setdefault("subagents", [])
        for rs in raw_subagents:
            if rs.get("name", "").lower() == subagent_name.lower():
                rs["enabled"] = enabled
                break
        else:
            raw_subagents.append({"name": subagent_name, "enabled": enabled})
        with config_path.open("w") as f:
            yaml.dump(raw, f, sort_keys=False)
        console.print(
            f"[green]{'Enabled' if enabled else 'Disabled'} subagent '{subagent_name}' for agent '{agent_name}'.[/green]"
        )
    else:
        console.print("[yellow]Config file not found — change not persisted.[/yellow]")


# ─────────────────────────────────────── models ───────────────────────────────


@cli.group()
def models() -> None:
    """Browse and manage model providers."""


@models.command(name="list")
@click.option("--provider", "-p", default=None, help="Filter by provider key")
@click.option("--installed", is_flag=True, help="Show only installed providers")
def models_list(provider: str | None, installed: bool) -> None:
    """List all supported model providers and their models."""
    from olano.models import PROVIDERS, get_provider_models, is_provider_available

    if provider:
        info = PROVIDERS.get(provider)
        if not info:
            console.print(f"[red]Unknown provider: {provider}[/red]")
            console.print(f"Available: {', '.join(PROVIDERS)}")
            return
        available = is_provider_available(provider)
        models_list_data = get_provider_models(provider)
        console.print(
            Panel(
                f"[bold]{info.name}[/bold]\n"
                f"Package:  {info.package}\n"
                f"Env var:  {info.env_var or 'none required'}\n"
                f"Status:   {'[green]installed[/green]' if available else '[yellow]not installed[/yellow]'}\n"
                + (f"Install:  uv add {info.package}\n" if not available else "")
                + (f"Note:     {info.notes}\n" if info.notes else "")
                + "\nModels:\n"
                + "\n".join(f"  {provider}:{m}" for m in models_list_data),
                title=f"Provider: {provider}",
            )
        )
        return

    table = Table(title="Model Providers", show_header=True, show_lines=False)
    table.add_column("", width=2)
    table.add_column("Provider", style="bold")
    table.add_column("Name")
    table.add_column("Example model")
    table.add_column("Env var")

    for key, info in PROVIDERS.items():
        if installed and not is_provider_available(key):
            continue
        available = is_provider_available(key)
        status = "[green]✓[/green]" if available else "[dim]○[/dim]"
        if key == "ollama":
            from olano.models import list_ollama_models

            local = list_ollama_models()
            example = f"ollama:{local[0]}" if local else "ollama:llama3.2"
        else:
            example = f"{key}:{info.default_models[0]}" if info.default_models else ""
        table.add_row(status, key, info.name, example, info.env_var or "-")

    console.print(table)
    console.print("\n[dim]✓ installed   ○ needs: uv add <package>[/dim]")
    console.print("[dim]Filter installed: olano models list --installed[/dim]")
    console.print("[dim]Provider detail:  olano models list --provider ollama[/dim]")


@models.command(name="ollama")
def models_ollama() -> None:
    """List locally available Ollama models."""
    import shutil

    from olano.models import list_ollama_models

    if not shutil.which("ollama"):
        console.print("[yellow]Ollama is not installed.[/yellow]")
        console.print("Install from: https://ollama.com")
        return

    local = list_ollama_models()
    if not local:
        console.print("[yellow]No Ollama models pulled yet.[/yellow]")
        console.print("Pull a model with: [bold]ollama pull llama3.2[/bold]")
        return

    table = Table(title="Local Ollama Models", show_header=True)
    table.add_column("Model string (use in agent.json5)")
    table.add_column("Name")
    for m in local:
        table.add_row(f"ollama:{m}", m)

    console.print(table)
    console.print(
        "\n[dim]Pull more: ollama pull <name>  |  Browse: https://ollama.com/library[/dim]"
    )


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Emit the raw JSON report")
def version(as_json: bool) -> None:
    """Show what image/commit this runtime is actually running.

    Answers "did my deploy take?" on the box itself. A managed image bakes the
    commit at build time; a source install reports its package version. When
    neither is available this says so plainly rather than inventing a value —
    a placeholder here is what made a stale deploy indistinguishable from a
    fresh one in the console.
    """
    from olano.deployment.version import version_report

    report = version_report()
    if as_json:
        console.print_json(data=report)
        return

    if report.get("known"):
        console.print(f"Runtime version: [green]{report['runtime_version']}[/green]")
    else:
        console.print(
            "Runtime version: [yellow]not reported[/yellow] — this build carries no identity"
        )
    for label, key in (
        ("image ref", "image_ref"),
        ("build sha", "build_sha"),
        ("built at", "build_time"),
        ("package", "package_version"),
    ):
        if report.get(key):
            console.print(f"  {label}: {report[key]}")
    if not report.get("known"):
        console.print(
            "\n[dim]Managed images bake OLANO_BUILD_SHA at build time. Nothing is broken, "
            "but this box cannot tell cloud.olano.ai what it is running.[/dim]"
        )


@models.command(name="gateway")
@click.option("--json", "as_json", is_flag=True, help="Emit the raw JSON report")
@click.option(
    "--probe",
    is_flag=True,
    help="Also ask the gateway which managed model groups it actually serves",
)
def models_gateway(as_json: bool, probe: bool) -> None:
    """Show Olano model-gateway health: the key, and what the gateway serves.

    Olano-managed tiers (olano:auto|turbo|pro|reasoning) and the managed
    image/vision/speech tools all authenticate with a per-deployment gateway
    key, pulled from cloud.olano.ai over the deployment token. A box that is
    ATTACHED but not ENROLLED looks connected everywhere else while every
    managed model call 401s — this is the command that tells them apart.

    --probe answers the other half: holding a key does not mean the gateway
    ROUTES what you ask it for. A deployment entitled to managed vision whose
    gateway serves no `olano-vision` group fails every image_describe call with
    an invalid-model error, while every local surface still reports vision as
    available. --probe names exactly which expected groups are missing.
    """
    from olano.deployment.gateway_key import GATEWAY_KEY_ENV as GATEWAY_KEY_ENV_NAME
    from olano.deployment.gateway_key import enrollment_status

    status = enrollment_status()
    reading = None
    if probe:
        from olano.deployment import gateway_probe

        reading = gateway_probe.probe(force=True)

    if as_json:
        data = dict(status)
        if reading is not None:
            data["served"] = reading.to_dict()
        console.print_json(data=data)
        return

    state = str(status.get("state"))
    headline = {
        "enrolled": "[green]enrolled[/green] — Olano-managed models are billed in credits",
        "unenrolled": (
            "[yellow]attached, not enrolled[/yellow] — Olano-managed models will fail "
            "until a key arrives"
        ),
        "unattached": (
            "[dim]not attached[/dim] — this box has no deployment token, so it cannot enroll"
        ),
    }[state]
    console.print(f"Olano gateway: {headline}")
    console.print(f"  endpoint: {status.get('gateway_url')}")

    if status.get("env_key_revoked"):
        env_name = status.get("env_key_name") or GATEWAY_KEY_ENV_NAME
        console.print(
            f"  [yellow]note:[/yellow] {env_name} in /opt/olano/olano.env holds a "
            "key the control plane revoked."
        )
        console.print(
            "  [dim]It is being ignored in favour of the enrolled key, so nothing is broken — "
            "but clear or update that line when convenient. Before this was detected, that "
            "value shadowed every freshly enrolled key and every managed call 401'd.[/dim]"
        )

    if state != "enrolled":
        failures = status.get("consecutive_failures") or 0
        if failures:
            console.print(f"  consecutive failures: {failures}")
        if status.get("last_error"):
            console.print(f"  last error: [red]{status['last_error']}[/red]")
        if status.get("last_attempt_at"):
            console.print(f"  last attempt: {status['last_attempt_at']}")
        if status.get("retry_in_seconds") is not None:
            console.print(f"  next retry in: ~{status['retry_in_seconds']}s")
        if not status.get("background_loop"):
            console.print(
                "  [dim]note: the runtime server is not running, so nothing is "
                "retrying in the background[/dim]"
            )
    if status.get("last_success_at"):
        console.print(f"  last success: {status['last_success_at']}")
    builds = status.get("unenrolled_model_builds") or 0
    if builds:
        console.print(
            f"  [yellow]{builds} model(s) were built with an unenrolled placeholder key[/yellow] "
            "— their calls 401 until enrollment succeeds"
        )
    if state == "unenrolled":
        console.print(
            "\n[dim]Retrying automatically with exponential backoff (60s → 30 min). "
            "If it persists, check that cloud.olano.ai is reachable and the deployment "
            f"token is valid; a key set as {GATEWAY_KEY_ENV_NAME} in "
            "/opt/olano/olano.env takes precedence over enrollment.[/dim]"
        )

    if reading is not None:
        console.print()
        if not reading.ok:
            console.print(f"Served groups: [red]could not read[/red] — {reading.error}")
        elif not reading.expected:
            console.print(
                "Served groups: [dim]nothing expected — this box is not a managed "
                "deployment, or holds no gateway credential[/dim]"
            )
        elif reading.missing:
            console.print(
                f"Served groups: [red]{len(reading.missing)} missing[/red] — the gateway "
                f"does not serve: {', '.join(reading.missing)}"
            )
            console.print(
                "  [dim]Calls to these fail with an invalid-model error. This is an "
                "Olano-side routing problem — the group needs publishing to the "
                "gateway; nothing on this box can fix it.[/dim]"
            )
            console.print(f"  [dim]serving: {', '.join(reading.served) or '(none)'}[/dim]")
        else:
            console.print(
                f"Served groups: [green]all {len(reading.expected)} expected group(s) "
                f"are served[/green] ({', '.join(reading.expected)})"
            )


@models.command(name="test")
@click.argument("model_string")
@click.option("--message", "-m", default="Say 'hello' in one word.", help="Test message")
def models_test(model_string: str, message: str) -> None:
    """Test that a model string works (makes a real API call)."""
    from olano.models import build_model, install_hint, parse_model_string

    provider, _ = parse_model_string(model_string)
    console.print(f"Testing [bold]{model_string}[/bold]...")

    try:
        model = build_model(model_string)
    except ImportError:
        hint = install_hint(provider) if provider else "unknown"
        console.print(f"[red]Package not installed.[/red] Run: [bold]{hint}[/bold]")
        return
    except Exception as e:
        console.print(f"[red]Build failed:[/red] {e}")
        return

    try:
        result = model.invoke(message)
        content = result.content if isinstance(result.content, str) else str(result.content)
        console.print(f"[green]✓ OK[/green] Response: {content[:200]}")
    except Exception as e:
        console.print(f"[red]Call failed:[/red] {e}")


@cli.group()
def catalog() -> None:
    """Cloud catalog sync (models, MCP directory): status and manual refresh."""


@catalog.command(name="status")
def catalog_status_cmd() -> None:
    """Show each synced catalog's revision, data source, and last fetch time."""
    from datetime import datetime

    from olano.catalog import credits_registry, models_registry
    from olano.catalog import store as catalog_store
    from olano.config.system_settings import cost_display_mode, load_catalog_sync_settings
    from olano.core import mcp_registry

    settings = load_catalog_sync_settings()
    console.print(
        f"Sync: {'[green]enabled[/green]' if settings.enabled else '[yellow]disabled[/yellow]'}"
        f" · manifest poll every {settings.manifest_poll_minutes} min"
    )

    def _when(ts: object) -> str:
        if not isinstance(ts, int | float) or ts <= 0:
            return "never"
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

    models_status = models_registry.catalog_status()
    table = Table(show_header=True, header_style="bold")
    table.add_column("Catalog")
    table.add_column("Source")
    table.add_column("Revision")
    table.add_column("Entries")
    table.add_column("Last fetch")
    table.add_row(
        "models",
        str(models_status.get("source")),
        str(models_status.get("revision")),
        f"{models_status.get('providers')} providers / {models_status.get('models')} models",
        _when(models_status.get("fetched_at")),
    )
    directory = mcp_registry.get_directory()
    mcp_state = catalog_store.state_entry("mcp")
    table.add_row(
        "mcp",
        str(directory.get("source")),
        "—",
        f"{len(directory.get('presets', []))} presets / {len(directory.get('examples', []))} examples",
        _when(mcp_state.get("fetched_at")),
    )
    credits_status = credits_registry.catalog_status()
    credits_state = catalog_store.state_entry("credits")
    table.add_row(
        "credits",
        str(credits_status.get("source")),
        str(credits_status.get("revision")),
        f"$1 = {credits_status.get('credits_per_usd')} credits",
        _when(credits_state.get("fetched_at")),
    )
    from olano.catalog import agents_registry, skills_registry

    agents_status = agents_registry.catalog_status()
    table.add_row(
        "agents",
        str(agents_status.get("source")),
        str(agents_status.get("revision")),
        f"{agents_status.get('archetypes')} archetypes",
        _when(agents_status.get("fetched_at")),
    )
    skills_status = skills_registry.catalog_status()
    table.add_row(
        "skills",
        str(skills_status.get("source")),
        str(skills_status.get("revision")),
        f"{skills_status.get('skills')} skills"
        + ("" if skills_status.get("token") else " (no deployment token: git only)"),
        _when(skills_status.get("fetched_at")),
    )
    console.print(table)
    # The rate is synced regardless; this line says whether it is actually
    # being *used* for display or the deployment is still showing dollars.
    mode = cost_display_mode()
    console.print(
        f"Cost display: [bold]{mode}[/bold]"
        + ("" if mode == "credits" else "  (set credits.display: credits to show olano credits)")
    )


@catalog.command(name="refresh")
def catalog_refresh_cmd() -> None:
    """Force-refresh every synced catalog from the cloud now."""
    from olano.catalog import sync as catalog_sync

    console.print("Refreshing catalogs from the cloud…")
    report = catalog_sync.sync_once(force=True)
    if not report.get("manifest"):
        console.print(
            "[yellow]Manifest unavailable (offline or older cloud) — forced fetch.[/yellow]"
        )
    for name, entry in report.get("catalogs", {}).items():
        status = entry.get("status")
        colour = {"updated": "green", "unchanged": "dim", "error": "red"}.get(status, "white")
        rev = f" (revision {entry.get('revision')})" if entry.get("revision") is not None else ""
        console.print(f"  [{colour}]{name}: {status}{rev}[/{colour}]")


# ─────────────────────────────────────── phase 0 helpers ─────────────────────


_SETUP_PROVIDER_CHOICES = ("anthropic", "openai", "openai-codex", "google_genai", "ollama")
_SECURITY_PRESETS: dict[str, dict[str, object]] = {
    "safe": {
        "profile": "safe",
        "trust_level": 2,
        "hitl_enabled": True,
        "hitl_shell": True,
        "hitl_write_tools": True,
        "hitl_auto_approve_low_risk": False,
    },
    "developer": {
        "profile": "developer",
        "trust_level": 4,
        "hitl_enabled": True,
        "hitl_shell": True,
        "hitl_write_tools": True,
        "hitl_auto_approve_low_risk": True,
    },
    "production": {
        "profile": "production",
        "trust_level": 1,
        "hitl_enabled": True,
        "hitl_shell": True,
        "hitl_write_tools": True,
        "hitl_auto_approve_low_risk": False,
    },
}
_SETUP_SAFETY_OPTIONS = (
    {
        "key": "safe",
        "label": "Guided workspace + shell",
        "emoji": "🛡️",
        "summary": "Can work with your files and docker shell, but checks with you first",
        "detail": "A calm default for most people. Olano can help fully, but it will ask before file edits and shell actions so nothing feels surprising.",
    },
    {
        "key": "developer",
        "label": "Faster local coding",
        "emoji": "🛠️",
        "summary": "Can edit workspace files and use docker shell with fewer interruptions",
        "detail": "A good fit for hands-on coding. Olano moves faster on low-risk work, but still stops for higher-risk actions.",
    },
    {
        "key": "production",
        "label": "Read-only planning",
        "emoji": "🏭",
        "summary": "No shell access and no local file changes",
        "detail": "Best when you want research, review, and planning help without letting the agent change anything on your machine.",
    },
)
_SETUP_AGENT_OPTIONS = (
    {
        "key": "general",
        "label": "Merli",
        "emoji": "🦁",
        "summary": "A flexible everyday assistant",
        "detail": "A good place to start if you want one agent who can help with research, planning, coordination, and general day-to-day work.",
    },
    {
        "key": "specialized",
        "label": "Built-in specialist",
        "emoji": "💼",
        "summary": "A ready-made agent for a specific kind of work",
        "detail": "Choose this if you already know you want help focused on something like coding, finance, marketing, or personal workflows.",
    },
    {
        "key": "custom",
        "label": "Custom agent",
        "emoji": "⚙️",
        "summary": "Start from scratch and shape it yourself",
        "detail": "Choose this if you want to decide the name, role, tone, and starting instructions on your own.",
    },
)
_SETUP_TITLE = "#7cc6ff"
_SETUP_HINT = "#dbeafe"
_SETUP_MUTED = "#e5e7eb"
_SETUP_BORDER = "#4c7eff"

_CUSTOM_MAIN_TOOL_OPTIONS = (
    {
        "key": "safe_shell",
        "label": "Shell access",
        "emoji": "🖥️",
        "summary": "Run guarded terminal commands",
    },
    {
        "key": "node_run",
        "label": "Node runner",
        "emoji": "📦",
        "summary": "Execute Node-based helpers and scripts",
    },
    {
        "key": "web_search",
        "label": "Web search",
        "emoji": "🔎",
        "summary": "Search the web directly from the main agent",
    },
    {
        "key": "page_read",
        "label": "Page read",
        "emoji": "📄",
        "summary": "Read public webpages quickly without browser automation",
    },
    {
        "key": "page_extract",
        "label": "Page extract",
        "emoji": "🪄",
        "summary": "Use a slower, higher-recall page extractor when needed",
    },
    {
        "key": "http_request",
        "label": "HTTP request",
        "emoji": "🌐",
        "summary": "Call APIs and raw HTTP endpoints with headers or request bodies",
    },
    {
        "key": "url_download",
        "label": "URL download",
        "emoji": "⬇️",
        "summary": "Download a file from a URL into the workspace",
    },
    {
        "key": "page_screenshot",
        "label": "Page screenshot",
        "emoji": "📸",
        "summary": "Capture a screenshot of a page without interactive browsing",
    },
    {
        "key": "image_describe",
        "label": "Image describe",
        "emoji": "🖼️",
        "summary": "Describe or read text from a screenshot or image",
    },
    {
        "key": "kb_search",
        "label": "Knowledge search",
        "emoji": "🧠",
        "summary": "Search the local knowledge base",
    },
)

_CUSTOM_BUNDLE_OPTIONS = (
    {
        "key": "google",
        "label": "Google Workspace",
        "emoji": "📬",
        "summary": "Gmail, Calendar, and Drive",
        "detail": "Useful for email, scheduling, and file management.",
        "default_name": "OfficeAgent",
        "default_prompt": "You handle Gmail, Calendar, and Drive tasks carefully and clearly.",
    },
    {
        "key": "github",
        "label": "GitHub",
        "emoji": "💻",
        "summary": "Repos, issues, pull requests, code search",
        "detail": "Best for software engineering and repository operations.",
        "default_name": "GitHubAgent",
        "default_prompt": "You handle GitHub tasks, repository research, issues, and pull requests.",
    },
    {
        "key": "web",
        "label": "Web Agent",
        "emoji": "🔎",
        "summary": "Search, read, request, download, and capture pages",
        "detail": "Best for fast web research, screenshots, downloads, and non-interactive web access.",
        "default_name": "WebAgent",
        "default_prompt": "You handle web research and page capture. Prioritize the fastest structured tool that can answer the question cleanly.",
    },
    {
        "key": "browser",
        "label": "Browser Automation",
        "emoji": "🧭",
        "summary": "Interactive click, type, and session workflows",
        "detail": "Use only when the task requires interaction with a webpage or a persistent browser session.",
        "default_name": "BrowserAgent",
        "default_prompt": "You use browser tools only for interactive workflows: navigate, click, type, inspect the resulting state, and close the session carefully.",
    },
    {
        "key": "knowledge",
        "label": "Knowledge Base",
        "emoji": "🧠",
        "summary": "Add, search, and manage local knowledge",
        "detail": "Good for memory-heavy assistants and research workflows.",
        "default_name": "KnowledgeAgent",
        "default_prompt": "You curate the knowledge base, keep it organized, and retrieve the right context quickly.",
    },
    {
        "key": "scheduler",
        "label": "Scheduler",
        "emoji": "⏰",
        "summary": "Recurring jobs and scheduled reminders",
        "detail": "Useful for follow-ups, recurring tasks, and automation.",
        "default_name": "SchedulerAgent",
        "default_prompt": "You manage recurring jobs and scheduled actions with clear confirmations.",
    },
    {
        "key": "finance",
        "label": "Finance",
        "emoji": "📈",
        "summary": "Quotes, history, and crypto tracking",
        "detail": "Useful for market research and personal finance support.",
        "default_name": "FinanceAgent",
        "default_prompt": "You handle market and finance research carefully, with clear caveats and risk awareness.",
    },
    {
        "key": "marketing",
        "label": "Marketing",
        "emoji": "📣",
        "summary": "Campaigns, posting, and audience workflows",
        "detail": "Good for social content and campaign execution.",
        "default_name": "MarketingAgent",
        "default_prompt": "You help with marketing campaigns, social planning, and audience-oriented content.",
    },
    {
        "key": "slack",
        "label": "Slack",
        "emoji": "💬",
        "summary": "Channels, messages, and search",
        "detail": "Useful for team coordination and workplace messaging.",
        "default_name": "SlackAgent",
        "default_prompt": "You manage Slack conversations, message drafting, and channel research.",
    },
    {
        "key": "notification",
        "label": "Notifications",
        "emoji": "📲",
        "summary": "Text, photo, document, and broadcast messages",
        "detail": "Useful for outbound notifications and alerting.",
        "default_name": "NotificationAgent",
        "default_prompt": "You send outbound notifications carefully and confirm high-impact communication.",
    },
)


def _lionclaw_env_path() -> Path:
    return get_olano_paths().env_path


def _default_agent_name() -> str:
    return load_home_config().default_agent


def _stdin_ready(fileno: int) -> bool:
    """True when ``fileno`` has data or EOF ready within a short poll.

    Uses ``poll()`` (no FD_SETSIZE ceiling) so it stays correct when the host has
    pushed stdin's descriptor past 1024 — ``select()`` raises there, which used to
    fall through to a blocking read and hang headless runs. Falls back to
    ``select()`` only where ``poll()`` is unavailable (e.g. Windows).
    """
    try:
        poller = select.poll()
    except AttributeError:  # pragma: no cover - no poll() (Windows)
        try:
            ready, _, _ = select.select([fileno], [], [], 0.1)
            return bool(ready)
        except (OSError, ValueError):
            return True  # cannot poll; fall through to a best-effort read
    poller.register(fileno, select.POLLIN | select.POLLHUP)
    try:
        return bool(poller.poll(100))  # ms
    except (OSError, ValueError):  # pragma: no cover - defensive
        return True
    finally:
        try:
            poller.unregister(fileno)
        except (KeyError, OSError, ValueError):  # pragma: no cover - defensive
            pass


def _read_piped_input(max_bytes: int = 10 * 1024 * 1024) -> str | None:
    """Return stripped piped stdin text, or None when stdin is interactive/empty."""
    if sys.stdin is None:
        return None

    try:
        if sys.stdin.isatty():
            return None
    except (AttributeError, OSError, ValueError):
        return None

    # Non-TTY stdin: only read when data (or EOF) is actually available. A
    # connected-but-idle pipe/socket (CI runners, supervisors, background
    # shells) never sends data or EOF, so an unconditional read() would block
    # forever. poll() reports immediately for real files and pipes that already
    # hold data or are closed.
    #
    # poll() (not select()) is deliberate: select() can't handle a file
    # descriptor >= FD_SETSIZE (1024) and raises "filedescriptor out of range",
    # which on a busy host (CI with coverage + many plugins holding fds open)
    # would fall through to a blocking read and hang the run. poll() has no such
    # ceiling.
    try:
        fileno = sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):
        fileno = None
    if fileno is not None and not _stdin_ready(fileno):
        return None

    try:
        payload = sys.stdin.read(max_bytes + 1)
    except UnicodeDecodeError as exc:
        raise click.UsageError("Could not read piped input. Ensure stdin is valid text.") from exc
    except (OSError, ValueError) as exc:
        raise click.UsageError(f"Failed to read piped input: {exc}") from exc

    if len(payload) > max_bytes:
        raise click.UsageError(
            f"Piped input exceeds {max_bytes // (1024 * 1024)} MiB. Pass a file reference instead."
        )

    payload = payload.strip()
    return payload or None


def _merge_prompt_inputs(
    initial_prompt: str | None,
    non_interactive_prompt: str | None,
    *,
    piped_input: str | None = None,
) -> tuple[str | None, str | None]:
    """Merge piped stdin into either the interactive or non-interactive prompt."""
    if piped_input is None:
        piped_input = _read_piped_input()
    if not piped_input:
        return initial_prompt, non_interactive_prompt
    if non_interactive_prompt:
        return initial_prompt, f"{piped_input}\n\n{non_interactive_prompt}"
    if initial_prompt:
        return f"{piped_input}\n\n{initial_prompt}", non_interactive_prompt
    return initial_prompt, piped_input


def _slugify_agent_name(name: str) -> str:
    cleaned = name.strip().lower().replace(" ", "_")
    return "".join(ch for ch in cleaned if ch.isalnum() or ch in {"_", "-"}).strip("_-")


def _next_available_agent_name(preferred: str, existing_agents: list[str]) -> str:
    existing = {name.lower() for name in existing_agents}
    if preferred.lower() not in existing:
        return preferred
    index = 2
    while True:
        candidate = f"{preferred} {index}"
        if candidate.lower() not in existing:
            return candidate
        index += 1


def _upsert_env_var(path: Path, key: str, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()

    updated = False
    new_lines: list[str] = []
    for line in lines:
        if line.startswith(f"{key}="):
            new_lines.append(f"{key}={value}")
            updated = True
        else:
            new_lines.append(line)
    if not updated:
        new_lines.append(f"{key}={value}")
    path.write_text("\n".join(new_lines).strip() + "\n", encoding="utf-8")


def _discover_identity_defaults() -> tuple[str, str]:
    from datetime import datetime

    name = os.environ.get("USER") or os.environ.get("USERNAME") or "User"
    tzinfo = datetime.now().astimezone().tzinfo
    timezone = os.environ.get("TZ") or getattr(tzinfo, "key", None) or str(tzinfo or "UTC")
    return name, timezone


def _write_setup_profile(
    workspace_root: Path,
    name: str,
    timezone: str,
    preferences: str,
    *,
    include_name: bool = True,
) -> None:
    lines = ["## Setup Profile", ""]
    if include_name and name.strip():
        lines.append(f"- Preferred name: {name}")
    lines.append(f"- Timezone: {timezone}")
    lines.append(f"- Preferences: {preferences or 'Not specified'}")
    profile = "\n".join(lines) + "\n"
    user_path = workspace_root / "USER.md"
    existing = user_path.read_text(encoding="utf-8") if user_path.exists() else "# User Profile\n"
    marker = "\n## Setup Profile\n"
    if marker in existing:
        existing = existing.split(marker, 1)[0].rstrip()
    user_path.write_text(existing.rstrip() + "\n\n" + profile, encoding="utf-8")


def _default_model_for_provider(provider: str) -> str:
    from olano.models import get_provider_models

    models = get_provider_models(provider)
    model_name = models[0] if models else ""
    return f"{provider}:{model_name}" if model_name else provider


def _model_name_for_provider(provider: str, model_string: str | None) -> str:
    from olano.models import parse_model_string

    if not model_string:
        return ""
    parsed_provider, model_name = parse_model_string(model_string)
    if parsed_provider in {None, provider}:
        return model_name
    return model_string


def _normalize_model_string(provider: str, model_string: str | None) -> str:
    from olano.models import parse_model_string

    raw = (model_string or "").strip()
    if not raw:
        return _default_model_for_provider(provider)
    parsed_provider, model_name = parse_model_string(raw)
    if parsed_provider:
        return raw
    return f"{provider}:{model_name}"


def _doctor_suggestions(rows: list[tuple[str, str, str]]) -> list[str]:
    suggestions: list[str] = []

    for name, state, detail in rows:
        if state != "FAIL":
            continue
        lower_detail = detail.lower()

        if name == "Model runtime":
            if "unknown provider" in lower_detail or "provider:model format" in lower_detail:
                suggestions.append(
                    "Use a fully qualified model string like `ollama:kimi-k2.5:cloud`, then rerun `olano setup --provider ollama --model ollama:kimi-k2.5:cloud` or update the agent's `model:` field."
                )
            elif "ollama model" in lower_detail and "not pulled locally" in lower_detail:
                suggestions.append(
                    "Pull the missing Ollama model first with `ollama pull <model>` and rerun `olano doctor`."
                )
            elif "not configured" in lower_detail and "_api_key" in lower_detail:
                suggestions.append(
                    "Add the provider credential with `olano vault set <ENV_VAR>` or rerun `olano setup` and choose the model step again."
                )
        elif name == "Model connectivity":
            suggestions.append(
                "Rerun `olano doctor --skip-model` to isolate runtime/config issues from live connectivity problems."
            )
        elif name == "Workspace valid":
            suggestions.append(
                "Rerun `olano setup` for this home if the workspace scaffold is incomplete."
            )

    deduped: list[str] = []
    for suggestion in suggestions:
        if suggestion not in deduped:
            deduped.append(suggestion)
    return deduped


def _render_doctor_report(rows: list[tuple[str, str, str]], hard_fail: bool) -> None:
    table = Table(title="Olano Doctor", show_header=True)
    table.add_column("Check", style="bold")
    table.add_column("Status")
    table.add_column("Detail")

    colors = {"PASS": "green", "WARN": "yellow", "FAIL": "red"}
    for name, state, detail in rows:
        table.add_row(name, f"[{colors[state]}]{state}[/{colors[state]}]", detail)

    console.print(table)
    summary = (
        "[green]✓ All required checks passed[/green]"
        if not hard_fail
        else "[red]✗ One or more required checks failed[/red]"
    )
    console.print(f"\n{summary}")
    suggestions = _doctor_suggestions(rows)
    if suggestions:
        console.print("\n[bold]Suggestions[/bold]")
        for suggestion in suggestions:
            console.print(f"  - {suggestion}")


def _suggested_models_for_provider(provider: str) -> list[str]:
    from olano.models import get_provider_models

    return list(get_provider_models(provider))


def _legacy_prompt_for_provider_model(
    provider: str,
    current_model: str | None,
) -> str:
    suggestions = _suggested_models_for_provider(provider)
    current_model_name = _model_name_for_provider(provider, current_model)

    console.print("[bold]Suggested models[/bold]")
    for index, model_name in enumerate(suggestions, start=1):
        console.print(f"  [bold]{index}[/bold]. {model_name}")
    console.print(f"  [bold]{len(suggestions) + 1}[/bold]. Custom model name")

    default_choice = (
        str(suggestions.index(current_model_name) + 1)
        if current_model_name in suggestions
        else str(len(suggestions) + 1)
    )
    raw_choice = _prompt_backable("  Model choice", default=default_choice).strip().lower()
    if raw_choice in {"b", "back"}:
        return raw_choice

    try:
        choice = int(raw_choice)
    except ValueError:
        choice = len(suggestions) + 1

    if 1 <= choice <= len(suggestions):
        return f"{provider}:{suggestions[choice - 1]}"

    custom_model = _setup_prompt(
        "  Custom model name",
        default=current_model_name or (suggestions[0] if suggestions else ""),
    )
    return _normalize_model_string(provider, custom_model)


def _apply_model_selection_to_template(
    template: dict[str, Any],
    *,
    main_model: str | None,
    subagent_models: dict[str, str | None] | None = None,
    force_all_subagents: bool = False,
) -> dict[str, Any]:
    import copy

    resolved = copy.deepcopy(template)
    if main_model:
        resolved["model"] = main_model

    def _apply(subagents: list[dict[str, Any]]) -> None:
        for subagent in subagents:
            name = str(subagent.get("name", ""))
            if force_all_subagents and main_model:
                subagent["model"] = main_model
            elif subagent_models and name in subagent_models:
                chosen_model = subagent_models[name]
                if chosen_model:
                    subagent["model"] = chosen_model
                else:
                    subagent.pop("model", None)
            _apply(list(subagent.get("subagents") or []))

    _apply(list(resolved.get("subagents") or []))
    return resolved


def _print_setup_step(step: int, total: int, title: str, subtitle: str | None = None) -> None:
    console.print()
    console.print(
        f"[bold {_SETUP_TITLE}]{step}/{total}[/bold {_SETUP_TITLE}] [bold white]{title}[/bold white]"
    )
    if subtitle:
        console.print(f"[{_SETUP_HINT}]{subtitle}[/{_SETUP_HINT}]")


def _setup_prompt_uses_raw_reader() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _format_setup_prompt(
    prompt_text: str,
    *,
    default: str | None = None,
    show_default: bool = True,
    prompt_suffix: str = ": ",
) -> str:
    rendered = prompt_text
    if show_default and default not in {None, ""}:
        rendered = f"{rendered} [{default}]"
    return f"{rendered}{prompt_suffix}"


def _read_setup_tty_line(
    prompt_text: str,
    *,
    default: str | None = None,
    hide_input: bool = False,
    show_default: bool = True,
    prompt_suffix: str = ": ",
    getchar=None,
    echo=None,
) -> str:
    getchar_fn = getchar or click.getchar
    echo_fn = echo or click.echo
    echo_fn(
        _format_setup_prompt(
            prompt_text,
            default=default,
            show_default=show_default,
            prompt_suffix=prompt_suffix,
        ),
        nl=False,
    )
    buffer: list[str] = []
    while True:
        char = getchar_fn()
        if char in {"\r", "\n"}:
            echo_fn()
            return "".join(buffer) if buffer else (default or "")
        if char in {"\x03", "\x04"}:
            echo_fn()
            raise click.Abort()
        if char in {"\b", "\x7f"}:
            if buffer:
                buffer.pop()
                if not hide_input:
                    echo_fn("\b \b", nl=False)
            continue
        if char.startswith("\x1b"):
            continue
        if len(char) != 1 or ord(char) < 32:
            continue
        buffer.append(char)
        if not hide_input:
            echo_fn(char, nl=False)


def _setup_prompt(
    prompt_text: str,
    *,
    default: str | None = None,
    hide_input: bool = False,
    show_default: bool = True,
    prompt_suffix: str = ": ",
) -> str:
    if not _setup_prompt_uses_raw_reader():
        return cast(
            str,
            click.prompt(
                prompt_text,
                default=default,
                hide_input=hide_input,
                show_default=show_default,
                prompt_suffix=prompt_suffix,
            ),
        )
    return _read_setup_tty_line(
        prompt_text,
        default=default,
        hide_input=hide_input,
        show_default=show_default,
        prompt_suffix=prompt_suffix,
    )


def _setup_confirm(prompt_text: str, *, default: bool = False) -> bool:
    if not _setup_prompt_uses_raw_reader():
        return click.confirm(prompt_text, default=default)
    default_hint = "[Y/n]" if default else "[y/N]"
    while True:
        answer = _read_setup_tty_line(
            f"{prompt_text} {default_hint}",
            default="",
            show_default=False,
        ).strip()
        if not answer:
            return default
        normalized = answer.lower()
        if normalized in {"y", "yes"}:
            return True
        if normalized in {"n", "no"}:
            return False
        click.echo("  Please enter 'y' or 'n'.")


def _prompt_backable(prompt_text: str, default: str | None = None) -> str:
    rendered = f"{prompt_text} (type 'b' to go back)"
    return _setup_prompt(rendered, default=default, show_default=default is not None)


def _render_menu_options(options: tuple[dict[str, str], ...]) -> None:
    for index, option in enumerate(options, start=1):
        console.print(
            f"  [bold]{index}[/bold]. {option['emoji']} [bold]{option['label']}[/bold]  "
            f"[{_SETUP_HINT}]{option['summary']}[/{_SETUP_HINT}]"
        )
        console.print(f"     [{_SETUP_MUTED}]{option['detail']}[/{_SETUP_MUTED}]")


def _read_setup_profile(workspace_root: Path) -> tuple[str | None, str | None, str | None]:
    user_path = workspace_root / "USER.md"
    if not user_path.exists():
        return None, None, None
    content = user_path.read_text(encoding="utf-8")
    marker = "## Setup Profile"
    if marker not in content:
        return None, None, None
    section = content.split(marker, 1)[1]
    values: dict[str, str | None] = {
        "Preferred name": None,
        "Timezone": None,
        "Preferences": None,
    }
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        key, _, value = line[2:].partition(":")
        key = key.strip()
        if key in values:
            cleaned = value.strip()
            values[key] = None if cleaned in {"", "Not specified"} else cleaned
    return values["Preferred name"], values["Timezone"], values["Preferences"]


def _agent_names_in_home(home_paths) -> list[str]:
    if not home_paths.agents_dir.exists():
        return []
    names: list[str] = []
    for entry in sorted(home_paths.agents_dir.iterdir()):
        if entry.is_dir() and (entry / "agent.json5").exists():
            names.append(entry.name)
    return names


def _home_has_existing_state(home_paths) -> bool:
    from olano.vault.store import known_vault_paths

    existing_agents = _agent_names_in_home(home_paths)
    return bool(
        existing_agents
        or home_paths.env_path.exists()
        or home_paths.approval_rules_path.exists()
        or any(path.exists() for path in known_vault_paths())
    )


def _infer_mode_from_agent_config(config) -> str:
    if getattr(config, "archetype", None) == "merli":
        return "general"
    if getattr(config, "archetype", None):
        return "specialized"
    return "custom"


def _normalize_setup_tool_name(tool_name: str) -> str:
    aliases = {
        "web_fetch_fast": "page_read",
        "web_fetch": "page_read",
        "web_extract_deep": "page_extract",
        "web_extract": "page_extract",
        "web_download_file": "url_download",
        "web_download": "url_download",
        "http_fetch_api": "http_request",
        "http_fetch": "http_request",
    }
    return aliases.get(tool_name, tool_name)


def _infer_setup_defaults_from_agent(agent_name: str) -> dict[str, Any]:
    from olano.config.loader import load_agent_config
    from olano.models import parse_model_string

    config = load_agent_config(agent_name, source="home")
    provider, _ = parse_model_string(config.model)
    # ``workspace`` is always populated by the config's post-load validator.
    user_name, timezone, preferences = _read_setup_profile(Path(cast(str, config.workspace)))
    return {
        "editing_agent": agent_name,
        "user_name": user_name,
        "timezone": timezone,
        "preferences": preferences,
        "provider": provider if provider in _SETUP_PROVIDER_CHOICES else None,
        "model": config.model,
        "preset": getattr(config.security, "profile", "safe"),
        "mode": _infer_mode_from_agent_config(config),
        "archetype": getattr(config, "archetype", None),
        "agent_name": config.name,
        "emoji": config.emoji,
        "description": config.description,
        "system_prompt": config.system_prompt,
        "custom_tools": [_normalize_setup_tool_name(name) for name in config.tools],
        "custom_subagents": [
            {
                "name": sub.name,
                "description": sub.description,
                "system_prompt": sub.system_prompt,
                "tools": [_normalize_setup_tool_name(name) for name in sub.tools],
            }
            for sub in config.subagents
        ],
    }


def _prompt_existing_home_action(home_paths, home_config) -> str | None:
    existing_agents = _agent_names_in_home(home_paths)
    console.print()
    console.print(
        Panel(
            "\n".join(
                [
                    f"[bold {_SETUP_TITLE}]Existing Olano Home Detected[/bold {_SETUP_TITLE}]",
                    f"[{_SETUP_HINT}]Home:[/{_SETUP_HINT}] {home_paths.root}",
                    f"[{_SETUP_HINT}]Default agent:[/{_SETUP_HINT}] {home_config.default_agent}",
                    f"[{_SETUP_HINT}]Agents:[/{_SETUP_HINT}] {', '.join(existing_agents) if existing_agents else 'none'}",
                ]
            ),
            border_style=_SETUP_BORDER,
            padding=(1, 2),
        )
    )
    if not _setup_confirm("  Continue working in this Olano home?", default=True):
        return None
    if not existing_agents:
        return "create"

    console.print()
    console.print("[bold]What do you want to do?[/bold]")
    console.print("  [bold]1[/bold]. Create a new agent")
    console.print("  [bold]2[/bold]. Modify an existing agent")
    console.print("  [bold]3[/bold]. Cancel")
    choice = _setup_prompt("  Selection", default="1").strip()
    if choice == "2":
        return "modify"
    if choice == "3":
        return None
    return "create"


def _select_existing_agent(existing_agents: list[str], current: str | None = None) -> str:
    console.print()
    console.print("[bold]Choose an agent to modify[/bold]")
    for index, name in enumerate(existing_agents, start=1):
        console.print(f"  [bold]{index}[/bold]. {name}")
    default_choice = "1"
    if current in existing_agents:
        default_choice = str(existing_agents.index(current) + 1)
    raw = _setup_prompt("  Selection", default=default_choice).strip()
    try:
        selected = int(raw)
    except ValueError:
        selected = int(default_choice)
    selected = max(1, min(selected, len(existing_agents)))
    return existing_agents[selected - 1]


def _select_multiple_options(
    title: str,
    subtitle: str,
    options: tuple[dict[str, str], ...],
    current_keys: list[str] | None = None,
) -> list[str] | None:
    console.print()
    console.print(f"[bold]{title}[/bold]")
    console.print(f"[{_SETUP_HINT}]{subtitle}[/{_SETUP_HINT}]")
    for index, option in enumerate(options, start=1):
        selected = "✓" if current_keys and option["key"] in current_keys else " "
        console.print(
            f"  [bold]{index}[/bold]. [{_SETUP_HINT}]{selected}[/{_SETUP_HINT}] "
            f"{option['emoji']} [bold]{option['label']}[/bold]  "
            f"[{_SETUP_HINT}]{option['summary']}[/{_SETUP_HINT}]"
        )
    default_values = current_keys or []
    default_indexes = [
        str(index)
        for index, option in enumerate(options, start=1)
        if option["key"] in default_values
    ]
    default_raw = ",".join(default_indexes)
    raw = (
        _prompt_backable("  Selection (comma-separated, blank for none)", default=default_raw)
        .strip()
        .lower()
    )
    if raw in {"b", "back"}:
        return None
    if not raw:
        return []
    indexes = _parse_number_list(raw, len(options))
    return [options[index - 1]["key"] for index in indexes]


def _select_tool_names(
    title: str,
    tool_names: list[str],
    current_tools: list[str] | None = None,
) -> list[str] | None:
    console.print()
    console.print(f"[bold]{title}[/bold]")
    for index, tool_name in enumerate(tool_names, start=1):
        selected = "✓" if current_tools and tool_name in current_tools else " "
        console.print(
            f"  [bold]{index}[/bold]. [{_SETUP_HINT}]{selected}[/{_SETUP_HINT}] {tool_name}"
        )
    default_indexes = [
        str(index)
        for index, tool_name in enumerate(tool_names, start=1)
        if current_tools and tool_name in current_tools
    ]
    raw = (
        _prompt_backable(
            "  Tools (comma-separated, blank for all)",
            default=",".join(default_indexes),
        )
        .strip()
        .lower()
    )
    if raw in {"b", "back"}:
        return None
    if not raw:
        return list(tool_names)
    indexes = _parse_number_list(raw, len(tool_names))
    return [tool_names[index - 1] for index in indexes]


def _default_subagent_description(bundle_label: str) -> str:
    return f"Handles {bundle_label.lower()} tasks."


def _configure_custom_agent(
    *,
    current_description: str,
    current_prompt: str,
    current_tools: list[str] | None,
    current_subagents: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    from olano.tools.registry import list_bundles

    bundle_tool_map = list_bundles()

    console.print()
    console.print("[bold]Customize your agent[/bold]")
    console.print(
        f"[{_SETUP_HINT}]Choose the main tools and the built-in helper subagents you want to enable.[/{_SETUP_HINT}]"
    )

    description = _setup_prompt("  Description", default=current_description)
    system_prompt = _setup_prompt(
        "  Main system prompt",
        default=current_prompt
        or f"You are a helpful AI assistant focused on {description.lower()}.",
    )

    direct_tools = _select_multiple_options(
        "Choose main-agent tools",
        "These tools stay directly on the main assistant.",
        _CUSTOM_MAIN_TOOL_OPTIONS,
        current_keys=current_tools or ["web_search", "page_read", "http_request"],
    )
    if direct_tools is None:
        return None

    selected_bundles = _select_multiple_options(
        "Choose helper subagents",
        "These specialized helpers get their own tools, prompts, and roles.",
        _CUSTOM_BUNDLE_OPTIONS,
        current_keys=[],
    )
    if selected_bundles is None:
        return None

    existing_by_name = {sub.get("name"): sub for sub in (current_subagents or [])}
    subagents: list[dict[str, Any]] = []
    for bundle in _CUSTOM_BUNDLE_OPTIONS:
        if bundle["key"] not in selected_bundles:
            continue
        tools_for_bundle = list(bundle_tool_map.get(bundle["key"], []))
        default_name = bundle["default_name"]
        current_sub = existing_by_name.get(default_name, {})
        console.print()
        console.print(f"[bold]{bundle['emoji']} {bundle['label']} subagent[/bold]")
        subagent_name = _setup_prompt(
            "  Subagent name", default=current_sub.get("name", default_name)
        )
        selected_tools = _select_tool_names(
            f"Pick tools for {subagent_name}",
            tools_for_bundle,
            current_tools=current_sub.get("tools") or tools_for_bundle,
        )
        if selected_tools is None:
            return None
        default_prompt = current_sub.get("system_prompt") or bundle["default_prompt"]
        subagent_prompt = _setup_prompt("  System prompt", default=default_prompt)
        subagents.append(
            {
                "name": subagent_name,
                "description": current_sub.get("description")
                or _default_subagent_description(bundle["label"]),
                "system_prompt": subagent_prompt,
                "tools": selected_tools,
            }
        )

    return {
        "description": description,
        "system_prompt": system_prompt,
        "custom_tools": direct_tools,
        "custom_subagents": subagents,
    }


def _select_safety_preset(current: str | None = None) -> str | None:
    _print_setup_step(3, 5, "Safety", "Choose how cautious Olano should be by default.")
    console.print("[bold]Choose a safety level[/bold]")
    _render_menu_options(_SETUP_SAFETY_OPTIONS)

    index_by_key = {option["key"]: idx for idx, option in enumerate(_SETUP_SAFETY_OPTIONS, start=1)}
    default_choice = str(index_by_key.get(current or "safe", 1))
    raw = _prompt_backable("  Selection", default=default_choice).strip().lower()
    if raw in {"b", "back"}:
        return None
    try:
        choice = int(raw)
    except ValueError:
        choice = int(default_choice)
    choice = max(1, min(choice, len(_SETUP_SAFETY_OPTIONS)))
    return _SETUP_SAFETY_OPTIONS[choice - 1]["key"]


def _select_first_agent_mode(current: str | None = None) -> str | None:
    _print_setup_step(4, 5, "First Agent", "Pick the kind of agent you want to start with.")
    console.print("[bold]Choose your first agent[/bold]")
    _render_menu_options(_SETUP_AGENT_OPTIONS)

    index_by_key = {option["key"]: idx for idx, option in enumerate(_SETUP_AGENT_OPTIONS, start=1)}
    default_choice = str(index_by_key.get(current or "general", 1))
    raw = _prompt_backable("  Selection", default=default_choice).strip().lower()
    if raw in {"b", "back"}:
        return None
    try:
        choice = int(raw)
    except ValueError:
        choice = int(default_choice)
    choice = max(1, min(choice, len(_SETUP_AGENT_OPTIONS)))
    return _SETUP_AGENT_OPTIONS[choice - 1]["key"]


def _ordered_specialists(archetypes: list[str]) -> list[str]:
    preferred = ["lionDev", "lionFinance", "lionMarketing", "lionLife"]
    remaining = [name for name in archetypes if name not in preferred and name != "merli"]
    ordered = [name for name in preferred if name in archetypes]
    ordered.extend(sorted(remaining))
    return ordered


def _default_specialist_archetype() -> str:
    """The archetype `setup --mode specialized` picks when none was named.

    Resolved from what is actually installed rather than hardcoded: the
    preferred names (lionDev and friends) ship in the canonical `olano-agents`
    pack, not in-package, so a box that never fetched the pack has none of them
    and a hardcoded default raises `FileNotFoundError` on the very next line.
    """
    from olano.archetypes.installer import list_archetypes

    ordered = _ordered_specialists(list_archetypes())
    return ordered[0] if ordered else "default"


def _select_specialist_archetype(
    available_archetypes: list[str],
    current: str | None = None,
) -> str | None:
    from olano.archetypes.installer import load_archetype_template

    specialists = _ordered_specialists([name for name in available_archetypes if name != "merli"])
    console.print()
    console.print("[bold]Choose a built-in specialist[/bold]")
    console.print(
        f"[{_SETUP_HINT}]These are pre-configured agents with domain-specific tools and subagents.[/{_SETUP_HINT}]"
    )
    for index, archetype_name in enumerate(specialists, start=1):
        template = load_archetype_template(archetype_name)
        console.print(
            f"  [bold]{index}[/bold]. {template.get('emoji', '🤖')} [bold]{archetype_name}[/bold]"
        )
        console.print(f"     [{_SETUP_MUTED}]{template.get('description', '')}[/{_SETUP_MUTED}]")
    console.print(f"  [bold]{len(specialists) + 1}[/bold]. ← Back")

    default_choice = "1"
    if current in specialists:
        default_choice = str(specialists.index(current) + 1)
    raw = _setup_prompt("  Selection", default=default_choice).strip().lower()
    try:
        choice = int(raw)
    except ValueError:
        choice = int(default_choice)
    if choice == len(specialists) + 1:
        return None
    choice = max(1, min(choice, len(specialists)))
    return specialists[choice - 1]


def _safety_label(key: str) -> str:
    labels = {option["key"]: option["label"] for option in _SETUP_SAFETY_OPTIONS}
    return labels.get(key, key)


def _setup_command_prefix(home: Path) -> str:
    return "olano" if is_default_home(home) else f'olano --home "{home}"'


def _run_quietly(fn, *args, **kwargs):
    return run_quietly(fn, *args, **kwargs)


async def _run_quietly_async(fn, *args, **kwargs):
    return await run_quietly_async(fn, *args, **kwargs)


def _can_use_fullscreen_setup_wizard() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _can_run_post_setup_doctor() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _run_textual_setup_wizard(
    *,
    home,
    provider: str | None,
    model: str | None,
    api_key: str | None,
    safety_preset: str | None,
    mode: str | None,
    archetype: str | None,
    agent_name: str | None,
    emoji: str | None,
) -> tuple[dict[str, Any] | None, bool]:
    from argparse import Namespace

    from olano.cli import setup_textual as setup_textual_cli

    plan = setup_textual_cli.run_setup_textual_prototype(
        Namespace(
            home=str(home),
            provider=provider,
            model=model,
            api_key=api_key,
            safety_preset=safety_preset,
            mode=mode,
            archetype=archetype,
            agent_name=agent_name,
            emoji=emoji,
        )
    )
    if plan is None:
        return None, False
    return setup_textual_cli.plan_to_wizard_state(plan), plan.skip_verify


def _prompt_interactive_setup_legacy(
    *,
    provider: str | None,
    model: str | None,
    api_key: str | None,
    safety_preset: str | None,
    mode: str | None,
    archetype: str | None,
    agent_name: str | None,
    emoji: str | None,
    available_archetypes: list[str],
    default_name: str,
    default_timezone: str,
    initial_state: dict[str, Any] | None = None,
    editing_existing: bool = False,
    existing_agents: list[str] | None = None,
) -> dict[str, Any]:
    """Deprecated plain-terminal setup fallback."""
    from olano.models import PROVIDERS

    initial_state = initial_state or {}
    existing_agents = existing_agents or []
    state: dict[str, Any] = {
        "user_name": initial_state.get("user_name") or default_name,
        "timezone": initial_state.get("timezone") or default_timezone,
        "preferences": initial_state.get("preferences") or "",
        "provider": initial_state.get("provider") or provider or "anthropic",
        "model": initial_state.get("model")
        or model
        or _default_model_for_provider(provider or "anthropic"),
        "api_key": api_key or "",
        "preset": initial_state.get("preset") or safety_preset or "safe",
        "mode": initial_state.get("mode") or mode or "general",
        "archetype": initial_state.get("archetype") or archetype or "",
        "agent_name": initial_state.get("agent_name") or agent_name or "",
        "emoji": initial_state.get("emoji") or emoji or "",
        "description": initial_state.get("description") or "General-purpose Olano assistant",
        "system_prompt": initial_state.get("system_prompt") or "",
        "custom_tools": list(initial_state.get("custom_tools") or []),
        "custom_subagents": list(initial_state.get("custom_subagents") or []),
    }

    step = 1
    while step <= 5:
        if step == 1:
            _print_setup_step(1, 5, "Identity", "Tell Olano a little about you.")
            state["user_name"] = _setup_prompt("  Your name", default=state["user_name"])
            state["timezone"] = _setup_prompt("  Your timezone", default=state["timezone"])
            state["preferences"] = _setup_prompt(
                "  Preferences to remember (optional)",
                default=state["preferences"],
                show_default=False,
            )
            step += 1
            continue

        if step == 2:
            _print_setup_step(2, 5, "Model", "Choose the provider and model for your first agent.")
            console.print("[bold]Choose a model provider[/bold]")
            for index, key in enumerate(_SETUP_PROVIDER_CHOICES, start=1):
                info = PROVIDERS[key]
                console.print(f"  [bold]{index}[/bold]. {info.name}")
            default_provider_index = _SETUP_PROVIDER_CHOICES.index(state["provider"]) + 1
            raw_provider = (
                _prompt_backable("  Provider", default=str(default_provider_index)).strip().lower()
            )
            if raw_provider in {"b", "back"}:
                step -= 1
                continue
            try:
                provider_index = int(raw_provider)
            except ValueError:
                provider_index = default_provider_index
            provider_index = max(1, min(provider_index, len(_SETUP_PROVIDER_CHOICES)))
            state["provider"] = _SETUP_PROVIDER_CHOICES[provider_index - 1]
            selected_model = _legacy_prompt_for_provider_model(state["provider"], state["model"])
            if selected_model in {"b", "back"}:
                step -= 1
                continue
            state["model"] = selected_model

            info = PROVIDERS[state["provider"]]
            if info.env_var:
                api_key_default = state["api_key"] or os.environ.get(info.env_var, "")
                entered_key = _setup_prompt(
                    f"  {info.name} API key (optional, press Enter to skip)",
                    default=api_key_default,
                    hide_input=True,
                    show_default=False,
                ).strip()
                state["api_key"] = entered_key
            else:
                state["api_key"] = ""
            step += 1
            continue

        if step == 3:
            selected_preset = _select_safety_preset(state["preset"])
            if not selected_preset:
                step -= 1
                continue
            state["preset"] = selected_preset
            step += 1
            continue

        if step == 4:
            selected_mode = _select_first_agent_mode(state["mode"])
            if not selected_mode:
                step -= 1
                continue
            state["mode"] = selected_mode
            if state["mode"] == "general":
                state["archetype"] = "merli"
                state["agent_name"] = state["agent_name"] or _next_available_agent_name(
                    "Merli", existing_agents
                )
                state["emoji"] = state["emoji"] or "🦁"
            elif state["mode"] == "specialized":
                selected_archetype = _select_specialist_archetype(
                    available_archetypes, state["archetype"] or None
                )
                if not selected_archetype:
                    continue
                state["archetype"] = selected_archetype
                state["agent_name"] = state["agent_name"] or _next_available_agent_name(
                    selected_archetype, existing_agents
                )
                if not state["emoji"]:
                    from olano.archetypes.installer import load_archetype_template

                    specialist_template = load_archetype_template(selected_archetype)
                    state["emoji"] = state["emoji"] or specialist_template.get("emoji", "🤖")
            else:
                state["archetype"] = ""
                state["emoji"] = state["emoji"] or "🦁"

            if editing_existing:
                console.print(
                    f"  [{_SETUP_HINT}]Editing existing agent id:[/{_SETUP_HINT}] [bold]{state['agent_name']}[/bold]"
                )
            else:
                state["agent_name"] = _setup_prompt(
                    "  Agent name",
                    default=state["agent_name"]
                    or (
                        _next_available_agent_name("Merli", existing_agents)
                        if state["mode"] != "specialized"
                        else _next_available_agent_name(state["archetype"], existing_agents)
                    ),
                )
            state["emoji"] = _setup_prompt("  Emoji", default=state["emoji"] or "🦁")
            if state["mode"] == "custom":
                custom_state = _configure_custom_agent(
                    current_description=state["description"],
                    current_prompt=state["system_prompt"],
                    current_tools=state["custom_tools"],
                    current_subagents=state["custom_subagents"],
                )
                if custom_state is None:
                    continue
                state.update(custom_state)
            step += 1
            continue

        _print_setup_step(
            5, 5, "Review", "Press Enter to install, or choose a section number to edit."
        )
        review = Table(show_header=False, box=None, padding=(0, 1))
        review.add_column(style="bold", width=10)
        review.add_column()
        review.add_row("1. Identity", f"{state['user_name']} · {state['timezone']}")
        review.add_row(
            "2. Model",
            f"{state['provider']} · {_model_name_for_provider(state['provider'], state['model'])}",
        )
        review.add_row("3. Safety", _safety_label(state["preset"]))
        if state["mode"] == "specialized":
            agent_detail = (
                f"{state['mode']} · {state['archetype']} · {state['agent_name']} {state['emoji']}"
            )
        elif state["mode"] == "custom":
            agent_detail = (
                f"{state['mode']} · {state['agent_name']} {state['emoji']} · "
                f"{len(state['custom_subagents'])} subagents · {len(state['custom_tools'])} main tools"
            )
        else:
            agent_detail = f"{state['mode']} · {state['agent_name']} {state['emoji']}"
        review.add_row("4. Agent", agent_detail)
        console.print(review)
        raw = _setup_prompt("  Install now", default="", show_default=False).strip().lower()
        if not raw:
            break
        if raw in {"1", "2", "3", "4"}:
            step = int(raw)
            continue

    return state


def _store_provider_credential(
    provider: str,
    api_key: str,
    *,
    vault_passphrase: str | None = None,
    prefer_vault: bool = True,
) -> str:
    from olano.models import PROVIDERS
    from olano.vault.store import known_vault_paths

    info = PROVIDERS.get(provider)
    env_var = info.env_var if info else None
    if info is None or not env_var:
        return "No credential required for this provider."

    os.environ[env_var] = api_key
    existing_vault = next((path for path in known_vault_paths() if path.exists()), None)
    if prefer_vault and existing_vault:
        try:
            vault, _ = _open_setup_vault(vault_passphrase)
            vault.set(
                env_var,
                api_key,
                description=f"{info.name} API key",
                category="model-provider",
            )
            return f"Stored securely in the Olano Vault at {vault._db_path}"
        except Exception as e:
            console.print(
                f"[yellow]⚠[/yellow]  Vault write failed, falling back to env config: {e}"
            )

    env_path = _lionclaw_env_path()
    _upsert_env_var(env_path, env_var, api_key)
    return f"Stored in env config at {env_path}"


def _import_env_credentials_to_vault(
    env_var_names: list[str], *, vault_passphrase: str | None = None
) -> list[str]:
    imported: list[str] = []
    if not env_var_names:
        return imported

    vault, _ = _open_setup_vault(vault_passphrase)
    for env_var in env_var_names:
        value = os.environ.get(env_var)
        if not value:
            continue
        vault.set(
            env_var,
            value,
            description=f"Imported from environment variable {env_var}",
            category="imported-env",
        )
        imported.append(env_var)
    return imported


def _credentials_available_for_setup(
    provider: str,
    *,
    secrets_strategy: str,
    selected_api_key: str | None,
    imported_env_vars: list[str] | None = None,
) -> bool:
    from olano.models import PROVIDERS

    env_var = PROVIDERS[provider].env_var if provider in PROVIDERS else None
    if not env_var:
        return True
    if selected_api_key:
        return True
    imported = set(imported_env_vars or [])
    if secrets_strategy == "vault" and env_var in imported:
        return True
    return bool(secrets_strategy == "env" and os.environ.get(env_var))


def _system_keyring_available() -> bool:
    # Single source of truth — only a SECURE OS backend counts (see vault.keyring_backend).
    from olano.vault.keyring_backend import secure_keyring_available

    return secure_keyring_available()


def _vault_unlock_mode(passphrase: str | None = None) -> str:
    from olano.vault.store import vault_key_from_env

    if passphrase:
        return "passphrase"
    if vault_key_from_env():
        return "env"
    if _system_keyring_available():
        return "keyring"
    return "ephemeral"


def _open_setup_vault(passphrase: str | None = None, *, allow_ephemeral: bool = False):
    from olano.vault.store import VaultStore, default_vault_path

    mode = _vault_unlock_mode(passphrase)
    if mode == "ephemeral":
        if not allow_ephemeral:
            raise click.ClickException(
                "Olano could not find a persistent vault unlock method. "
                "Use a vault passphrase during setup, set OLANO_VAULT_KEY, or install keyring."
            )
        return VaultStore.open(db_path=default_vault_path(), use_keyring=False), mode

    vault_path = default_vault_path()
    if mode == "passphrase":
        return VaultStore.open(passphrase=passphrase, db_path=vault_path, use_keyring=False), mode
    if mode == "env":
        from olano.vault.store import vault_key_from_env

        return VaultStore.open(
            passphrase=vault_key_from_env(),
            db_path=vault_path,
            use_keyring=False,
        ), mode
    return VaultStore.open(db_path=vault_path, use_keyring=True), mode


def _interactive_unlock_vault() -> None:
    """Unlock the credentials vault before agent boot, prompting if needed.

    Skips silently when no vault DB exists. Prompts the user (TTY-safe) only
    when a vault is present but no env var / keyring entry can unlock it.
    Hydrates os.environ from the vault on success so MCP servers and tools
    that read env vars (Alpaca, Stripe, …) just work.
    """
    from olano.vault.runtime import (
        maybe_hydrate_env_from_vault,
        unlock_vault_for_session,
    )
    from olano.vault.store import default_vault_path

    if not default_vault_path().exists():
        return

    allow_prompt = sys.stdin.isatty() and sys.stdout.isatty()
    result = unlock_vault_for_session(allow_prompt=allow_prompt)
    if result.vault is None:
        if result.error:
            console.print(
                f"[yellow]⚠[/yellow]  Credentials vault locked ({result.error}); "
                "agent will not see vault-managed env vars this session."
            )
        return
    # Hardened default: no global env hydration (toolkits read the vault directly).
    # LIONCLAW_LEGACY_ENV_HYDRATE=1 restores it; /reload-vault hydrates explicitly.
    injected = maybe_hydrate_env_from_vault(result.vault)
    if injected:
        console.print(
            f"[#7cc6ff]vault[/#7cc6ff] unlocked via [bold]{result.mode}[/bold] · "
            f"injected {len(injected)} credential(s) into env"
        )


def _probe_model_connectivity(model_string: str) -> tuple[bool, str]:
    from olano.models import PROVIDERS, build_model, parse_model_string

    provider, _ = parse_model_string(model_string)
    env_var = PROVIDERS[provider].env_var if provider in PROVIDERS else None
    if env_var and not os.environ.get(env_var):
        return False, f"{env_var} is not configured"

    try:
        model = _run_quietly(build_model, model_string)
        result = _run_quietly(model.invoke, "Reply with exactly OK.")
        # List content (OpenAI Responses models, Anthropic) is a list of blocks;
        # printing it raw filled the doctor table with a dict dump.
        raw = result.content
        if isinstance(raw, str):
            content = raw
        else:
            content = "".join(
                str(block.get("text", "")) if isinstance(block, dict) else str(block)
                for block in raw
                if not isinstance(block, dict) or block.get("type") == "text"
            )
        return True, content.strip()[:120]
    except Exception as e:
        return False, str(e)


def _parse_number_list(raw: str, max_value: int) -> list[int]:
    values: list[int] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            choice = int(piece)
        except ValueError:
            continue
        if 1 <= choice <= max_value and choice not in values:
            values.append(choice)
    return values


def _apply_security_preset(config_path: Path, preset: str) -> None:
    import yaml

    # Atomic like every other config write — a truncated read of a config file
    # is silently coalesced to "no settings" by its readers. Uses the primitive
    # rather than write_home_config_text/write_agent_config_text because this
    # helper is currently unreferenced and its target file is ambiguous: it
    # yaml.dumps, which would rewrite an agent.json5 as YAML. Wire it to the
    # matching config-specific writer if it ever gains a caller.
    from olano.memory.markdown_memory import atomic_write_text

    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    security = data.setdefault("security", {})
    security.update(_SECURITY_PRESETS[preset])
    atomic_write_text(config_path, yaml.dump(data, sort_keys=False, allow_unicode=True))


def _ensure_vault_initialized(
    passphrase: str | None = None, *, allow_ephemeral: bool = False
) -> tuple[str, str]:
    from olano.vault.store import default_vault_path

    vault_path = default_vault_path()
    existed = vault_path.exists()
    vault, mode = _open_setup_vault(passphrase, allow_ephemeral=allow_ephemeral)
    status = "ready" if existed else "initialized"
    labels = {
        "keyring": "system keychain",
        "env": "OLANO_VAULT_KEY",
        "passphrase": "vault passphrase",
        "ephemeral": "temporary session key",
    }
    return f"Vault {status} at {vault._db_path} using {labels[mode]}", mode


def _write_agent_config_file(agent_dir: Path, config_data: dict[str, Any]) -> Path:
    import json

    from olano.config.loader import write_agent_config_text

    config_path = agent_dir / "agent.json5"
    write_agent_config_text(
        config_path, json.dumps(config_data, indent=2, ensure_ascii=False) + "\n"
    )
    return config_path


def _install_or_update_archetype_agent(
    *,
    archetype_name: str,
    agent_id: str,
    emoji: str,
    security_preset: str,
    overwrite_existing: bool,
    main_model: str | None = None,
    subagent_models: dict[str, str | None] | None = None,
    force_all_subagents: bool = False,
    display_name: str = "",
) -> Path:
    from olano.archetypes.installer import (
        load_archetype_template,
        resolve_template_display_name,
        scaffold_agent_workspace,
    )
    from olano.config.home import get_olano_paths
    from olano.config.json5_fast import loads as _load_json5

    agent_dir = get_olano_paths().agents_dir / agent_id
    config_path = agent_dir / "agent.json5"
    if config_path.exists() and not overwrite_existing:
        raise FileExistsError(
            f"Agent '{agent_id}' already exists at {config_path}. Choose a different name or modify it."
        )

    template = load_archetype_template(archetype_name)
    template = _apply_model_selection_to_template(
        template,
        main_model=main_model,
        subagent_models=subagent_models,
        force_all_subagents=force_all_subagents,
    )
    template["name"] = agent_id
    template["archetype"] = archetype_name
    # What the user typed → (when re-generating an existing agent) the display
    # name it already has → the archetype's own display_name/label. The
    # on-disk fallback matters in edit mode: this function rewrites the whole
    # agent.json5 from the template, and a rename the owner made in the
    # dashboard must survive an `olano setup` edit pass that typed nothing.
    existing_display = ""
    if not display_name.strip() and config_path.exists():
        try:
            existing_display = str(
                _load_json5(config_path.read_text(encoding="utf-8")).get("display_name") or ""
            ).strip()
        except Exception:
            existing_display = ""
    resolved_display = resolve_template_display_name(
        template, display_name.strip() or existing_display
    )
    if resolved_display:
        template["display_name"] = resolved_display
    template["emoji"] = emoji or template.get("emoji", "🦁")
    template["security"] = {
        **dict(template.get("security", {}) or {}),
        **dict(_SECURITY_PRESETS[security_preset]),
    }

    agent_dir.mkdir(parents=True, exist_ok=True)
    workspace_root = agent_dir / "workspace"
    template["workspace"] = str(workspace_root)
    written_path = _write_agent_config_file(agent_dir, template)
    scaffold_agent_workspace(agent_id, template, workspace_root, overwrite=False)
    (agent_dir / "skills").mkdir(exist_ok=True)
    return written_path


def _build_custom_agent(
    agent_name: str,
    model: str,
    description: str,
    emoji: str,
    security_preset: str,
    system_prompt: str,
    tools: list[str],
    subagents: list[dict[str, Any]],
    overwrite_existing: bool = False,
    display_name: str = "",
) -> Path:
    from olano.archetypes.installer import scaffold_agent_workspace
    from olano.config.home import get_olano_paths

    agent_id = _slugify_agent_name(agent_name) or "merli"
    agent_dir = get_olano_paths().agents_dir / agent_id
    config_path = agent_dir / "agent.json5"
    if config_path.exists() and not overwrite_existing:
        raise FileExistsError(
            f"Agent '{agent_id}' already exists at {config_path}. Choose a different name or modify it."
        )
    agent_dir.mkdir(parents=True, exist_ok=True)
    workspace_root = agent_dir / "workspace"

    label = display_name.strip() or agent_name
    config_data = {
        "name": agent_id,
        "display_name": display_name.strip(),
        "description": description or f"{label} agent",
        "emoji": emoji,
        "model": model,
        "system_prompt": system_prompt or f"You are {label}, a helpful AI assistant.",
        "workspace": str(workspace_root),
        "tools": tools,
        "subagents": subagents,
        "skills": [],
        "mcp_servers": [],
        "channels": [],
        "schedule": [],
        "memory": {"markdown": True, "rag": False, "self_learning": True},
        "security": dict(_SECURITY_PRESETS[security_preset]),
        "acp_enabled": False,
    }
    _write_agent_config_file(agent_dir, config_data)
    scaffold_agent_workspace(agent_id, config_data, workspace_root, overwrite=False)
    (agent_dir / "skills").mkdir(exist_ok=True)
    return config_path


def _load_runtime_agent_config(agent_name: str, *, prefer_home_default: bool = False):
    from olano.config.loader import load_agent_config

    source = "auto"
    if prefer_home_default and agent_name == _default_agent_name():
        source = "home"
    return load_agent_config(agent_name, source=source), source


def _resolve_doctor_model(
    agent_name: str | None, model_string: str | None
) -> tuple[str | None, str]:
    from olano.config.loader import list_agents, load_agent_config

    if model_string:
        return model_string, "explicit --model"
    if agent_name:
        try:
            config, source = _load_runtime_agent_config(agent_name, prefer_home_default=True)
            label = "home agent" if source == "home" else "agent"
            return config.model, f"{label} '{agent_name}'"
        except FileNotFoundError:
            return None, f"agent '{agent_name}' not found"

    default_agent = _default_agent_name()
    try:
        config = load_agent_config(default_agent, source="home")
        return config.model, f"default home agent '{config.name}'"
    except FileNotFoundError:
        pass

    agents = list_agents()
    if not agents:
        return None, "no configured agents"

    config = load_agent_config(agents[0])
    return config.model, f"agent '{config.name}'"


def _check_model_runtime(model_string: str) -> tuple[bool, str]:
    import shutil

    from olano.models import (
        PROVIDERS,
        _hydrate_provider_env_from_vault,
        is_provider_available,
        list_ollama_models,
        parse_model_string,
    )

    provider, model_name = parse_model_string(model_string)
    if not provider:
        return False, "model must use provider:model format"
    if provider not in PROVIDERS:
        return False, f"unknown provider '{provider}'"
    if not is_provider_available(provider):
        return False, f"{provider} package missing ({PROVIDERS[provider].package})"

    if provider == "ollama":
        if not shutil.which("ollama"):
            return False, "ollama is not installed"
        local_models = list_ollama_models()
        if model_name not in local_models:
            return False, f"ollama model '{model_name}' is not pulled locally"
        return True, f"ollama model '{model_name}' is installed"

    _hydrate_provider_env_from_vault(provider)
    env_var = PROVIDERS[provider].env_var
    if env_var and not os.environ.get(env_var):
        return False, f"{env_var} is not configured"
    return True, f"{provider} credentials available"


def _run_doctor_checks(
    agent_name: str | None,
    model_string: str | None,
    skip_model: bool,
) -> tuple[list[tuple[str, str, str]], bool]:
    import importlib.util
    import shutil
    import subprocess

    from olano.config.loader import list_agents
    from olano.vault.store import known_vault_paths

    rows: list[tuple[str, str, str]] = []
    hard_fail = False

    def add(name: str, state: str, detail: str, fail: bool = False) -> None:
        nonlocal hard_fail
        rows.append((name, state, detail))
        hard_fail = hard_fail or fail

    py_version = ".".join(str(part) for part in sys.version_info[:3])
    python_ok = sys.version_info >= (3, 11)
    add("Python >= 3.11", "PASS" if python_ok else "FAIL", py_version, fail=not python_ok)

    deepagents_ok = importlib.util.find_spec("deepagents") is not None
    add(
        "deepagents installed",
        "PASS" if deepagents_ok else "FAIL",
        "importlib.util.find_spec('deepagents')",
        fail=not deepagents_ok,
    )

    docker = shutil.which("docker")
    if not docker:
        add("Docker available", "FAIL", "docker not found in PATH", fail=True)
    else:
        try:
            result = subprocess.run([docker, "info"], capture_output=True, text=True, timeout=10)
            docker_ok = result.returncode == 0
            detail = (
                "docker info ok" if docker_ok else (result.stderr.strip() or result.stdout.strip())
            )
            add(
                "Docker available",
                "PASS" if docker_ok else "FAIL",
                detail[:120],
                fail=not docker_ok,
            )
        except Exception as e:
            add("Docker available", "FAIL", str(e), fail=True)

    ollama = shutil.which("ollama")
    if not ollama:
        add("Ollama available", "WARN", "ollama not installed")
    else:
        try:
            result = subprocess.run([ollama, "list"], capture_output=True, text=True, timeout=10)
            ollama_ok = result.returncode == 0
            detail = (
                "ollama list ok" if ollama_ok else (result.stderr.strip() or result.stdout.strip())
            )
            add("Ollama available", "PASS" if ollama_ok else "WARN", detail[:120])
        except Exception as e:
            add("Ollama available", "WARN", str(e))

    vault_path = next((path for path in known_vault_paths() if path.exists()), None)
    vault_ok = bool(vault_path and os.access(vault_path, os.R_OK))
    add(
        "Vault initialized",
        "PASS" if vault_ok else "WARN",
        str(vault_path) if vault_path else "no vault database found",
    )

    home_paths = get_olano_paths()
    if agent_name:
        try:
            config, _ = _load_runtime_agent_config(agent_name, prefer_home_default=True)
            workspace_root = Path(config.workspace or "")
        except FileNotFoundError:
            workspace_root = home_paths.root
    else:
        workspace_root = home_paths.root

    workspace_ok = workspace_root.exists() and (
        workspace_root == home_paths.root or (workspace_root / "AGENTS.md").exists()
    )
    missing = [] if workspace_ok else ["AGENTS.md"]
    detail = (
        str(workspace_root) if workspace_ok else f"{workspace_root} missing {', '.join(missing)}"
    )
    add("Workspace valid", "PASS" if workspace_ok else "FAIL", detail, fail=not workspace_ok)

    free_mb = shutil.disk_usage(workspace_root).free // (1024 * 1024)
    disk_ok = free_mb >= 500
    add("Disk space >= 500MB", "PASS" if disk_ok else "FAIL", f"{free_mb}MB free", fail=not disk_ok)

    agents = list_agents()
    add("Configured agents", "PASS" if agents else "WARN", ", ".join(agents) if agents else "none")

    if skip_model:
        add("Model connectivity", "WARN", "skipped by --skip-model")
    else:
        selected_model, source = _resolve_doctor_model(agent_name, model_string)
        if not selected_model:
            add("Model connectivity", "WARN", f"not checked: {source}")
        else:
            runtime_ok, runtime_detail = _check_model_runtime(selected_model)
            add(
                "Model runtime",
                "PASS" if runtime_ok else "FAIL",
                f"{selected_model}: {runtime_detail}",
                fail=not runtime_ok,
            )
            if not runtime_ok:
                add("Model connectivity", "WARN", f"skipped: {runtime_detail}")
                return rows, hard_fail

            ok, detail = _probe_model_connectivity(selected_model)
            add(
                "Model connectivity",
                "PASS" if ok else "FAIL",
                f"{selected_model} via {source}: {detail}",
                fail=not ok,
            )

    return rows, hard_fail


# ─────────────────────────────────────── setup ────────────────────────────────


@cli.command(
    epilog=(
        "Examples:\n"
        "  olano setup\n"
        "  olano --home ~/.lionclaw setup                  # a pre-rename home\n"
        "  olano --home ~/client-a-olano setup --yes --mode general --agent-name Merli\n"
        "  olano setup --mode specialized --archetype lionDev --agent-name Coder"
    ),
)
@click.option(
    "--yes", "-y", is_flag=True, help="Skip interactive prompts and use sensible defaults"
)
@click.option(
    "--provider",
    type=click.Choice(_SETUP_PROVIDER_CHOICES),
    default=None,
    help="Model provider to configure",
)
@click.option("--model", default=None, help="Model string to use for installed agents")
@click.option("--api-key", default=None, help="API key for the selected provider")
@click.option(
    "--safety-preset",
    type=click.Choice(sorted(_SECURITY_PRESETS)),
    default=None,
    help="Security preset to apply to newly installed agents",
)
@click.option(
    "--mode",
    type=click.Choice(("general", "specialized", "custom")),
    default=None,
    help="Initial agent setup mode",
)
@click.option(
    "--archetype", default=None, help="Starter agent template to install for specialized mode"
)
@click.option("--agent-name", default=None, help="Initial agent name (default: Merli)")
@click.option("--emoji", default=None, help="Initial agent emoji")
@click.option("--skip-verify", is_flag=True, help="Skip the final model connectivity check")
@click.option(
    "--reconfigure",
    is_flag=True,
    help="Re-run model picker on an existing install without recreating the agent",
)
def setup(
    yes: bool,
    provider: str | None,
    model: str | None,
    api_key: str | None,
    safety_preset: str | None,
    mode: str | None,
    archetype: str | None,
    agent_name: str | None,
    emoji: str | None,
    skip_verify: bool,
    reconfigure: bool,
) -> None:
    """Interactive first-run setup wizard for one Olano home and its first agent."""
    from olano.archetypes.installer import (
        list_archetypes,
        load_archetype_template,
    )
    from olano.models import PROVIDERS, parse_model_string

    home_paths = ensure_olano_home()
    home_config = load_home_config()

    console.print()
    console.print(
        Panel(
            "[bold #7cc6ff]🦁  Welcome to Olano[/bold #7cc6ff]\n\n"
            "[#dbeafe]Setup for your Olano home, vault, and agent.[/#dbeafe]",
            border_style=_SETUP_BORDER,
            padding=(1, 4),
        )
    )
    console.print()
    console.print(f"[green]✓[/green]  Olano home: [{_SETUP_HINT}]{home_paths.root}[/{_SETUP_HINT}]")

    # Best-effort fetch of the canonical archetype pack so first-run users get
    # the full catalog without an extra step. Non-fatal — bundled archetypes
    # always work regardless of whether this clone succeeds.
    from olano.archetypes.sync import fetch_lionclaw_agents, is_pack_installed

    if not is_pack_installed():
        ok, msg = fetch_lionclaw_agents(quiet=True)
        if ok and "skipping" not in msg.lower():
            console.print(f"[green]✓[/green]  Archetype pack: [{_SETUP_HINT}]{msg}[/{_SETUP_HINT}]")
        elif not ok:
            console.print(
                f"[yellow]·[/yellow]  Archetype pack skipped: [{_SETUP_HINT}]{msg}[/{_SETUP_HINT}]"
            )
            console.print(
                "   [dim]Run `olano archetypes update --url <repo>` later to install.[/dim]"
            )
    console.print()

    default_name, default_timezone = _discover_identity_defaults()
    existing_agents = _agent_names_in_home(home_paths)
    existing_action = "create"
    editing_agent_name: str | None = None
    initial_state: dict[str, Any] = {}

    if reconfigure and home_config.default_agent in existing_agents:
        # --reconfigure: skip new-home prompts; pre-fill from existing agent
        try:
            initial_state = _infer_setup_defaults_from_agent(home_config.default_agent)
            editing_agent_name = home_config.default_agent
        except FileNotFoundError:
            pass
    elif not yes and _home_has_existing_state(home_paths):
        existing_action = _prompt_existing_home_action(home_paths, home_config) or ""
        if not existing_action:
            return
        if existing_action == "modify":
            editing_agent_name = _select_existing_agent(existing_agents, home_config.default_agent)
            initial_state = _infer_setup_defaults_from_agent(editing_agent_name)
        elif home_config.default_agent in existing_agents:
            try:
                initial_state = _infer_setup_defaults_from_agent(home_config.default_agent)
                initial_state.pop("editing_agent", None)
                initial_state["agent_name"] = agent_name or ""
            except FileNotFoundError:
                initial_state = {}

    if yes:
        user_name = default_name
        timezone = default_timezone
        preferences = ""
    available_archetypes = list_archetypes()
    preferred_home_model = home_config.preferred_model
    inferred_provider, _ = parse_model_string(model or preferred_home_model or "")
    if inferred_provider not in _SETUP_PROVIDER_CHOICES:
        inferred_provider = None

    if yes:
        selected_provider = provider or inferred_provider or "anthropic"
        selected_model = _normalize_model_string(
            selected_provider,
            model or preferred_home_model or _default_model_for_provider(selected_provider),
        )
        selected_mode = mode or "general"
        selected_secrets_strategy = "vault"
        vault_passphrase = None
        imported_env_vars: list[str] = []
        selected_archetype = archetype
        if selected_mode == "general":
            selected_archetype = "merli"
        elif selected_mode == "specialized" and not selected_archetype:
            selected_archetype = _default_specialist_archetype()
        entered_agent_name = agent_name or (
            "Merli" if selected_mode == "general" else selected_archetype or "Agent"
        )
        if selected_mode == "specialized" and not emoji and selected_archetype:
            specialist_template = load_archetype_template(selected_archetype)
            entered_emoji = specialist_template.get("emoji", "🤖")
        else:
            entered_emoji = emoji or ("🦁" if selected_mode in {"general", "custom"} else "🤖")
        preset = safety_preset or "developer"
        selected_api_key = api_key or (
            os.environ.get(PROVIDERS[selected_provider].env_var or "")
            if PROVIDERS[selected_provider].env_var
            else None
        )
        custom_description = "General-purpose Olano assistant"
    else:
        if _can_use_fullscreen_setup_wizard() and not editing_agent_name and not initial_state:
            try:
                wizard_state, textual_skip_verify = _run_textual_setup_wizard(
                    home=home_paths.root,
                    provider=provider or inferred_provider,
                    model=model or preferred_home_model,
                    api_key=api_key,
                    safety_preset=safety_preset,
                    mode=mode,
                    archetype=archetype,
                    agent_name=agent_name,
                    emoji=emoji,
                )
            except Exception as exc:
                raise click.ClickException(f"Textual setup wizard failed: {exc}") from exc
            skip_verify = skip_verify or textual_skip_verify
        else:
            wizard_state = _prompt_interactive_setup_legacy(
                provider=provider or inferred_provider,
                model=model or preferred_home_model,
                api_key=api_key,
                safety_preset=safety_preset,
                mode=mode,
                archetype=archetype,
                agent_name=agent_name,
                emoji=emoji,
                available_archetypes=available_archetypes,
                default_name=initial_state.get("user_name") or default_name,
                default_timezone=initial_state.get("timezone") or default_timezone,
                initial_state=initial_state,
                editing_existing=bool(editing_agent_name),
                existing_agents=existing_agents,
            )

        if not wizard_state:
            return
        user_name = wizard_state["user_name"]
        timezone = wizard_state["timezone"]
        preferences = wizard_state["preferences"]
        selected_secrets_strategy = wizard_state.get("secrets_strategy") or "vault"
        vault_passphrase = wizard_state.get("vault_passphrase") or None
        imported_env_vars = list(wizard_state.get("import_env_vars") or [])
        selected_provider = wizard_state["provider"]
        selected_model = _normalize_model_string(selected_provider, wizard_state["model"])
        selected_api_key = wizard_state["api_key"] or None
        preset = wizard_state["preset"]
        selected_mode = wizard_state["mode"]
        selected_archetype = wizard_state["archetype"] or (
            "merli" if selected_mode == "general" else None
        )
        entered_agent_name = editing_agent_name or wizard_state["agent_name"]
        entered_emoji = wizard_state["emoji"]
        custom_description = wizard_state["description"]
        custom_tools = list(wizard_state.get("custom_tools") or [])
        custom_subagents = list(wizard_state.get("custom_subagents") or [])
        custom_system_prompt = wizard_state.get("system_prompt") or ""

    if yes:
        custom_tools = (
            ["safe_shell", "node_run", "web_search", "page_read", "http_request"]
            if selected_mode == "custom"
            else []
        )
        custom_subagents = []
        custom_system_prompt = f"You are {entered_agent_name}, a helpful AI assistant."

    active_vault_mode = "disabled"
    if selected_secrets_strategy == "vault":
        try:
            vault_status, active_vault_mode = _ensure_vault_initialized(
                vault_passphrase, allow_ephemeral=yes
            )
            console.print(f"[green]✓[/green]  {vault_status}")
            if imported_env_vars:
                imported = _import_env_credentials_to_vault(
                    imported_env_vars, vault_passphrase=vault_passphrase
                )
                if imported:
                    console.print(
                        f"[green]✓[/green]  Imported {len(imported)} environment secret"
                        f"{'s' if len(imported) != 1 else ''} into the local encrypted vault."
                    )
            if active_vault_mode == "passphrase":
                console.print(
                    "[#dbeafe]Keep that passphrase safe. "
                    "You will need it again unless you export OLANO_VAULT_KEY in your shell.[/#dbeafe]"
                )
            elif active_vault_mode == "ephemeral":
                console.print(
                    "[yellow]⚠[/yellow]  This vault is temporary for the current session only. "
                    "Configure keyring or OLANO_VAULT_KEY for persistent secure storage."
                )
        except Exception as e:
            active_vault_mode = "ephemeral"
            console.print(f"[yellow]⚠[/yellow]  Vault initialization skipped: {e}")
        console.print()
    elif selected_secrets_strategy == "env":
        console.print(
            "[green]✓[/green]  Secrets strategy: [bold]environment variables[/bold]. "
            "Olano will read provider credentials from your shell or home .env when available."
        )
        console.print()
    else:
        console.print(
            "[yellow]⚠[/yellow]  Secrets will be configured later. "
            "Cloud model verification may be skipped and chat may not work until credentials are added."
        )
        console.print()

    info = PROVIDERS[selected_provider]
    if selected_api_key:
        storage_result = _store_provider_credential(
            selected_provider,
            selected_api_key,
            vault_passphrase=vault_passphrase,
            prefer_vault=selected_secrets_strategy == "vault" and active_vault_mode != "ephemeral",
        )
        console.print(f"[green]✓[/green]  {storage_result}")
    elif info.env_var and os.environ.get(info.env_var):
        console.print(
            f"[green]✓[/green]  Using existing [bold]{info.env_var}[/bold] from your shell."
        )
    elif info.env_var:
        console.print(
            f"[yellow]⚠[/yellow]  {info.env_var} is not configured yet. "
            "Model verification may fail."
        )
    else:
        console.print(f"[green]✓[/green]  {info.name} does not require an API key")
    console.print()

    console.print(
        f"[green]✓[/green]  Safety level: [bold]{_safety_label(preset)}[/bold] "
        f"[{_SETUP_HINT}]({preset})[/{_SETUP_HINT}]"
    )
    console.print()

    archetype_main_model: str | None = selected_model
    archetype_subagent_models: dict[str, str | None] = {}
    force_all_subagent_models = selected_mode in {"general", "specialized"}

    verification_model = archetype_main_model or selected_model
    verification_provider, _ = parse_model_string(verification_model)
    can_verify_model = _credentials_available_for_setup(
        verification_provider or selected_provider,
        secrets_strategy=selected_secrets_strategy,
        selected_api_key=selected_api_key
        if (verification_provider or selected_provider) == selected_provider
        else None,
        imported_env_vars=imported_env_vars,
    )

    agent_id = editing_agent_name or _slugify_agent_name(entered_agent_name) or "merli"
    while (
        not yes
        and not editing_agent_name
        and (
            (home_paths.agents_dir / agent_id / "agent.json5").exists()
            or (home_paths.agents_dir / agent_id / "agent.json5").exists()
        )
    ):
        console.print()
        console.print(
            f"[yellow]⚠[/yellow]  Agent name '[bold]{entered_agent_name}[/bold]' is already in use."
        )
        entered_agent_name = _setup_prompt(
            "  Choose a different agent name",
            default=_next_available_agent_name(entered_agent_name, existing_agents),
        )
        agent_id = _slugify_agent_name(entered_agent_name) or "merli"
    installed_path: Path

    try:
        if selected_mode in {"general", "specialized"}:
            installed_path = _install_or_update_archetype_agent(
                archetype_name=selected_archetype or "merli",
                agent_id=agent_id,
                emoji=entered_emoji,
                security_preset=preset,
                overwrite_existing=bool(editing_agent_name),
                main_model=archetype_main_model,
                subagent_models=archetype_subagent_models,
                force_all_subagents=force_all_subagent_models,
                display_name=(
                    entered_agent_name
                    if not editing_agent_name
                    and entered_agent_name
                    and _slugify_agent_name(entered_agent_name) != entered_agent_name
                    else ""
                ),
            )
        else:
            description = "General-purpose Olano assistant" if yes else custom_description
            installed_path = _build_custom_agent(
                agent_name=agent_id,
                model=selected_model,
                description=description,
                emoji=entered_emoji or "🦁",
                security_preset=preset,
                system_prompt=custom_system_prompt,
                tools=custom_tools,
                subagents=custom_subagents,
                overwrite_existing=bool(editing_agent_name),
                display_name=(
                    entered_agent_name
                    if not editing_agent_name
                    and entered_agent_name
                    and _slugify_agent_name(entered_agent_name) != entered_agent_name
                    else ""
                ),
            )
    except FileExistsError as e:
        console.print(f"[yellow]{e}[/yellow]")
        sys.exit(1)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)

    workspace_root = installed_path.parent / "workspace"
    _write_setup_profile(workspace_root, user_name, timezone, preferences, include_name=True)
    action_word = "updated" if editing_agent_name else "installed"
    console.print(
        f"[green]✓[/green]  Agent {action_word} at [{_SETUP_HINT}]{installed_path.parent}[/{_SETUP_HINT}]"
    )
    console.print(
        f"[green]✓[/green]  Workspace ready at [{_SETUP_HINT}]{workspace_root}[/{_SETUP_HINT}]"
    )

    save_home_config(
        HomeConfig(
            default_agent=agent_id,
            preferred_model=verification_model,
            runtime_api_port=home_config.runtime_api_port,
            dashboard_port=home_config.dashboard_port,
        )
    )

    # Sync built-in skills into the home's managed skills dir
    try:
        from olano.skills.built_in_sync import sync_builtin_skills

        skill_report = sync_builtin_skills(home_paths.root)
        if skill_report.new or skill_report.updated:
            console.print(f"[green]✓[/green]  {skill_report}")
    except Exception:
        pass  # non-fatal

    # Overlay the canonical external skills catalog (olano-ai/skills) into the
    # same common skills dir. Non-fatal — bundled skills already work if this
    # clone can't run (no git/network).
    try:
        from olano.skills.sync import fetch_olano_skills

        ok, msg = fetch_olano_skills(home_paths.root, quiet=True)
        if ok and "skipping" not in msg.lower():
            console.print(f"[green]✓[/green]  Skills catalog: [{_SETUP_HINT}]{msg}[/{_SETUP_HINT}]")
    except Exception:
        pass  # non-fatal

    console.print()

    should_skip_verify = skip_verify or not can_verify_model
    if not should_skip_verify:
        ok, detail = _probe_model_connectivity(verification_model)
        status = "[green]✓[/green]" if ok else "[yellow]⚠[/yellow]"
        console.print(f"{status}  Verification: [bold]{verification_model}[/bold] → {detail}")
        console.print()
    elif not skip_verify and not can_verify_model:
        console.print(
            f"[yellow]⚠[/yellow]  Verification skipped: [bold]{verification_model}[/bold] credentials are not configured yet."
        )
        console.print()

    command_prefix = _setup_command_prefix(home_paths.root)
    run_doctor = not yes and _can_run_post_setup_doctor()

    console.print(
        Panel(
            "\n".join(
                [
                    f"[bold #7cc6ff]{entered_emoji} {agent_id}[/bold #7cc6ff] is ready.",
                    "",
                    "[bold]Home:[/bold]",
                    f"  {home_paths.root}",
                    "",
                    "[bold]Commands:[/bold]",
                    f"  {command_prefix} dashboard",
                    f"  {command_prefix} doctor --agent {agent_id}",
                    f"  {command_prefix} agents list",
                ]
            ),
            title="Setup Complete",
            border_style=_SETUP_BORDER,
            padding=(1, 2),
        )
    )
    console.print()

    if run_doctor:
        console.print(f"[bold {_SETUP_TITLE}]Running doctor for {agent_id}…[/bold {_SETUP_TITLE}]")
        rows, hard_fail = _run_doctor_checks(agent_id, verification_model, should_skip_verify)
        _render_doctor_report(rows, hard_fail)
        console.print()
        if hard_fail:
            console.print(
                f"[yellow]⚠[/yellow]  Doctor reported a blocking issue for [bold]{agent_id}[/bold]; fix it before opening the dashboard."
            )
        else:
            console.print(
                f"Chat with [bold]{agent_id}[/bold] in the dashboard: [bold]{command_prefix} dashboard[/bold]"
            )


# ─────────────────────────────────────── quick-setup ──────────────────────────


@cli.command(
    name="quick-setup",
    epilog=(
        "Examples:\n"
        "  olano quick-setup --yes\n"
        "  LIONCLAW_HOME=/tmp/lc olano quick-setup --yes\n"
        "  olano quick-setup -y --admin-email me@example.com --admin-password 's3cret!'\n"
    ),
)
@click.option(
    "--yes", "-y", is_flag=True, help="Run fully non-interactive using the defaults below"
)
@click.option(
    "--admin-email",
    default=None,
    envvar="LIONCLAW_ADMIN_EMAIL",
    help="Admin login email (env: LIONCLAW_ADMIN_EMAIL; default: admin@example.com)",
)
@click.option(
    "--admin-password",
    default=None,
    envvar="LIONCLAW_ADMIN_PASSWORD",
    help=(
        "Admin password (env: LIONCLAW_ADMIN_PASSWORD). Default: a one-time "
        "password is generated, printed once at the end, and kept under "
        "<home>/run/initial-admin-password until the account's password changes."
    ),
)
@click.option(
    "--admin-name",
    default=None,
    envvar="LIONCLAW_ADMIN_NAME",
    help="Admin display name (env: LIONCLAW_ADMIN_NAME; default: Admin)",
)
@click.option(
    "--vault-password",
    default=None,
    envvar="LIONCLAW_VAULT_PASSWORD",
    help=(
        "Vault passphrase (env: LIONCLAW_VAULT_PASSWORD). Default: OLANO_VAULT_KEY "
        "when set, else a random passphrase stored in the vault's boot-key file so "
        "'olano serve' unlocks the vault unattended."
    ),
)
@click.option(
    "--agent",
    default=None,
    envvar="LIONCLAW_DEFAULT_AGENT",
    help=(
        "Archetype to install as default agent (env: LIONCLAW_DEFAULT_AGENT; "
        "default: merli). Managed cloud deployments set this to 'agentfather' "
        "via cloud-init so the deployment conductor is the first agent."
    ),
)
@click.option(
    "--skip-catalogs",
    is_flag=True,
    envvar="LIONCLAW_QUICK_SETUP_SKIP_CATALOGS",
    help=(
        "Skip the built-in skill sync and the cloud skills/archetype catalog "
        "fetches (steps 6 and 7), leaving them to the running server. Pass this "
        "when the runtime is ALREADY UP — every deploy script waits for "
        "'Runtime API is up' before calling quick-setup, and a running server "
        "syncs built-in skills through run_startup_upgrades and both cloud "
        "catalogs through CatalogSyncService ~20s after boot. Without the flag "
        "the same ~8 MB of catalogs is downloaded twice, once here in the "
        "foreground and again by that service. Leave it off for a CLI install "
        "with no server running: then this is the only thing that populates "
        "them (env: LIONCLAW_QUICK_SETUP_SKIP_CATALOGS)."
    ),
)
def quick_setup(
    yes: bool,
    admin_email: str | None,
    admin_password: str | None,
    admin_name: str | None,
    vault_password: str | None,
    agent: str | None,
    skip_catalogs: bool,
) -> None:
    """Zero-to-usable non-interactive bootstrap (home + workspace + agent + admin + vault).

    Idempotent: safe to re-run; steps already done are skipped. Intended for
    fresh Docker/EC2 deploys so the backend isn't empty on first boot.
    """
    from olano.cli.quick_setup import (
        DEFAULT_ADMIN_EMAIL,
        DEFAULT_ADMIN_NAME,
        DEFAULT_AGENT,
        run_quick_setup,
        vault_key_for_runtime,
    )

    email = admin_email or DEFAULT_ADMIN_EMAIL
    name = admin_name or DEFAULT_ADMIN_NAME
    agent_name = agent or DEFAULT_AGENT

    if not yes:
        console.print(
            "quick-setup runs non-interactively: it installs the default agent, "
            "creates the admin account (with a generated one-time password unless "
            "you pass one) and initializes the vault. Pass [bold]--yes[/bold] to skip "
            "this prompt."
        )
        if not click.confirm("  Proceed with quick-setup?", default=False):
            console.print("[yellow]Cancelled.[/yellow]")
            return

    console.print("[bold #7cc6ff]🦁  Olano quick-setup[/bold #7cc6ff]")

    try:
        result = run_quick_setup(
            admin_email=email,
            admin_password=admin_password or None,
            admin_name=name,
            vault_password=vault_password or None,
            agent=agent_name,
            sync_catalogs=not skip_catalogs,
            log=lambda msg: console.print(f"[green]✓[/green] {msg}"),
        )
    except Exception as exc:  # pragma: no cover - surfaced to operator
        console.print(f"[red]quick-setup failed:[/red] {exc}")
        raise SystemExit(1) from exc

    summary = [
        f"[bold #7cc6ff]{result.agent}[/bold #7cc6ff] is ready to serve.",
        "",
        f"[bold]Home:[/bold]  {result.home}",
        f"[bold]Admin:[/bold] {result.admin_email}",
    ]
    if result.generated_admin_password:
        # Printed exactly once. The note on disk is what a deploy banner points
        # at, and the first password change removes it.
        summary += [
            "[bold]One-time password:[/bold] "
            f"[bold cyan]{result.generated_admin_password}[/bold cyan]",
            "",
            "Sign in with it, then set your own password from the account menu.",
        ]
        if result.initial_password_path is not None:
            summary.append(
                "Until then a copy is kept, readable by this user only, at "
                f"{result.initial_password_path}"
            )
    if result.vault_passphrase_generated and result.boot_key_path is not None:
        summary += [
            "",
            "[bold]Vault:[/bold] a random passphrase was generated; [bold]olano serve[/bold] "
            f"unlocks the vault from {result.boot_key_path}. To choose your own, "
            "rekey the vault from the dashboard (Config, Vault).",
        ]
    summary += ["", "Start the runtime with: [bold]olano serve[/bold]"]
    console.print()
    console.print(
        Panel(
            "\n".join(summary),
            title="Quick-setup complete",
            border_style="#5f87ff",
            padding=(1, 2),
        )
    )

    # A passphrase the operator chose reaches the runtime through
    # OLANO_VAULT_KEY, and a CLI process cannot persist an env var to the
    # parent shell, so tell them. A generated one is in the boot-key file already.
    vault_key = vault_key_for_runtime(vault_password or None)
    if vault_key is not None:
        console.print()
        console.print(
            "[bold]Vault key:[/bold] export it so [bold]olano serve[/bold] can unlock the vault:"
        )
        console.print(f"  [cyan]export OLANO_VAULT_KEY={vault_key}[/cyan]")

    # A fenced step that raised (agent install refused, vault passphrase
    # rejected) is reported here and exits non-zero, AFTER the admin account
    # has landed and been announced above — the deploy scripts treat a
    # non-zero quick-setup as "continue", and a re-run retries exactly the
    # steps that failed.
    failed = result.failed_steps()
    if failed:
        console.print()
        console.print(
            Panel(
                "\n".join(f"[bold]{label}[/bold]: {reason}" for label, reason in failed)
                + "\n\nEverything else above is in place. Re-run quick-setup to retry "
                "the failed step(s); the runtime also retries a failed agent install "
                "at its next start.",
                title="Some steps failed",
                border_style="yellow",
                padding=(1, 2),
            )
        )
        raise SystemExit(1)


# ─────────────────────────────────────── ACP ──────────────────────────────────


@cli.command()
@click.option("--agent", "-a", required=True, help="Agent name")
@click.option("--http", is_flag=True, help="Run HTTP/SSE mode instead of stdio")
@click.option("--port", default=8765, help="Port for HTTP mode")
def acp(agent: str, http: bool, port: int) -> None:
    """Start an ACP server for an agent (editor integration)."""
    from olano.acp.server import ACPServer
    from olano.config.loader import load_agent_config
    from olano.core.builder import build_agent_async, build_agent_with_mcp

    config = load_agent_config(agent)

    async def _run_acp() -> None:
        builder = build_agent_with_mcp if config.mcp_servers else build_agent_async
        ag = await _run_quietly_async(builder, config)
        server = ACPServer(ag, agent)

        if http:
            console.print(f"[green]ACP HTTP server for '{agent}' on port {port}[/green]")
            await server.run_http(port=port)
        else:
            console.print(f"[green]ACP stdio server for '{agent}'[/green]")
            await server.run_stdio()

    asyncio.run(_run_acp())


# ─────────────────────────────────────── schedule ─────────────────────────────


@cli.group()
def schedule() -> None:
    """Manage scheduled jobs."""


@schedule.command(name="list")
def schedule_list() -> None:
    """List all scheduled jobs across all agents."""
    from olano.config.loader import list_agents, load_agent_config

    table = Table(title="Scheduled Jobs", show_header=True)
    table.add_column("Agent")
    table.add_column("Cron")
    table.add_column("Message")

    for name in list_agents():
        try:
            cfg = load_agent_config(name)
            for job in cfg.schedule:
                cron = (
                    job.cron or f"{job.minute} {job.hour} {job.day} {job.month} {job.day_of_week}"
                )
                table.add_row(f"{cfg.emoji} {cfg.name}", cron, job.message[:60])
        except Exception:
            pass

    console.print(table)


# ─────────────────────────────────────── doctor ───────────────────────────────


@cli.command(
    epilog=(
        "Examples:\n"
        "  olano doctor\n"
        "  olano doctor --agent merli\n"
        "  olano doctor --model anthropic:claude-sonnet-4-6\n"
        "  olano doctor --skip-model"
    ),
)
@click.option(
    "--agent", "agent_name", default=None, help="Agent name to use for model connectivity checks"
)
@click.option("--model", "model_string", default=None, help="Explicit model string to validate")
@click.option("--skip-model", is_flag=True, help="Skip the live model connectivity check")
def doctor(agent_name: str | None, model_string: str | None, skip_model: bool) -> None:
    """Run a full health check across runtime dependencies, workspace, vault, and model access."""
    rows, hard_fail = _run_doctor_checks(agent_name, model_string, skip_model)
    _render_doctor_report(rows, hard_fail)
    if hard_fail:
        sys.exit(1)


@cli.command(
    name="mem",
    epilog=(
        "Examples:\n"
        "  olano mem                     # report the running daemon's memory\n"
        "  olano mem --json              # raw JSON\n"
        "  olano mem --monitor on        # start the background sampler\n"
        "  olano mem --monitor off       # stop it (also stops tracemalloc)"
    ),
)
@click.option("--json", "as_json", is_flag=True, help="Emit the raw JSON report")
@click.option("--no-gc", is_flag=True, help="Skip the gc object-type census")
@click.option("--no-tracemalloc", is_flag=True, help="Skip tracemalloc allocations")
@click.option(
    "--monitor",
    "monitor_action",
    default=None,
    type=click.Choice(
        ["on", "off", "status", "tracemalloc-on", "tracemalloc-off"], case_sensitive=False
    ),
    help="Toggle the background memory monitor instead of printing a report",
)
def mem(as_json: bool, no_gc: bool, no_tracemalloc: bool, monitor_action: str | None) -> None:
    """Show process memory + a breakdown of olano's in-process structures.

    Reports the running ``olano serve`` daemon's memory when it's up (via the
    runtime API); otherwise a local snapshot. ``--monitor on|off`` turns the
    background sampler + tracemalloc on/off at runtime (off frees the monitor's
    own memory).
    """
    from olano import admin

    if monitor_action:
        state = admin.set_memory_monitor(monitor_action.lower())
        if as_json:
            console.print_json(data=state)
        else:
            err = f"  [red]{state['error']}[/red]" if state.get("error") else ""
            console.print(
                f"Memory monitor: {'ON' if state.get('enabled') else 'off'} "
                f"· sample every {state.get('interval_seconds')}s "
                f"· tracemalloc {'on' if state.get('tracemalloc') else 'off'}{err}"
            )
        return

    from olano.admin.memory import render_memory_report_text

    report = admin.memory_report(include_gc=not no_gc, include_tracemalloc=not no_tracemalloc)
    if as_json:
        console.print_json(data=report)
    else:
        console.print(render_memory_report_text(report), highlight=False)


@cli.command(
    name="disk",
    epilog=(
        "Examples:\n"
        "  olano disk                    # usage + monitor state (daemon-aware)\n"
        "  olano disk --json             # raw JSON report\n"
        "  olano disk --monitor off      # disable automatic cleanup\n"
        "  olano disk --monitor on       # re-enable automatic cleanup\n"
        "  olano disk --clean            # run the enabled cleanup actions now\n"
        "  olano disk --clean --yes      # ...without the confirmation prompt\n"
        "  olano disk --scan             # start the directory-size scan now"
    ),
)
@click.option("--json", "as_json", is_flag=True, help="Emit the raw JSON report")
@click.option(
    "--monitor",
    "monitor_action",
    default=None,
    type=click.Choice(["on", "off", "status"], case_sensitive=False),
    help="Toggle automatic cleanup (persists to config.yaml; applies within one check interval)",
)
@click.option("--clean", "clean_now", is_flag=True, help="Run the enabled cleanup actions now")
@click.option("--yes", "assume_yes", is_flag=True, help="Skip the --clean confirmation prompt")
@click.option(
    "--scan",
    "scan_now",
    is_flag=True,
    help="Start the directory-size scan now (where the bytes are, not just how full)",
)
def disk(
    as_json: bool, monitor_action: str | None, clean_now: bool, assume_yes: bool, scan_now: bool
) -> None:
    """Show disk usage + the disk monitor's state, toggle automatic cleanup,
    or run a cleanup now.

    The runtime alerts from the warn threshold and, at the cleanup threshold,
    runs the allow-listed cleanup actions automatically — all of them by
    default, including the aggressive ``docker_prune``; deselect actions in
    the dashboard Config → Disk section or the ``disk_monitor.cleanup.actions``
    config block. Reports the running daemon's view when it's up.
    """
    import time as _time

    from olano import admin
    from olano.admin.disk import render_disk_report_text

    if monitor_action:
        state = admin.set_disk_monitor(monitor_action.lower())
        if as_json:
            console.print_json(data=state)
        else:
            err = f"  [red]{state['error']}[/red]" if state.get("error") else ""
            cleanup = (state.get("settings") or {}).get("cleanup") or {}
            actions = ", ".join(cleanup.get("actions") or []) or "none"
            console.print(
                f"Automatic disk cleanup: {'ON' if cleanup.get('enabled') else 'off'} "
                f"· actions: {actions}{err}"
            )
        return

    if scan_now:
        state = admin.set_disk_monitor("scan")
        if state.get("scanning"):
            console.print("Directory scan started (background; throttled + niced)…")
            for _ in range(900):  # cap the poll at ~30 min
                _time.sleep(2)
                if not admin.disk_report().get("scan_running", False):
                    break
        elif state.get("error") == "scan already running":
            console.print("A directory scan is already running — showing the last snapshot.")
        else:
            # No daemon — walk from this process with the same settings.
            from olano.config.home import get_olano_home
            from olano.config.system_settings import load_disk_monitor_settings
            from olano.runtime import disk_scan as _disk_scan

            console.print("No daemon running — scanning from this process…")
            scan_cfg = load_disk_monitor_settings().scan
            home = get_olano_home()
            result = _disk_scan.scan_directory_sizes(
                home,
                depth=scan_cfg.depth,
                agent_depth=scan_cfg.agent_depth,
                time_budget_seconds=scan_cfg.time_budget_seconds,
                throttle_ms=scan_cfg.throttle_ms,
                max_entries=scan_cfg.max_entries,
            )
            _disk_scan.save_snapshot(home, result)

    report = admin.disk_report()

    if clean_now:
        settings = report.get("settings") or {}
        selected_actions = (settings.get("cleanup") or {}).get("actions") or []
        if not selected_actions:
            console.print(
                "[yellow]No cleanup actions selected "
                "(disk_monitor.cleanup.actions is empty).[/yellow]"
            )
            return
        if not assume_yes:
            console.print("Will run: " + ", ".join(selected_actions))
            if not click.confirm("Run disk cleanup now?"):
                return
        if report.get("source") == "local":
            # No daemon — run the cleanup in this process.
            from datetime import datetime, timezone

            from olano.config.home import get_olano_home
            from olano.config.system_settings import load_disk_monitor_settings
            from olano.runtime import disk_monitor as dm_engine

            console.print("No daemon running — cleaning up from this process…")
            dm_settings = load_disk_monitor_settings()
            home = get_olano_home()
            now = datetime.now(timezone.utc)
            rep = dm_engine.run_cleanup(settings=dm_settings, home=home, trigger="manual", now=now)
            spath = dm_engine.state_path(home)
            st = dm_engine.load_state(spath)
            st["last_cleanup_at"] = now.isoformat()
            st["last_report"] = rep.to_dict()
            dm_engine.save_state(spath, st)
            if as_json:
                console.print_json(data=rep.to_dict())
            else:
                console.print(str(rep))
            return
        state = admin.set_disk_monitor("run")
        if not state.get("running"):
            console.print(f"[red]{state.get('error') or 'could not start cleanup'}[/red]")
            return
        console.print("Cleanup started in the running daemon (this can take minutes)…")
        for _ in range(900):  # cap the poll at ~30 min
            _time.sleep(2)
            status = admin.set_disk_monitor("status")
            if not status.get("cleanup_running", False):
                break
        report = admin.disk_report()

    if as_json:
        console.print_json(data=report)
    else:
        console.print(render_disk_report_text(report), highlight=False)


@cli.command(
    name="backfill-credits",
    epilog=(
        "Examples:\n"
        "  # Preview what would be reconstructed (writes nothing)\n"
        "  olano backfill-credits --dry-run\n"
        "  # Run it now instead of waiting for the server's background pass\n"
        "  olano backfill-credits\n"
        "  # Ignore the live-recording cutoff day (per-thread dedup still applies)\n"
        "  olano backfill-credits --force"
    ),
)
@click.option("--dry-run", is_flag=True, help="Report what would be written without writing")
@click.option(
    "--force",
    is_flag=True,
    help=(
        "Also consider threads on/after the day live cognition recording began "
        "(normally skipped so the boundary day can't double count). The "
        "per-thread dedup still prevents any duplicate rows."
    ),
)
def backfill_credits(dry_run: bool, force: bool) -> None:
    """Reconstruct historical cognition credits into the local usage ledger.

    Cognition ran on raw checkpoint threads before the ledger learned to record
    it, so the Insights charts had no credits history for it. This mines those
    `cog::` threads and writes one back-dated ledger row per run — replaying the
    gateway's real per-call charges where the checkpoints kept them, estimating
    from tokens at the tier's published rates otherwise. Idempotent (per-thread
    dedup); rows are marked local-only so the cloud is never double-billed. The
    runtime server also runs this automatically in the background until done.
    """
    from olano.analytics.backfill import backfill_cognition_credits

    stats = backfill_cognition_credits(dry_run=dry_run, force=force)
    verb = "Would write" if dry_run else "Wrote"
    click.echo(
        f"{verb} {stats['written']} ledger row(s) from {stats['cog_threads']} cognition "
        f"thread(s) — {stats['replayed']} replayed from gateway charges, "
        f"{stats['estimated']} estimated from tokens."
    )
    skipped = {
        "already backfilled": stats["skipped_dedup"],
        "after live-recording cutoff": stats["skipped_cutoff"],
        "no run day": stats["skipped_blank_day"],
        "BYOK parent": stats["skipped_byok"],
        "empty run": stats["skipped_empty"],
    }
    for reason, count in skipped.items():
        if count:
            click.echo(f"  skipped {count}: {reason}")
    if not stats["complete"]:
        click.echo("Partial pass (mining still in progress) — run again to continue.")


@cli.command(
    name="migrate-checkpoints",
    epilog=(
        "Examples:\n"
        "  # Migrate the default home's threads.db into the configured Postgres\n"
        "  LIONCLAW_CHECKPOINT_DB_URL=postgresql://olano:<password>@localhost:5432/olano \\\n"
        "    olano migrate-checkpoints\n"
        "  # Explicit source + destination, preview only\n"
        "  olano migrate-checkpoints --from ~/.olano/sessions/threads.db \\\n"
        "    --to-url postgresql://user:pw@host/db --dry-run\n"
        "  # Bind existing checkpoint keys to their owning agent (preview first)\n"
        "  olano migrate-checkpoints --scope-by-agent --dry-run\n"
        "  olano migrate-checkpoints --scope-by-agent"
    ),
)
@click.option(
    "--from",
    "source",
    default=None,
    help="Source SQLite threads.db (default: the active home's sessions/threads.db)",
)
@click.option(
    "--to-url",
    "dest_url",
    default=None,
    help="Destination Postgres URL (default: LIONCLAW_CHECKPOINT_DB_URL / config.yaml)",
)
@click.option(
    "--scope-by-agent",
    is_flag=True,
    help=(
        "Rewrite legacy unscoped checkpoint keys to <agent>::<thread_id> so agents "
        "never share a lineage (SQLite, in place). Renames single-agent threads, "
        "resets cross-agent-contaminated ones."
    ),
)
@click.option(
    "--drop-orphans",
    is_flag=True,
    help="With --scope-by-agent: also delete checkpoints that have no owning transcript.",
)
@click.option("--dry-run", is_flag=True, help="Report what would be copied without writing")
def migrate_checkpoints(
    source: str | None,
    dest_url: str | None,
    scope_by_agent: bool,
    drop_orphans: bool,
    dry_run: bool,
) -> None:
    """Migrate thread checkpoints (best-effort).

    Default: copy SQLite threads.db → Postgres (latest checkpoint per (thread,
    namespace)) so conversations stay resumable on the Postgres backend. With
    --scope-by-agent: rewrite an existing SQLite threads.db so its keys are bound to
    the owning agent, closing the cross-agent identity-bleed bug. SQLite remains the
    zero-config default.
    """
    from olano.config.home import (
        is_postgres_available,
        load_home_config,
        resolve_checkpoint_backend,
    )
    from olano.core.checkpoint_migrate import (
        migrate_sqlite_to_postgres,
        scope_checkpoints_by_agent,
    )

    # ── agent-scoping migration (SQLite, in place — no Postgres required) ──
    if scope_by_agent:
        src_path = Path(source).expanduser() if source else get_olano_paths().thread_db_path
        sessions_dir = get_olano_paths().sessions_dir
        click.echo(f"Scoping checkpoints by agent in {src_path}{' (dry-run)' if dry_run else ''}")
        scope_result = scope_checkpoints_by_agent(
            db_path=src_path,
            sessions_dir=sessions_dir,
            dry_run=dry_run,
            drop_orphans=drop_orphans,
            log=click.echo,
        )
        for line in scope_result.details:
            click.echo(f"  {line}")
        return

    if not is_postgres_available():
        click.echo(
            'The Postgres extra is not installed. Install it with: uv add "olano[postgres]"',
            err=True,
        )
        sys.exit(1)

    # Resolve destination URL: explicit flag wins, else the configured checkpoint DSN.
    url = dest_url
    if not url:
        try:
            backend, cfg_url = resolve_checkpoint_backend(load_home_config())
        except ValueError:
            backend, cfg_url = "sqlite", None
        url = cfg_url if backend == "postgres" else None
    if not url:
        click.echo(
            "No destination Postgres URL. Pass --to-url or set LIONCLAW_CHECKPOINT_DB_URL "
            "(or checkpoint_db_url in config.yaml).",
            err=True,
        )
        sys.exit(1)

    src_path = Path(source).expanduser() if source else get_olano_paths().thread_db_path
    click.echo(f"Migrating checkpoints: {src_path} → Postgres{' (dry-run)' if dry_run else ''}")
    result = migrate_sqlite_to_postgres(
        source_db=src_path, dest_url=url, dry_run=dry_run, log=click.echo
    )
    if result.failed_threads:
        sys.exit(1)


# ─────────────────────────────────────── vault ────────────────────────────────


@cli.group(
    "vault",
    epilog=(
        "Examples:\n"
        "  olano vault init\n"
        "  olano vault set OPENAI_API_KEY\n"
        "  olano vault list --category model-provider"
    ),
)
@click.option(
    "--passphrase",
    "-p",
    default=None,
    envvar=["OLANO_VAULT_KEY", "LIONCLAW_VAULT_KEY"],
    help="Vault master passphrase",
)
@click.pass_context
def vault_cmd(ctx: click.Context, passphrase: str | None) -> None:
    """Manage encrypted credentials (stored in encrypted SQLite)."""
    ctx.ensure_object(dict)
    ctx.obj["passphrase"] = passphrase


def _open_vault_from_ctx(ctx: click.Context, *, allow_create: bool = False):
    """Unlock the vault for `olano vault …` CLI commands.

    If a passphrase / env var / keyring entry is available, use it. Otherwise
    prompt the user interactively (TTY only). When `allow_create=True`
    (e.g. the `init` subcommand) and no DB exists yet, fall back to the
    legacy `VaultStore.open()` behaviour so we can create a fresh store
    with whatever unlock method is available.
    """
    from olano.vault.runtime import unlock_vault_for_session
    from olano.vault.store import VaultStore, default_vault_path

    passphrase = ctx.obj.get("passphrase") if ctx.obj else None
    db_existed = default_vault_path().exists()

    if not db_existed and allow_create:
        return VaultStore.open(passphrase=passphrase)

    allow_prompt = sys.stdin.isatty() and sys.stdout.isatty()
    result = unlock_vault_for_session(
        passphrase=passphrase,
        allow_prompt=allow_prompt,
    )
    if result.vault is not None:
        return result.vault

    # Fall back to the legacy ephemeral path so the command still runs and
    # surfaces a meaningful error (e.g. "Decryption failed") instead of None.
    return VaultStore.open(passphrase=passphrase)


@vault_cmd.command("init")
@click.pass_context
def vault_init(ctx: click.Context) -> None:
    """Initialize the vault (creates the encrypted store)."""
    vault = _open_vault_from_ctx(ctx, allow_create=True)
    console.print("[green]✓ Vault initialized.[/green]")
    console.print(f"  Location: {vault._db_path}")
    console.print("  Tip: set OLANO_VAULT_KEY env var or install 'keyring' for automatic unlock.")


@vault_cmd.command("set")
@click.argument("name")
@click.option("--value", "-v", default=None, help="Credential value (prompted if omitted)")
@click.option("--description", "-d", default="", help="Optional description")
@click.option("--category", "-c", default="general", help="Category for grouping")
@click.option(
    "--agent", "-a", default=None, help="Store a per-agent override instead of the common value"
)
@click.pass_context
def vault_set(
    ctx: click.Context,
    name: str,
    value: str | None,
    description: str,
    category: str,
    agent: str | None,
) -> None:
    """Store or update a credential (common, or a per-agent override with --agent)."""
    if not value:
        value = click.prompt(f"Value for '{name}'", hide_input=True, confirmation_prompt=True)
    vault = _open_vault_from_ctx(ctx)
    vault.set(name, value, description=description, category=category, agent=agent or None)
    scope = f" for agent '{agent}'" if agent else ""
    console.print(f"[green]✓ Stored '{name}'{scope}[/green]")


@vault_cmd.command("get")
@click.argument("name")
@click.option("--show", is_flag=True, help="Print the value to stdout (careful!)")
@click.option("--agent", "-a", default=None, help="Resolve as this agent (override → common)")
@click.pass_context
def vault_get(ctx: click.Context, name: str, show: bool, agent: str | None) -> None:
    """Retrieve a credential (masked by default)."""
    from olano.vault.store import VaultError

    vault = _open_vault_from_ctx(ctx)
    try:
        value = vault.get(name, agent=agent or None)
        if show:
            console.print(value)
        else:
            masked = value[:4] + "*" * max(4, len(value) - 4)
            console.print(f"'{name}': {masked}  (use --show to reveal)")
    except VaultError as e:
        console.print(f"[red]Error:[/red] {e}")


@vault_cmd.command("list")
@click.option("--category", "-c", default=None, help="Filter by category")
@click.option(
    "--agent", "-a", default=None, help="List a specific agent's overrides instead of common"
)
@click.pass_context
def vault_list(ctx: click.Context, category: str | None, agent: str | None) -> None:
    """List stored credentials (names only, no values)."""
    vault = _open_vault_from_ctx(ctx)
    if agent:
        creds = vault.list_overrides(agent=agent)
        title = f"Overrides for agent '{agent}'"
    else:
        creds = vault.list_names(category=category)
        title = "Stored Credentials"
    if not creds:
        console.print("[yellow]No credentials stored.[/yellow]")
        return
    table = Table(title=title, show_header=True)
    table.add_column("Name", style="bold")
    table.add_column("Scope" if agent else "Category")
    table.add_column("Description")
    table.add_column("Updated")
    table.add_column("Last Used")
    for c in creds:
        table.add_row(
            c["name"],
            (f"agent:{agent}" if agent else c.get("category", "")),
            c["description"] or "-",
            (c["updated_at"] or "")[:10],
            (c["last_used"] or "never")[:10],
        )
    console.print(table)


@vault_cmd.command("absorb-channel-secrets")
@click.option("--agent", "-a", default=None, help="Only sweep this agent (default: every agent)")
@click.option("--dry-run", is_flag=True, help="Show what would move; write nothing")
@click.pass_context
def vault_absorb_channel_secrets(ctx: click.Context, agent: str | None, dry_run: bool) -> None:
    """Move channel credentials out of agent.json5 and into the vault.

    New and edited connectors are vaulted automatically on save, and upgrades
    sweep existing configs; this is the one-shot sweep for anything written by
    hand. Each token becomes a per-agent override under its canonical key (a
    Telegram token → TELEGRAM_BOT_TOKEN for that agent) and the field is
    **removed** — connectors resolve their own key at boot, so the config has
    nothing to say about it. A ${vault:KEY} reference naming that same
    canonical key is redundant for the same reason and is dropped too; one
    naming a different key is a deliberate override and is left alone.
    Re-running is safe.
    """
    from olano.config.channel_secrets import absorb_channel_secrets
    from olano.config.loader import list_agents
    from olano.runtime.api import _read_agent_config, _write_agent_config

    vault = None if dry_run else _open_vault_from_ctx(ctx)
    names = [agent] if agent else list_agents(include_hidden=True)
    total = 0

    for name in names:
        try:
            data = _read_agent_config(name)
        except Exception as exc:
            console.print(f"[yellow]Skipping '{name}': {exc}[/yellow]")
            continue

        report = absorb_channel_secrets(name, data, vault=vault, dry_run=dry_run)
        if not report.changed:
            continue
        for ch_type, field_name, key in report.vaulted:
            verb = "would move" if dry_run else "moved"
            console.print(f"  {name}: {verb} {ch_type}.{field_name} → vault:{key} (agent-scoped)")
        for ch_type, field_name, key in report.stripped:
            verb = "would drop" if dry_run else "dropped"
            console.print(f"  {name}: {verb} redundant {ch_type}.{field_name} (resolves {key})")
        total += len(report.vaulted) + len(report.stripped)
        if not dry_run:
            # data no longer carries any credential field, so the write path's
            # own absorb pass is a no-op — this is just the normal guarded write.
            _write_agent_config(name, data)

    if not total:
        console.print("[green]✓ No channel credentials left in any config.[/green]")
        return
    if dry_run:
        console.print(f"\n[yellow]{total} field(s) would move. Re-run without --dry-run.[/yellow]")
    else:
        console.print(
            f"\n[green]✓ Cleaned {total} field(s).[/green] "
            "Restart the affected agents so their connectors resolve from the vault."
        )


@vault_cmd.command("delete")
@click.argument("name")
@click.option(
    "--agent", "-a", default=None, help="Delete a per-agent override instead of the common value"
)
@click.confirmation_option(prompt="Are you sure you want to delete this credential?")
@click.pass_context
def vault_delete(ctx: click.Context, name: str, agent: str | None) -> None:
    """Delete a credential from the vault."""
    vault = _open_vault_from_ctx(ctx)
    if vault.delete(name, agent=agent or None):
        scope = f" (agent '{agent}')" if agent else ""
        console.print(f"[green]✓ Deleted '{name}'{scope}[/green]")
    else:
        console.print(f"[yellow]Credential '{name}' not found.[/yellow]")


@vault_cmd.command("audit")
@click.pass_context
def vault_audit(ctx: click.Context) -> None:
    """Show credentials due for rotation."""
    vault = _open_vault_from_ctx(ctx)
    due = vault.needs_rotation()
    if not due:
        console.print("[green]✓ All credentials are up to date.[/green]")
    else:
        console.print(f"[yellow]⚠ {len(due)} credential(s) due for rotation:[/yellow]")
        for name in due:
            console.print(f"  - {name}")


@vault_cmd.command("status")
@click.pass_context
def vault_status(ctx: click.Context) -> None:
    """Show vault location, unlock mode, entry count, and rotation health."""
    from collections import Counter

    from olano.vault.runtime import detect_unlock_mode
    from olano.vault.store import VaultError, default_vault_path

    db_path = default_vault_path()
    passphrase = ctx.obj.get("passphrase") if ctx.obj else None
    mode = detect_unlock_mode(passphrase)

    table = Table(title="Vault Status", show_header=False, box=None, padding=(0, 1))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("DB path", str(db_path))
    table.add_row("DB exists", "[green]yes[/green]" if db_path.exists() else "[red]no[/red]")
    table.add_row("Unlock mode", mode)

    if not db_path.exists():
        table.add_row(
            "Next step",
            "[yellow]Run `olano vault init` to create the encrypted store.[/yellow]",
        )
        console.print(table)
        return

    if mode == "ephemeral":
        table.add_row(
            "Note",
            "[yellow]No persistent unlock method — set OLANO_VAULT_KEY or install "
            "the 'keyring' package, otherwise stored values cannot be decrypted "
            "across restarts.[/yellow]",
        )

    try:
        vault = _open_vault_from_ctx(ctx)
        entries = vault.list_names()
        table.add_row("Entries", str(len(entries)))

        cats = Counter(e["category"] or "general" for e in entries)
        if cats:
            cat_str = ", ".join(f"{k}={v}" for k, v in sorted(cats.items()))
            table.add_row("By category", cat_str)

        try:
            due = vault.needs_rotation()
            table.add_row(
                "Due for rotation",
                f"{len(due)}" if not due else f"[yellow]{len(due)} (run `vault audit`)[/yellow]",
            )
        except Exception as exc:
            table.add_row("Due for rotation", f"[red]error: {exc}[/red]")

        # Sanity check — try a real decrypt if any entries exist.
        if entries:
            try:
                vault.get(entries[0]["name"])
                table.add_row("Decryption", "[green]ok[/green]")
            except VaultError as exc:
                table.add_row(
                    "Decryption",
                    f"[red]FAILED — {exc}[/red]",
                )
                table.add_row(
                    "Hint",
                    "Wrong passphrase / key. Ensure OLANO_VAULT_KEY matches the one "
                    "used when entries were stored, or pass `-p PASSPHRASE`.",
                )
    except Exception as exc:
        table.add_row("Status", f"[red]error: {exc}[/red]")

    console.print(table)


# ─────────────────────────────────────── pages ────────────────────────────────


@cli.group(
    "pages",
    epilog=(
        "Examples:\n"
        "  olano pages set-password -a peter   # prompted, hidden input\n"
        "  olano pages list\n"
        "  olano pages logs crm --build\n"
        "  olano pages restart crm\n"
        "  olano pages remove my-report"
    ),
)
def pages_cmd() -> None:
    """Manage agent-published pages (served at /pages/<slug>/ behind Basic Auth)."""


@pages_cmd.command("set-password")
@click.option("--agent", "-a", required=True, help="Agent whose pages this credential guards")
@click.option("--username", "-u", default="owner", show_default=True, help="Basic-Auth username")
def pages_set_password(agent: str, username: str) -> None:
    """Set the viewer password for one AGENT's published pages (owner-only).

    Each agent has its own credential; it guards every page that agent
    publishes. The password is prompted with hidden input, hashed immediately
    (PBKDF2), and only the hash is stored — agents can neither set nor read it.
    Until this is run, that agent's published pages answer 503 and serve nothing.
    """
    from olano.pages.registry import PagesError, set_page_auth

    password = click.prompt("Viewer password", hide_input=True, confirmation_prompt=True)
    try:
        set_page_auth(agent, username, password)
    except PagesError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    console.print(
        f"[green]✓ Viewer credential stored[/green] for agent '{agent}' (username '{username}')."
    )
    console.print("  That agent's pages now require this login. Agents never see the password.")


@pages_cmd.command("list")
def pages_list() -> None:
    """List every published page, its owning agent, and whether that agent has a password."""
    from olano.pages.registry import list_pages, page_auth_configured
    from olano.pages.services import all_page_status
    from olano.pages.spec import record_spec

    pages = list_pages()
    observed = all_page_status()
    if not pages:
        console.print("[yellow]No pages published.[/yellow]")
        return
    table = Table(title="Published Pages", show_header=True)
    table.add_column("Slug", style="bold")
    table.add_column("Agent")
    table.add_column("Directory")
    table.add_column("State")
    table.add_column("Runs")
    table.add_column("Password")
    table.add_column("Updated")
    unlocked: list[str] = []
    for record in pages:
        state = "[green]serving[/green]" if record.get("enabled") else "[yellow]stopped[/yellow]"
        agent = record.get("agent", "?")
        has_pw = page_auth_configured(agent)
        if not has_pw and agent not in unlocked:
            unlocked.append(agent)
        spec = record_spec(record)
        runs = "files"
        if spec is not None:
            current = (observed.get(record["slug"]) or {}).get("state") or "pending"
            color = {"running": "green", "live": "green", "failed": "red"}.get(current, "yellow")
            runs = f"{spec.kind}: [{color}]{current}[/{color}]"
        table.add_row(
            record["slug"],
            agent,
            record.get("directory", "?"),
            state,
            runs,
            "[green]set[/green]" if has_pw else "[red]none — 503[/red]",
            record.get("updated_at", "?"),
        )
    console.print(table)
    for agent in unlocked:
        console.print(
            f"[yellow]Agent '{agent}' has no viewer password — its pages answer 503. "
            f"Run `olano pages set-password --agent {agent}`.[/yellow]"
        )


@pages_cmd.command("remove")
@click.argument("slug")
def pages_remove(slug: str) -> None:
    """Unpublish a page and release its slug (any agent's, as owner cleanup).

    A deployed page's server stops within a few seconds (the running runtime's
    supervisor notices the page is gone) and its built releases are deleted.
    """
    from olano.pages.registry import PagesError, admin_delete_page, normalize_slug
    from olano.pages.releases import delete_releases

    try:
        admin_delete_page(slug)
    except PagesError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    delete_releases(normalize_slug(slug))
    console.print(f"[green]✓ Page '{slug}' removed and slug released.[/green]")


def _pages_set_enabled(slug: str, enabled: bool) -> None:
    from olano.pages.registry import PagesError, admin_set_enabled, get_page, normalize_slug
    from olano.pages.workspaces import agent_workspace_root

    try:
        record = get_page(normalize_slug(slug))
        if record is None:
            raise PagesError(f"no page registered under slug '{slug}'")
        root = agent_workspace_root(str(record["agent"])) if enabled else None
        admin_set_enabled(slug, enabled, root)
    except PagesError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


@pages_cmd.command("stop")
@click.argument("slug")
def pages_stop_cmd(slug: str) -> None:
    """Stop serving a page (any agent's); a deployed page's server stops too."""
    _pages_set_enabled(slug, False)
    console.print(f"[green]✓ Page '{slug}' stopped.[/green] The slug stays reserved for its agent.")


@pages_cmd.command("restart")
@click.argument("slug")
def pages_restart_cmd(slug: str) -> None:
    """Resume a page; a deployed page re-runs its build and restarts its server."""
    _pages_set_enabled(slug, True)
    console.print(
        f"[green]✓ Page '{slug}' restarting.[/green] The running runtime picks it up "
        "within a few seconds; follow it with `olano pages list` and `olano pages logs`."
    )


@pages_cmd.command("logs")
@click.argument("slug")
@click.option("--build", "build_log", is_flag=True, help="Show the last build's log instead")
@click.option("--lines", "-n", default=80, show_default=True, help="Lines from the end")
def pages_logs_cmd(slug: str, build_log: bool, lines: int) -> None:
    """Print the end of a deployed page's server log (or, with --build, its build log)."""
    from olano.pages.paths import build_log_path, service_log_path
    from olano.pages.registry import get_page, normalize_slug
    from olano.pages.services import page_status, tail_text
    from olano.pages.workspaces import agent_workspace_root

    record = get_page(normalize_slug(slug))
    if record is None:
        console.print(f"[red]Error:[/red] no page registered under slug '{slug}'")
        sys.exit(1)
    root = agent_workspace_root(str(record["agent"]))
    if root is None:
        console.print(f"[red]Error:[/red] agent '{record['agent']}' has no workspace")
        sys.exit(1)
    path = (build_log_path if build_log else service_log_path)(root, record["slug"])
    state = page_status(record["slug"]) or {}
    if state:
        console.print(
            f"[bold]{record['slug']}[/bold]: {state.get('state')}  {state.get('detail') or ''}"
        )
    text = tail_text(path, lines=max(1, min(2000, lines)), max_bytes=1024 * 1024)
    console.print(f"[dim]{path}[/dim]")
    click.echo(text or "(empty)")


# ─────────────────────────────────────── archetype ────────────────────────────


def _render_archetype_list() -> None:
    from olano.archetypes.installer import list_archetypes, load_archetype_template

    names = list_archetypes()
    if not names:
        console.print("[yellow]No archetypes found.[/yellow]")
        return

    table = Table(title="Available Archetypes", show_header=True)
    table.add_column("Name", style="bold")
    table.add_column("Emoji")
    table.add_column("Description")
    table.add_column("Model")

    for name in names:
        try:
            tmpl = load_archetype_template(name)
            table.add_row(
                name,
                tmpl.get("emoji", "🤖"),
                tmpl.get("description", ""),
                tmpl.get("model", ""),
            )
        except Exception:
            table.add_row(name, "?", "[red]error loading[/red]", "-")

    console.print(table)


def _install_archetype_and_report(
    archetype_name: str,
    as_name: str | None,
    overwrite: bool,
    model: str | None = None,
) -> None:
    from olano.archetypes.installer import install_archetype, load_archetype_template

    try:
        tmpl = load_archetype_template(archetype_name)
        target_name = as_name or archetype_name

        path = install_archetype(
            archetype_name,
            agent_name=as_name,
            overwrite=overwrite,
            model=model,
        )
        agent_dir = path.parent
        workspace_root = agent_dir / "workspace"

        console.print()
        console.print(
            f"[green]✓[/green]  [bold]{tmpl.get('emoji', '🤖')}  {target_name}[/bold]  "
            f"[dim]({archetype_name} archetype)[/dim]"
        )
        console.print()
        console.print(f"  Config:    [dim]{path}[/dim]")
        console.print(f"  Workspace: [dim]{workspace_root}[/dim]")
        console.print()
        console.print("  Workspace files created:")
        for fname in ["IDENTITY.md", "SOUL.md", "AGENTS.md", "MEMORY.md", "USER.md", "TOOLS.md"]:
            fpath = workspace_root / fname
            status = "[green]✓[/green]" if fpath.exists() else "[yellow]–[/yellow]"
            console.print(f"    {status}  {fname}")
        console.print()
        console.print(
            f"  In its first conversation, [bold]{target_name}[/bold] will introduce itself\n"
            f"  and learn about you.\n"
        )
        console.print("  Chat with it in the dashboard: [bold]olano dashboard[/bold]")
        console.print()
    except FileExistsError as e:
        console.print(f"[yellow]{e}[/yellow]")
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
    except Exception as e:
        from olano.deployment.enforcement import DeploymentLimitExceeded

        if isinstance(e, DeploymentLimitExceeded):
            console.print(f"[red]{e.decision.message}[/red]")
            console.print(f"[dim]{e.decision.remediation}[/dim]")
            sys.exit(1)
        raise


@cli.command(name="agentfather")
@click.option("--message", "-m", default=None, help="Send one message to AgentFather (one-shot)")
@click.option("--overwrite", is_flag=True, help="Reinstall the AgentFather archetype")
def agentfather(message: str | None, overwrite: bool) -> None:
    """Install AgentFather, the agent that builds and operates olano.

    Installs the AgentFather archetype on first run (if missing). Chat with it in
    the dashboard (`olano dashboard`), or pass -m to send it one message from the
    terminal. AgentFather designs agent teams, scaffolds them, wires tools and
    MCP servers, and inspects logs, gating every change behind a human approval.
    """
    from olano.config.loader import find_agent_config_path
    from olano.deployment import enforcement as dep_enforcement

    father_decision = dep_enforcement.feature_decision("father_agent", label="FatherAgent")
    if not father_decision.allowed:
        console.print(f"[red]{father_decision.message}[/red]")
        console.print(f"[dim]{father_decision.remediation}[/dim]")
        sys.exit(1)

    installed_now = overwrite or find_agent_config_path("agentfather", source="home") is None
    if installed_now:
        console.print("[dim]Installing the AgentFather archetype…[/dim]")
        _install_archetype_and_report(
            "agentfather", None, overwrite, model=load_home_config().preferred_model
        )

    if not message:
        if not installed_now:
            console.print("Chat with AgentFather in the dashboard: [bold]olano dashboard[/bold]")
        return

    from olano.cli.non_interactive import run_non_interactive

    sys.exit(
        asyncio.run(
            run_non_interactive("agentfather", message, thread_id=None, config_source="home")
        )
    )


@cli.group()
def archetype() -> None:
    """Manage agent archetypes (pre-configured templates)."""


@archetype.command(name="list")
def archetype_list() -> None:
    """List available agent archetypes."""
    _render_archetype_list()


@archetype.command(name="install")
@click.argument("archetype_name")
@click.option("--as", "as_name", default=None, help="Name for the installed agent")
@click.option("--overwrite", is_flag=True, help="Overwrite existing agent config")
def archetype_install(archetype_name: str, as_name: str | None, overwrite: bool) -> None:
    """Install an archetype as a new agent with full workspace scaffold."""
    _install_archetype_and_report(archetype_name, as_name, overwrite)


# ─────────────────────────────────────── issues ───────────────────────────────


@cli.group()
def issues() -> None:
    """Issue tracker — durable work items between agents and humans.

    Operates the local issues.db directly (no server needed). Default actor
    is human-owner; --as records a different principal (ai-<agent> or
    human-<user>) as the author.
    """


@issues.command(name="create")
@click.argument("title")
@click.option("--body", default="", help="Issue description")
@click.option(
    "--assign", "assignee", default="", help="Assignee: bare name, ai-<agent>, or human-<user>"
)
@click.option("--label", "labels", multiple=True, help="Label (repeatable)")
@click.option("--type", "issue_type", default="task", help="task|bug|feature|question|incident")
@click.option("--as", "as_user", default="", help="Act as this principal (default human-owner)")
def issues_create(
    title: str, body: str, assignee: str, labels: tuple[str, ...], issue_type: str, as_user: str
) -> None:
    """Open a new issue."""
    from olano.cli.issues_cmd import (
        deliver_assignment,
        format_issue_line,
        resolve_actor,
        resolve_assignee_arg,
    )
    from olano.issues.store import IssueError, get_issue_store

    ensure_olano_home()
    actor = resolve_actor(as_user)
    try:
        issue = get_issue_store().create(
            title=title,
            body=body,
            author=actor,
            assignee=resolve_assignee_arg(assignee),
            labels=list(labels),
            issue_type=issue_type,
        )
    except IssueError as exc:
        raise click.ClickException(str(exc)) from exc
    deliver_assignment(issue, actor)
    click.echo(format_issue_line(issue))


@issues.command(name="list")
@click.option("--state", default="open", help="open|closed|all (default open)")
@click.option("--assignee", default="", help="Filter by assignee (bare or canonical)")
@click.option("--author", default="", help="Filter by author principal")
@click.option("--label", default="", help="Filter by label")
@click.option("--type", "issue_type", default="", help="Filter by type")
@click.option(
    "--search",
    "query",
    default="",
    help="Search title/body/comments (hybrid keyword + semantic, best match first)",
)
def issues_list(
    state: str, assignee: str, author: str, label: str, issue_type: str, query: str
) -> None:
    """List issues."""
    from olano.cli.issues_cmd import format_issue_line, resolve_assignee_arg
    from olano.issues.store import get_issue_store

    ensure_olano_home()
    ranked = None
    if query:
        try:
            from olano.issues.semantic import hybrid_ranked_ids

            ranked = hybrid_ranked_ids(query, limit=200)
        except Exception:
            ranked = None
    rows = get_issue_store().list_issues(
        state="" if state == "all" else state,
        assignee=resolve_assignee_arg(assignee),
        author=author,
        label=label,
        issue_type=issue_type,
        query=query,
        sort="relevance" if query else "updated",
        limit=100,
        ranked_ids=ranked,
    )
    if not rows:
        click.echo("No matching issues.")
        return
    for issue in rows:
        click.echo(format_issue_line(issue))


@issues.command(name="show")
@click.argument("issue_id", type=int)
def issues_show(issue_id: int) -> None:
    """Show one issue with its full comment/state thread."""
    from olano.cli.issues_cmd import format_issue_thread
    from olano.issues.store import get_issue_store

    ensure_olano_home()
    store = get_issue_store()
    issue = store.get(issue_id)
    if issue is None:
        raise click.ClickException(f"No issue #{issue_id}")
    click.echo(format_issue_thread(issue, store.events(issue_id)))


@issues.command(name="comment")
@click.argument("issue_id", type=int)
@click.argument("body")
@click.option("--as", "as_user", default="", help="Act as this principal (default human-owner)")
def issues_comment(issue_id: int, body: str, as_user: str) -> None:
    """Comment on an issue."""
    from olano.cli.issues_cmd import format_issue_line, resolve_actor
    from olano.issues.store import IssueError, get_issue_store

    ensure_olano_home()
    try:
        issue = get_issue_store().comment(issue_id, actor=resolve_actor(as_user), body=body)
    except IssueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(format_issue_line(issue))


@issues.command(name="close")
@click.argument("issue_id", type=int)
@click.option("--reason", default="", help="Why it's being closed")
@click.option("--as", "as_user", default="", help="Act as this principal (default human-owner)")
def issues_close(issue_id: int, reason: str, as_user: str) -> None:
    """Close an open issue."""
    from olano.cli.issues_cmd import format_issue_line, resolve_actor
    from olano.issues.store import IssueError, get_issue_store

    ensure_olano_home()
    try:
        issue = get_issue_store().set_state(
            issue_id, actor=resolve_actor(as_user), state="closed", reason=reason
        )
    except IssueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(format_issue_line(issue))


@issues.command(name="reopen")
@click.argument("issue_id", type=int)
@click.option("--reason", default="", help="Why it's being reopened")
@click.option("--as", "as_user", default="", help="Act as this principal (default human-owner)")
def issues_reopen(issue_id: int, reason: str, as_user: str) -> None:
    """Reopen a closed issue."""
    from olano.cli.issues_cmd import format_issue_line, resolve_actor
    from olano.issues.store import IssueError, get_issue_store

    ensure_olano_home()
    try:
        issue = get_issue_store().set_state(
            issue_id, actor=resolve_actor(as_user), state="open", reason=reason
        )
    except IssueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(format_issue_line(issue))


@issues.command(name="assign")
@click.argument("issue_id", type=int)
@click.argument("assignee")
@click.option("--as", "as_user", default="", help="Act as this principal (default human-owner)")
def issues_assign(issue_id: int, assignee: str, as_user: str) -> None:
    """Assign an issue to an agent or human (agents: heartbeat; humans: notification)."""
    from olano.cli.issues_cmd import (
        deliver_assignment,
        format_issue_line,
        resolve_actor,
        resolve_assignee_arg,
    )
    from olano.issues.store import IssueError, get_issue_store

    ensure_olano_home()
    actor = resolve_actor(as_user)
    try:
        issue = get_issue_store().assign(
            issue_id, actor=actor, assignee=resolve_assignee_arg(assignee)
        )
    except IssueError as exc:
        raise click.ClickException(str(exc)) from exc
    deliver_assignment(issue, actor)
    click.echo(format_issue_line(issue))


# ─────────────────────────────────────── users ────────────────────────────────


@cli.group()
def users() -> None:
    """Manage the dashboard's Team accounts (owner/admin + members).

    "Team accounts" is the per-person dashboard sign-in layer (config key
    ``multi_user_enabled``) — humans operating this install. Not the per-agent
    "Multi-user mode" tab, which isolates a shared agent's chat end-users.
    """


@users.command(name="create-admin")
@click.option("--email", required=True, help="Admin email (login identifier)")
@click.option(
    "--password",
    prompt=True,
    hide_input=True,
    confirmation_prompt=True,
    help="Admin password (prompted securely if omitted)",
)
@click.option("--name", default="", help="Display name")
@click.option(
    "--username",
    default="",
    help=(
        "Permanent public handle (lowercase letters, digits, dashes). "
        "Refused if taken; leave empty to derive one from --name or the email."
    ),
)
def users_create_admin(email: str, password: str, name: str, username: str) -> None:
    """Create an admin user and enable Team accounts (per-person dashboard login).

    The first admin is also granted ownership of every existing agent so the
    prior single operator keeps full control.
    """
    ensure_olano_home()
    from olano.runtime.auth import set_multi_user_enabled
    from olano.users.store import get_user_store

    store = get_user_store()
    first = store.db.count_users() == 0
    try:
        if first:
            user = store.bootstrap_admin(
                email=email, password=password, name=name, username=username
            )
        else:
            user = store.create_user(
                email=email, password=password, name=name, is_admin=True, username=username
            )
    except Exception as exc:
        console.print(f"[red]Failed to create admin: {exc}[/red]")
        raise SystemExit(1) from exc

    set_multi_user_enabled(True)
    console.print(f"[green]Admin created:[/green] {user.email} (@{user.username})")
    if first:
        console.print("Granted ownership of all existing agents; Team accounts login is ON.")
    else:
        console.print("Team accounts login is ON.")


@users.command(name="reset-password")
@click.option("--email", required=True, help="Email of the account to reset")
def users_reset_password(email: str) -> None:
    """Generate a one-time password-reset link for a user (or your own admin account)."""
    ensure_olano_home()
    from olano.users.store import get_user_store

    store = get_user_store()
    user = store.db.get_user_by_email(email)
    if user is None:
        console.print(f"[red]No user with email {email}[/red]")
        raise SystemExit(1)
    result = store.reset_password(user.id, created_by="cli")
    if result is None:
        console.print("[red]Could not create a reset link.[/red]")
        raise SystemExit(1)
    token, expires_at = result
    console.print(f"[green]Password-reset link for {user.email}:[/green]")
    console.print(f"  /invite/{token}")
    console.print(f"  Open it at  http://<dashboard-host>/invite/{token}")
    console.print(f"  (single-use; expires {expires_at})")


# ─────────────────────────────── customers ────────────────────────────────
# End-customers of a multi-user agent (customers/<platform>-<id>/ folders) —
# distinct from `users`, which manages the humans operating the dashboard.


@cli.group()
def customers() -> None:
    """Inspect and moderate a multi-user agent's end-customers."""


@customers.command(name="list")
@click.argument("agent")
def customers_list(agent: str) -> None:
    """List an agent's customers with their mute/block state."""
    from olano.memory.customer_memory import customers_base
    from olano.memory.customer_prefs import read_prefs

    base = customers_base(agent)
    folders = (
        sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.name)
        if base.is_dir()
        else []
    )
    if not folders:
        console.print(f"No customers yet for agent '{agent}'.")
        return
    for folder in folders:
        prefs = read_prefs(folder)
        flags = [f for f in ("blocked", "muted") if prefs.get(f)]
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        console.print(f"  {folder.name}{suffix}")
    console.print(f"{len(folders)} customer(s).")


@customers.command(name="block")
@click.argument("agent")
@click.argument("customer_key")
def customers_block(agent: str, customer_key: str) -> None:
    """Block one customer: all scheduled runs and deliveries to them stop.

    CUSTOMER_KEY is the folder name (e.g. telegram-449281). This is the
    operator-only switch — no agent conversation can set or lift it.
    """
    from olano.memory.customer_prefs import set_blocked

    if set_blocked(agent, customer_key, True):
        console.print(f"[green]Blocked[/green] {customer_key} for agent '{agent}'.")
    else:
        console.print(f"[red]No customer folder '{customer_key}' for agent '{agent}'.[/red]")
        raise SystemExit(1)


@customers.command(name="unblock")
@click.argument("agent")
@click.argument("customer_key")
def customers_unblock(agent: str, customer_key: str) -> None:
    """Lift a block set with `olano customers block`."""
    from olano.memory.customer_prefs import set_blocked

    if set_blocked(agent, customer_key, False):
        console.print(f"[green]Unblocked[/green] {customer_key} for agent '{agent}'.")
    else:
        console.print(f"[red]No customer folder '{customer_key}' for agent '{agent}'.[/red]")
        raise SystemExit(1)


@cli.command("login")
@click.argument("email", required=False)
@click.option(
    "--password",
    "-p",
    default=None,
    help="Password (prompted securely if omitted).",
)
def login(email: str | None, password: str | None) -> None:
    """Verify a user's email + password (read-only diagnostic; no session created).

    Examples:
      olano login                       # prompts for email + password
      olano login you@example.com       # prompts for password
      olano login you@example.com -p secret
    """
    ensure_olano_home()
    from olano.users.store import get_user_store

    if not email:
        email = click.prompt("Email")
    if not password:
        password = click.prompt("Password", hide_input=True)

    store = get_user_store()
    if store.db.count_users() == 0:
        console.print("[red]No users exist yet.[/red] Create one with: olano users create-admin")
        raise SystemExit(1)

    # Read-only: get_user_by_email → verify_password → is_active check.
    user = store.authenticate(email, password)
    if user is not None:
        role = "admin" if user.is_admin else "user"
        console.print(f"[green]✓[/green] Authenticated as {user.email} ({role}).")
        return

    # Enrich the failure message for the inactive-account case (still read-only).
    existing = store.db.get_user_by_email(email)
    if existing is not None and not existing.is_active:
        console.print("[red]✗ Account is inactive (pending invite).[/red]")
    else:
        console.print("[red]✗ Invalid email or password.[/red]")
    raise SystemExit(1)


# ─────────────────────────────────────── auth ─────────────────────────────────


@cli.group()
def auth() -> None:
    """Authenticate with external services."""


@auth.command(name="openai-codex")
@click.option("--force", is_flag=True, help="Ignore existing credentials and start a new login")
@click.option(
    "--no-import",
    is_flag=True,
    help="Do not offer to import existing Codex CLI credentials from ~/.codex/auth.json",
)
def auth_openai_codex(force: bool, no_import: bool) -> None:
    """Authenticate to OpenAI Codex with the ChatGPT device-code flow."""
    from olano.models.codex_auth import (
        CodexAuthError,
        auth_status,
        codex_access_token_is_expiring,
        codex_device_code_login,
        import_codex_cli_tokens,
        save_codex_tokens,
    )

    if not force:
        status = auth_status()
        if status.get("logged_in") and not status.get("expires_soon"):
            console.print("[green]OpenAI Codex credentials are already stored.[/green]")
            console.print(f"Auth store: {status.get('auth_store')}")
            console.print("Use --force to start a fresh device-code login.")
            return

    if not force and not no_import:
        cli_tokens = import_codex_cli_tokens()
        if cli_tokens and not codex_access_token_is_expiring(cli_tokens.get("access_token"), 60):
            console.print("Found existing Codex CLI credentials at ~/.codex/auth.json.")
            if click.confirm(
                "Import them into Olano instead of starting a new login?",
                default=False,
            ):
                save_codex_tokens(cli_tokens)
                console.print("[green]Imported OpenAI Codex credentials.[/green]")
                return

    console.print("Signing in to OpenAI Codex...")
    console.print("Olano will store its own OAuth session in your Olano home.")
    console.print()
    try:
        creds = codex_device_code_login(display=console.print)
        save_codex_tokens(creds["tokens"], creds.get("last_refresh"))
    except KeyboardInterrupt:
        console.print("\n[yellow]Login cancelled.[/yellow]")
        raise click.exceptions.Exit(130) from None
    except CodexAuthError as exc:
        console.print(f"[red]Login failed:[/red] {exc}")
        raise click.exceptions.Exit(1) from None

    console.print()
    console.print("[green]Login successful.[/green]")
    console.print("Use model string: [bold]openai-codex:gpt-5.3-codex[/bold]")


@auth.command(name="google")
@click.option("--credentials", "-c", default=None, help="Path to Google OAuth client_secret.json")
@click.option(
    "--client-id",
    default=None,
    help="OAuth Client ID (with --client-secret; alternative to a client_secret.json path)",
)
@click.option(
    "--client-secret",
    default=None,
    help="OAuth Client Secret (with --client-id)",
)
def auth_google(credentials: str | None, client_id: str | None, client_secret: str | None) -> None:
    """Authenticate with Google services (Gmail, Calendar, Drive).

    With no options, the OAuth client resolves from the vault/env:
    GOOGLE_OAUTH_CLIENT (JSON with client_id/client_secret), the
    GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET pair, or the legacy
    GOOGLE_OAUTH_CREDENTIALS path.
    """
    try:
        from olano.tools.google_tools import run_google_auth_flow

        console.print("[bold]Starting Google OAuth2 flow...[/bold]")
        console.print("A browser window will open. Please authorize olano.")
        run_google_auth_flow(
            creds_path=credentials, client_id=client_id, client_secret=client_secret
        )
        console.print("[green]✓ Google authentication successful![/green]")
        console.print("You can now use gmail_read, gmail_send, calendar_*, drive_* tools.")
    except Exception as e:
        console.print(f"[red]Google auth failed:[/red] {e}")
        console.print("\nMake sure you have:")
        console.print("  1. A Google Cloud project with an OAuth client (Desktop app for this")
        console.print("     local flow; Web application for the dashboard Connect flow)")
        console.print("  2. The client stored: olano vault set GOOGLE_OAUTH_CLIENT")
        console.print('     -v \'{"client_id":"…","client_secret":"…"}\' — or pass')
        console.print("     --client-id/--client-secret, or set")
        console.print("     GOOGLE_OAUTH_CREDENTIALS=/path/to/client_secret.json")
        sys.exit(1)


# ─────────────────────────────────────── channel ─────────────────────────────


@cli.group()
def channel() -> None:
    """Manage messaging channel integrations (Telegram, Slack, Discord, email)."""


@channel.command(name="add")
@click.argument(
    "channel_type", metavar="TYPE", type=click.Choice(["telegram", "slack", "discord", "email"])
)
@click.option(
    "--agent",
    "agent_name",
    default=None,
    help="Agent to add the channel to (default: home default)",
)
@click.pass_context
def channel_add(ctx: click.Context, channel_type: str, agent_name: str | None) -> None:
    """Add a messaging channel interactively."""

    from olano.config.loader import load_agent_config, save_agent_config
    from olano.platforms.setup import get_setup_fn

    home = get_olano_home(ctx.obj.get("home") if ctx.obj else None)
    selected = agent_name or load_home_config().default_agent
    try:
        config = load_agent_config(selected)
    except FileNotFoundError:
        console.print(f"[red]Agent '{selected}' not found.[/red]")
        sys.exit(1)

    setup_fn = get_setup_fn(channel_type)
    try:
        channel_cfg = setup_fn(home)
    except ValueError as e:
        console.print(f"[red]Channel setup failed: {e}[/red]")
        sys.exit(1)

    # Avoid duplicate entries. Match on the type the setup function actually
    # returns — most now write "platform:<name>", so comparing the argument
    # ("slack") against the configured type ("platform:slack") would miss every
    # existing connector and append a second copy on every re-run.
    added_type = str(channel_cfg.get("type", channel_type))
    existing_types = {(c.type if hasattr(c, "type") else c) for c in config.channels}
    if added_type not in existing_types:
        from olano.config.agent_config import ChannelConfig

        config.channels.append(ChannelConfig(**channel_cfg))
        save_agent_config(config)
        console.print(f"[green]✓[/green] {added_type} channel added to {selected}.")
    else:
        console.print(f"[yellow]Channel '{added_type}' already configured.[/yellow]")


@channel.command(name="list")
@click.option("--agent", "agent_name", default=None, help="Agent name")
def channel_list(agent_name: str | None) -> None:
    """List configured channels for an agent."""
    from olano.config.loader import load_agent_config

    selected = agent_name or load_home_config().default_agent
    try:
        config = load_agent_config(selected)
    except FileNotFoundError:
        console.print(f"[red]Agent '{selected}' not found.[/red]")
        sys.exit(1)

    if not config.channels:
        console.print(f"No channels configured for '{selected}'.")
        return
    for c in config.channels:
        ctype = c.type if hasattr(c, "type") else str(c)
        console.print(f"  {ctype}")


@channel.command(name="remove")
@click.argument("channel_type")
@click.option("--agent", "agent_name", default=None, help="Agent name")
def channel_remove(channel_type: str, agent_name: str | None) -> None:
    """Remove a channel from an agent's configuration."""
    from olano.config.loader import load_agent_config, save_agent_config

    selected = agent_name or load_home_config().default_agent
    try:
        config = load_agent_config(selected)
    except FileNotFoundError:
        console.print(f"[red]Agent '{selected}' not found.[/red]")
        sys.exit(1)

    # A bare name removes both generations of that connector: "slack" matches
    # the configured "platform:slack" too, so the argument stays the thing the
    # user typed into `channel add` even though the config now records the
    # platform type. An explicit "platform:slack" removes only that one.
    def _matches(configured: str) -> bool:
        if configured == channel_type:
            return True
        return not channel_type.startswith("platform:") and configured == f"platform:{channel_type}"

    before = len(config.channels)
    config.channels = [
        c for c in config.channels if not _matches(c.type if hasattr(c, "type") else str(c))
    ]
    if len(config.channels) < before:
        save_agent_config(config)
        console.print(f"[green]✓[/green] Removed '{channel_type}' from {selected}.")
    else:
        console.print(
            f"[yellow]Channel '{channel_type}' was not configured (nothing to remove).[/yellow]"
        )


# ─────────────────────────────────────── gateway ─────────────────────────────


@cli.group()
def gateway() -> None:
    """Manage the always-on gateway daemon (long-running channel listener)."""


@gateway.command(name="install")
@click.pass_context
def gateway_install(ctx: click.Context) -> None:
    """Install the gateway daemon as a system service (launchd / systemd / docker)."""
    from olano.gateway import install

    home = get_olano_home(ctx.obj.get("home") if ctx.obj else None)
    path = install(home)
    console.print(f"[green]✓[/green] Gateway unit installed at [dim]{path}[/dim]")
    console.print("  Run [bold]olano gateway start[/bold] to activate it.")


@gateway.command(name="status")
def gateway_status() -> None:
    """Show gateway daemon status."""
    from olano.gateway import status

    s = status()
    icon = "[green]●[/green]" if s == "running" else "[yellow]○[/yellow]"
    console.print(f"{icon} Gateway: {s}")


@gateway.command(name="start")
def gateway_start() -> None:
    """Start the gateway daemon."""
    from olano.gateway import start

    start()
    console.print("[green]✓[/green] Gateway started.")


@gateway.command(name="stop")
def gateway_stop() -> None:
    """Stop the gateway daemon."""
    from olano.gateway import stop

    stop()
    console.print("[green]✓[/green] Gateway stopped.")


@gateway.command(name="restart")
def gateway_restart() -> None:
    """Restart the gateway daemon."""
    from olano.gateway import restart

    restart()
    console.print("[green]✓[/green] Gateway restarted.")


# ─────────────────────────────────────── dashboard ────────────────────────────


@cli.command(
    epilog=(
        "Examples:\n"
        "  olano dashboard\n"
        "  olano dashboard --agents merli --port 3200\n"
        "  olano dashboard --no-browser"
    ),
)
@click.option("--agents", "-a", multiple=True, help="Specific agents to load (default: all)")
@click.option("--port", default=None, type=int, help="Dashboard port (default: from home config)")
@click.option("--no-browser", is_flag=True, help="Don't open browser automatically")
def dashboard(agents: tuple[str, ...], port: int | None, no_browser: bool) -> None:
    """Start the Mission Control dashboard (runtime + REST API + web UI)."""
    import subprocess
    import threading
    import time

    from olano.runtime.server import RuntimeServer

    home_config = load_home_config()
    dashboard_port = port or int(
        os.environ.get("LIONCLAW_DASHBOARD_PORT", str(home_config.dashboard_port))
    )
    runtime_port = int(os.environ.get("LIONCLAW_RUNTIME_PORT", str(home_config.runtime_api_port)))
    agent_list = _expand_agent_names(agents) if agents else None
    console.print("[bold green]Starting Olano Mission Control...[/bold green]")
    console.print(f"Dashboard: [bold]http://localhost:{dashboard_port}[/bold]")

    # Start Node.js dashboard server in background.  `server.mjs` is a generic
    # static-file + /api-proxy server; it lives alongside the Dashboard 2.0
    # frontend and serves its Vite build from dist/.
    dashboard_dir = Path(__file__).parent.parent / "dashboard_2.0"

    node_proc = None
    if dashboard_dir.exists() and (dashboard_dir / "server.mjs").exists():
        import os as _os

        env = {
            **_os.environ,
            "LIONCLAW_RUNTIME_PORT": str(runtime_port),
            "LIONCLAW_RUNTIME_URL": f"http://localhost:{runtime_port}",
            "LIONCLAW_DASHBOARD_PORT": str(dashboard_port),
            # Serve the Dashboard 2.0 Vite build output (dist/).
            "LIONCLAW_DASHBOARD_PUBLIC_DIR": str(dashboard_dir / "dist"),
        }
        try:
            node_proc = subprocess.Popen(
                ["node", str(dashboard_dir / "server.mjs")],
                env=env,
                cwd=str(dashboard_dir),
            )
            console.print(f"[dim]Dashboard server started (PID {node_proc.pid})[/dim]")
        except FileNotFoundError:
            console.print(
                "[yellow]Node.js not found — dashboard UI will not be available.[/yellow]"
            )
    else:
        console.print("[yellow]Dashboard files not found — API only mode.[/yellow]")

    if not no_browser:

        def _open_browser():
            time.sleep(2)
            import webbrowser

            webbrowser.open(f"http://localhost:{dashboard_port}")

        threading.Thread(target=_open_browser, daemon=True).start()

    # Check if a runtime is already listening on the API port (e.g. started
    # via `olano server`).  If so, just keep the dashboard proxy alive
    # instead of trying to bind a second RuntimeServer on the same port.
    #
    # We probe TWO signals — the HTTP API AND the daemon PID file — because
    # the API can be unreachable (silent boot failure, TIME_WAIT after
    # SIGKILL, etc.) while the daemon process is still running.  Booting a
    # second RuntimeServer in that case causes catastrophic resource
    # collisions: Telegram 409 conflicts, WhatsApp session lock errors,
    # duplicate scheduler jobs, double-message-delivery, etc.
    from olano.runtime.daemon import _http_get_json, is_pid_alive, read_pid

    def _api_up() -> bool:
        return _http_get_json(runtime_port, "/api/status") is not None

    daemon_pid = read_pid()
    daemon_alive = daemon_pid is not None and is_pid_alive(daemon_pid)
    runtime_already_up = _api_up()

    if daemon_alive and not runtime_already_up:
        console.print(
            f"[dim]Daemon PID {daemon_pid} found — waiting up to 10s for its API to come up...[/dim]"
        )
        for _ in range(20):
            time.sleep(0.5)
            if _api_up():
                runtime_already_up = True
                break

    if daemon_alive and not runtime_already_up:
        console.print(
            f"[bold red]A olano daemon is running (PID {daemon_pid}) but its runtime API on "
            f"port {runtime_port} is not responding.[/bold red]"
        )
        console.print(
            "[yellow]This usually means the API failed to bind at daemon startup "
            "(commonly a port still in TIME_WAIT after a SIGKILL restart).[/yellow]"
        )
        console.print(
            "[dim]Booting a second runtime here would cause Telegram 409 conflicts, "
            "WhatsApp session-lock errors, and duplicate message delivery.[/dim]\n"
            "[dim]Fix it with:[/dim]\n"
            "  [bold]olano server stop[/bold]\n"
            "  [bold]olano server start[/bold]   "
            "[dim](or use foreground mode: `olano serve`)[/dim]\n"
            f"[dim]Then check: [bold]curl http://localhost:{runtime_port}/api/status[/bold][/dim]"
        )
        if node_proc:
            node_proc.terminate()
        return

    if runtime_already_up:
        console.print(
            f"[dim]Runtime already running on port {runtime_port} — dashboard proxy only.[/dim]"
        )
        console.print("[green]Runtime ready.[/green] Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            if node_proc:
                node_proc.terminate()
    else:
        try:
            asyncio.run(RuntimeServer(agent_names=agent_list).start())
        finally:
            if node_proc:
                node_proc.terminate()


@cli.command(name="rebuild-dashboard")
def rebuild_dashboard_cmd() -> None:
    """Rebuild the Mission Control dashboard SPA for this deployment.

    Recompiles the Vite bundle the right way for how olano is installed
    (local checkout, systemd host, or docker) so a UI change actually shows up.
    Same logic `/olano rebuild` uses — run this from the host when there is no
    server bound to a chat.
    """
    from olano.platform_control.detect import (
        detect_install_mode,
        detect_supervision,
        repo_root,
    )
    from olano.update.dashboard import rebuild_dashboard

    console.print("[bold]Rebuilding dashboard…[/bold]")
    dash = rebuild_dashboard(repo_root(), detect_install_mode(), detect_supervision())
    if dash.built:
        console.print(f"  [green]✓[/green] {dash.detail}")
    elif dash.host_command:
        console.print(f"  [yellow]→[/yellow] {dash.detail}")
        console.print(f"  Run on the host: [bold]{dash.host_command}[/bold]")
    elif dash.attempted:
        console.print(f"  [red]✗[/red] {dash.detail}")
        tail = dash.log[-1000:].strip() if dash.log else ""
        if tail:
            console.print(f"[dim]{tail}[/dim]")
    else:
        console.print(f"  [dim]{dash.detail}[/dim]")


# ─────────────────────────────────────── update ──────────────────────────────


def _render_agent_refresh_results(results: list[Any], *, check: bool) -> None:
    """Print a per-agent summary for `agents refresh` / `update --all`."""
    verb = "would update" if check else "updated"
    if not results:
        console.print("[yellow]No agents found.[/yellow]")
        return
    header = "Previewing system-zone refresh" if check else "Refreshing agent system zones"
    console.print(f"[bold]{header}[/bold]\n")
    any_change = False
    for r in results:
        if r.error:
            console.print(f"  [yellow]⚠[/yellow] {r.agent}: [red]{r.error}[/red]")
            continue
        if r.changed:
            any_change = True
            console.print(f"  [green]{r.agent}[/green]: {len(r.changed)} file(s) {verb}")
            for relpath in r.changed:
                console.print(f"      [dim]{relpath}[/dim]")
            if r.pulse_id:
                console.print(f"      [dim]PULSE review item {r.pulse_id}[/dim]")
        else:
            console.print(f"  [dim]{r.agent}: up-to-date[/dim]")
    if check and any_change:
        console.print(
            "\n  [dim]Dry-run — no files were written. Re-run without --check to apply.[/dim]"
        )


@cli.command(
    epilog=(
        "Examples:\n"
        "  olano update                       # full self-update (package + skills + migrations)\n"
        "  olano update --check               # dry-run: show what would change\n"
        "  olano update --all                 # refresh every agent's system/ zone\n"
        "  olano update --rollback            # reinstall previous version\n"
        "  olano update --agent merli         # refresh only that agent's system/ zone (legacy)\n"
        "  olano update --workspace ./workspace\n"
        "  uv tool upgrade olano"
    ),
)
@click.option(
    "--all",
    "refresh_all",
    is_flag=True,
    help="Refresh every agent's system/ zone (prompts, knowledge, schemas) to the installed release",
)
@click.option(
    "--agent",
    default=None,
    help="Agent whose workspace system/ zone should be refreshed (legacy path)",
)
@click.option("--workspace", default=None, help="Workspace root to refresh (legacy path)")
@click.option(
    "--check", "check_mode", is_flag=True, help="Dry-run: show what would change without writing"
)
@click.option("--rollback", is_flag=True, help="Reinstall the previous version from history")
@click.option(
    "--post-upgrade",
    "post_upgrade",
    is_flag=True,
    hidden=True,
    help="Internal: run post-upgrade steps after package replacement",
)
@click.pass_context
def update(
    ctx: click.Context,
    refresh_all: bool,
    agent: str | None,
    workspace: str | None,
    check_mode: bool,
    rollback: bool,
    post_upgrade: bool,
) -> None:
    """Upgrade Olano: package, built-in skills, and migrations. Or refresh a workspace system/ zone."""
    active_home = get_olano_home(ctx.obj.get("home") if ctx.obj else None)

    # ── Refresh-all path: --all (re-sync every agent's system zone) ───────────
    if refresh_all:
        from olano.update.refresh import refresh_all_agents

        _render_agent_refresh_results(
            refresh_all_agents(source="home", check=check_mode), check=check_mode
        )
        return

    # ── Legacy path: --agent or --workspace ──────────────────────────────────
    if agent or workspace:
        from olano.archetypes.installer import emit_system_update_pulse, sync_system_zone
        from olano.config.loader import load_agent_config

        template: dict[str, Any]
        agent_name = "Olano"

        if workspace:
            workspace_root = Path(workspace).expanduser().resolve()
            template = {
                "description": "Olano agent workspace",
                "system_prompt": "You are a Olano agent.",
                "subagents": [],
                "tools": [],
                "archetype": "default",
            }
        else:
            selected_agent = agent or _default_agent_name()
            config_source = "auto" if agent else "home"
            config = load_agent_config(selected_agent, source=config_source)
            workspace_root = Path(config.workspace or ".").expanduser().resolve()
            agent_name = config.name
            template = {
                "description": config.description,
                "system_prompt": config.system_prompt,
                "subagents": [sub.model_dump() for sub in config.subagents],
                "tools": list(config.tools),
                "emoji": config.emoji,
                "archetype": config.archetype or config.name,
            }

        changed = sync_system_zone(
            workspace_root,
            agent_name=agent_name,
            template=template,
            overwrite=True,
        )
        pulse_item_id = emit_system_update_pulse(
            workspace_root,
            agent_name=agent_name,
            changed=changed,
        )

        console.print(
            f"[bold]System zone refreshed[/bold]  [dim]{workspace_root / 'system'}[/dim]\n"
        )
        if changed:
            for relpath in changed:
                console.print(f"  [green]updated[/green] {relpath}")
            if pulse_item_id:
                console.print(
                    f"\n  [bold]PULSE[/bold] queued system update review item [dim]{pulse_item_id}[/dim]"
                )
        else:
            console.print("  [dim]No files changed.[/dim]")

        # Also sync built-in skills on legacy path
        from olano.skills.built_in_sync import sync_builtin_skills

        skill_report = sync_builtin_skills(active_home)
        if skill_report.new or skill_report.updated:
            console.print(f"  [green]✓[/green] {skill_report}")
        return

    # ── New self-update path ──────────────────────────────────────────────────
    from olano.update import run_update
    from olano.update.install_state import read_install_state

    state = read_install_state(active_home)
    console.print(f"[bold]Olano update[/bold]  home: [dim]{active_home}[/dim]")
    if state.version:
        console.print(f"  Current version: [dim]{state.version}[/dim]")

    report = run_update(active_home, check=check_mode, rollback=rollback, post_upgrade=post_upgrade)

    if report.check_only:
        console.print(f"  Latest available: [dim]{report.to_version or 'unknown'}[/dim]")
        if report.to_version and report.to_version != report.from_version:
            console.print(
                "  [yellow]→[/yellow] Update available. Run [bold]olano update[/bold] to install."
            )
        else:
            console.print("  [green]✓[/green] Already up to date.")
        return

    if report.rolled_back:
        if report.to_version:
            console.print(f"  [green]✓[/green] Rolled back to {report.to_version}")
        else:
            console.print("  [yellow]⚠[/yellow] No previous version in history to roll back to.")
        return

    if report.package_upgraded:
        console.print(f"  [green]✓[/green] Package: {report.from_version} → {report.to_version}")

    if report.dashboard is not None:
        d = report.dashboard
        if d.built:
            console.print(f"  [green]✓[/green] {d.detail}")
        elif d.host_command:
            console.print(f"  [yellow]→[/yellow] {d.detail}")
            console.print(f"      Run on the host: [bold]{d.host_command}[/bold]")
        elif d.attempted:
            console.print(f"  [red]✗[/red] {d.detail}")

    if report.skills.new or report.skills.updated:
        console.print(f"  [green]✓[/green] {report.skills}")

    if report.migrations_applied:
        for m in report.migrations_applied:
            console.print(f"  [green]✓[/green] Migration: {m}")

    refreshed = [r for r in report.agents_refreshed if r.changed or r.error]
    for r in refreshed:
        if r.error:
            console.print(f"  [yellow]⚠[/yellow] {r.agent}: refresh failed — {r.error}")
        else:
            console.print(f"  [green]✓[/green] {r.agent}: {len(r.changed)} system file(s) updated")

    if not (
        report.package_upgraded
        or report.skills.new
        or report.skills.updated
        or report.migrations_applied
        or refreshed
    ):
        console.print("  [dim]Nothing to update.[/dim]")


# ─────────────────────────────────────── chat (removed) ───────────────────────

_CHAT_REMOVED_NOTICE = (
    "The interactive terminal chat (`olano chat`) has been removed.\n"
    "  Chat with your agents in the dashboard:  olano dashboard\n"
    '  Run a single task from the terminal:     olano --agent <name> -n "<task>"'
)


@cli.command(hidden=True)
@click.option("--agent", "-a", default=None, help="Agent name")
@click.option("--thread", "--resume", "-t", default=None, help="Thread ID to resume")
@click.option("--message", "-m", default=None, help="Task to run once")
@click.option("--non-interactive", "-n", "non_interactive", default=None, help="Task to run once")
@click.option("--quiet", "-q", is_flag=True, help="Pipe-safe output; diagnostics go to stderr")
@click.option("--no-stream", is_flag=True, help="Buffer output and flush once at the end")
@click.option("--shell-allow-list", default=None, help="Typed shell allow-list entries")
@click.option("--model", default=None, help="Override agent model (provider:model)")
@click.option("--no-housekeeping", "no_housekeeping", is_flag=True, hidden=True)
def chat(
    agent: str | None,
    thread: str | None,
    message: str | None,
    non_interactive: str | None,
    quiet: bool,
    no_stream: bool,
    shell_allow_list: str | None,
    model: str | None,
    no_housekeeping: bool,
) -> None:
    """Removed. Kept only so `olano chat -n "..."` scripts keep running one-shot."""
    from olano.cli.non_interactive import run_non_interactive

    if message and non_interactive:
        raise click.UsageError("--message and --non-interactive cannot be used together.")
    message, non_interactive = _merge_prompt_inputs(message, non_interactive)
    task = non_interactive or message
    if not task:
        click.echo(_CHAT_REMOVED_NOTICE, err=True)
        sys.exit(2)

    exit_code = asyncio.run(
        run_non_interactive(
            agent or _default_agent_name(),
            task,
            thread_id=thread,
            model_override=model,
            quiet=quiet,
            stream=not no_stream,
            shell_allow_list=shell_allow_list,
            config_source="auto" if agent else "home",
        )
    )
    sys.exit(exit_code)


# ── Deployment settings (managed entitlements) ──────────────────────────────


@cli.group(
    epilog=(
        "Examples:\n"
        "  olano deployment status\n"
        "  olano deployment install ./deployment.lic\n"
        "  olano deployment refresh"
    ),
)
def deployment() -> None:
    """Deployment settings — the signed, vendor-managed entitlements layer."""


@deployment.command(name="status")
def deployment_status_cmd() -> None:
    """Show the current posture, source, and effective limits."""
    from olano.deployment import resolver

    status = resolver.deployment_status()
    s = status.settings

    posture_style = {"open": "green", "enforced": "cyan", "quarantine": "bold red"}.get(
        status.posture, "white"
    )
    table = Table(title="Deployment Settings", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Posture", f"[{posture_style}]{status.posture}[/{posture_style}]")
    table.add_row("Source", status.source)
    if status.mode:
        table.add_row("Mode", status.mode)
    if status.deployment_id:
        table.add_row("Deployment", status.deployment_id)
    if status.plan:
        table.add_row("Plan", status.plan)
    if status.policy_revision is not None:
        table.add_row("Policy revision", str(status.policy_revision))
    for label, epoch in (
        ("Issued", status.issued_at),
        ("Expires", status.expires_at),
        ("Last refresh", status.last_refresh),
    ):
        if epoch:
            from datetime import datetime, timezone

            table.add_row(label, datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat())
    if status.error:
        table.add_row("Error", f"[red]{status.error}[/red]")
    console.print(table)

    if status.posture == "open":
        console.print("[#d4d4d8]Unmanaged installation — no limits enforced.[/#d4d4d8]")
        return

    limits = Table(title="Limits", show_header=True)
    limits.add_column("Limit")
    limits.add_column("Value", justify="right")
    limits.add_column("Window")
    r = s.resources
    for name, value in (
        ("max agents", r.max_agents),
        ("max connectors / agent", r.max_connectors_per_agent),
        ("max connectors total", r.max_connectors_total),
        ("max MCP servers total", r.max_mcp_servers_total),
        ("max webhooks total", r.max_webhooks_total),
        ("max users", r.max_users),
    ):
        limits.add_row(name, "∞" if value is None else str(value), "—")
    console.print(limits)

    # Usage budgets get their own table: unlike the resource caps they are
    # metered, and a locally overridden one must never be printed as if it
    # came down signed — "why is my agent refused at a number the plan doesn't
    # say" is exactly the question this table exists to answer.
    from olano.deployment.usage_budgets import alert_percents, effective_usage_limits
    from olano.deployment.usage_counters import current_usage_for_limit

    budgets = Table(title="Usage budgets", show_header=True)
    budgets.add_column("Budget")
    budgets.add_column("Used", justify="right")
    budgets.add_column("Limit", justify="right")
    budgets.add_column("Window")
    budgets.add_column("Source")
    for eff in effective_usage_limits(s.usage):
        used = current_usage_for_limit(eff.key)
        if eff.unit == "micro_usd":
            used_text = "—" if used is None else f"${used / 1_000_000:,.2f}"
            limit_text = "∞" if eff.limit.value is None else f"${eff.limit.value / 1_000_000:,.2f}"
        else:
            used_text = "—" if used is None else f"{used:,}"
            limit_text = "∞" if eff.limit.value is None else f"{eff.limit.value:,}"
        if eff.overridden:
            was = "∞" if eff.deployment_limit.value is None else f"{eff.deployment_limit.value:,}"
            source = f"[yellow]config.yaml[/yellow] (plan: {was})"
        else:
            source = "deployment"
        budgets.add_row(eff.label, used_text, limit_text, eff.limit.window, source)
    console.print(budgets)
    percents = alert_percents()
    console.print(
        f"[#d4d4d8]Budget alerts at {', '.join(f'{p}%' for p in percents)} and 100% "
        f"(config.yaml → usage_budgets.alert_percents).[/#d4d4d8]"
        if percents
        else "[#d4d4d8]Budget alerts are off (usage_budgets.alerts_enabled: false).[/#d4d4d8]"
    )

    disabled_features = sorted(k for k, v in s.features.items() if not v)
    disabled_tabs = sorted(k for k, v in s.tabs.items() if not v)
    if disabled_features:
        console.print(f"[bold]Disabled features:[/bold] {', '.join(disabled_features)}")
    if disabled_tabs:
        console.print(f"[bold]Hidden tabs:[/bold] {', '.join(disabled_tabs)}")
    if s.credits_left is not None:
        console.print(f"[bold]Credits left:[/bold] {s.credits_left}")


@deployment.command(name="install")
@click.argument("license_source")
def deployment_install(license_source: str) -> None:
    """Install a signed local license (path or the literal license string).

    The license is verified — signature, channel, expiry, and deployment
    binding — BEFORE anything is written.
    """
    from pathlib import Path

    from olano.deployment import resolver, sources
    from olano.deployment.envelope import LicenseError, verify_license

    candidate = Path(license_source)
    text = (
        candidate.read_text(encoding="utf-8").strip()
        if candidate.exists()
        else license_source.strip()
    )

    expected_id: str | None = None
    try:
        manifest = sources.read_manifest()
    except sources.ManifestError as exc:
        console.print(f"[red]Installation manifest is invalid: {exc}[/red]")
        raise SystemExit(1) from exc
    if manifest and manifest.managed:
        expected_id = manifest.deployment_id
        if manifest.mode == "cloud":
            console.print(
                "[yellow]This installation is cloud-managed — a local license is "
                "ignored while cloud mode is active.[/yellow]"
            )

    try:
        payload = verify_license(text, expected_channel="local", expected_deployment_id=expected_id)
    except LicenseError as exc:
        console.print(f"[red]License rejected ({exc.code}): {exc}[/red]")
        raise SystemExit(1) from exc

    paths = get_olano_paths()
    paths.deployment_license_path.write_text(text + "\n", encoding="utf-8")
    paths.deployment_license_path.chmod(0o600)
    resolver.invalidate()
    console.print(
        f"[green]License installed[/green] — deployment [bold]{payload.deployment_id}[/bold], "
        f"plan {payload.plan or '—'}, revision {payload.policy_revision}."
    )
    if manifest is None or not manifest.managed:
        console.print(
            "[yellow]No installation manifest found — this install is unmanaged and the "
            "license has no effect until the manifest exists.[/yellow]"
        )


@deployment.command(name="refresh")
def deployment_refresh() -> None:
    """Force a settings refresh from the control plane (managed-cloud)."""
    from olano.deployment import resolver

    status = resolver.refresh()
    style = {"open": "green", "enforced": "cyan", "quarantine": "bold red"}.get(
        status.posture, "white"
    )
    console.print(
        f"Posture [{style}]{status.posture}[/{style}], source {status.source}"
        + (f", error: [red]{status.error}[/red]" if status.error else "")
    )


@deployment.command(name="remove")
@click.confirmation_option(prompt="Remove the local license and cached settings?")
def deployment_remove() -> None:
    """Remove the local license + cache.

    On a MANAGED installation this leads to quarantine (never back to the
    open posture) — the installation manifest, not the license file, decides
    that this deployment is managed.
    """
    from olano.deployment import resolver, sources

    try:
        manifest = sources.read_manifest()
    except sources.ManifestError:
        manifest = None
    paths = get_olano_paths()
    removed = []
    if paths.deployment_license_path.exists():
        paths.deployment_license_path.unlink()
        removed.append(str(paths.deployment_license_path))
    for name in (sources.CACHE_FILE, sources.CACHE_META_FILE, sources.REVISION_FILE):
        p = paths.deployment_dir / name
        if p.exists():
            p.unlink()
            removed.append(str(p))
    resolver.invalidate()
    console.print("Removed: " + (", ".join(removed) if removed else "nothing to remove"))
    if manifest and manifest.managed:
        console.print(
            "[bold red]This installation is managed — without a valid license it is now "
            "quarantined (recovery-only).[/bold red] Install a valid license to restore service."
        )


@deployment.command(name="unmanage")
def deployment_unmanage() -> None:
    """Development only: delete the installation manifest (returns to open).

    Refuses when the manifest is root-owned and this process is not root —
    on a provisioned customer box this is not an available operation.
    """
    from olano.deployment import resolver, sources

    path = sources.manifest_path()
    if not path.exists():
        console.print("No installation manifest — already unmanaged.")
        return
    try:
        owner_uid = path.stat().st_uid
    except OSError as exc:
        console.print(f"[red]Cannot stat manifest: {exc}[/red]")
        raise SystemExit(1) from exc
    if owner_uid == 0 and os.geteuid() != 0:
        console.print(
            "[red]The installation manifest is root-owned — unmanage is not available "
            "on a managed deployment.[/red]"
        )
        raise SystemExit(1)
    path.unlink()
    resolver.invalidate()
    console.print("[green]Installation manifest removed — this install is now unmanaged.[/green]")
