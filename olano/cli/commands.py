"""
Slash command dispatcher for the messaging platforms (Telegram, Discord, Slack, …).

Supported commands:
    /help, /commands          — list commands
    /status                   — show agent/session info
    /whoami, /id              — show current thread/session
    /tools [compact|verbose|full] — list available tools
    /skills                   — list loaded skills
    /agents                   — list all configured agents
    /model [name]             — show or switch model
    /mode [full|task|minimal|none] — show or switch prompt mode
    /memory [section]         — show MEMORY.md
    /new, /clear              — start a fresh session (new thread)
    /reload-agent             — reload agent.json5 from disk and rebuild the agent
    /reload-vault             — re-read vault secrets and rebuild the agent
    /think <level>            — hint thinking level (off|low|medium|high)
    /verbosity [mode]         — set reply detail (quiet|normal|verbose)
    /verbose [on|off]         — toggle verbose mode
    /quiet, /normal           — verbosity shortcuts
"""

from __future__ import annotations

import logging
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from pathlib import Path

    from olano.config.agent_config import AgentConfig
    from olano.core.runner import AgentRunner

from olano.config.commands_catalog import COMMAND_GROUPS, COMMAND_SPECS

logger = logging.getLogger(__name__)

# Descriptions for the slash commands we expose to chat clients (Telegram
# menu, Discord slash list, …), **derived** from the canonical catalog in
# :mod:`olano.config.commands_catalog`.
#
# This used to be a hand-maintained dict, which meant two lists of the same 32
# commands with different wording and no notion of which are *sensitive* — so
# the published menus advertised commands the per-agent exposure config turns
# off, and tapping one answered "🔒 … is disabled for this agent". Deriving it
# makes that drift impossible; ``tests/test_command_menu.py`` pins the two.
#
# Insertion order is catalog order, which is also the order the dashboard
# Commands tab renders — so a menu and the tab that controls it agree.
SLASH_COMMAND_DESCRIPTIONS: dict[str, str] = {f"/{_s.name}": _s.description for _s in COMMAND_SPECS}


# Short argument hints for the built-ins that take one, so Discord/Slack can
# describe the parameter. Keyed by canonical name; a custom command's argument
# is free text (whatever the user types lands in ``{args}``), so it has none.
_ARG_HINTS: dict[str, str] = {
    "tools": "compact|verbose|full",
    "model": "provider:model",
    "mode": "full|task|minimal|none",
    "memory": "section",
    "think": "off|low|medium|high",
    "verbosity": "quiet|normal|verbose",
    "verbose": "on|off",
    "pulse": "list|generate|accept|reject|defer|clear",
    "approvals": "list|approve|reject|defer|clear",
    "ops": "list|reject|defer|clear",
    "trust": "0-4",
    "subagent": "enable|disable name",
    "search": "query",
    "share": "add|list|remove path",
    "olano": "status|update|rebuild|restart|shutdown",
}


@dataclass(frozen=True)
class MenuCommand:
    """One entry in a command menu published to a surface.

    ``name`` is the canonical slash name without the leading ``/``.
    ``custom`` marks an owner-defined command from ``commands.custom`` — those
    have no dispatcher handler (they run their prompt as an ordinary turn), so
    anything asserting "every advertised command has a handler" must skip them.
    """

    name: str
    description: str
    args_hint: str = ""
    custom: bool = False


def _custom_menu_description(cmd: Any) -> str:
    """A menu-worthy description for a custom command.

    The owner may leave ``description`` blank, and a menu entry with no text is
    useless — fall back to the first line of the prompt, which is what the
    command actually does.
    """
    described = str(getattr(cmd, "description", "") or "").strip()
    if described:
        return described
    prompt = str(getattr(cmd, "prompt", "") or "").strip()
    first_line = prompt.splitlines()[0].strip() if prompt else ""
    return first_line[:60] if first_line else "Custom command"


def agent_menu_commands(
    commands: Any | None = None,
    *,
    owner_console: bool = False,
) -> list[MenuCommand]:
    """The commands one agent advertises, in catalog order, plus its custom ones.

    ``commands`` is the agent's ``CommandsConfig`` (duck-typed: only
    ``is_exposed`` / ``enabled`` / ``custom`` are read). ``None`` means "no
    agent in hand" and keeps every built-in — the historical behaviour, used by
    callers that have no agent context.

    Exposure is **surface-aware**, matching the runtime gate in
    :func:`olano.platforms.inbound.maybe_handle_command`:

    * messaging platforms (``owner_console=False``) ask ``is_exposed()``, so the
      *sensitive* built-ins are off unless the owner turned them on;
    * an owner console (``owner_console=True``: a caller with no per-sender
      identity) keeps every built-in **except** one the owner explicitly
      switched off, which is the same rule the dashboard chat applies
      client-side.
    """
    enabled = dict(getattr(commands, "enabled", None) or {}) if commands is not None else {}

    def _exposed(name: str) -> bool:
        if commands is None:
            return True
        if owner_console:
            return bool(enabled.get(name, True))
        try:
            return bool(commands.is_exposed(name))
        except Exception:  # a malformed config must not empty the whole menu
            return True

    menu: list[MenuCommand] = []
    for spec in COMMAND_SPECS:
        if not _exposed(spec.name):
            continue
        menu.append(
            MenuCommand(
                name=spec.name,
                description=spec.description,
                args_hint=_ARG_HINTS.get(spec.name, ""),
            )
        )

    for cmd in getattr(commands, "custom", None) or []:
        name = str(getattr(cmd, "name", "") or "").strip().lstrip("/").lower()
        if not name:
            continue
        menu.append(MenuCommand(name=name, description=_custom_menu_description(cmd), custom=True))
    return menu


# Telegram's setMyCommands rule: each command name is 1-32 chars of lowercase
# letters, digits, and underscores. A single invalid name makes the WHOLE batch
# fail with ``BadRequest: Bot_command_invalid`` — so hyphenated commands such as
# ``reload-agent`` / ``self-improve`` must be filtered out of the menu (they stay
# usable when typed, they just don't get an autocomplete hint).
_TELEGRAM_CMD_RE = re.compile(r"^[a-z0-9_]{1,32}$")


def telegram_menu_commands(
    max_commands: int = 100,
    *,
    commands: Any | None = None,
) -> tuple[list[tuple[str, str]], int]:
    """Return ``(menu, hidden_count)`` suitable for ``Bot.set_my_commands``.

    ``commands`` is the agent's ``CommandsConfig``; the menu then advertises
    exactly what that agent will actually run — the built-ins it exposes plus
    its custom commands — instead of every command that exists. ``None`` keeps
    the historical "everything" behaviour.

    Telegram requires command names *without* the leading ``/`` and caps
    descriptions at 256 chars (we use 40 for compactness).  Names that don't
    satisfy Telegram's command-name rule are **dropped** (otherwise the entire
    ``setMyCommands`` call is rejected).  ``max_commands`` bounds the number of
    entries; the count of dropped-for-limit entries is returned so the caller
    can log "X hidden, type /help for the full list".

    When the cap bites, **built-ins are trimmed before custom commands**: the
    owner added those deliberately, and a built-in dropped from the menu is
    still discoverable through ``/help``.
    """
    if max_commands <= 0:
        return [], len(agent_menu_commands(commands))

    builtin: list[tuple[str, str]] = []
    custom: list[tuple[str, str]] = []
    for entry in agent_menu_commands(commands):
        if not _TELEGRAM_CMD_RE.match(entry.name):
            continue
        (custom if entry.custom else builtin).append((entry.name, entry.description[:40]))

    keep_builtin = max(0, max_commands - len(custom))
    menu = builtin[:keep_builtin] + custom[:max_commands]
    hidden = (len(builtin) + len(custom)) - len(menu)
    return menu, max(0, hidden)


def telegram_unlistable_custom(commands: Any | None = None) -> list[str]:
    """Custom command names Telegram's ``/`` menu cannot carry.

    Custom names may legally contain ``-`` (the config validator allows it) but
    Telegram's command-name rule does not, so those get filtered out of the
    menu. They still work when typed — the adapter logs this list so an owner
    can see why their ``/my-cmd`` never autocompletes.
    """
    return [
        entry.name
        for entry in agent_menu_commands(commands)
        if entry.custom and not _TELEGRAM_CMD_RE.match(entry.name)
    ]


@dataclass
class GatewayCommand:
    """One entry in the gateway/platform command registry.

    ``name`` is the slash command without leading ``/``.  ``description`` is
    the user-visible summary.  ``args_hint`` (optional) is a short hint such
    as ``"section"`` shown next to the args in Discord/Slack.  ``custom``
    marks an owner-defined command, which has no dispatcher handler.
    """

    name: str
    description: str
    args_hint: str = ""
    aliases: tuple[str, ...] = ()
    custom: bool = False


