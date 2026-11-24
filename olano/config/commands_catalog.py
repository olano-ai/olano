"""Canonical catalog of built-in chat slash commands.

This is the **single source of truth** for which built-in commands exist, how
they group, and whether they are *sensitive* (hidden from chat users by
default). It is intentionally dependency-free (pure data) so it can be imported
by the config schema (:mod:`olano.config.agent_config`), the command
dispatcher (:mod:`olano.cli.commands`), the platform inbound layer
(:mod:`olano.platforms.inbound`) and the dashboard API without import cycles.

A test (`tests/test_command_catalog.py`) cross-checks this list against the
dispatcher's real handler map so the two can't silently drift.

"Exposure" governs the **chat surfaces**: messaging platforms/channels and the
dashboard chat.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Command groups, in display order, with a human label for the dashboard UI.
COMMAND_GROUPS: list[tuple[str, str]] = [
    ("info", "Info & inspection"),
    ("conversation", "Conversation control"),
    ("curation", "Curation & review"),
    ("sensitive", "State-changing / sensitive"),
    ("olano", "Deployment control (owner-only)"),
]

# Canonical slash name of the deployment-control command (server update /
# restart / shutdown). Named "olano" rather than "platform" to avoid
# colliding with the messaging *platforms* (olano/platforms/). Renaming it
# means changing this constant, the catalog entry below, and the two data
# files that spell it out: the agentfather archetype json5 and the knowledge
# docs under olano/archetypes/.
OLANO_COMMAND = "olano"


@dataclass(frozen=True)
class CommandSpec:
    """One built-in command. ``name`` is the canonical slash name (no slash)."""

    name: str
    description: str
    group: str
    sensitive: bool = False
    aliases: tuple[str, ...] = field(default_factory=tuple)

    @property
    def default_exposed(self) -> bool:
        """Whether the command is exposed to chat users on a fresh agent.

        Sensitive commands default to **off**; everything else defaults to
        **on**. The owner overrides this per agent via ``commands.enabled``.
        """
        return not self.sensitive


# The catalog. Keep canonical names + aliases in sync with
# ``CommandDispatcher._handlers`` (enforced by tests).
COMMAND_SPECS: tuple[CommandSpec, ...] = (
    # ── info / read-only ──────────────────────────────────────────────────
    CommandSpec("help", "List all commands", "info", aliases=("commands",)),
    CommandSpec("status", "Agent, model, thread, session info", "info"),
    CommandSpec("context", "Show context-window usage breakdown", "info"),
    CommandSpec("mem", "Process memory + structure breakdown (on|off toggles the monitor)", "info"),
    CommandSpec("whoami", "Current thread ID", "info", aliases=("id",)),
    CommandSpec("session", "Show session file paths", "info"),
    CommandSpec("tools", "List the agent's tools", "info"),
    CommandSpec("skills", "List loaded skills", "info"),
    CommandSpec("agents", "List all configured agents", "info"),
    CommandSpec("memory", "Show MEMORY.md", "info"),
    CommandSpec("search", "Search memory + knowledge base", "info"),
    CommandSpec("backend", "Show effective backend configuration", "info"),
    # ── conversation control (per-chat, safe) ─────────────────────────────
    CommandSpec("new", "Start a fresh session/thread", "conversation", aliases=("clear",)),
    CommandSpec("think", "Set thinking level", "conversation", aliases=("thinking", "t")),
    CommandSpec(
        "verbosity",
        "Set reply detail (quiet|normal|verbose)",
        "conversation",
    ),
    CommandSpec("quiet", "Replies show the answer only", "conversation"),
    CommandSpec("normal", "Replies use the default detail level", "conversation"),
    CommandSpec("verbose", "Toggle verbose output", "conversation", aliases=("v",)),
    # ── curation / review ─────────────────────────────────────────────────
    CommandSpec("pulse", "Show or manage curated PULSE items", "curation"),
    CommandSpec("approvals", "Show or manage approval items", "curation"),
    CommandSpec("ops", "Show or manage ops/runtime items", "curation"),
    # ── state-changing / sensitive (default OFF) ──────────────────────────
    CommandSpec(
        "model", "Show or switch the model", "sensitive", sensitive=True, aliases=("models",)
    ),
    CommandSpec("mode", "Show or switch prompt mode", "sensitive", sensitive=True),
    CommandSpec("trust", "Show or set agent trust level", "sensitive", sensitive=True),
    CommandSpec(
        "subagent",
        "Enable/disable subagents",
        "sensitive",
        sensitive=True,
        aliases=("subagents",),
    ),
    CommandSpec("self-improve", "Trigger a self-improvement cycle", "sensitive", sensitive=True),
    CommandSpec(
        "reload-agent", "Reload agent config from disk + rebuild", "sensitive", sensitive=True
    ),
    CommandSpec(
        "reload-vault", "Re-read vault secrets + rebuild agent", "sensitive", sensitive=True
    ),
    CommandSpec("share", "Manage shared host paths", "sensitive", sensitive=True),
    # ── deployment control (owner-only, default OFF) ──────────────────────
    # Handled entirely by the inbound platform layer — never reaches the
    # model. On top of this per-agent exposure gate, the handler enforces a
    # per-sender owner gate against the channel's ``allowed_users`` allowlist.
    CommandSpec(
        OLANO_COMMAND,
        "Owner-only: update / rebuild / restart / shut down the Olano server",
        "olano",
        sensitive=True,
    ),
)


# ── derived lookups ──────────────────────────────────────────────────────────

COMMAND_BY_NAME: dict[str, CommandSpec] = {spec.name: spec for spec in COMMAND_SPECS}

# Every name (canonical + alias) → canonical name.
ALIAS_TO_CANONICAL: dict[str, str] = {}
for _spec in COMMAND_SPECS:
    ALIAS_TO_CANONICAL[_spec.name] = _spec.name
    for _alias in _spec.aliases:
        ALIAS_TO_CANONICAL[_alias] = _spec.name

SENSITIVE_COMMANDS: frozenset[str] = frozenset(s.name for s in COMMAND_SPECS if s.sensitive)


def canonical_command(name: str) -> str:
    """Resolve a typed command name/alias to its canonical name (lowercased)."""
    return ALIAS_TO_CANONICAL.get(name.lower(), name.lower())


def default_exposed(name: str) -> bool:
    """Default chat exposure for *name* (canonical or alias).

    Unknown names default to exposed (``True``) so a freshly-added built-in is
    available until someone marks it sensitive here.
    """
    spec = COMMAND_BY_NAME.get(canonical_command(name))
    return spec.default_exposed if spec else True


def default_enabled_map() -> dict[str, bool]:
    """The full default exposure map keyed by canonical name."""
    return {spec.name: spec.default_exposed for spec in COMMAND_SPECS}
