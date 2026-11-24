"""The owner-only ``/olano`` chat command.

Dispatched from :class:`olano.cli.commands.CommandDispatcher` (never the
model). Two gates apply before anything runs:

1. Per-agent exposure — ``platform`` is a *sensitive* catalog entry, default
   OFF; the agentfather archetype enables it and owners can grant it to
   other agents via ``commands.enabled`` (handled upstream in
   ``maybe_handle_command``).
2. Per-sender ownership — the sender must match the channel's
   ``allowed_users`` allowlist (:func:`_owner_gate` below). Open bots (no
   allowlist) are refused with setup guidance. A caller with no sender (an
   owner console) is never gated.

Every destructive subcommand (update / restart / shutdown) stages first and
requires a ``… confirm`` follow-up within :data:`CONFIRM_TTL` seconds.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from olano.cli.commands import CommandContext, CommandResult
from olano.platform_control.controller import (
    Notify,
    get_platform_controller,
)
from olano.platform_control.detect import SupervisionMode, systemd_unit_name

if TYPE_CHECKING:
    from olano.platform_control.controller import PlatformController

CONFIRM_TTL = 120.0

_USAGE = (
    "Usage: /olano <command>\n"
    "\n"
    "Common:\n"
    "  status               Health, version, uptime + dashboard freshness\n"
    "  connect <service>    Send yourself a magic link (github, google, a key…)\n"
    "  dashboard [section]  Sign in to the dashboard on your phone (prompt, vault…)\n"
    "  update               Pull code, sync deps, rebuild the dashboard, restart\n"
    "  restart              Restart the server\n"
    "  shutdown             Stop the server\n"
    "\n"
    "Advanced:\n"
    "  connect list         What can be connected on this deployment\n"
    "  connect secret NAME  Secure form to store a credential in the vault\n"
    "  connect provider P   Store a model provider's API key (anthropic, groq…)\n"
    "  version              Version / commit\n"
    "  update check         Preview what an update would change\n"
    "  update --force       Re-sync deps + rebuild the dashboard even if unchanged\n"
    "  update --no-restart  Apply without restarting\n"
    "  rebuild [dashboard]  Rebuild the dashboard now (no code pull)\n"
    "  restart server       Restart just the backend\n"
    "\n"
    "Destructive actions ask for a `… confirm` follow-up."
)

_RESTART_TARGETS = {"all", "server", "dashboard"}
_REBUILD_TARGETS = {"dashboard", "ui"}


@dataclass
class _Pending:
    action: str  # "restart" | "shutdown" | "update"
    target: str  # restart target ("server"/"all"); "" otherwise
    expires: float


# Staged confirmations, keyed per agent + chat so one owner's pending action
# can't be confirmed from another conversation.
_PENDING: dict[str, _Pending] = {}


def _pending_key(ctx: CommandContext) -> str:
    chat = str(getattr(ctx.sender, "chat_id", "") or "") if ctx.sender is not None else ""
    return f"{ctx.config.name}:{chat or ctx.thread_id}"


def _stage(ctx: CommandContext, action: str, target: str = "") -> None:
    _PENDING[_pending_key(ctx)] = _Pending(action, target, time.time() + CONFIRM_TTL)


def _take_pending(ctx: CommandContext, action: str) -> _Pending | None:
    key = _pending_key(ctx)
    pending = _PENDING.get(key)
    if pending is None or pending.action != action:
        return None
    del _PENDING[key]
    if pending.expires < time.time():
        return None
    return pending


def _notify_from(ctx: CommandContext) -> Notify | None:
    sender = ctx.sender
    if sender is None:
        return None
    chat_id = str(getattr(sender, "chat_id", "") or "")
    channel = str(getattr(sender, "channel", "") or "")
    if not (chat_id and channel):
        return None
    return Notify(agent=ctx.config.name, channel=channel, target=chat_id)


def _owner_gate(ctx: CommandContext) -> CommandResult | None:
    sender = ctx.sender
    if sender is None:
        # Owner console (no per-sender identity): never gated.
        return None
    if not getattr(sender, "allowlist_configured", False):
        return CommandResult(
            output=(
                "🔒 /olano is owner-only, and this channel has no allowlist — "
                "anyone who finds this bot could run it.\n"
                "Add your user id to the channel's `allowed_users` in the agent "
                "config (dashboard → agent → Channels), restart, then try again."
            )
        )
    if not getattr(sender, "is_trusted", False):
        return CommandResult(output="🔒 /olano is restricted to this deployment's owner.")
    return None


def _fmt_uptime(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    minutes, _ = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _restart_hint(mode: SupervisionMode) -> str:
    return {
        SupervisionMode.DOCKER: "the container supervisor brings me back automatically",
        SupervisionMode.SYSTEMD: "systemd brings me back automatically",
        SupervisionMode.DAEMON: "the daemon re-executes in place",
        SupervisionMode.FOREGROUND: "the process re-executes in place",
    }[mode]


async def handle_platform_command(args: list[str], ctx: CommandContext) -> CommandResult:
    gate = _owner_gate(ctx)
    if gate is not None:
        return gate

    controller = get_platform_controller()
    sub = args[0].lower() if args else "status"
    rest = [a.lower() for a in args[1:]]

    # Read-only introspection works anywhere; the lifecycle actions need a live
    # server bound to this process; outside one there is nothing to act on.
    # (`rebuild` reports its result via a follow-up to the live server's chat,
    # so it needs one too; from a shell use `olano rebuild-dashboard` instead.)
    if sub in ("restart", "shutdown", "update", "rebuild") and not controller.can_control():
        return CommandResult(
            output=(
                "🚫 Platform control isn't available here: this process has no "
                "running Olano server bound to it. Run it where the server is "
                "running, or from the dashboard."
            )
        )

    if sub == "status":
        return await _status(controller)
    if sub == "version":
        return _version(controller)
    if sub == "connect":
        # Note: args[1:] verbatim, not the lower-cased `rest` — a credential
        # name and an MCP URL are both case-bearing.
        return _connect(args[1:], ctx)
    if sub == "dashboard":
        return _dashboard(rest, ctx)
    if sub == "restart":
        return await _restart(controller, rest, ctx)
    if sub == "shutdown":
        return _shutdown(controller, rest, ctx)
    if sub == "update":
        return await _update(controller, rest, ctx)
    if sub == "rebuild":
        return await _rebuild(controller, rest, ctx)
    return CommandResult(output=f"Unknown /olano subcommand: {sub}\n{_USAGE}")


def _busy_reply(strategy: str) -> CommandResult | None:
    """Map a ``busy-*`` scheduler result to a user-facing refusal."""
    if not strategy.startswith("busy-"):
        return None
    if strategy == "busy-update":
        return CommandResult(
            output="⏳ An update is in progress — I'll restart automatically when it finishes."
        )
    return CommandResult(output="⏳ A restart/shutdown is already scheduled — hang tight.")


# ── status / version ──────────────────────────────────────────────────────


async def _status(controller: PlatformController) -> CommandResult:
    from olano.platform_control.detect import repo_root
    from olano.update.dashboard import dashboard_freshness

    info = controller.status()
    reachable = "reachable" if await controller.dashboard_reachable() else "not detected"
    freshness = dashboard_freshness(repo_root(), controller.install_mode, controller.supervision)
    version = info["version"] + (f" ({info['commit']})" if info.get("commit") else "")
    supervision = info["supervision"] + (
        f" ({info['systemd_unit']})" if info.get("systemd_unit") else ""
    )
    agents = info.get("agents") or []
    lines = [
        "🖥️ Olano platform",
        f"Version:      {version}",
        f"Install:      {info['install_mode']}",
        f"Supervision:  {supervision}",
        f"Uptime:       {_fmt_uptime(info.get('uptime_seconds'))}",
        f"Agents:       {len(agents)} running" + (f" ({', '.join(agents)})" if agents else ""),
        f"Dashboard:    {reachable} · build {freshness}",
        f"Update:       {'in progress' if info.get('update_in_progress') else 'idle'}",
    ]
    return CommandResult(output="\n".join(lines))


def _version(controller: PlatformController) -> CommandResult:
    info = controller.status()
    version = info["version"] + (f" ({info['commit']})" if info.get("commit") else "")
    return CommandResult(output=f"Olano v{version} — installed via {info['install_mode']}")


# ── connect (auth magic links) ────────────────────────────────────────
# The deterministic twin of the `magic_link_connect_service` agent tool: same gates, same
# link, but no model in the loop — which is what you want when the agent is
# confused, unavailable, or simply not trusted with the tool.


_CONNECT_USAGE = (
    "Usage: /olano connect <what>\n"
    "\n"
    "  github                    Connect GitHub (OAuth app or a sign-in code)\n"
    "  google [agent]            Connect a Google account\n"
    "  codex                     Connect OpenAI Codex (ChatGPT account)\n"
    "  <service> [agent]         A connector: microsoft365, shopify, salesforce, …\n"
    "  https://…/mcp <agent>     Authorize an MCP server for an agent\n"
    "  secret NAME [agent]       Secure form to store a credential in the vault\n"
    "  edit PATH [agent]         Edit a file in a browser (config, .env, notes)\n"
    "  provider NAME [agent]     Store a model provider's API key\n"
    "  list                      What this deployment can connect\n"
    "\n"
    "You get a link + PIN; open it on your phone. I'll confirm here when it lands."
)


def _connect_origin(ctx: CommandContext) -> tuple[dict, dict]:
    """``(requested_by, notify)`` for a link minted from a chat command."""
    from olano.security.request_context import get_request_context

    sender = ctx.sender
    agent = ctx.config.name
    channel = str(getattr(sender, "channel", "") or "")
    chat_id = str(getattr(sender, "chat_id", "") or "")
    requested_by = {
        "channel": channel or "local",
        "user_id": str(getattr(sender, "user_id", "") or ""),
        "username": str(getattr(sender, "username", "") or ""),
        "agent": agent,
        "is_group": bool(getattr(get_request_context(), "is_group", False)),
    }
    notify = {"channel_type": channel, "target": chat_id or "owner", "agent_name": agent}
    return requested_by, notify


def _connect_list() -> CommandResult:
    from olano.core.connectors import provider_ids
    from olano.magic_links.service import magic_links_available

    ok, reason = magic_links_available()
    lines = ["🔗 Connectable on this deployment", ""]
    if not ok:
        lines.append(f"⚠️ Links are unavailable: {reason}")
        lines.append("")
    lines += [
        "Built-in:  github · google · codex",
        "Vault:     secret <NAME> · provider <anthropic|openai|groq|…>",
        "MCP:       paste the server URL (needs an agent name)",
        "",
        "Connectors: " + ", ".join(provider_ids()),
    ]
    return CommandResult(output="\n".join(lines))


def _connect(args: list[str], ctx: CommandContext) -> CommandResult:
    if not args:
        return CommandResult(output=_CONNECT_USAGE)

    head = args[0].strip()
    lowered = head.lower()
    if lowered in ("list", "ls", "what", "help"):
        return _connect_list()

    rest = [a.strip() for a in args[1:] if a.strip()]
    flow: str
    params: dict[str, object]

    if lowered in ("edit", "file"):
        if not rest:
            return CommandResult(
                output="Which file? e.g. /olano connect edit config/settings.yaml <agent>"
            )
        flow = "edit-file"
        params = {"path": rest[0], "agent": rest[1] if len(rest) > 1 else ""}
    elif lowered in ("secret", "key"):
        if not rest:
            return CommandResult(
                output="Which credential? e.g. /olano connect secret STRIPE_API_KEY"
            )
        flow = "secret-entry"
        params = {"name": rest[0], "agent": rest[1] if len(rest) > 1 else ""}
    elif lowered in ("provider", "model"):
        if not rest:
            return CommandResult(output="Which provider? e.g. /olano connect provider anthropic")
        flow = "provider-key"
        params = {"provider": rest[0], "agent": rest[1] if len(rest) > 1 else ""}
    elif lowered.startswith(("http://", "https://")):
        if not rest:
            return CommandResult(
                output="An MCP server is authorized per agent — say which: "
                f"/olano connect {head} <agent>"
            )
        flow, params = "mcp-oauth", {"url": head, "agent": rest[0]}
    else:
        from olano.magic_links.routing import resolve_connect_flow

        try:
            flow, params = resolve_connect_flow(head, agent=rest[0] if rest else "")
        except ValueError as exc:
            return CommandResult(output=str(exc))

    return _mint_and_reply(flow, params, ctx)


def _mint_and_reply(flow: str, params: dict, ctx: CommandContext) -> CommandResult:
    """Mint a magic link and format the link + PIN reply for the chat."""
    from olano.magic_links import service
    from olano.magic_links import watch as watch_mod

    requested_by, notify = _connect_origin(ctx)
    try:
        minted = service.mint_link(
            flow,
            params=params,
            requested_by=requested_by,
            notify=notify,
            minted_by=f"olano-command:{requested_by.get('username') or requested_by.get('user_id')}",
        )
    except service.MagicLinkError as exc:
        return CommandResult(output=f"🚫 {exc}")

    watch_mod.spawn(minted.id)
    lines = [f"🔐 {minted.label} — open this on your phone:", minted.url]
    if minted.pin:
        lines.append(f"PIN: {minted.pin}")
    lines.append(f"Valid {minted.ttl_minutes} min. I'll confirm here when it's done.")
    return CommandResult(output="\n".join(lines))


_DASHBOARD_SECTIONS = {"overview", "prompt", "vault", "connections", "providers", "config", "files"}


def _dashboard(rest: list[str], ctx: CommandContext) -> CommandResult:
    """``/olano dashboard [section]`` — a link that signs the owner into the dashboard."""
    section = rest[0] if rest else "overview"
    if section not in _DASHBOARD_SECTIONS:
        return CommandResult(
            output=(
                f"Unknown section '{section}'. Choose one of: "
                f"{', '.join(sorted(_DASHBOARD_SECTIONS))}."
            )
        )
    params: dict = {"section": section}
    if section == "prompt":
        params["agent"] = ctx.config.name  # this agent's config page
    return _mint_and_reply("owner-login", params, ctx)


# ── restart ───────────────────────────────────────────────────────────────


async def _restart(
    controller: PlatformController, rest: list[str], ctx: CommandContext
) -> CommandResult:
    confirm = "confirm" in rest
    targets = [a for a in rest if a != "confirm"]
    target = targets[0] if targets else "all"
    if target not in _RESTART_TARGETS:
        return CommandResult(
            output=f"Unknown restart target: {target}. Use all, server, or dashboard."
        )

    if target == "dashboard":
        return await _restart_dashboard_info(controller)

    if confirm:
        pending = _take_pending(ctx, "restart")
        if pending is None:
            return CommandResult(
                output=(
                    "Nothing staged (or the 2-minute window expired). Run `/olano restart` first."
                )
            )
        strategy_hint = _restart_hint(controller.supervision)
        strategy = controller.schedule_restart(notify=_notify_from(ctx))
        busy = _busy_reply(strategy)
        if busy is not None:
            return busy
        return CommandResult(
            output=(
                f"🔄 Restarting the Olano server now — {strategy_hint}. "
                "I'll message you here when I'm back online (typically 15–60s)."
            )
        )

    _stage(ctx, "restart", target)
    note = (
        " (`all` restarts the server; see `/olano restart dashboard` for the dashboard)"
        if target == "all"
        else ""
    )
    return CommandResult(
        output=(
            f"🔄 Restart the Olano server?{note}\n"
            f"All agents and channels reconnect; in-flight work is stopped. "
            f"After the restart {_restart_hint(controller.supervision)}.\n"
            f"Reply `/olano restart confirm` within 2 minutes."
        )
    )


async def _restart_dashboard_info(controller: PlatformController) -> CommandResult:
    reachable = await controller.dashboard_reachable()
    state = "it currently responds to ping" if reachable else "it does not respond to ping"
    # Restarting the dashboard *process* keeps serving the same built files, so
    # it never surfaces a UI change. `/olano rebuild` recompiles the SPA the
    # right way for this deployment — point there instead.
    return CommandResult(
        output=(
            f"ℹ️ Dashboard status: {state}.\n"
            "To pick up UI changes run `/olano rebuild` — a plain process restart "
            "keeps serving the previously-built files."
        )
    )


# ── shutdown ──────────────────────────────────────────────────────────────


def _shutdown(
    controller: PlatformController, rest: list[str], ctx: CommandContext
) -> CommandResult:
    mode = controller.supervision
    if mode is SupervisionMode.DOCKER:
        return CommandResult(
            output=(
                "🛑 I can't shut myself down from inside the container — the "
                "supervisor would just restart me, and stopping the container "
                "is a host-side action.\nFrom the host: `docker stop <container>` "
                "or `docker compose down`."
            )
        )

    if "confirm" in rest:
        pending = _take_pending(ctx, "shutdown")
        if pending is None:
            return CommandResult(
                output=(
                    "Nothing staged (or the 2-minute window expired). Run `/olano shutdown` first."
                )
            )
        strategy = controller.schedule_shutdown(notify=_notify_from(ctx))
        busy = _busy_reply(strategy)
        if busy is not None:
            return busy
        if strategy == "systemctl":
            unit = systemd_unit_name() or "olano.service"
            return CommandResult(
                output=(
                    f"🛑 Asking systemd to stop `{unit}`. If it needs root I'll "
                    "message you the host command instead. Goodbye for now."
                )
            )
        return CommandResult(
            output="🛑 Shutting down. Restart me from the host when you need me. Goodbye."
        )

    if mode is SupervisionMode.SYSTEMD:
        unit = systemd_unit_name() or "olano.service"
        back = f"`sudo systemctl start {unit}` on the host"
    else:
        back = "`olano server start` (or `olano serve`) on the host"
    _stage(ctx, "shutdown")
    return CommandResult(
        output=(
            "🛑 Shut down the Olano server?\n"
            f"**After shutdown I cannot be restarted from chat** — you'll need {back}.\n"
            "Reply `/olano shutdown confirm` within 2 minutes."
        )
    )


# ── update ────────────────────────────────────────────────────────────────


def _has_flag(rest: list[str], *names: str) -> bool:
    return any(a in names for a in rest)


async def _update(
    controller: PlatformController, rest: list[str], ctx: CommandContext
) -> CommandResult:
    from olano.config.home import get_olano_home
    from olano.update.strategies import check_update

    check_only = _has_flag(rest, "check", "--check")
    confirm = "confirm" in rest
    force = _has_flag(rest, "--force", "force")
    no_restart = _has_flag(rest, "--no-restart", "no-restart")

    if controller.update_in_progress():
        return CommandResult(output="⏳ An update is already in progress — hang tight.")

    if confirm:
        pending = _take_pending(ctx, "update")
        if pending is None:
            return CommandResult(
                output=(
                    "Nothing staged (or the 2-minute window expired). Run `/olano update` first."
                )
            )
        # Flags may be given at stage time (persisted in the pending target) or
        # repeated on the confirm line — honour either.
        staged = set(pending.target.split(",")) if pending.target else set()
        force = force or "force" in staged
        no_restart = no_restart or "no-restart" in staged
        if not controller.begin_update(
            notify=_notify_from(ctx), force=force, restart=not no_restart
        ):
            return CommandResult(
                output="⏳ An update or restart is already in progress — hang tight."
            )
        forced = " (forced)" if force else ""
        if no_restart:
            return CommandResult(
                output=f"⬆️ Updating{forced}… I'll message you when it's done (no auto-restart)."
            )
        return CommandResult(
            output=(
                f"⬆️ Updating{forced}… I'll message you here when it's done, then restart. "
                "Other conversations keep working meanwhile."
            )
        )

    check = await asyncio.to_thread(check_update, get_olano_home())
    if not check.can_apply:
        return CommandResult(output=f"ℹ️ {check.details}")
    if not check.update_available and not force:
        return CommandResult(output=f"✅ Already up to date. {check.details}")
    if check_only:
        return CommandResult(
            output=f"⬆️ Update available. {check.details}\nRun `/olano update` to apply."
        )

    flags = [f for f, on in (("force", force), ("no-restart", no_restart)) if on]
    _stage(ctx, "update", ",".join(flags))
    head = "⬆️ Update available." if check.update_available else "⬆️ Re-applying current version."
    steps = (
        "update the code, sync dependencies, and rebuild the dashboard if it changed"
        if no_restart
        else "update the code, sync dependencies, rebuild the dashboard if it changed, "
        "and **restart the server**"
    )
    return CommandResult(
        output=(
            f"{head} {check.details}\n"
            f"Applying will {steps}.\n"
            "Reply `/olano update confirm` within 2 minutes."
        )
    )


# ── rebuild ───────────────────────────────────────────────────────────────


async def _rebuild(
    controller: PlatformController, rest: list[str], ctx: CommandContext
) -> CommandResult:
    target = next((a for a in rest if a != "confirm"), "dashboard")
    if target not in _REBUILD_TARGETS:
        return CommandResult(output=f"Unknown rebuild target: {target}. Use `dashboard`.")
    if not controller.begin_rebuild(notify=_notify_from(ctx)):
        return CommandResult(output="⏳ An update or rebuild is already in progress — hang tight.")
    return CommandResult(
        output=(
            "🔧 Rebuilding the dashboard now — this can take a minute. "
            "I'll message you here when it's done."
        )
    )