def gateway_command_registry(commands: Any | None = None) -> list[GatewayCommand]:
    """Return the slash commands a platform should expose for one agent.

    Derived from :func:`agent_menu_commands`, so passing the agent's
    ``CommandsConfig`` yields exactly what that agent will run: the built-ins it
    exposes plus its custom commands. ``None`` keeps every built-in, which is
    what callers with no agent context get.
    """
    return [
        GatewayCommand(
            name=entry.name,
            description=entry.description,
            args_hint=entry.args_hint,
            custom=entry.custom,
        )
        for entry in agent_menu_commands(commands)
    ]


def is_gateway_command_available(
    cmd: GatewayCommand,
    config_overrides: dict[str, bool] | None = None,
) -> bool:
    """Whether a gateway command should be exposed on a platform.

    :func:`gateway_command_registry` already applies the agent's exposure
    config when it is handed one, so this is the second, narrower filter: a
    caller can pass ``config_overrides`` (``{"pulse": False, ...}``) to suppress
    specific commands from its own auto-registration sweep.
    """
    overrides = config_overrides or {}
    return overrides.get(cmd.name, True)


def discord_skill_commands_by_category(
    reserved_names: set[str] | None = None,
    workspace_root: Path | None = None,
) -> tuple[dict[str, list[tuple[str, str, str]]], list[tuple[str, str, str]], int]:
    """Group installed skills by category for Discord slash registration.

    Returns ``(categories, uncategorized, hidden_count)`` where each entry
    is ``(slash_name, category, description)``.  ``slash_name`` is the
    lowercase, hyphenated name clamped to Discord's 32-char limit.

    Replaces ``hermes_cli.commands.discord_skill_commands_by_category``.
    """
    from pathlib import Path as _Path  # local import: this helper is rarely hit

    reserved = {n.lower() for n in (reserved_names or set())}

    # Resolve workspace skills dir.  When the caller doesn't pass one we
    # ask the workspace helper for the active root.
    if workspace_root is None:
        try:
            from olano.config.workspace import get_workspace

            workspace_root = get_workspace(None).root
        except Exception:
            workspace_root = _Path.cwd()

    skills_dir = _Path(workspace_root) / "skills"

    try:
        from olano.skills.loader import discover_skills

        skills = discover_skills(skills_dir)
    except Exception:
        skills = []

    categories: dict[str, list[tuple[str, str, str]]] = {}
    uncategorized: list[tuple[str, str, str]] = []
    hidden = 0
    for skill in skills:
        slash_name = str(skill.get("name", "")).strip().lower().replace("_", "-")[:32]
        if not slash_name or slash_name in reserved:
            hidden += 1
            continue
        category = str(skill.get("category", "")).strip() or ""
        description = str(skill.get("description", ""))[:100]
        entry = (slash_name, category, description)
        if category and category != "general":
            categories.setdefault(category, []).append(entry)
        else:
            uncategorized.append(entry)
        reserved.add(slash_name)
    return categories, uncategorized, hidden


def slack_subcommand_map(commands: Any | None = None) -> dict[str, str]:
    """Map Slack subcommand names → the full ``/command`` they resolve to.

    Slack apps expose one top-level slash command (e.g. ``/olano``) and
    delegate to subcommands.  When a user types ``/olano memory`` the
    handler swaps ``memory`` for ``/memory`` and runs it through the
    dispatcher.

    Passing an agent's ``CommandsConfig`` **adds** its custom commands, so
    ``/olano standup`` works.  Built-ins are deliberately **never** filtered
    out: a disabled one must still resolve to ``/model`` so the inbound gate
    can answer "🔒 ``/model`` is disabled for this agent" — dropping it here
    would instead forward ``model gpt-4`` to the agent as a plain question.
    """
    mapping = {name.lstrip("/"): name for name in SLASH_COMMAND_DESCRIPTIONS}
    for cmd in getattr(commands, "custom", None) or []:
        name = str(getattr(cmd, "name", "") or "").strip().lstrip("/").lower()
        if name:
            mapping.setdefault(name, f"/{name}")
    return mapping


def should_bypass_active_session(text: str) -> bool:
    """Whether a chat-platform message should bypass the active agent session.

    Slash commands are dispatched directly instead of being fed into the agent
    loop.  Replaces ``hermes_cli.commands.should_bypass_active_session``.
    """
    if not text:
        return False
    return text.strip().startswith("/")


@dataclass
class CommandContext:
    config: AgentConfig
    runner: AgentRunner
    thread_id: str
    # Output verbosity: "quiet" | "normal" | "verbose". Chat platforms render
    # replies according to this.
    # Callers seed it from the chat's stored choice, falling back to the
    # agent's ``default_verbosity`` (see platforms/inbound.get_session_settings).
    verbosity: str = "normal"
    think_level: str = "off"
    # Set when the user ran a verbosity command this turn, so the chat's choice
    # is persisted even when it matches what the chat was already showing.
    # Without it, "/quiet" on a chat already resolving to quiet via the agent
    # default would write nothing and leave the chat following that default —
    # so a later change to it would silently override an explicit request.
    verbosity_explicit: bool = False
    # Who sent the message, when it arrived via a chat platform: a
    # ``olano.platforms.inbound.SenderInfo``. ``None`` means a caller with no
    # per-sender identity (an owner console), which is never gated.
    sender: Any | None = None
    # mutable so commands can replace it
    _thread_id_ref: list[str] = field(default_factory=list)

    @property
    def verbose(self) -> bool:
        """Back-compat boolean view of :attr:`verbosity` (verbose vs not)."""
        return self.verbosity == "verbose"

    @verbose.setter
    def verbose(self, value: bool) -> None:
        self.verbosity = "verbose" if value else "normal"

    def set_verbosity(self, mode: str) -> None:
        """Record a user-requested verbosity (see :attr:`verbosity_explicit`)."""
        self.verbosity = mode
        self.verbosity_explicit = True

    def reset_thread(self, new_thread_id: str) -> None:
        self.thread_id = new_thread_id
        if self._thread_id_ref:
            self._thread_id_ref[0] = new_thread_id


@dataclass
class CommandResult:
    output: str = ""
    renderable: Any | None = None
    new_thread_id: str | None = None  # if set, switch to this thread
    pass_through: str | None = None  # if set, forward this text to the agent


def _pulse_icon(type_name: str) -> str:
    return {
        "insight": "💡",
        "opportunity": "🌱",
        "question": "❓",
        "recommendation": "🧭",
        "suggestion": "✨",
        "finding": "🔎",
    }.get(type_name, "•")


def _pulse_type_bg(type_name: str) -> str:
    return {
        "insight": "#f59e0b",
        "opportunity": "#22c55e",
        "question": "#f97316",
        "recommendation": "#14b8a6",
        "suggestion": "#7cc6ff",
        "finding": "#94a3b8",
    }.get(type_name, "#7cc6ff")


def _pulse_priority_bg(priority: str) -> str:
    return {
        "urgent": "#ef4444",
        "normal": "#60a5fa",
        "low": "#94a3b8",
    }.get(priority, "#60a5fa")


def _pulse_plain_lines(items: list[Any], *, heading: str) -> list[str]:
    lines = [heading]
    for item in items:
        label = item.title or item.content
        lines.append(
            "  "
            f"[{item.id}] [{item.type.value}] [{item.priority_level.value}] "
            f"[{item.source}] {label[:100]}"
        )
        if item.why_now:
            lines.append(f"    Why now: {item.why_now[:160]}")
        if item.suggested_next_step:
            lines.append(f"    Next: {item.suggested_next_step[:160]}")
    return lines


def _render_pulse_items(
    items: list[Any],
    *,
    heading: str,
    approvals_count: int = 0,
    show_actions: bool = True,
) -> Any:
    from rich import box
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text

    def _badge(text: str, bg: str) -> Text:
        return Text(f" {text.upper()} ", style=f"bold #111827 on {bg}")

    def _item_panel(item: Any) -> Panel:
        title = item.title or item.content
        type_name = item.type.value
        priority = item.priority_level.value

        header = Text()
        header.append(f"{_pulse_icon(type_name)} ", style="bold")
        header.append(title, style="bold #f8fafc")
        header.append("  ")
        header.append_text(_badge(type_name, _pulse_type_bg(type_name)))
        header.append(" ")
        header.append_text(_badge(priority, _pulse_priority_bg(priority)))

        meta = Text()
        meta.append("ID ", style="bold #7cc6ff")
        meta.append(item.id, style="#dbeafe")
        meta.append("   SOURCE ", style="bold #34d399")
        meta.append(str(item.source), style="#d1fae5")

        body_parts: list[Any] = [header, meta]
        if item.content and item.content != title:
            body_parts.append(Text(""))
            body_parts.append(Text(item.content, style="#e5e7eb"))
        if item.why_now:
            body_parts.append(Text(""))
            why = Text()
            why.append("⏱ Why now  ", style="bold #fbbf24")
            why.append(item.why_now, style="#fef3c7")
            body_parts.append(why)
        if item.suggested_next_step:
            nxt = Text()
            nxt.append("→ Next step  ", style="bold #34d399")
            nxt.append(item.suggested_next_step, style="#dcfce7")
            body_parts.append(nxt)
        if item.references:
            refs = Text()
            refs.append("🔗 Refs  ", style="bold #93c5fd")
            refs.append(", ".join(item.references[:3]), style="#bfdbfe")
            body_parts.append(refs)

        return Panel(
            Group(*body_parts),
            border_style=_pulse_type_bg(type_name),
            box=box.ROUNDED,
            padding=(0, 1),
            style="on #111827",
            expand=True,
        )

    header_text = Text()
    header_text.append("PULSE", style="bold #7cc6ff")
    header_text.append("  ")
    header_text.append(heading, style="bold #f8fafc")
    if approvals_count:
        header_text.append("\n")
        header_text.append("APPROVALS ", style="bold #fbbf24")
        header_text.append(
            f"{approvals_count} pending review item(s) — use /approvals list",
            style="#fde68a",
        )

    sections: list[Any] = [
        Panel(
            header_text,
            border_style="#4c7eff",
            box=box.ROUNDED,
            padding=(0, 1),
            style="on #111827",
            expand=True,
        )
    ]

    for item in items:
        sections.append(_item_panel(item))

    if show_actions:
        actions = Text()
        actions.append("⚙ Actions  ", style="bold #7cc6ff")
        for idx, command in enumerate(
            [
                "/pulse generate",
                "/pulse accept <id>",
                "/pulse reject <id>",
                "/pulse defer <id> [hours]",
            ]
        ):
            if idx:
                actions.append("  ")
            actions.append(command, style="bold #dbeafe on #1e293b")
        sections.append(
            Panel(
                actions,
                border_style="#334155",
                box=box.ROUNDED,
                padding=(0, 1),
                style="on #111827",
                expand=True,
            )
        )

    return Group(*sections)


def _clip(text: str, limit: int = 100) -> str:
    """Collapse whitespace and truncate at a word boundary for one-line listings."""
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit].rsplit(" ", 1)[0].rstrip(" ,;:.") or flat[:limit]
    return cut + "…"


# ─────────────────────────────────── dispatcher ───────────────────────────────


class CommandDispatcher:
    """Parse and dispatch /commands."""

    DIRECTIVES = {"/think", "/fast", "/verbose", "/model", "/mode", "/reasoning"}

    def is_command(self, text: str) -> bool:
        return text.strip().startswith("/")

    def parse(self, text: str) -> tuple[str, list[str]]:
        """Return (command, args)."""
        text = text.strip()
        parts = text.split(None, 1)
        cmd = parts[0].lower().rstrip(":")
        args_str = parts[1] if len(parts) > 1 else ""
        if not args_str:
            args = []
        else:
            try:
                args = shlex.split(args_str)
            except ValueError:
                args = args_str.split()
        return cmd, args

    def strip_directives(self, text: str) -> tuple[str, dict[str, str]]:
        """
        Remove inline directives from a message, return (clean_text, directives).
        Directives: /think high, /verbose on, /model opus, /fast on, /reasoning on
        """
        directives: dict[str, str] = {}
        words = text.split()
        clean: list[str] = []
        i = 0
        while i < len(words):
            w = words[i].lower()
            if w in self.DIRECTIVES:
                val = words[i + 1] if i + 1 < len(words) else ""
                directives[w] = val
                i += 2
            else:
                clean.append(words[i])
                i += 1
        return " ".join(clean).strip(), directives

    # Per-instance handler overlay populated by ``register``.  The class-level
    # ``_handlers`` dict still defines built-ins; registration appends/overrides
    # without mutating the class so plugins or tests can scope new commands to
    # a single dispatcher instance.
    @property
    def _instance_handlers(self) -> dict[str, Callable]:
        overlay = self.__dict__.setdefault("_overlay_handlers", {})
        return cast("dict[str, Callable]", overlay)

    def register(
        self,
        name: str,
        handler: Callable,
        *,
        aliases: tuple[str, ...] = (),
        override: bool = False,
    ) -> None:
        """Register a new slash command on this dispatcher.

        ``name`` must start with ``/``.  ``aliases`` are
        additional names that map to the same handler.  Pass ``override=True``
        to replace a built-in handler — otherwise raises ``ValueError``.

        Plugin authors / tests can call this without subclassing.  Returned
        handlers receive ``(dispatcher, args, ctx)``.
        """
        if not name.startswith("/"):
            raise ValueError(f"Command name must start with '/': {name!r}")
        all_names = (name, *aliases)
        for n in all_names:
            if not override and (n in self._handlers or n in self._instance_handlers):
                raise ValueError(
                    f"Command {n!r} already registered. Pass override=True to replace."
                )
        for n in all_names:
            self._instance_handlers[n] = handler

    def unregister(self, name: str) -> None:
        """Remove a command from this dispatcher's overlay (built-ins survive)."""
        self._instance_handlers.pop(name, None)

    def _resolve_handler(self, cmd: str) -> Callable | None:
        # Instance overlay wins over class-level built-ins so registrations
        # can override behavior without monkey-patching.
        return self._instance_handlers.get(cmd) or self._handlers.get(cmd)

    async def dispatch(self, text: str, ctx: CommandContext) -> CommandResult:
        """Dispatch a command and return a CommandResult."""
        cmd, args = self.parse(text)
        handler = self._resolve_handler(cmd)
        if handler:
            return cast(CommandResult, await handler(self, args, ctx))
        return CommandResult(output=f"Unknown command: {cmd}. Type /help for a list.")

    # ─────────────────────────────── handlers ─────────────────────────────────

    async def _help(self, args: list[str], ctx: CommandContext) -> CommandResult:
        """List the commands *this* agent exposes on *this* surface.

        Generated from the catalog and the agent's ``commands`` config rather
        than hand-written, so it can never advertise a command that the inbound
        gate then refuses with "🔒 … is disabled for this agent" — and so an
        owner's custom commands are listed where people look for them.

        ``ctx.sender is None`` means an owner console (no per-sender identity),
        which keeps every built-in except one explicitly turned off.
        """
        commands = getattr(ctx.config, "commands", None)
        owner_console = ctx.sender is None
        entries = agent_menu_commands(commands, owner_console=owner_console)

        by_name = {spec.name: spec for spec in COMMAND_SPECS}
        alias_labels: dict[str, str] = {}
        for spec in COMMAND_SPECS:
            names = [f"/{spec.name}"] + [f"/{a}" for a in spec.aliases]
            alias_labels[spec.name] = ", ".join(names)

        grouped: dict[str, list[MenuCommand]] = {}
        custom: list[MenuCommand] = []
        for entry in entries:
            if entry.custom:
                custom.append(entry)
                continue
            entry_spec = by_name.get(entry.name)
            grouped.setdefault(entry_spec.group if entry_spec else "info", []).append(entry)

        lines: list[str] = ["Available commands:"]
        for group_id, group_label in COMMAND_GROUPS:
            rows = grouped.get(group_id)
            if not rows:
                continue
            lines.append("")
            lines.append(f"{group_label}:")
            for entry in rows:
                label = alias_labels.get(entry.name, f"/{entry.name}")
                if entry.args_hint:
                    label += f" <{entry.args_hint}>"
                lines.append(f"  {label} — {entry.description}")

        if custom:
            lines.append("")
            lines.append("Custom commands:")
            for entry in custom:
                lines.append(f"  /{entry.name} — {entry.description}")

        # Inline directives. The ones that are also catalog commands follow the
        # same exposure rule — listing "/model <name>" under a heading of its
        # own would advertise a disabled command by the back door.
        directive_hints = {
            "/think": "<level>",
            "/verbose": "on|off",
            "/model": "<name>",
            "/mode": "<name>",
            "/fast": "on|off",
            "/reasoning": "on|off",
        }
        exposed = {entry.name for entry in entries}
        directives = [
            f"{name} {hint}"
            for name, hint in directive_hints.items()
            if name in self.DIRECTIVES
            and (name.lstrip("/") not in by_name or name.lstrip("/") in exposed)
        ]
        if directives:
            lines.append("")
            lines.append("Directives (inline or standalone):")
            lines.append("  " + "  ".join(directives))
        return CommandResult(output="\n".join(lines))

    async def _status(self, args: list[str], ctx: CommandContext) -> CommandResult:
        from datetime import datetime, timezone

        cfg = ctx.config
        session = ctx.runner.load_session(ctx.thread_id)
        lines = [
            f"Agent:   {cfg.emoji} {cfg.name}",
            f"Model:   {cfg.model}",
            f"Thread:  {ctx.thread_id}",
            f"Turns:   {len(session)}",
            f"Verbosity: {ctx.verbosity}",
            f"Think:   {ctx.think_level}",
            f"Prompt:  {ctx.config.prompt_mode}",
            f"Backend: {ctx.config.backend.type}",
            f"Time:    {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        ]
        if cfg.subagents:
            lines.append(f"Subagents: {', '.join(s.name for s in cfg.subagents)}")
        if cfg.schedule:
            lines.append(f"Scheduled: {len(cfg.schedule)} job(s)")
        return CommandResult(output="\n".join(lines))

    async def _mem(self, args: list[str], ctx: CommandContext) -> CommandResult:
        """Process memory + a breakdown of olano's in-process structures.

        ``/mem on|off|status|tracemalloc-on|tracemalloc-off`` toggles the
        background monitor (``off`` also stops tracemalloc)."""
        from olano import admin
        from olano.admin.memory import render_memory_report_text

        actions = {"on", "off", "status", "tracemalloc-on", "tracemalloc-off"}
        if args and args[0].lower() in actions:
            state = admin.set_memory_monitor(args[0].lower())
            return CommandResult(
                output=(
                    f"Memory monitor: {'ON' if state.get('enabled') else 'off'} "
                    f"· sample every {state.get('interval_seconds')}s "
                    f"· tracemalloc {'on' if state.get('tracemalloc') else 'off'}"
                )
            )
        return CommandResult(output=render_memory_report_text(admin.memory_report()))

    async def _context(self, args: list[str], ctx: CommandContext) -> CommandResult:
        """Show an estimated breakdown of what fills the model's context window.

        The static footprint (system prompt, tool schemas, subagents, memory
        files, skill summaries) comes from the same estimator the dashboard
        ``/context`` card uses; the live *Messages* fill is estimated from the
        active thread's session so the total reflects how full the window is.
        """
        from olano.config.workspace import get_workspace
        from olano.runtime.context_report import (
            render_context_report_text,
            report_for,
            session_message_tokens,
        )

        ws = get_workspace(ctx.config.workspace)
        report = report_for(ctx.config, ws)
        try:
            entries = ctx.runner.load_session(ctx.thread_id)
        except Exception:
            entries = []
        message_tokens = session_message_tokens(entries)
        return CommandResult(
            output=render_context_report_text(
                report,
                message_tokens=message_tokens,
                # ``sender is None`` = an owner console (no per-sender
                # identity). Chat users never see which model backs
                # the bot — that is what makes /model sensitive.
                show_model=ctx.sender is None,
            )
        )

    async def _whoami(self, args: list[str], ctx: CommandContext) -> CommandResult:
        return CommandResult(output=f"Thread: {ctx.thread_id}\nAgent:  {ctx.config.name}")

    async def _session(self, args: list[str], ctx: CommandContext) -> CommandResult:
        from olano.config.home import get_olano_paths

        paths = get_olano_paths()
        jsonl_path = paths.sessions_dir / ctx.config.name / f"{ctx.thread_id}.jsonl"
        lines = [
            f"Thread:  {ctx.thread_id}",
            f"JSONL:   {jsonl_path}",
        ]
        return CommandResult(output="\n".join(lines))

    async def _tools(self, args: list[str], ctx: CommandContext) -> CommandResult:
        mode = args[0].lower() if args else "compact"
        if mode == "full":
            # Whole registry catalog (every available tool, not just this
            # agent's) + skills, via the capability banner.
            from olano.cli.capability_banner import (
                get_skills_from_dirs,
                get_tool_names_from_registry,
                render_banner,
            )
            from olano.config.workspace import get_workspace
            from olano.skills.loader import load_skills_for_agent

            ws = get_workspace(ctx.config.workspace)
            skill_dirs = load_skills_for_agent(ctx.config, ws)
            tool_names = get_tool_names_from_registry()
            skills = get_skills_from_dirs(skill_dirs, ctx.config)
            return CommandResult(output=render_banner(tool_names, skills, full=True))

        from olano.config.ops_defaults import DEEPAGENTS_BUILTIN_TOOL_NAMES
        from olano.tools.registry import resolve_tools

        tools = resolve_tools(ctx.config.tools)

        # Group by owning toolkit via the static name index (no toolkit imports);
        # tools the index doesn't know fall back to their name prefix.
        try:
            from olano.tools._name_index import NAME_INDEX
        except Exception:  # generated index missing — prefix grouping still works
            NAME_INDEX = {}
        groups: dict[str, list[tuple[str, str]]] = {}
        seen_tool_names: set[str] = set()
        for t in tools:
            name = str(getattr(t, "name", t))
            # Aliases (get_current_date → get_current_time) resolve to the same
            # tool object; list it once.
            if name in seen_tool_names:
                continue
            seen_tool_names.add(name)
            module = NAME_INDEX.get(name, "")
            if module:
                group = module.rsplit(".", 1)[-1].removesuffix("_tools")
            elif "_" in name:
                group = name.split("_", 1)[0]
            else:
                group = "other"
            desc = str(getattr(t, "description", "") or "")
            groups.setdefault(group, []).append((name, desc))

        lines = [f"🔧 Tools — {len(seen_tool_names)} loaded"]
        verbose = mode == "verbose"
        for group in sorted(groups):
            members = sorted(groups[group])
            if verbose:
                lines.append("")
                lines.append(f"{group}:")
                for name, desc in members:
                    summary = _clip(desc)
                    lines.append(f"• {name} — {summary}" if summary else f"• {name}")
            else:
                names = ", ".join(name for name, _desc in members)
                lines.append(f"• {group} ({len(members)}): {names}")

        lines.append("")
        lines.append(f"Built-in (always available): {', '.join(DEEPAGENTS_BUILTIN_TOOL_NAMES)}")
        if mode == "compact":
            lines.append("")
            lines.append("/tools verbose — with descriptions · /tools full — the whole catalog")
        return CommandResult(output="\n".join(lines))

    async def _skills(self, args: list[str], ctx: CommandContext) -> CommandResult:
        from pathlib import Path

        from olano.config.workspace import get_workspace
        from olano.skills.loader import (
            compute_quarantined_shadows,
            compute_skipped_skills,
            disabled_skill_names,
            discover_skills,
            effective_tool_names,
            gated_skill_reason,
            load_skills_for_agent,
        )

        ws = get_workspace(ctx.config.workspace)
        skill_dirs = load_skills_for_agent(ctx.config, ws)
        skipped = compute_skipped_skills(ctx.config, ws)
        quarantined = compute_quarantined_shadows(ctx.config, ws)
        disabled = disabled_skill_names(ctx.config)
        effective = effective_tool_names(ctx.config)
        # Dedupe by name: discover_skills recurses, and skill_dirs overlap
        # (skills/ contains skills/learned/), so a skill can surface more than once.
        # Quarantined learned shadows are skipped by LOCATION first so the
        # original of the same name is the entry that survives the dedupe.
        all_skills = []
        skipped_found: dict[str, dict[str, str]] = {}
        disabled_found: list[dict[str, Any]] = []
        gated_found: list[tuple[dict[str, Any], str]] = []
        seen_names: set[str] = set()
        for d in skill_dirs:
            for s in discover_skills(Path(d)):
                if s["location"] in quarantined:
                    continue
                if s["name"] in seen_names:
                    continue
                seen_names.add(s["name"])
                if s["name"] in skipped:
                    skipped_found[s["name"]] = s
                    continue
                # Off for this agent: listed in its own section rather than
                # dropped, so "why isn't it using that skill" is answerable
                # without opening agent.json5.
                if str(s["name"]).lower() in disabled:
                    disabled_found.append(s)
                    continue
                # Written for an agent with tools this one lacks (`requires_tools`):
                # its own section, naming the tools, so "why don't I have the
                # admin skill" is answered without reading the SKILL.md.
                gate = gated_skill_reason(s, effective)
                if gate:
                    gated_found.append((s, gate))
                    continue
                all_skills.append(s)
        if (
            not all_skills
            and not skipped_found
            and not quarantined
            and not disabled_found
            and not gated_found
        ):
            return CommandResult(
                output="No skills loaded. Add SKILL.md files to workspace/skills/<name>/"
            )

        # Names + descriptions only — deliberately NO file paths. Chat adapters
        # auto-attach absolute paths that resolve to real files
        # (extract_local_files), so listing each skill's SKILL.md location
        # turned /skills into a stack of file attachments on Telegram.
        def _entry(name: str, description: str, *, marker: str = "") -> str:
            summary = _clip(description)
            head = f"• {name}{marker}"
            return f"{head} — {summary}" if summary else head

        lines = [f"🧩 Skills — {len(all_skills)} loaded"]
        by_category: dict[str, list[dict[str, Any]]] = {}
        for s in all_skills:
            category = str(s.get("category") or "general").strip() or "general"
            by_category.setdefault(category, []).append(s)
        show_categories = len(by_category) > 1
        if not show_categories and all_skills:
            lines.append("")
        for category in sorted(by_category, key=str.lower):
            if show_categories:
                lines.append("")
                lines.append(f"{category}:")
            for s in sorted(by_category[category], key=lambda x: str(x["name"]).lower()):
                marker = " (subagent)" if s.get("tier") == "subagent" else ""
                # A spec-violating name still loads, so it belongs in the main
                # list — flagged, not hidden in a separate section next to the
                # skills that genuinely aren't running.
                if s.get("spec_error"):
                    marker += " ⚠️ folder name mismatch"
                lines.append(_entry(s["name"], s["description"], marker=marker))
        if disabled_found:
            lines.append("")
            lines.append(f"⏸️  Turned off for this agent ({len(disabled_found)}):")
            for s in sorted(disabled_found, key=lambda x: str(x["name"]).lower()):
                lines.append(_entry(str(s["name"]), str(s.get("description") or "")))
        if gated_found:
            lines.append("")
            lines.append(f"🔒 Needs tools this agent lacks ({len(gated_found)}):")
            for s, gate in sorted(gated_found, key=lambda x: str(x[0]["name"]).lower()):
                lines.append(_entry(str(s["name"]), gate))
        if skipped_found:
            lines.append("")
            lines.append(f"🚫 Skipped by security audit ({len(skipped_found)}):")
            for name in sorted(skipped_found):
                lines.append(_entry(name, skipped[name]))
        if quarantined:
            lines.append("")
            lines.append(
                f"⚠️ Quarantined learned copies ({len(quarantined)}) — degraded vs the "
                "original, which serves instead:"
            )
            for loc in sorted(quarantined):
                q = quarantined[loc]
                lines.append(_entry(q["name"], q["reason"]))
        return CommandResult(output="\n".join(lines))

    async def _agents(self, args: list[str], ctx: CommandContext) -> CommandResult:
        from olano.config.loader import list_agents, load_agent_config

        names = list_agents()
        if not names:
            return CommandResult(output="No agents found.")
        lines = [f"Agents ({len(names)}):"]
        for name in names:
            try:
                cfg = load_agent_config(name)
                marker = " ◀ current" if name == ctx.config.name else ""
                lines.append(f"  {cfg.emoji} {cfg.name} [{cfg.model}]{marker}")
                if cfg.description:
                    lines.append(f"     {cfg.description}")
            except Exception:
                lines.append(f"  {name} (error loading config)")
        return CommandResult(output="\n".join(lines))

    async def _model(self, args: list[str], ctx: CommandContext) -> CommandResult:
        from olano.models import (
            PROVIDERS,
            get_provider_models,
            is_provider_available,
            list_ollama_models,
        )

        # /model or /model list → show catalog
        if not args or args[0] in ("list", "status"):
            lines = [f"Current model: {ctx.config.model}\n"]
            lines.append("Available providers (provider:model format):\n")
            for key, info in PROVIDERS.items():
                available = is_provider_available(key)
                status = "✓" if available else "○"
                if key == "ollama":
                    local = list_ollama_models()
                    model_hint = ", ".join(local[:3]) if local else "run: ollama pull llama3.2"
                else:
                    model_hint = info.default_models[0] if info.default_models else ""
                lines.append(f"  {status} {key:20s} {info.name:25s} example: {model_hint}")
            lines.append("\n✓ = package installed   ○ = needs: uv add <package>")
            lines.append("\nUsage: /model anthropic:claude-sonnet-4-6")
            return CommandResult(output="\n".join(lines))

        # /model <provider> → list models for that provider
        if args[0] in PROVIDERS and len(args) == 1:
            info = PROVIDERS[args[0]]
            models = get_provider_models(args[0])
            lines = [f"{info.name} models:"]
            for m in models:
                lines.append(f"  {args[0]}:{m}")
            if info.notes:
                lines.append(f"\nNote: {info.notes}")
            if not is_provider_available(args[0]):
                lines.append(f"\nInstall: uv add {info.package}")
            return CommandResult(output="\n".join(lines))

        # /model <provider:model> → switch model live
        new_model = args[0]
        old_model = ctx.config.model
        ctx.config.model = new_model
        from olano.core.builder import build_agent_async, build_agent_with_mcp

        try:
            new_agent = await (
                build_agent_with_mcp(ctx.config)
                if ctx.config.mcp_servers
                else build_agent_async(ctx.config)
            )
            ctx.runner.set_agent(new_agent)
            return CommandResult(
                output=f"Model: {old_model}  →  {new_model}\nThread history preserved."
            )
        except ImportError as e:
            ctx.config.model = old_model  # rollback
            from olano.models import install_hint, parse_model_string

            provider, _ = parse_model_string(new_model)
            hint = install_hint(provider) if provider else str(e)
            return CommandResult(output=f"Package not installed. Run: {hint}")
        except Exception as e:
            ctx.config.model = old_model  # rollback
            return CommandResult(output=f"Failed to switch model: {e}")

    async def _mode(self, args: list[str], ctx: CommandContext) -> CommandResult:
        valid_modes = {"full", "task", "minimal", "none"}
        if not args:
            return CommandResult(output=f"Prompt mode: {ctx.config.prompt_mode}")

        new_mode = args[0].strip().lower()
        if new_mode not in valid_modes:
            return CommandResult(output="Invalid prompt mode. Use: full, task, minimal, none")

        flag_args = {arg.strip().lower() for arg in args[1:]}
        carry_history_requested = flag_args.intersection({"--carry-history", "carry"})
        fresh_requested = flag_args.intersection({"--fresh", "fresh"})
        if carry_history_requested and fresh_requested:
            return CommandResult(
                output="Choose either --fresh or --carry-history for /mode, not both."
            )

        old_mode = ctx.config.prompt_mode
        if new_mode == old_mode:
            return CommandResult(output=f"Prompt mode already set to {new_mode}.")

        ctx.config.prompt_mode = cast('Literal["full", "task", "minimal", "none"]', new_mode)
        from olano.core.builder import build_agent_async, build_agent_with_mcp

        try:
            new_agent = await (
                build_agent_with_mcp(ctx.config)
                if ctx.config.mcp_servers
                else build_agent_async(ctx.config)
            )
            ctx.runner.set_agent(new_agent)
            carry_history = bool(carry_history_requested)
            if not carry_history_requested and not fresh_requested:
                carry_history = new_mode != "none"

            new_thread_id = await ctx.runner.fork_thread(
                ctx.thread_id,
                carry_history=carry_history,
                carry_state=carry_history,
            )
            history_msg = (
                "Visible history copied into a fresh thread. Hidden startup/system turns discarded."
                if carry_history
                else "Started a fresh empty thread so the new prompt mode applies cleanly."
            )
            return CommandResult(
                output=f"Prompt mode: {old_mode} -> {new_mode}\n{history_msg}",
                new_thread_id=new_thread_id,
            )
        except Exception as e:
            ctx.config.prompt_mode = old_mode
            return CommandResult(output=f"Failed to switch prompt mode: {e}")

    async def _memory(self, args: list[str], ctx: CommandContext) -> CommandResult:
        from olano.config.workspace import get_workspace
        from olano.memory.markdown_memory import MarkdownMemory

        ws = get_workspace(ctx.config.workspace)
        mem = MarkdownMemory(ws.root)
        if args:
            section = " ".join(args)
            content = mem.get_section(section)
            return CommandResult(output=content or f"No section '{section}' found.")
        content = mem.read()
        if len(content) > 3000:
            content = content[:3000] + "\n\n[...truncated, use /memory <section> for details]"
        return CommandResult(output=content)

    async def _new_session(self, args: list[str], ctx: CommandContext) -> CommandResult:
        import uuid

        new_tid = str(uuid.uuid4())
        ctx.thread_id = new_tid
        return CommandResult(
            output=f"New session started. Thread: {new_tid}",
            new_thread_id=new_tid,
        )

    async def _reload_agent(self, args: list[str], ctx: CommandContext) -> CommandResult:
        """Reload the agent's ``agent.json5`` from disk and rebuild the live agent.

        Picks up edits made outside this session (e.g. via the dashboard agent
        editor) without restarting the process. Thread history is preserved.
        """
        from olano.config.loader import load_agent_config
        from olano.core.builder import build_agent_async, build_agent_with_mcp

        name = ctx.config.name
        try:
            fresh = load_agent_config(name)
        except Exception as e:
            return CommandResult(output=f"Failed to reload config for '{name}': {e}")

        # Copy fresh values into the live config object *in place* so every
        # holder (ctx.config, runner.config) observes the update; they
        # all share a single AgentConfig reference.
        for field_name in type(fresh).model_fields:
            setattr(ctx.config, field_name, getattr(fresh, field_name))

        try:
            new_agent = await (
                build_agent_with_mcp(ctx.config)
                if ctx.config.mcp_servers
                else build_agent_async(ctx.config)
            )
            ctx.runner.set_agent(new_agent)
        except Exception as e:
            return CommandResult(output=f"Reloaded config but failed to rebuild agent: {e}")

        # The reload may have changed which commands this agent exposes; the
        # connectors keep running, so their published menus need rebuilding or
        # they would advertise commands the refreshed gate now refuses.
        try:
            from olano.platforms.registry import republish_command_menus

            await republish_command_menus(name)
        except Exception as e:  # pragma: no cover - a menu is a hint, not the reload
            logger.debug("Command menu re-publish skipped for '%s': %s", name, e)

        return CommandResult(
            output=(
                f"Reloaded agent '{name}' from disk — config changes applied.\n"
                "Thread history preserved."
            )
        )

    async def _reload_vault(self, args: list[str], ctx: CommandContext) -> CommandResult:
        """Re-read the credentials vault and rebuild the agent.

        Re-hydrates env vars from the vault and rebuilds the agent so refreshed
        ``${vault:...}`` references (e.g. a rotated API key) take effect without
        restarting the process.
        """
        from olano.core.builder import build_agent_async, build_agent_with_mcp
        from olano.vault.resolver import get_vault
        from olano.vault.runtime import hydrate_env_from_vault, unlock_vault_for_session

        vault = get_vault()
        if vault is None:
            # Not unlocked yet this session — try the non-interactive paths
            # (OLANO_VAULT_KEY / system keyring). A chat command has no
            # terminal to prompt on.
            vault = unlock_vault_for_session(allow_prompt=False).vault
        if vault is None:
            return CommandResult(
                output=(
                    "No unlocked vault available. Unlock it (OLANO_VAULT_KEY "
                    "or system keyring) and retry."
                )
            )

        injected = hydrate_env_from_vault(vault, overwrite=True)
        try:
            secret_count = len(vault.list_names())
        except Exception:
            secret_count = 0

        try:
            new_agent = await (
                build_agent_with_mcp(ctx.config)
                if ctx.config.mcp_servers
                else build_agent_async(ctx.config)
            )
            ctx.runner.set_agent(new_agent)
        except Exception as e:
            return CommandResult(output=f"Vault reloaded but failed to rebuild agent: {e}")

        return CommandResult(
            output=(
                f"Vault reloaded — {secret_count} secret(s) available, "
                f"{len(injected)} env var(s) refreshed. Agent rebuilt."
            )
        )

    async def _think(self, args: list[str], ctx: CommandContext) -> CommandResult:
        level = args[0] if args else "medium"
        valid = {"off", "minimal", "low", "medium", "high", "xhigh"}
        if level not in valid:
            return CommandResult(output=f"Invalid level. Choose from: {', '.join(sorted(valid))}")
        ctx.think_level = level
        return CommandResult(
            output=f"Think level set to: {level} (hint injected into next message)"
        )

    async def _improve(self, args: list[str], ctx: CommandContext) -> CommandResult:
        """Run self-improvement analysis on demand."""
        from olano.config.workspace import get_workspace
        from olano.memory.markdown_memory import MarkdownMemory
        from olano.memory.self_learning import SelfLearningService

        ws = get_workspace(ctx.config.workspace)
        md_mem = MarkdownMemory(ws.root)
        svc = SelfLearningService(
            agent_config=ctx.config,
            workspace_root=ws.root,
            markdown_memory=md_mem,
        )
        result = await svc.self_improve()
        return CommandResult(output=result)

    async def _verbose(self, args: list[str], ctx: CommandContext) -> CommandResult:
        """``/verbose [on|off|<mode>]`` — toggle or set output verbosity.

        Back-compat: ``on`` → verbose, ``off`` → normal. With no argument it
        toggles between verbose and normal. A full mode name (quiet/normal/
        verbose) is also accepted.
        """
        from olano.platforms.verbosity import normalize_verbosity

        if not args:
            ctx.set_verbosity("normal" if ctx.verbosity == "verbose" else "verbose")
            return CommandResult(output=f"Verbosity: {ctx.verbosity}")
        val = args[0].strip().lower()
        if val in ("on", "true", "1", "yes"):
            ctx.set_verbosity("verbose")
        elif val in ("off", "false", "0", "no"):
            ctx.set_verbosity("normal")
        else:
            mode = normalize_verbosity(val)
            if mode is None:
                return CommandResult(
                    output="Usage: /verbose [on|off]  (or /verbosity quiet|normal|verbose)"
                )
            ctx.set_verbosity(mode)
        return CommandResult(output=f"Verbosity: {ctx.verbosity}")

    async def _verbosity(self, args: list[str], ctx: CommandContext) -> CommandResult:
        """``/verbosity [quiet|normal|verbose]`` — show or set reply verbosity."""
        from olano.platforms.inbound import agent_default_verbosity
        from olano.platforms.verbosity import normalize_verbosity

        if not args:
            agent_default = agent_default_verbosity(ctx.config)
            return CommandResult(
                output=(
                    f"Verbosity: {ctx.verbosity}\n"
                    "  quiet   — final answer only\n"
                    "  normal  — final answer, plus a note when a turn only ran tools\n"
                    "  verbose — show tool calls, results & subagent activity\n"
                    f"This agent starts new chats on: {agent_default}\n"
                    "Set with: /verbosity quiet|normal|verbose  (or /quiet, /normal, /verbose)"
                )
            )
        mode = normalize_verbosity(args[0])
        if mode is None:
            return CommandResult(output="Invalid mode. Use: quiet, normal, or verbose.")
        ctx.set_verbosity(mode)
        return CommandResult(output=f"Verbosity set to: {mode}")

    async def _quiet(self, args: list[str], ctx: CommandContext) -> CommandResult:
        ctx.set_verbosity("quiet")
        return CommandResult(output="Verbosity set to: quiet (answers only)")

    async def _normal(self, args: list[str], ctx: CommandContext) -> CommandResult:
        ctx.set_verbosity("normal")
        return CommandResult(output="Verbosity set to: normal")

    async def _pulse(self, args: list[str], ctx: CommandContext) -> CommandResult:
        """Show or manage curated PULSE insights."""
        from olano.config.workspace import get_workspace
        from olano.pulse.engine import PulseEngine
        from olano.pulse.generators import PulseGenerator
        from olano.pulse.models import PulseLane

        ws = get_workspace(ctx.config.workspace)
        engine = PulseEngine(ctx.config, ws.state_dir)

        sub = (args[0] if args else "list").lower()
        if sub in ("show", "list"):
            items = engine.list_pending(lane=PulseLane.PULSE)
            if not items:
                return CommandResult(output="No curated PULSE items pending.")
            approvals_count = engine.count_pending(lane=PulseLane.APPROVAL)
            lines = _pulse_plain_lines(items, heading=f"PULSE ({len(items)} items):")
            lines.append("")
            lines.append(
                "Actions: /pulse generate | /pulse accept <id> | /pulse reject <id> | /pulse defer <id> [hours]"
            )
            return CommandResult(
                output="\n".join(lines),
                renderable=_render_pulse_items(
                    items,
                    heading=f"{len(items)} curated item(s)",
                    approvals_count=approvals_count,
                    show_actions=True,
                ),
            )
        if sub == "generate":
            items = await PulseGenerator(ctx.config, ws.root).capture_agent_curated_pulse(
                thread_id=ctx.thread_id,
                force=True,
            )
            if not items:
                return CommandResult(output="No new curated PULSE items were generated.")
            lines = _pulse_plain_lines(
                items, heading=f"Generated {len(items)} curated PULSE item(s):"
            )
            return CommandResult(
                output="\n".join(lines),
                renderable=_render_pulse_items(
                    items,
                    heading=f"Generated {len(items)} new curated item(s)",
                    approvals_count=engine.count_pending(lane=PulseLane.APPROVAL),
                    show_actions=True,
                ),
            )
        if sub == "clear":
            removed = engine.clear_expired(lane=PulseLane.PULSE)
            return CommandResult(output=f"Expired curated PULSE items cleared: {removed}.")
        if sub in {"accept", "approve"} and len(args) > 1:
            ok, message = engine.accept(args[1])
            return CommandResult(output=message if ok else f"Could not accept {args[1]}: {message}")
        if sub in {"reject", "dismiss"} and len(args) > 1:
            if engine.reject(args[1]):
                return CommandResult(output=f"Rejected PULSE item {args[1]}.")
            return CommandResult(output=f"PULSE item {args[1]} not found.")
        if sub == "defer" and len(args) > 1:
            hours = 24.0
            if len(args) > 2:
                try:
                    hours = float(args[2])
                except ValueError:
                    return CommandResult(output="Usage: /pulse defer <id> [hours]")
            if engine.defer(args[1], hours=hours):
                return CommandResult(output=f"Deferred PULSE item {args[1]} for {hours:g} hours.")
            return CommandResult(output=f"PULSE item {args[1]} not found.")
        return CommandResult(
            output="Usage: /pulse [list|generate|accept <id>|reject <id>|defer <id> [hours]|clear]"
        )

    async def _approvals(self, args: list[str], ctx: CommandContext) -> CommandResult:
        """Show or manage approval items."""
        from olano.config.workspace import get_workspace
        from olano.pulse.engine import PulseEngine
        from olano.pulse.models import PulseLane

        ws = get_workspace(ctx.config.workspace)
        engine = PulseEngine(ctx.config, ws.state_dir)

        sub = (args[0] if args else "list").lower()
        if sub in ("show", "list"):
            items = engine.list_pending(lane=PulseLane.APPROVAL)
            if not items:
                return CommandResult(output="No approval items pending.")
            lines = [f"APPROVALS ({len(items)} items):"]
            for item in items:
                label = item.title or item.content
                lines.append(
                    "  "
                    f"[{item.id}] [{item.type.value}] [{item.priority_level.value}] "
                    f"[{item.source}] {label[:100]}"
                )
                if item.why_now:
                    lines.append(f"    Why now: {item.why_now[:160]}")
                if item.suggested_next_step:
                    lines.append(f"    Next: {item.suggested_next_step[:160]}")
            lines.append("")
            lines.append(
                "Actions: /approvals approve <id> | /approvals reject <id> | /approvals defer <id> [hours]"
            )
            return CommandResult(output="\n".join(lines))
        if sub == "clear":
            removed = engine.clear_expired(lane=PulseLane.APPROVAL)
            return CommandResult(output=f"Expired approval items cleared: {removed}.")
        if sub == "approve" and len(args) > 1:
            ok, message = engine.approve(args[1])
            return CommandResult(
                output=message if ok else f"Could not approve {args[1]}: {message}"
            )
        if sub in {"reject", "dismiss"} and len(args) > 1:
            if engine.reject(args[1]):
                return CommandResult(output=f"Rejected approval item {args[1]}.")
            return CommandResult(output=f"Approval item {args[1]} not found.")
        if sub == "defer" and len(args) > 1:
            hours = 24.0
            if len(args) > 2:
                try:
                    hours = float(args[2])
                except ValueError:
                    return CommandResult(output="Usage: /approvals defer <id> [hours]")
            if engine.defer(args[1], hours=hours):
                return CommandResult(
                    output=f"Deferred approval item {args[1]} for {hours:g} hours."
                )
            return CommandResult(output=f"Approval item {args[1]} not found.")
        return CommandResult(
            output="Usage: /approvals [list|approve <id>|reject <id>|defer <id> [hours]|clear]"
        )

    async def _ops(self, args: list[str], ctx: CommandContext) -> CommandResult:
        """Show or manage runtime/system ops items."""
        from olano.config.workspace import get_workspace
        from olano.pulse.engine import PulseEngine
        from olano.pulse.models import PulseLane

        ws = get_workspace(ctx.config.workspace)
        engine = PulseEngine(ctx.config, ws.state_dir)

        sub = (args[0] if args else "list").lower()
        if sub in ("show", "list"):
            items = engine.list_pending(lane=PulseLane.OPS)
            if not items:
                return CommandResult(output="No ops items pending.")
            lines = [f"OPS ({len(items)} items):"]
            for item in items:
                label = item.title or item.content
                lines.append(
                    "  "
                    f"[{item.id}] [{item.type.value}] [{item.priority_level.value}] "
                    f"[{item.source}] {label[:100]}"
                )
            lines.append("")
            lines.append("Actions: /ops reject <id> | /ops defer <id> [hours]")
            return CommandResult(output="\n".join(lines))
        if sub == "clear":
            removed = engine.clear_expired(lane=PulseLane.OPS)
            return CommandResult(output=f"Expired ops items cleared: {removed}.")
        if sub in {"reject", "dismiss"} and len(args) > 1:
            if engine.reject(args[1]):
                return CommandResult(output=f"Rejected ops item {args[1]}.")
            return CommandResult(output=f"Ops item {args[1]} not found.")
        if sub == "defer" and len(args) > 1:
            hours = 24.0
            if len(args) > 2:
                try:
                    hours = float(args[2])
                except ValueError:
                    return CommandResult(output="Usage: /ops defer <id> [hours]")
            if engine.defer(args[1], hours=hours):
                return CommandResult(output=f"Deferred ops item {args[1]} for {hours:g} hours.")
            return CommandResult(output=f"Ops item {args[1]} not found.")
        return CommandResult(output="Usage: /ops [list|reject <id>|defer <id> [hours]|clear]")

    async def _trust(self, args: list[str], ctx: CommandContext) -> CommandResult:
        """Show or set agent trust level."""
        from olano.security.trust_levels import TRUST_LEVEL_DESCRIPTIONS, TrustLevel

        current = ctx.config.security.trust_level
        if not args:
            desc = TRUST_LEVEL_DESCRIPTIONS.get(TrustLevel(current), "unknown")
            lines = [
                f"Current trust level: {current} ({TrustLevel(current).name})",
                f"  {desc}",
                "\nAvailable levels:",
            ]
            for lvl, d in TRUST_LEVEL_DESCRIPTIONS.items():
                marker = " ◀ current" if lvl == current else ""
                lines.append(f"  {lvl} ({TrustLevel(lvl).name}): {d}{marker}")
            return CommandResult(output="\n".join(lines))
        try:
            new_level = int(args[0])
            if not 0 <= new_level <= 4:
                return CommandResult(output="Trust level must be 0–4.")
            ctx.config.security.trust_level = new_level
            return CommandResult(
                output=f"Trust level set to {new_level} ({TrustLevel(new_level).name})"
            )
        except ValueError:
            return CommandResult(output="Usage: /trust [0-4]")

    async def _subagent(self, args: list[str], ctx: CommandContext) -> CommandResult:
        """List or enable/disable subagents."""
        if not ctx.config.subagents:
            return CommandResult(output="No subagents configured for this agent.")

        if not args:
            lines = [f"Subagents ({len(ctx.config.subagents)}):"]
            for sa in ctx.config.subagents:
                status = "[enabled]" if sa.enabled else "[disabled]"
                lines.append(f"  {status} {sa.name} — {sa.description}")
            return CommandResult(output="\n".join(lines))

        sub_cmd = args[0].lower()
        if sub_cmd in ("enable", "disable") and len(args) > 1:
            name = args[1]
            for sa in ctx.config.subagents:
                if sa.name.lower() == name.lower():
                    sa.enabled = sub_cmd == "enable"
                    return CommandResult(output=f"Subagent '{sa.name}' {sub_cmd}d.")
            return CommandResult(output=f"Subagent '{name}' not found.")
        return CommandResult(output="Usage: /subagent [enable|disable <name>]")

    async def _search(self, args: list[str], ctx: CommandContext) -> CommandResult:
        """Search memory, skills, and workspace for a query."""
        if not args:
            return CommandResult(output="Usage: /search <query>")
        query = " ".join(args)
        results: list[str] = [f"Search: '{query}'\n"]

        # Search MEMORY.md
        from olano.config.workspace import get_workspace
        from olano.memory.markdown_memory import MarkdownMemory

        ws = get_workspace(ctx.config.workspace)
        mem = MarkdownMemory(ws.root)
        mem_results = mem.search(query)
        if mem_results:
            results.append(f"Memory ({len(mem_results)} results):")
            for r in mem_results[:5]:
                results.append(f"  {r[:100]}")

        # Search typed memory
        try:
            from olano.memory.typed_memory import TypedMemoryStore

            store = TypedMemoryStore(ws.state_memory_dir)
            facts = store.search(query, max_results=5)
            if facts:
                results.append(f"\nTyped Memory ({len(facts)} results):")
                for f in facts:
                    results.append(f"  [{f.type}] {f.content[:100]}")
        except Exception:
            pass

        # RAG search if enabled
        if ctx.config.memory.rag:
            try:
                # NOTE: ``get_rag`` is not exported by ``rag_memory``; this import
                # fails at runtime and is intentionally swallowed by the
                # surrounding ``except Exception``. Suppress the attr-defined
                # error rather than silently changing this dead branch's behaviour.
                from olano.memory.rag_memory import (  # type: ignore[attr-defined]
                    get_rag,
                )

                rag = get_rag()
                if rag:
                    rag_results = rag.search(query, n_results=5)
                    if rag_results:
                        results.append(f"\nKnowledge Base ({len(rag_results)} results):")
                        for r in rag_results:
                            results.append(f"  {r['content'][:100]}")
            except Exception:
                pass

        return CommandResult(output="\n".join(results) or "No results found.")

    async def _share(self, args: list[str], ctx: CommandContext) -> CommandResult:
        """Manage session or persistent shared path grants."""
        from olano.config.agent_config import SharedPathConfig, ShareSubjectConfig
        from olano.config.loader import find_agent_config_path, save_agent_config
        from olano.config.workspace import get_workspace
        from olano.security.sharing import (
            canonicalize_share_source,
            normalize_share_target,
            resolve_shared_mounts,
            sanitize_mount_name,
            sync_workspace_share_projections,
            validate_share_source_path,
        )

        ws = get_workspace(ctx.config.workspace)

        def _session_grants() -> list[SharedPathConfig]:
            state = ctx.runner.load_thread_state(ctx.thread_id)
            raw = state.get("session_shared_paths", [])
            return [SharedPathConfig.model_validate(item) for item in raw]

        def _save_session_grants(grants: list[SharedPathConfig]) -> None:
            ctx.runner.update_thread_state(
                ctx.thread_id,
                session_shared_paths=[grant.model_dump() for grant in grants],
            )

        def _sync() -> None:
            sync_workspace_share_projections(
                ws.root, resolve_shared_mounts(ctx.config, _session_grants())
            )

        if not args or args[0] in {"list", "ls"}:
            session_grants = _session_grants()
            mounts = resolve_shared_mounts(ctx.config, session_grants)
            lines = [f"Shared paths ({len(mounts)} effective):"]
            for mount in mounts:
                lines.append(
                    f"  {mount.id}  {mount.mode}  {mount.persistence}  {mount.target} -> {mount.source}"
                )
            return CommandResult(output="\n".join(lines))

        subcmd = args[0]
        if subcmd == "add":
            if len(args) < 2:
                return CommandResult(
                    output=(
                        "Usage: /share add <path> [--mode ro|rw] [--as mount-name] "
                        "[--session] [--subagent <name> | --all-subagents]"
                    )
                )
            source_arg = args[1]
            mode = "ro"
            target_name: str | None = None
            persistence = "persistent"
            subjects = [ShareSubjectConfig(type="agent", name="self")]

            i = 2
            while i < len(args):
                token = args[i]
                if token == "--mode" and i + 1 < len(args):
                    mode = args[i + 1]
                    i += 2
                    continue
                if token == "--as" and i + 1 < len(args):
                    target_name = sanitize_mount_name(args[i + 1])
                    i += 2
                    continue
                if token == "--session":
                    persistence = "session"
                    i += 1
                    continue
                if token == "--subagent" and i + 1 < len(args):
                    subjects = [ShareSubjectConfig(type="subagent", name=args[i + 1])]
                    i += 2
                    continue
                if token == "--all-subagents":
                    subjects = [ShareSubjectConfig(type="all_subagents", name="*")]
                    i += 1
                    continue
                return CommandResult(output=f"Unknown flag: {token}")

            if mode not in {"ro", "rw"}:
                return CommandResult(output="Share mode must be 'ro' or 'rw'.")

            source = canonicalize_share_source(source_arg)
            path_error = validate_share_source_path(source)
            if path_error:
                return CommandResult(output=path_error)

            grant = SharedPathConfig(
                source=str(source),
                target=normalize_share_target(target_name, source),
                mode=cast('Literal["ro", "rw"]', mode),
                persistence=cast('Literal["session", "persistent"]', persistence),
                subjects=subjects,
            )

            if persistence == "session":
                grants = _session_grants()
                grants = [g for g in grants if g.id != grant.id and g.target != grant.target]
                grants.append(grant)
                _save_session_grants(grants)
            else:
                ctx.config.shared_paths = [
                    g
                    for g in ctx.config.shared_paths
                    if g.id != grant.id and g.target != grant.target
                ]
                ctx.config.shared_paths.append(grant)
                existing_path = find_agent_config_path(ctx.config.name)
                base = existing_path.parent.parent if existing_path else None
                save_agent_config(ctx.config, base=base)

            _sync()
            return CommandResult(
                output=(
                    f"Shared {source} as {grant.target} "
                    f"({grant.mode}, {grant.persistence}, {grant.subjects[0].type})."
                )
            )

        if subcmd == "remove":
            if len(args) < 2:
                return CommandResult(output="Usage: /share remove <id>")
            share_id = args[1]
            removed = False

            session_grants = [g for g in _session_grants() if g.id != share_id]
            if len(session_grants) != len(_session_grants()):
                _save_session_grants(session_grants)
                removed = True

            persistent = [g for g in ctx.config.shared_paths if g.id != share_id]
            if len(persistent) != len(ctx.config.shared_paths):
                ctx.config.shared_paths = persistent
                existing_path = find_agent_config_path(ctx.config.name)
                base = existing_path.parent.parent if existing_path else None
                save_agent_config(ctx.config, base=base)
                removed = True

            if removed:
                _sync()
                return CommandResult(output=f"Removed shared path '{share_id}'.")
            return CommandResult(output=f"No shared path found for id '{share_id}'.")

        return CommandResult(output="Usage: /share [add|list|remove]")

    async def _backend(self, args: list[str], ctx: CommandContext) -> CommandResult:
        """Show effective backend configuration for this agent."""
        from olano.config.backend_config import OlanoDockerBackendConfig
        from olano.config.workspace import get_workspace

        cfg = ctx.config.backend
        ws = get_workspace(ctx.config.workspace)

        lines = [f"Backend type:  {cfg.type}"]

        if hasattr(cfg, "root_dir"):
            resolved = str(ws.root) if cfg.root_dir == "workspace" else cfg.root_dir
            lines.append(f"Root dir:      {resolved}")
        if hasattr(cfg, "virtual_mode"):
            lines.append(f"Virtual mode:  {cfg.virtual_mode}")

        if isinstance(cfg, OlanoDockerBackendConfig):
            lines.append(f"Persistent:    {cfg.persistent}")
            lines.append(f"Image:         {cfg.image}")
            lines.append(f"Network:       {cfg.network}")
            lines.append(f"Mounts:        {', '.join(cfg.mounts)}")
            if cfg.snapshot_on_stop:
                lines.append("Snapshot:      on stop")

        if ctx.config.shared_paths:
            lines.append(f"Shared paths:  {len(ctx.config.shared_paths)} configured")
        return CommandResult(output="\n".join(lines))

    async def _platform(self, args: list[str], ctx: CommandContext) -> CommandResult:
        # Imported lazily: platform_control pulls in runtime/lifecycle machinery
        # that must stay off the import path of the plain dispatcher.
        from olano.platform_control.commands import handle_platform_command

        return await handle_platform_command(args, ctx)

    # ──────────────────────────────── handler map ─────────────────────────────

    _handlers: dict[str, Callable] = {
        "/help": _help,
        "/commands": _help,
        "/self-improve": _improve,
        "/status": _status,
        "/context": _context,
        "/mem": _mem,
        "/whoami": _whoami,
        "/id": _whoami,
        "/session": _session,
        "/tools": _tools,
        "/skills": _skills,
        "/agents": _agents,
        "/model": _model,
        "/models": _model,
        "/mode": _mode,
        "/memory": _memory,
        "/new": _new_session,
        "/clear": _new_session,
        "/reload-agent": _reload_agent,
        "/reload-vault": _reload_vault,
        "/think": _think,
        "/thinking": _think,
        "/t": _think,
        "/verbose": _verbose,
        "/v": _verbose,
        "/verbosity": _verbosity,
        "/quiet": _quiet,
        "/normal": _normal,
        # 2.0 commands
        "/pulse": _pulse,
        "/approvals": _approvals,
        "/ops": _ops,
        "/trust": _trust,
        "/subagent": _subagent,
        "/subagents": _subagent,
        "/search": _search,
        "/share": _share,
        "/backend": _backend,
        "/olano": _platform,
    }
