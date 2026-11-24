"""
Runtime server that hosts multiple agents, their channels,
scheduler, and self-learning services.

olano serve  → starts the runtime
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import threading as _thread_mod
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

from olano.config.agent_state import is_agent_paused
from olano.config.home import load_home_config
from olano.config.loader import list_agents, load_agent_config
from olano.config.workspace import get_workspace
from olano.core.boot_profile import all_profiles, fleet_summary, profile_boot
from olano.core.boot_profile import phase as boot_phase
from olano.core.boot_profile import record as record_phase
from olano.core.builder import build_agent_async
from olano.core.prompt_state import compute_prompt_state
from olano.core.runner import AgentRunner
from olano.memory.markdown_memory import MarkdownMemory
from olano.memory.rag_memory import RAGMemory
from olano.memory.self_learning import SelfLearningService
from olano.orchestration.agent_registry import AgentEntry, AgentRegistry
from olano.orchestration.event_bus import EventBus, get_event_bus
from olano.platforms.catalog import (
    DEPRECATED_CHANNELS as _CATALOG_DEPRECATED_CHANNELS,
)
from olano.runtime import seal_guard
from olano.scheduler.cron import OlanoScheduler
from olano.security.request_context import AGENT_ADMIN_KEY, push_request_context

logger = logging.getLogger(__name__)

CHANNEL_MAP = {
    "telegram": "olano.channels.telegram_channel.TelegramChannel",
    "discord": "olano.channels.discord_channel.DiscordChannel",
    "slack": "olano.channels.slack_channel.SlackChannel",
    "whatsapp": "olano.channels.slack_channel.WhatsAppChannel",
    "email": "olano.channels.email_channel.EmailChannel",
    "imap": "olano.channels.email_channel.EmailChannel",  # alias
}

# New hermes-derived platform adapters (BasePlatformAdapter subclasses).
# Opt in by setting a channel ``type`` with the ``platform:`` prefix,
# e.g. ``type: platform:telegram``. Without the prefix the legacy
# BaseChannel adapter from CHANNEL_MAP is used.
PLATFORM_MAP = {
    "telegram": ("olano.platforms.telegram.TelegramAdapter", "TELEGRAM"),
    "discord": ("olano.platforms.discord.DiscordAdapter", "DISCORD"),
    "slack": ("olano.platforms.slack.SlackAdapter", "SLACK"),
    "email": ("olano.platforms.email.EmailAdapter", "EMAIL"),
    "signal": ("olano.platforms.signal.SignalAdapter", "SIGNAL"),
    "matrix": ("olano.platforms.matrix.MatrixAdapter", "MATRIX"),
    "whatsapp": ("olano.platforms.whatsapp.WhatsAppAdapter", "WHATSAPP"),
    "whatsapp_twilio": (
        "olano.platforms.whatsapp_twilio.WhatsAppTwilioAdapter",
        "WHATSAPP_TWILIO",
    ),
    "whatsapp_meta_api": (
        "olano.platforms.whatsapp_meta_api.WhatsAppMetaAPIAdapter",
        "WHATSAPP_META_API",
    ),
    "mattermost": ("olano.platforms.mattermost.MattermostAdapter", "MATTERMOST"),
    "homeassistant": (
        "olano.platforms.homeassistant.HomeAssistantAdapter",
        "HOMEASSISTANT",
    ),
    "sms": ("olano.platforms.sms.SmsAdapter", "SMS"),
    "webhook": ("olano.platforms.webhook.WebhookAdapter", "WEBHOOK"),
    "dingtalk": ("olano.platforms.dingtalk.DingTalkAdapter", "DINGTALK"),
    "feishu": ("olano.platforms.feishu.FeishuAdapter", "FEISHU"),
    "wecom": ("olano.platforms.wecom.WeComAdapter", "WECOM"),
    "weixin": ("olano.platforms.weixin.WeixinAdapter", "WEIXIN"),
    "bluebubbles": ("olano.platforms.bluebubbles.BlueBubblesAdapter", "BLUEBUBBLES"),
    "qqbot": ("olano.platforms.qqbot.adapter.QQAdapter", "QQBOT"),
    "debox": ("olano.platforms.debox.DeBoxAdapter", "DEBOX"),
    "api_server": ("olano.platforms.api_server.APIServerAdapter", "API_SERVER"),
}

# Legacy channel types that still boot but are duplicated by a platform
# adapter — every entry in CHANNEL_MAP now is. Existing configs keep working;
# the boot path logs a deprecation warning pointing at the replacement type
# (the dashboard also hides these from the connector picker). The map itself
# lives in platforms/catalog.py next to the note the dashboard shows, so the
# warning and the note can never name different replacements; re-exported here
# because DEPRECATED_CHANNELS has been importable from this module.
DEPRECATED_CHANNELS = _CATALOG_DEPRECATED_CHANNELS

# Upper bound on how long a single channel's ``start()`` may block during boot.
# A channel that talks to a remote service (e.g. Telegram's bootstrap webhook
# call) can hang on a network timeout; without this bound one unreachable
# channel would stall the entire runtime — including the REST API and login.
_CHANNEL_START_TIMEOUT = 30.0

# Threads in the loop's default executor. See _install_default_executor: the
# pool is shared by every to_thread call, langchain's sync tools and the
# adapters' blocking sends, so the stock cpu_count+4 (6 here) is far too narrow.
_DEFAULT_EXECUTOR_WORKERS = 32


def _load_channel_class(name: str) -> type:
    import importlib

    dotted = CHANNEL_MAP.get(name)
    if not dotted:
        raise ValueError(f"Unknown channel: '{name}'. Available: {list(CHANNEL_MAP)}")
    module_path, class_name = dotted.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return cast(type, getattr(module, class_name))


# Tracks (channel_type → token-fingerprint → agent_name) so we can detect
# when the same bot token is configured on more than one agent. Only the
# first 6 + last 4 characters of the token are kept (never the whole
# secret) — enough to fingerprint without leaking it into logs.
_TOKEN_OWNERS: dict[str, dict[str, str]] = {}

# Tracks process-wide resource owners so we can warn when two agents
# resolve to the same TCP port or session/auth path. The same agent may
# re-register on restart — store the owner so restarts don't trigger a
# false-positive warning.
_PORT_OWNERS: dict[int, str] = {}
_PATH_OWNERS: dict[str, str] = {}


def _token_fingerprint(value: str) -> str:
    if not value or len(value) < 12:
        return value or ""
    return f"{value[:6]}…{value[-4:]}"


def _clear_resource_owners_for_agent(agent_name: str) -> None:
    """Remove any port/path claims belonging to ``agent_name``. Called
    on agent stop/restart so the second boot doesn't trip the
    collision warning against its own previous registration."""
    for port in [p for p, owner in _PORT_OWNERS.items() if owner == agent_name]:
        _PORT_OWNERS.pop(port, None)
    for path in [p for p, owner in _PATH_OWNERS.items() if owner == agent_name]:
        _PATH_OWNERS.pop(path, None)
    for chan in _TOKEN_OWNERS.values():
        for fp in [f for f, owner in chan.items() if owner == agent_name]:
            chan.pop(fp, None)


def _check_resource_collision(
    channel_type: str,
    ch_cfg: dict,
    agent_name: str,
) -> None:
    """Warn (and ideally surface in the dashboard) when two agents resolve
    to the same TCP port or on-disk session/auth path. Without this guard
    the second adapter wins the bind / overwrites the session and the
    first agent silently dies.

    The set of fields we inspect is hand-maintained because each
    collision-prone adapter names its port/path keys differently:

      • WhatsApp        → ``bridge_port`` + ``session_path``
      • Webhook         → ``port``
      • SMS             → env var (``SMS_WEBHOOK_PORT``); no per-config knob today
      • BlueBubbles     → ``webhook_port``
      • Feishu          → ``webhook_port``
      • API server      → ``port``
      • WeCom callback  → ``port``
      • Telegram (webhook mode, platform adapter) → env var (``TELEGRAM_WEBHOOK_PORT``)

    Adapters that *only* read from env vars without a per-config override
    can't be deconflicted here — they're flagged in the report instead.
    """
    port_keys = ("bridge_port", "port", "webhook_port")
    path_keys = ("session_path", "session_dir", "auth_dir", "store_path")

    def _claim_port(value: Any) -> None:
        try:
            port = int(value)
        except (TypeError, ValueError):
            return
        if port <= 0:
            return
        prior = _PORT_OWNERS.get(port)
        if prior and prior != agent_name:
            logger.warning(
                "Port %d is configured by BOTH agent '%s' and '%s' "
                "(channel '%s'). The second adapter will fail to bind. "
                "Override the port in this agent's connector config.",
                port,
                prior,
                agent_name,
                channel_type,
            )
        _PORT_OWNERS[port] = agent_name

    def _claim_path(value: Any) -> None:
        try:
            normalized = str(Path(str(value)).expanduser().resolve())
        except Exception:
            normalized = str(value)
        if not normalized:
            return
        prior = _PATH_OWNERS.get(normalized)
        if prior and prior != agent_name:
            logger.warning(
                "Session/auth path '%s' is configured by BOTH agent "
                "'%s' and '%s' (channel '%s'). Sharing this directory "
                "will corrupt credentials. Set a per-agent path in this "
                "connector's config.",
                normalized,
                prior,
                agent_name,
                channel_type,
            )
        _PATH_OWNERS[normalized] = agent_name

    for key in port_keys:
        if key in ch_cfg:
            _claim_port(ch_cfg[key])
    for key in path_keys:
        if key in ch_cfg:
            _claim_path(ch_cfg[key])


# Connectors whose ``allowed_users`` admits customers rather than naming the
# bot's owners; on these only ``owner_users`` makes a sender trusted.
_CUSTOMER_ALLOWLIST_PLATFORMS = frozenset({"whatsapp_twilio", "whatsapp_meta_api"})


def _normalize_allowed_users(raw: Any) -> frozenset[str]:
    """Lowered ``allowed_users`` entries from a channel config.

    Accepts the list form plus the str/int singleton forms the legacy
    channels tolerate (see ``TelegramChannel.__init__``). Entries are
    compared case-insensitively with any leading ``@`` stripped.
    """
    if raw is None:
        return frozenset()
    if isinstance(raw, str | int):
        raw = [raw]
    try:
        # strip() BEFORE lstrip("@") so a padded entry like " @Boss " still
        # loses its leading @ (lstrip stops at the leading space otherwise, and
        # the stray @ would then never match an inbound handle).
        return frozenset(str(u).strip().lstrip("@").lower() for u in raw if str(u).strip())
    except TypeError:
        return frozenset()


def _sender_is_trusted(
    user_id: str, username: str, allowed_users: frozenset[str], phone: str = ""
) -> bool:
    """Whether an inbound sender is an owner per the channel's trust lists.

    Callers pass the union of ``allowed_users`` and ``owner_users`` — the
    latter designates owners without gating who may message the bot, so an
    open bot can still have a real owner. Trust keys ONLY on the stable
    identifiers — the numeric/user id and the @handle — never a display name,
    which is non-unique and freely settable by the sender on Discord/Telegram
    (so trusting it would let anyone rename themselves to an allowlisted name
    and run owner-only commands like /olano shutdown). An empty union trusts
    nobody.
    """
    if not allowed_users:
        return False
    # lstrip("@") mirrors _normalize_allowed_users: Matrix user_ids arrive as
    # full MXIDs (@alice:example.org) and would otherwise never match the
    # @-stripped allowlist entries.
    uid = (user_id or "").lstrip("@").lower()
    handle = (username or "").lstrip("@").lower()
    if uid in allowed_users or (bool(handle) and handle in allowed_users):
        return True
    # WhatsApp only (the caller passes ``phone`` for its connectors alone): owners are
    # typed as phone numbers ("+65 9123 4567") while the sender arrives as a
    # JID ("6591234567@s.whatsapp.net") or a privacy ``@lid`` id with the
    # phone JID beside it, so an exact string compare never matched and an
    # owner on the list was treated as a stranger. Compare the digits of real
    # phone identifiers only; an ``@lid`` or a handle never yields digits.
    if phone:
        from olano.platforms.chat_approval import phone_digits

        digits = phone_digits(phone) or phone_digits(user_id)
        if digits:
            return any(phone_digits(entry) == digits for entry in allowed_users)
    return False


def _check_token_collision(channel_type: str, ch_cfg: dict, agent_name: str) -> None:
    """Warn when two agents are configured with the same single-instance
    bot token. Telegram, Discord, and Slack all reject a second client
    that identifies with a token already held by an active session — the
    failure mode is platform-specific (Telegram 409 Conflict, Discord
    "Identifying with the same token" disconnect, Slack token revoke
    on auth_test) but the symptom is identical: one of the two agents
    silently stops receiving messages.

    Each entry in the table maps a channel type to: (config-key, fallback
    env-var, human-readable platform name, human-readable remediation).
    """
    # Each channel must have ONE bot per token. Webhook-style platforms
    # (email, signal, sms, webhook, …) aren't listed because they don't
    # share this constraint.
    token_specs: dict[str, tuple[str, str, str, str]] = {
        # type            cfg-key        env-var fallback         platform     remediation
        "telegram": (
            "token",
            "TELEGRAM_BOT_TOKEN",
            "Telegram",
            "Create a separate bot via @BotFather and use a unique token per agent.",
        ),
        "discord": (
            "token",
            "DISCORD_BOT_TOKEN",
            "Discord",
            "Register a separate application + bot at https://discord.com/developers/applications and use a unique token per agent.",
        ),
        "slack": (
            "bot_token",
            "SLACK_BOT_TOKEN",
            "Slack",
            "Register a separate Slack app at https://api.slack.com/apps and use a unique bot_token (xoxb-...) per agent.",
        ),
    }
    spec = token_specs.get(channel_type)
    if not spec:
        return
    cfg_key, env_var, platform, remediation = spec
    raw = ch_cfg.get(cfg_key) or os.environ.get(env_var, "")
    if not raw or (isinstance(raw, str) and raw.startswith("${")):
        return  # placeholder still unresolved — handled by _flag_unresolved_secrets
    fp = _token_fingerprint(raw)
    owners = _TOKEN_OWNERS.setdefault(channel_type, {})
    prior = owners.get(fp)
    if prior and prior != agent_name:
        logger.warning(
            "%s token %s is configured on BOTH '%s' and '%s'. Only one "
            "active client per token is allowed — one of these agents "
            "will fail to receive messages. %s",
            platform,
            fp,
            prior,
            agent_name,
            remediation,
        )
    owners[fp] = agent_name


def _flag_unresolved_secrets(ch_cfg: dict, *, agent: str, channel: str) -> list[str]:
    """Walk the channel config and warn for any value still wearing a
    ${vault:...} or ${ENV_VAR} placeholder after resolution. Most adapters
    treat such strings as the literal token, so the upstream service then
    fails with a confusing 401/Unauthorized.

    Returns the list of still-unresolved field paths (empty when all resolved),
    so the caller can track the agent for an auto-reboot once the vault unlocks.
    """
    unresolved: list[str] = []

    def _walk(node: object, path: str = "") -> None:
        if isinstance(node, str):
            if node.startswith("${"):
                unresolved.append(path or "<root>")
                logger.warning(
                    "Agent '%s' channel '%s': field %s still has unresolved "
                    "placeholder %r. Likely cause: the vault is locked, the "
                    "entry doesn't exist, or the env var isn't set. The "
                    "channel will not work until this is resolved.",
                    agent,
                    channel,
                    path or "<root>",
                    node,
                )
        elif isinstance(node, dict):
            for k, v in node.items():
                _walk(v, f"{path}.{k}" if path else k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                _walk(v, f"{path}[{i}]")

    _walk(ch_cfg)
    return unresolved


def _missing_credentials_reason(missing: list[tuple[str, str]]) -> str:
    """Why a connector was skipped, in the words the dashboard shows.

    Read by the connector's diagnostics (``/api/agents/<name>/connectors/health``)
    and the bell. It names the dashboard route and never the CLI: a managed
    box's operator has no shell, and the connector form stores the value in
    the vault when it is saved.
    """
    keys = ", ".join(key for _, key in missing)
    return (
        f"Not started: no value for {keys} for this agent. Open this connector, enter "
        "it in its field (the connector's Save stores it in the vault), then restart "
        "the agent."
    )


def _report_missing_channel_credentials(
    agent: str,
    channel: str,
    missing: list[tuple[str, str]],
) -> None:
    """Explain a skipped connector — in the log and in the dashboard bell.

    Credentials are resolved from the vault by convention, so "nothing found"
    has exactly one fix: put the value under that key for this agent. Naming the
    key (and, in the log, the command) turns a silent dead connector into a
    one-line repair.
    """
    keys = [key for _, key in missing]
    fix = " ".join(f"`olano vault set {key} --agent {agent}`" for key in keys)
    logger.error(
        "Agent '%s': connector '%s' not started — no credential for %s. "
        "Set it for this agent (per-agent override wins over the shared value): %s",
        agent,
        channel,
        ", ".join(keys),
        fix,
    )
    try:
        from olano.runtime.notifications import notify

        notify(
            kind="channel_credential_missing",
            level="error",
            title=f"{channel} is not running for {agent}",
            body=_missing_credentials_reason(missing),
            agent=agent,
            # One notification per (agent, channel) — a reboot loop must not
            # stack duplicates in the bell.
            dedupe_key=f"channel_credential_missing:{agent}:{channel}",
            action={"label": "Open connector", "href": f"/agents/{agent}?tab=connectors"},
        )
    except Exception:  # pragma: no cover - a notification must never block boot
        logger.debug("Could not raise the missing-credential notification", exc_info=True)


def _align_skill_names_at_boot(agent: str, config: Any, workspace: Any) -> bool:
    """Rewrite illegal skill names to their folder before the agent loads them.

    The backstop for SKILL.md files no tool saw being written (a shell
    heredoc, a script, an older release): every illegal ``name`` whose folder
    is legal becomes that folder, the one fix that moves no path
    (``skills.naming.align_skill_names``). A changed name is carried into the
    ``disabled_skills`` of every agent that can see the skill, so a skill that
    was switched off stays off.

    Returns ``True`` when *agent*'s own config changed, so the caller reloads it
    before building. Never raises.
    """
    try:
        from pathlib import Path as _Path

        from olano.admin.skills import carry_disabled_skill_renames
        from olano.skills.loader import _skill_source_dirs
        from olano.skills.naming import align_skill_names

        rewritten = align_skill_names(_skill_source_dirs(config, workspace))
        if not rewritten:
            return False
        managed = _Path(workspace.managed_skills_dir)
        shared = {r["from"]: r["to"] for r in rewritten if _Path(r["path"]).is_relative_to(managed)}
        own = {r["from"]: r["to"] for r in rewritten}
        changed: list[str] = []
        if shared:
            from olano.config.loader import list_agents

            changed += carry_disabled_skill_renames(shared, list_agents())
        changed += carry_disabled_skill_renames(own, [agent])
        return agent in changed
    except Exception:  # pragma: no cover - never block a boot
        logger.debug("Could not align skill names for '%s'", agent, exc_info=True)
        return False


def _report_skill_spec_violations(agent: str, config: Any, workspace: Any) -> None:
    """Put an agent's spec-violating skill names on the bell; clear when fixed.

    A skill whose ``name`` differs from its folder makes deepagents log a
    warning on every boot and, per its own comment, keeps loading it only "for
    backwards compatibility" — so today it is noise, and one upstream release
    away from the agent silently losing the skill. Neither state is visible to
    an operator without reading the log, which is exactly the gap this reports.

    By the time this runs, :func:`_align_skill_names_at_boot` has already fixed
    every illegal name in a legal folder, so what is left needs a folder
    renamed, which Repair does on request.

    One row per agent (not per skill): three renamed folders are one thing to
    go fix, and the body names each of them. Never raises — a boot must not
    fail over a naming rule.
    """
    try:
        from pathlib import Path as _Path

        from olano.skills.loader import _skill_source_dirs, discover_skills

        offenders: list[str] = []
        seen: set[str] = set()
        for source_dir in _skill_source_dirs(config, workspace):
            for skill in discover_skills(_Path(source_dir)):
                error = skill.get("spec_error")
                location = skill.get("location", "")
                if not error or location in seen:
                    continue
                seen.add(location)
                offenders.append(f"{_Path(location).parent.name}/ — {error}")

        key = f"skills_spec_violation:{agent}"
        from olano.runtime import notifications

        if not offenders:
            notifications.resolve(key)
            return
        notifications.notify(
            kind="skills_spec_violation",
            level="warn",
            title=f"{len(offenders)} skill(s) have a name that doesn't match their folder",
            body=(
                "The Agent Skills spec requires a skill's name to match the folder holding "
                "its SKILL.md. These still load, but every agent boot logs a warning and a "
                "future update may skip them: " + "; ".join(sorted(offenders)) + ". "
                "Repair renames each folder to match the skill's name (or, when the name "
                "itself is not a legal skill name, sets it to the folder's), and keeps any "
                "skill you switched off switched off."
            ),
            agent=agent,
            dedupe_key=key,
            action={"label": "Open Files", "href": f"/files?agent={agent}"},
        )
    except Exception:  # pragma: no cover - never block a boot
        logger.debug("Could not check skill name compliance for '%s'", agent, exc_info=True)


def _clear_missing_channel_credentials(agent: str, channel: str) -> None:
    """The connector has its credential again — take the alert off the bell."""
    try:
        from olano.runtime.notifications import resolve

        resolve(f"channel_credential_missing:{agent}:{channel}")
    except Exception:  # pragma: no cover - never block a successful boot
        logger.debug("Could not clear the missing-credential notification", exc_info=True)


def _mcp_auth_key(agent: str, url: str) -> str:
    """Dedupe key for one agent's re-authorization alert on one MCP server."""
    return f"mcp_auth_required:{agent}:{url}"


def _report_mcp_auth_required(agent: str, rows: list[dict[str, Any]]) -> None:
    """Put MCP servers that need a human re-authorization on the bell.

    *rows* are per-server dicts carrying at least ``url``, ``ok`` and
    ``status`` — the shape of both ``builder.get_mcp_summary()`` and a health
    sweep's ``ServerReport.to_dict()``, so boot and the sweep ring through one
    producer rather than two that can disagree.

    This is the end of the road for a grant the runtime cannot renew: an OAuth
    token that expired with nothing to refresh from, or one the provider has
    revoked. Everything upstream of it already retries — the SDK refresh, the
    endpoint rediscovery, the sweep's reconnect backoff — and none of them can
    invent consent, so the *only* thing that moves this forward is a person
    clicking Authorize. The sole trace of that used to be a log line, which
    left the visible symptom as an agent that quietly stopped having the tools
    it was configured with — reading as the model being lazy rather than as a
    connection being down.

    One row per (agent, server) with the server's own MCP panel attached, and
    resolved the moment it connects again — the bell reflects the world, not
    the history. Never raises: a boot must not fail over an alert.

    *rows* must be the agent's **complete** server list (every caller passes
    one: the build summary has a row per configured server, the sweep a row
    per config entry, and ``[]`` for an agent with none). Any of this agent's
    alerts for a URL not among them is resolved, because a server removed
    from the config has no row left to resolve it by key, and its alert
    otherwise stayed on the bell forever.
    """
    try:
        from olano.runtime import notifications

        keep: set[str] = set()
        for row in rows:
            url = str(row.get("url") or "")
            if not url:
                continue
            key = _mcp_auth_key(agent, url)
            keep.add(key)
            if row.get("ok") or row.get("enabled") is False or row.get("status") != "auth_required":
                # A disabled server is not something to go authorize, even
                # while the last build's summary still says `auth_required`
                # (the sweep reads `enabled` off the config, which can be
                # newer than the build).
                notifications.resolve(key)
                continue
            if row.get("action") == "reload_requested":
                # The sweep has queued a reconnect for this one *this tick* —
                # someone authorized it since the last build, most likely. Stay
                # quiet: if the reconnect works the row would have been wrong,
                # and if it doesn't the next tick rings with no reconnect
                # pending. Only the sweep's rows carry `action`; a boot summary
                # has none, so boot always rings.
                continue
            host = urlparse(url).hostname or url
            notifications.notify(
                kind="mcp_auth_required",
                level="warn",
                title=f"{host} needs to be re-authorized for {agent}",
                body=(
                    f"The stored OAuth grant for {url} can no longer be renewed, so "
                    f"'{agent}' is running without that server's tools and will say it "
                    "cannot do those things. Reconnecting cannot fix this on its own — "
                    "open the agent's MCP panel and complete 'Authorize' to restore it."
                ),
                agent=agent,
                dedupe_key=key,
                action={"label": "Open MCP setup", "href": f"/agents/{agent}?tab=mcp"},
            )
        prefix = _mcp_auth_key(agent, "")
        notifications.resolve_where(
            lambda n: (n.dedupe_key or "").startswith(prefix) and n.dedupe_key not in keep
        )
    except Exception:  # pragma: no cover - never block a boot or a sweep
        logger.debug("Could not report MCP authorization state for '%s'", agent, exc_info=True)


def _prune_orphaned_mcp_auth_alerts() -> int:
    """Clear re-authorization alerts for agents that no longer exist.

    Deleting an agent clears its notifications now, but a row raised before
    that (or by a delete that went around the runtime, such as a hand-removed
    directory) has no producer left to resolve it. Blocking (a config-path
    lookup per distinct agent); the sweep runs it in a thread. Never raises.
    """
    try:
        from olano.config.loader import find_agent_config_path
        from olano.runtime import notifications

        known: dict[str, bool] = {}

        def _orphan(n: Any) -> bool:
            if n.kind != "mcp_auth_required" or not n.agent:
                return False
            if n.agent not in known:
                known[n.agent] = find_agent_config_path(n.agent) is not None
            return not known[n.agent]

        return notifications.resolve_where(_orphan)
    except Exception:  # pragma: no cover - housekeeping, never block the sweep
        logger.debug("Could not prune orphaned MCP authorization alerts", exc_info=True)
        return 0


def _load_platform_class(name: str) -> tuple[type, str]:
    """Resolve ``platform:<name>`` to an adapter class + Platform enum member name."""
    import importlib

    entry = PLATFORM_MAP.get(name)
    if not entry:
        raise ValueError(f"Unknown platform: '{name}'. Available: {list(PLATFORM_MAP)}")
    dotted, platform_enum_name = entry
    module_path, class_name = dotted.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name), platform_enum_name


# ``PlatformConfig`` reads these off the top level of a channel config; every
# other key it only ever looks for inside ``extra``.
_PLATFORM_CONFIG_TOP_LEVEL_CREDENTIALS = ("token", "api_key")


def _fold_credentials_into_extra(platform_name: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Move vault-hydrated credentials into ``extra`` for a platform adapter.

    ``hydrate_channel_credentials`` injects each credential at the config's top
    level, but ``PlatformConfig.from_dict`` keeps only its own named fields
    there and drops the rest — while the adapters read ``extra``. Without this
    fold a vaulted ``WEIXIN_APP_TOKEN`` or ``WECOM_SECRET`` is resolved at boot
    and then silently discarded on the way to the adapter.

    ``token``/``api_key`` are left alone: those ARE named fields, so they
    survive on their own and moving them would break the adapters reading them.
    An explicit value already in ``extra`` wins — the connector said it
    deliberately, and the canonical vault key is only the default.
    """
    from olano.config.channel_secrets import credential_fields

    cfg = dict(cfg)
    extra = dict(cfg.get("extra") or {})
    for field_name in credential_fields(f"platform:{platform_name}"):
        if field_name in _PLATFORM_CONFIG_TOP_LEVEL_CREDENTIALS:
            continue
        value = cfg.pop(field_name, None)
        if isinstance(value, str) and value.strip():
            extra.setdefault(field_name, value)
    # Plain settings an earlier dashboard form saved at the top level too (the
    # same drop, minus the vault). Same rule: an explicit `extra` value wins.
    for field_name in _LEGACY_TOP_LEVEL_EXTRA.get(platform_name, ()):
        value = cfg.pop(field_name, None)
        if value not in (None, "", [], {}):
            extra.setdefault(field_name, value)
    if extra:
        cfg["extra"] = extra
    return cfg


# Non-credential connector settings the first version of a dashboard form
# wrote at the top level of the channel instead of under `extra`, keyed by
# platform. The form now writes `extra` (and reads the old spot once, via
# `legacyTopLevel` in connectorCatalog.ts); this keeps a connector saved before
# that fix working without a re-save. Adding a platform here is only ever a
# repair for a form that shipped without `extraKey`.
_LEGACY_TOP_LEVEL_EXTRA: dict[str, tuple[str, ...]] = {
    "whatsapp_twilio": (
        "account_sid",
        "from_number",
        "service_sid",
        "mcs_region",
        "allowed_users",
    ),
}


def _format_mcp_notice(online: list[dict[str, Any]], offline: list[dict[str, Any]]) -> str | None:
    """Render an MCP online/offline delta as a one-shot note for the agent."""
    parts: list[str] = []
    for s in online:
        tools = s.get("tools") or []
        shown = ", ".join(tools[:15]) + (" …" if len(tools) > 15 else "")
        parts.append(
            f"🔌 MCP server **{s.get('url', '')}** is now ONLINE "
            f"({len(tools)} tool(s){': ' + shown if shown else ''}). "
            "You can use these tools now."
        )
    for s in offline:
        parts.append(
            f"⚠️ MCP server **{s.get('url', '')}** is now OFFLINE "
            f"({s.get('status', 'unavailable')}). Do not attempt its tools until it reconnects."
        )
    if not parts:
        return None
    return "[MCP status update]\n" + "\n".join(parts)


def _boot_hidden_agents_enabled() -> bool:
    """Whether the runtime should boot ``hidden: true`` (internal/test) agents.

    Off by default: hidden agents are a pytest / ``olano --agent X -n "..."``
    convenience and must not run inside the long-lived daemon. Set
    ``LIONCLAW_BOOT_HIDDEN_AGENTS=1`` to boot them anyway (dev/debug runtime).
    """
    return os.environ.get("LIONCLAW_BOOT_HIDDEN_AGENTS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


class RuntimeServer:
    """
    Central runtime that manages all agents, channels, scheduler, and memory services.

    2.0 additions:
    - AgentRegistry: tracks health and capabilities of all running agents
    - EventBus: pub/sub for operational events (message received, tool called, etc.)
    - ChannelRegistry: unified outbound delivery across all channels
    - PULSE delivery loop: routes pending PULSE items to configured channels
    """

    def __init__(self, agent_names: list[str] | None = None) -> None:
        # When the caller names agents explicitly (e.g. `olano serve --agents X`),
        # honor that request and ignore the persisted paused flag. Only the
        # default "boot everything" path respects per-agent paused state.
        self._explicit_agents = agent_names is not None
        self.agent_names = agent_names or self._on_disk_roster()
        self.scheduler = OlanoScheduler()
        self._runners: dict[str, AgentRunner] = {}
        self._channels: list[Any] = []
        self._platforms: list[Any] = []  # BasePlatformAdapter instances (new path)
        self._self_learning: list[SelfLearningService] = []
        self._acp_servers: list[Any] = []
        # port → the task serving it. One site per port, shared by every agent
        # configured on that port (the ACP routes key on the agent registry).
        self._acp_sites: dict[int, asyncio.Task[Any]] = {}
        # Inbound MCP + A2A servers (olano/mcp_server, olano/a2a/server): built
        # by create_api; agents with `external_access.mcp` / `.a2a` register
        # here at boot and leave at stop.
        self.mcp_server: Any = None
        self.a2a_server: Any = None
        self._background_tasks: list[asyncio.Task[Any]] = []
        # Per-agent resource bookkeeping so we can tear down a single agent
        # without disturbing the others. Populated by `_boot_agent`.
        self._agent_resources: dict[str, dict[str, list[Any]]] = {}
        # One lock per agent, serializing that agent's boot/stop. A boot takes
        # seconds to minutes (model build + MCP handshakes + RAG ingest) and
        # only lands in `_runners` at the very end, so two overlapping start
        # requests — an impatient second click, a second dashboard on another
        # device — both sailed past the "already running?" check and booted the
        # same agent twice: two Telegram pollers on one token (a permanent 409
        # storm plus every inbound message answered twice), two sets of
        # scheduler jobs, two RAG watchers, and an `_agent_resources` entry that
        # only remembers one of the two for teardown. See `_boot_agent`.
        self._boot_locks: dict[str, asyncio.Lock] = {}
        # Agents whose channel/tool secrets were still ${NAME} placeholders at
        # boot (vault locked / entry missing). Used to auto-reboot them once the
        # vault is unlocked mid-session — see `reboot_unresolved_secret_agents`.
        self._agents_with_unresolved_secrets: set[str] = set()
        # agent → {connector type: why it was not started}. A connector skipped
        # at boot has no adapter to ask, so without this its diagnostics could
        # only say "not running" and point at the log, which the operator of a
        # managed box cannot read. Rebuilt on every boot of the agent.
        self._skipped_channels: dict[str, dict[str, str]] = {}
        # Agents kept offline by the deployment-settings layer (max_agents cap
        # or quarantine): name → human-readable reason. Surfaced by /api/agents
        # and GET /api/system/deployment so the dashboard can show why.
        self._blocked_agents: dict[str, str] = {}
        # The agents the startup loop intends to boot, resolved before it starts
        # (see start()). Lets /api/status report boot progress as "N of M"
        # rather than an unbounded wait. Empty until the loop is reached.
        self._agents_expected: list[str] = []
        # Prompt refresh sweep state: agents nudged for an immediate stable-
        # section fingerprint check ("*" = all running) and the wake event the
        # sweep loop waits on between periodic ticks. See `_prompt_refresh_loop`.
        self._prompt_refresh_pending: set[str] = set()
        self._prompt_refresh_wake: asyncio.Event = asyncio.Event()
        self._cognition_drainer_wake: asyncio.Event = asyncio.Event()
        # Last MCP health sweep, agent → report dict (see `_mcp_health_loop` and
        # GET /api/diagnostics/mcp). Empty until the first tick completes.
        self._mcp_health: dict[str, dict[str, Any]] = {}
        self._mcp_health_checked_at: float | None = None
        # Cloud settings refresh loop (managed-cloud installs only; see start()).
        self._settings_poller: Any = None
        # Cloud usage reporter (any install with a deployment token; see start()).
        self._usage_reporter: Any = None
        # Credential-rotation reporter (temporary-password purge signal; see start()).
        self._rotation_reporter: Any = None
        # Daily disk report to the cloud console (metrics.disk on /api/v1/health).
        self._disk_reporter: Any = None
        # Host update-agent reconciler (managed boxes; see start()).
        self._host_agent_sync: Any = None
        # Cloud catalog sync (models/MCP directory manifest poller; see start()).
        self._catalog_sync: Any = None
        self._uvicorn_server: Any = None
        self._shutdown_event: asyncio.Event | None = None
        self._stopped = False
        # True while the initial agent-boot loop is running. The API binds
        # before agents boot, so /api/status uses this to report "booting"
        # instead of implying every agent is already up.
        self._agents_booting = False
        # Process start instant. /api/status reports this as "uptime" and the
        # dashboard renders it as (now - value), so it must be the START time,
        # not the current time.
        self._started_at = datetime.now(timezone.utc)
        # Set by platform_control before a chat-triggered restart: runs after
        # stop() completes (an os.execv re-exec never returns from it).
        self._post_stop_action: Callable[[], None] | None = None
        # Signal-handling state.  ``_signals_seen`` lets a second Ctrl+C
        # escalate to a hard kill if graceful teardown is stuck on a
        # misbehaving adapter.
        self._signals_seen = 0
        self._signal_loop: asyncio.AbstractEventLoop | None = None
        # The loop start() runs on. Sync admin tools (executor threads) use it
        # to dispatch lifecycle coroutines in-process instead of a loopback
        # HTTP call, which multi-user cookie auth would 401 (see
        # olano.admin.lifecycle). _signal_loop is not a substitute — it is
        # only set where signal handlers install.
        self._loop: asyncio.AbstractEventLoop | None = None

        # 2.0 infrastructure
        self.registry = AgentRegistry()
        self.event_bus: EventBus = get_event_bus()

        from olano.platforms import ChannelRegistry

        self.channel_registry = ChannelRegistry()

        # Outbound webhooks — subscribes to the event bus and fans agent events
        # out to per-agent webhook URLs. Registration is config-driven so hooks
        # come back automatically after a restart.
        from olano.webhooks import WebhookDispatcher

        self.webhook_dispatcher = WebhookDispatcher(self.event_bus)

        # Memory monitor: the background sampler + tracemalloc tracing, opt-in and
        # toggleable at runtime (API / MCP tool / CLI / slash) so the monitor's
        # own overhead can be shed without a restart. Off unless
        # LIONCLAW_MEM_SAMPLE_SECONDS / LIONCLAW_TRACEMALLOC are set at boot.
        from olano.admin.memory import monitor_from_env

        self._memory_monitor = monitor_from_env()
        # Last checkpoint history sweep report (see `run_checkpoint_gc`).
        self._checkpoint_gc_last: dict[str, Any] | None = None

        # Disk monitor: threshold alerts + automatic cleanup (config.yaml
        # `disk_monitor:` block; LIONCLAW_NO_DISK_MONITOR=1 kills the loop).
        from olano.runtime.disk_monitor import monitor_from_env as disk_monitor_from_env

        self._disk_monitor = disk_monitor_from_env()

    @staticmethod
    def _on_disk_roster() -> list[str]:
        """Every agent on disk, hidden ones included.

        The boot loop is the filter point for hidden agents
        (``_should_boot_on_start`` + ``LIONCLAW_BOOT_HIDDEN_AGENTS``), not
        discovery — so this is the one ``include_hidden`` opt-in the runtime
        makes, read at construction and again after first-boot provisioning
        installs the default agent (``_provision_on_first_boot``).
        """
        return list_agents(include_hidden=True)

    def _install_default_executor(self) -> None:
        """Widen the loop's default thread pool before anything can queue on it.

        Everything this process pushes off the loop lands in ONE shared pool:
        ``asyncio.to_thread`` (the API's config parses, vault KDFs, rmtree,
        60s ``npm install``, 30s IMAP/SMTP probes), langchain's sync tools
        (``BaseTool._arun`` → ``run_in_executor(None, …)``), and the channel
        adapters' blocking sends. Python sizes that pool at ``cpu_count + 4`` —
        6 slots on a 2-core box — so a handful of slow-but-legitimate calls can
        queue everything behind them, and a sync tool whose completion needs a
        second slot (notification_tools' ``_deliver_sync`` → adapter ``send``)
        can deadlock outright.

        Sized for blocking I/O, not CPU: these threads spend their time waiting
        on disk, sockets and subprocesses, so the count is unrelated to core
        count. ``LIONCLAW_EXECUTOR_WORKERS`` overrides; a value below 1 is
        ignored rather than crashing the boot.
        """
        workers = _DEFAULT_EXECUTOR_WORKERS
        raw = os.environ.get("LIONCLAW_EXECUTOR_WORKERS", "").strip()
        if raw:
            try:
                workers = max(1, int(raw))
            except ValueError:
                logger.warning("Ignoring non-numeric LIONCLAW_EXECUTOR_WORKERS=%r", raw)
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=workers, thread_name_prefix="olano")
        )
        logger.debug("Default executor: %d threads", workers)

    async def _provision_on_first_boot(self) -> None:
        """Seed the admin account + default agent when the environment asks for
        them and the home has none (``runtime/first_boot.py``).

        Off the loop: it hashes a password and scaffolds a workspace. Never
        fails the boot — a box that cannot provision must still come up so the
        reason is reachable through the dashboard log rather than a dead
        container. When it installs the default agent, the roster snapshot
        taken in ``__init__`` is refreshed so the boot loop below picks the
        newcomer up — that snapshot predating the install is exactly how a
        managed box used to come up without its AgentFather.
        """
        try:
            from olano.runtime.first_boot import first_boot_provisioning

            result = await asyncio.to_thread(first_boot_provisioning)
        except Exception:
            logger.exception("First-boot provisioning failed (continuing boot)")
            return
        await self._announce_setup_code()
        if result is None:
            return
        logger.info(
            "First-boot provisioning: %s",
            ", ".join(f"{label}={status}" for label, status in result.steps),
        )
        if not self._explicit_agents:
            try:
                self.agent_names = self._on_disk_roster()
            except Exception:
                logger.exception("agent roster refresh after provisioning failed")
                return
            logger.info("Runtime roster after provisioning — agents: %s", self.agent_names)

    async def _announce_setup_code(self) -> None:
        """Print the first-run setup code on an unmanaged box with no accounts.

        ``runtime/setup_code.py``: the create-account screen asks for it, so an
        operator who started the box without ``setup.sh`` needs to find it in
        the log (``docker compose logs olano``). Never fails the boot.
        """
        try:
            from olano.runtime.setup_code import announce_at_boot

            await asyncio.to_thread(announce_at_boot)
        except Exception:
            logger.exception("First-run setup code: announcement failed (continuing boot)")

    async def _retry_provisioning_after_settings(self) -> None:
        """Re-run a first-boot step the boot-time quarantine refused, now that
        the settings have arrived -- and bring the agent it installs online.

        Hooked to ``SettingsPoller(on_settled=...)``. ``first_boot_provisioning``
        already retries only steps recorded ``failed:`` and is a no-op
        otherwise, so this can fire on every settled transition for the life
        of the process at no cost. When the retry installs the default agent
        it is booted here explicitly: ``_provision_on_first_boot`` gets that
        for free by running before the boot loop, but this runs after it.
        """
        from olano.runtime import first_boot

        pending = first_boot.steps_to_retry(first_boot.load_state())
        if not pending:
            return
        logger.info(
            "Deployment settings settled — retrying first-boot step(s): %s", ", ".join(pending)
        )
        try:
            result = await asyncio.to_thread(first_boot.first_boot_provisioning)
        except Exception:
            logger.exception("First-boot retry after settings failed")
            return
        if result is None:
            return
        steps = dict(result.steps)
        logger.info(
            "First-boot retry: %s",
            ", ".join(f"{label}={status}" for label, status in steps.items()),
        )
        if steps.get("agent") != "installed":
            return
        name = result.agent
        if not self._explicit_agents:
            try:
                self.agent_names = self._on_disk_roster()
            except Exception:
                logger.exception("agent roster refresh after first-boot retry failed")
        try:
            await self._boot_agent(name)
        except Exception:
            logger.exception("booting '%s' after first-boot retry failed", name)
            return
        try:
            from olano.runtime.api import _push_event

            _push_event("agents_update", {"name": name})
        except Exception:
            logger.debug("agents_update broadcast after first-boot retry failed", exc_info=True)

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._install_default_executor()
        logger.info("Runtime starting — agents: %s", self.agent_names)
        self.webhook_dispatcher.start()

        # Install signal handlers FIRST so Ctrl+C works during boot too —
        # MCP server discovery, model warm-up, and the housekeeping pass
        # below can each take 10+ seconds, and the user shouldn't have to
        # wait for them to finish before they can interrupt.
        self._shutdown_event = asyncio.Event()
        self._install_signal_handlers()

        # Refuse to touch a home that is not on the sealed data volume this box
        # was provisioned with — a no-op without /etc/olano/seal.json, i.e. on
        # every existing deployment. MUST precede first-boot provisioning: that
        # is the step which would otherwise seed a fresh admin + agent onto an
        # empty mountpoint on the unencrypted disk. Exit non-zero so
        # `restart: unless-stopped` retries once the volume is mounted.
        # (Local import: later blocks in this method import the same name
        # locally, which makes it function-scoped — a module-level import would
        # be unbound here.)
        from olano.config.home import get_olano_home

        try:
            seal_guard.assert_home_on_sealed_volume(get_olano_home())
        except seal_guard.SealGuardError as exc:
            logger.critical("%s", exc)
            raise SystemExit(3) from exc

        # First-boot provisioning (admin account, default agent, vault) from
        # the environment's first-run credentials — BEFORE the vault unlock
        # (it may create the vault this process then opens) and, above all,
        # BEFORE the API binds: a runtime answering with zero users serves the
        # account-creation screen to whoever finds the URL first. See
        # olano/runtime/first_boot.py. No-op without LIONCLAW_ADMIN_PASSWORD.
        await self._provision_on_first_boot()

        # Unlock the credentials vault BEFORE booting agents so channel/tool
        # secrets written as ${NAME} resolve against the vault (per-agent
        # override → common) instead of silently falling back to a stale
        # os.environ value. MUST run before the _boot_agent loop below.
        self._unlock_vault_for_boot()

        # Migrate this deployment's MCP relay credential from the legacy shared
        # HMAC secret to an Ed25519 keypair, if it hasn't been already. Runs here
        # because it needs the unlocked vault to persist the private key, and
        # before agents boot so the first MCP OAuth of the session already uses
        # the new credential. A no-op on every boot after the first (and on any
        # box that has no relay, no managed-cloud channel, or already holds a
        # private key). Never raises — see the module docstring.
        self._enroll_mcp_relay_key()

        # Startup housekeeping
        if os.environ.get("LIONCLAW_NO_HOUSEKEEPING") != "1":
            try:
                from olano.config.home import get_olano_home
                from olano.runtime.housekeeping import run_housekeeping

                home = get_olano_home()
                hk_report = run_housekeeping(home)
                if hk_report.actions_taken:
                    logger.info(str(hk_report))
            except Exception as exc:
                logger.debug("Housekeeping skipped: %s", exc)

        # Boot-time auto-upgrade: when the running code changed since the last
        # boot (git pull + restart, package upgrade, new docker image) or
        # config migrations are pending, apply them, re-sync built-in skills,
        # and refresh every agent's system zone BEFORE agents boot — so
        # existing agents pick up a new release's upgrades automatically, no
        # matter how the code was updated. No-op on unchanged code. Opt out
        # with LIONCLAW_NO_AUTO_UPGRADE=1.
        if os.environ.get("LIONCLAW_NO_AUTO_UPGRADE") != "1":
            try:
                from olano.config.home import get_olano_home
                from olano.update.auto import run_startup_upgrades

                upgrade_report = await asyncio.to_thread(run_startup_upgrades, get_olano_home())
                if upgrade_report is not None:
                    logger.info("Startup auto-upgrade: %s", upgrade_report.summary())
            except Exception as exc:
                logger.warning("Startup auto-upgrade failed (continuing boot): %s", exc)

        # Wire the talk_to tool to this runtime so any agent that loads
        # the tool can reach its peers via _runners. The publisher pushes
        # SSE events to the dashboard's `/api/stream` so users can watch
        # the conversation live.
        try:
            from olano.runtime.api import _push_event as _api_push
            from olano.tools import talk_to as _tt

            _tt.bind_runtime(self, publisher=_api_push)
        except Exception as exc:
            logger.debug("talk_to bind skipped: %s", exc)

        # Wire the platform-control layer (/olano command + REST) to this
        # runtime so restart/shutdown can set _shutdown_event, and so a
        # pending "back online" notice from a pre-restart marker is sent.
        try:
            from olano import platform_control as _pc

            _pc.bind_runtime(self)
        except Exception as exc:
            logger.debug("platform_control bind skipped: %s", exc)

        # Start the REST API (FastAPI + uvicorn) BEFORE agents and channels.
        # Agent boot can stall for a long time on external services — an MCP
        # server downloading its package on first run, a stdio wrapper like
        # `mcp-remote` sitting in an interactive OAuth prompt, model warm-up —
        # and channel/platform startup can each make blocking network calls
        # (a Telegram webhook bootstrap, a Matrix sync, etc.). Binding the API
        # first guarantees /api/status, the dashboard, and login stay reachable
        # throughout boot (installers poll /api/status for readiness).
        await self._start_api()

        # The dashboard is reachable from here on, so its catalogs must be too.
        # Two stages, both off the loop and both BEFORE the serial agent boot,
        # which can run for minutes on a fleet with slow MCP servers:
        #   1. snapshots — read the disk-cached catalogs into memory. Tens of
        #      milliseconds, so it is awaited: every catalog answers correctly
        #      from the first request, even mid-boot.
        #   2. the full warm — rebuilds/probes anything the snapshots didn't
        #      cover. Runs concurrently with agent boot rather than after it.
        # This used to be one task created at the very end of start(), which
        # meant every catalog was cold for the whole boot — exactly the window
        # in which an operator opens the dashboard to watch agents come up.
        await self._warm_catalog_snapshots()
        self._background_tasks.append(
            asyncio.create_task(self._warm_catalog_caches(), name="warm_catalogs")
        )
        # The thread-list summaries (`runtime/thread_summaries`): what the first
        # list request after this restart would otherwise read on its own time.
        self._background_tasks.append(
            asyncio.create_task(self._warm_thread_summaries(), name="warm_thread_summaries")
        )

        # Tool-server containers the previous process left running are nobody's
        # now (see lionclaw_docker.reap_ephemeral_containers). Concurrent with
        # the agent boot rather than before it: removing a thousand containers
        # takes minutes on the box that needs it most, and the boot must not
        # wait on that — the bridge only has to be clear by the time a sandbox
        # asks for a port, and the reaper frees them faster than the boot
        # claims them.
        self._background_tasks.append(
            asyncio.create_task(self._reap_ephemeral_containers("boot"), name="reap_ephemeral")
        )

        # Its netfilter sibling: forget sandbox egress rules whose container is
        # gone. A runtime that was killed rather than stopped leaves its rules
        # behind, and they name an address docker hands to the next container
        # that asks — so a stale rule does not merely leak, it confines somebody
        # else's sandbox to a proxy that is not listening for it. A thread
        # rather than a task, unlike the reaper above: this one shells out to
        # docker AND iptables synchronously, so it would block the loop.
        def _sweep_sandbox_egress() -> None:
            try:
                from olano.security.egress_firewall import sweep_stale_from_docker

                sweep_stale_from_docker()
            except Exception:  # pragma: no cover - never block a boot
                logger.debug("sandbox egress sweep failed", exc_info=True)

        _thread_mod.Thread(target=_sweep_sandbox_egress, name="egress_sweep", daemon=True).start()

        # Historical cognition-credits backfill (one-time; resumes each boot
        # until its state file says done). Niced daemon thread, entirely off
        # the boot path — see analytics/backfill for the dedup/cutoff rules
        # that make overlap and crashes harmless.
        try:
            from olano.analytics.backfill import maybe_spawn_backfill

            maybe_spawn_backfill()
        except Exception:  # pragma: no cover - reconstruction never blocks boot
            logger.debug("credits backfill spawn failed", exc_info=True)

        # Deployment entitlements: resolve once (seeds the cloud cache) and log
        # the posture; compute which agents the max_agents cap allows to boot.
        # Off the loop: eligibility json5-parses every agent's config (hundreds
        # of ms each) while installers are already polling /api/status.
        deployment_boot_allowed = await asyncio.to_thread(self._resolve_deployment_boot_set)

        # Boot each agent, skipping any the user has explicitly paused (stays
        # offline across restarts) or any hidden/internal test agent (never
        # booted by the long-running runtime — see _should_boot_on_start).
        # Resolve the eligible set BEFORE booting any of it, so the runtime can
        # answer "how many agents are still to come?" while the loop runs. The
        # dashboard turns that into "6 of 15 ready" instead of an open-ended
        # spinner — a boot with a known end is a status, one without is an
        # outage, and they should not look the same.
        eligible: list[str] = []
        for name in self.agent_names:
            if not await asyncio.to_thread(self._should_boot_on_start, name):
                reason = (
                    "hidden/internal (test)"
                    if await asyncio.to_thread(self._agent_is_hidden, name)
                    else "paused"
                )
                logger.info("Skipping %s agent: %s", reason, name)
                continue
            if deployment_boot_allowed is not None and name not in deployment_boot_allowed:
                self._blocked_agents[name] = "blocked by deployment settings (max_agents cap)"
                logger.warning("Not booting agent %s: over the deployment max_agents cap", name)
                continue
            eligible.append(name)
        self._agents_expected = list(eligible)

        self._agents_booting = True
        try:
            for name in eligible:
                await self._boot_agent(name)
        finally:
            self._agents_booting = False
            # One line, always: a fleet whose startup is dominated by a single
            # agent should say so without a profiling flag having been set in
            # advance — that flag can only ever measure the *next* slow boot.
            _booted = [p for p in all_profiles() if p.agent in set(eligible)]
            if _summary := fleet_summary(_booted):
                logger.info("%s", _summary)
        self._flag_locked_vault()

        # Booting N agents churns a lot of short-lived scaffolding (config
        # parsing, tool + MCP schema construction, prompt assembly). Those pages
        # are free by now but glibc keeps them mapped, pinning RSS at the boot
        # peak for the life of the process. Hand them back once, here, at the
        # point of maximum garbage — worth tens to hundreds of MB on a
        # many-agent host, and the difference between fitting in RAM and
        # swapping. Best-effort: a no-op off glibc.
        from olano.admin.memory import release_free_heap

        _reclaimed = await asyncio.to_thread(release_free_heap)
        if _reclaimed:
            logger.info("Released %.1f MB of free heap back to the OS after boot.", _reclaimed)

        # Start scheduler
        self.scheduler.start()

        # Start channels (legacy BaseChannel path), each isolated + bounded so
        # one unreachable channel can't stall the whole boot.
        await self._start_channels()

        # Connect platforms (new BasePlatformAdapter path)
        for platform_adapter in self._platforms:
            try:
                connected = await platform_adapter.connect()
                if not connected:
                    logger.warning(
                        "Platform '%s' connect returned False — skipping",
                        platform_adapter.platform.value,
                    )
            except Exception as exc:
                logger.warning(
                    "Platform '%s' failed to connect: %s",
                    getattr(platform_adapter, "platform", "?"),
                    exc,
                )
        # Mark that the initial connect loop has run so that _boot_platform_adapter
        # on single-agent restarts knows it must call connect() itself.
        self._server_started = True

        # Channels + platforms are connected — release the held "back online"
        # notice from a pre-restart marker (platform_control holds delivery
        # until adapters can actually send).
        try:
            from olano import platform_control as _pc_ready

            _pc_ready.notify_runtime_ready()
        except Exception as exc:
            logger.debug("platform_control ready signal skipped: %s", exc)

        # Re-arm auth handshake links across the restart. Links that had already
        # started a flow are expired (the OAuth/device session died with the old
        # process, so nothing could ever complete them) and their chat is told
        # to ask for a new one; links not yet opened are still perfectly good and
        # get their watcher back. Must run after channels connect — the expiry
        # notice is delivered over one.
        try:
            from olano.magic_links.watch import resume_pending

            await resume_pending()
        except Exception as exc:  # never block boot on link bookkeeping
            logger.debug("handshake: resume skipped: %s", exc)

        # Start self-learning
        for svc in self._self_learning:
            svc.start()

        # Start PULSE delivery loop
        self._background_tasks.append(
            asyncio.create_task(self._pulse_delivery_loop(), name="pulse_delivery")
        )

        # Start the memory sampler loop (idle unless the monitor is enabled; it
        # is cancelled with the other background tasks in stop()).
        self._background_tasks.append(
            asyncio.create_task(self._memory_sampler_loop(), name="memory_sampler")
        )

        # Start the disk monitor loop (threshold alerts + automatic cleanup;
        # settings re-read each tick so config changes apply without restart).
        self._background_tasks.append(
            asyncio.create_task(self._disk_monitor_loop(), name="disk_monitor")
        )

        # Start the prompt refresh sweep: detects changes to the stable prompt
        # inputs (config prompts, customer-service extension, agent roster,
        # identity/policy files, tool surface) and hot-swaps a rebuilt graph so
        # edits apply without a restart; existing threads then get a one-shot
        # in-conversation update notice on their next turn. Settings re-read
        # each tick (config.yaml → prompt_refresh).
        self._background_tasks.append(
            asyncio.create_task(self._prompt_refresh_loop(), name="prompt_refresh")
        )

        # Start the MCP health sweep: renews OAuth grants before they lapse and
        # re-connects servers that dropped out, so an MCP connection does not
        # need re-authorizing by hand every time a token expires or a server
        # blips. Settings re-read each tick (config.yaml → mcp_health).
        self._background_tasks.append(
            asyncio.create_task(self._mcp_health_loop(), name="mcp_health")
        )

        # Start the checkpoint history sweep: LangGraph never deletes a
        # checkpoint, and a load needs only the tail back to each delta
        # channel's last snapshot. Settings re-read each pass
        # (config.yaml → checkpoint_gc). Postgres only.
        self._background_tasks.append(
            asyncio.create_task(self._checkpoint_gc_loop(), name="checkpoint_gc")
        )

        # Start the page supervisor: builds and runs every deployed page
        # (pages_deploy) and keeps its server healthy. It reconciles from the
        # registry file, so pages deployed before this boot come back on their
        # own. ``LIONCLAW_NO_PAGE_SERVICES=1`` skips it for one boot.
        if (os.environ.get("LIONCLAW_NO_PAGE_SERVICES") or "").strip() not in {"1", "true", "yes"}:
            try:
                from olano.pages.services import start_manager

                await start_manager()
            except Exception:
                logger.exception("page supervisor failed to start; deployed pages will not run")

        # Take ownership of gateway-key enrollment (managed installs only). From
        # here on `ensure_gateway_key()` is cache-only on the build path, so a
        # degraded control plane costs a background retry instead of a 10s stall
        # inside every agent build.
        self._background_tasks.append(
            asyncio.create_task(self._gateway_enrollment_loop(), name="gateway_enrollment")
        )

        # A self-hosted box with an Olano API key enrolls for the central MCP
        # sign-in relay in the background (core/mcp_relay_selfhost.py). Never on
        # the boot path: the cloud may be slow or down, and boot must not wait.
        # A no-op loop on managed boxes and on boxes without a key.
        self._background_tasks.append(
            asyncio.create_task(self._selfhost_relay_loop(), name="selfhost_relay_enrollment")
        )

        # The demo deployment deletes visitor accounts after a day, with their
        # conversations (runtime/demo_access.py). Returns at once elsewhere.
        self._background_tasks.append(
            asyncio.create_task(self._demo_visitor_sweep_loop(), name="demo_visitor_sweep")
        )

        # Tell the control plane what image this box is running. Boot is exactly
        # when that can have changed — an Update is a pull + recreate — and it
        # was previously reported by nothing at all, so the console showed a
        # placeholder that made a stale deploy look identical to a fresh one.
        self._background_tasks.append(
            asyncio.create_task(self._report_runtime_version(), name="runtime_version_report")
        )

        # Start the deployment-settings poller (managed-cloud installs only):
        # keeps the signed entitlements fresh and applies live transitions
        # (closing terminals, SSE broadcast) when the control plane changes them.
        try:
            from olano.deployment import sources as dep_sources

            manifest = dep_sources.read_manifest()
        except Exception:
            manifest = None
        if manifest and manifest.managed and manifest.mode == "cloud":
            from olano.deployment.poller import SettingsPoller
            from olano.runtime.api import _push_event as _dep_push

            self._settings_poller = SettingsPoller(
                push_event=_dep_push, on_settled=self._retry_provisioning_after_settings
            )
            self._settings_poller.start()

        # Start the cloud usage reporter whenever a deployment token is
        # configured (not gated on the managed manifest — any instance the
        # cloud dashboard tracks carries the token). Ships the usage ledger to
        # POST {cloud}/api/v1/usage so cloud.olano.ai → Usage fills in.
        try:
            from olano.deployment import usage_reporter as dep_usage_reporter

            if dep_usage_reporter.reporting_enabled():
                self._usage_reporter = dep_usage_reporter.UsageReporter()
                self._usage_reporter.start()
                logger.info("Cloud usage reporter started")
        except Exception:
            logger.exception("cloud usage reporter failed to start")

        # Report bootstrap-credential rotations (admin password change / vault
        # rekey) so the cloud console purges its temporary-password copy. The
        # first pass runs at start — a rotation that happened while the control
        # plane (or this process) was down still converges.
        try:
            from olano.deployment import credential_rotation as dep_rotation

            if dep_rotation.reporting_enabled():
                self._rotation_reporter = dep_rotation.RotationReporter()
                self._rotation_reporter.start()
                logger.info("Credential-rotation reporter started")
        except Exception:
            logger.exception("credential-rotation reporter failed to start")

        # Daily disk report: filesystem totals + directory-scan summary +
        # docker image usage, shipped as metrics.disk on POST /api/v1/health so
        # the cloud console can show where the bytes are. Same token gate as
        # the other reporters; LIONCLAW_DISK_REPORT=0 disables.
        try:
            from olano.deployment import disk_reporter as dep_disk_reporter

            if dep_disk_reporter.reporting_enabled():
                self._disk_reporter = dep_disk_reporter.DiskReporter()
                self._disk_reporter.start()
                logger.info("Cloud disk reporter started")
        except Exception:
            logger.exception("cloud disk reporter failed to start")

        # Reconcile the HOST-side managed-update agent with the control plane
        # (managed boxes only; docker socket required). Boot is exactly when
        # this code can have changed — an update is a pull + recreate — and the
        # host agent script cannot fix itself when it predates its own
        # self-refresh mechanism. One pass with bounded retries, then quiet;
        # LIONCLAW_HOST_AGENT_SYNC=0 disables. See olano/deployment/host_agent.py.
        try:
            from olano.deployment import host_agent as dep_host_agent

            if dep_host_agent.sync_enabled():
                self._host_agent_sync = dep_host_agent.HostAgentSync()
                self._host_agent_sync.start()
                logger.info("Host update-agent sync started")
        except Exception:
            logger.exception("host update-agent sync failed to start")

        # Start the cloud catalog sync: keeps the model catalog and MCP
        # directory fresh from cloud.olano.ai by polling the tiny per-catalog
        # revision manifest and re-fetching only what changed. Browsing always
        # works without it (disk cache → bundled snapshot), so it runs on every
        # install unless explicitly disabled.
        if not os.environ.get("LIONCLAW_NO_CATALOG_SYNC"):
            try:
                from olano.catalog.sync import CatalogSyncService
                from olano.runtime.api import _push_event as _catalog_push

                self._catalog_sync = CatalogSyncService(push_event=_catalog_push)
                self._catalog_sync.start()
            except Exception:
                logger.exception("catalog sync failed to start")

        # Home revision tracking: debounced background commits of allowlisted
        # config/markdown into <home>/revisions.git, authored per agent turn.
        # Settings-gated (revisions: enabled) and a no-op without git.
        try:
            from olano.revisions.worker import get_revisions_worker

            if get_revisions_worker(start=True) is not None:
                logger.info("Revision tracking started (<home>/revisions.git)")
        except Exception:
            logger.exception("revision tracking failed to start")

        # Push notifications to the mobile app, relayed by the Olano Cloud.
        # Settings-gated (push: enabled) and a no-op on a box with no cloud
        # channel, so a self-hosted install never starts it.
        try:
            from olano.push.dispatcher import get_push_dispatcher

            if get_push_dispatcher(start=True) is not None:
                logger.info("Push notifications enabled (relayed by the Olano Cloud)")
        except Exception:
            logger.exception("push dispatcher failed to start")

        # Start cognition drainer (runs queued run_worker pulse actions)
        self._background_tasks.append(
            asyncio.create_task(self._cognition_drainer_loop(), name="cognition_drainer")
        )

        logger.info(
            "Runtime ready — %d agents, %d channels", len(self._runners), len(self._channels)
        )
        # Now that the dashboard / channels are up, swap the boot-phase
        # info line for an actionable hint.  The signal handlers installed
        # at the top of ``start()`` are still active.
        try:
            from rich.console import Console

            Console().print(
                "[dim]Runtime ready. Press Ctrl+C to stop "
                "(again to force-quit if shutdown stalls).[/dim]"
            )
        except Exception:
            pass

        try:
            await self._shutdown_event.wait()
        finally:
            await self.stop()
            # Now that ``stop()`` has finished cleaning up, drop the
            # signal handlers so a third Ctrl+C during ``asyncio.run``'s
            # own teardown isn't intercepted.
            self._remove_signal_handlers()
            # Chat-triggered restart on unsupervised deployments: re-exec
            # in place now that teardown is done (never returns on success).
            if self._post_stop_action is not None:
                action, self._post_stop_action = self._post_stop_action, None
                try:
                    action()
                except Exception:
                    logger.exception("post-stop action failed")

    def _install_signal_handlers(self) -> None:
        """Install SIGINT/SIGTERM handlers on the running loop.

        First signal: graceful shutdown via ``stop()``.
        Second signal: cancel ``stop()`` and let asyncio.run unwind hard.
        Third signal: ``os._exit(130)`` — last-resort escape if a C-level
        thread (some bot SDKs spawn them) is ignoring cancellation.
        """
        loop = asyncio.get_running_loop()
        self._signal_loop = loop
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._on_signal)
            except NotImplementedError:
                # add_signal_handler is unsupported on Windows; the default
                # handler will raise KeyboardInterrupt which still works
                # for graceful exits, just not the escalation path.
                pass

    def _remove_signal_handlers(self) -> None:
        loop = self._signal_loop or asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError):
                pass

    def _on_signal(self) -> None:
        self._signals_seen += 1
        if self._signals_seen == 1:
            logger.info("Shutdown requested — stopping runtime gracefully (Ctrl+C again to force).")
            if self._shutdown_event and not self._shutdown_event.is_set():
                self._shutdown_event.set()
            return
        if self._signals_seen == 2:
            logger.warning(
                "Second interrupt — cancelling all tasks. One more Ctrl+C will hard-exit."
            )
            for task in asyncio.all_tasks(loop=self._signal_loop):
                if task is not asyncio.current_task():
                    task.cancel()
            return
        # Third+: get out at any cost.  os._exit avoids running atexit
        # handlers but guarantees we leave even if a C extension thread
        # is holding the GIL.
        logger.error("Forced exit (signal #%d) — terminating now.", self._signals_seen)
        os._exit(130)  # 128 + SIGINT(2) — matches shell convention

    def _request_shutdown(self) -> None:
        """Backward-compat alias used by external callers / tests."""
        self._on_signal()

    async def _start_channels(self) -> None:
        """Start each legacy BaseChannel, isolated and time-bounded.

        A channel's ``start()`` may block on a remote call (e.g. Telegram's
        webhook bootstrap). Each is wrapped in a timeout + try/except so a
        single slow or unreachable channel is skipped with a warning instead
        of hanging the whole runtime — the REST API is already up by now, so
        the dashboard and login stay reachable regardless.
        """
        for channel in self._channels:
            label = type(channel).__name__
            try:
                await asyncio.wait_for(channel.start(), timeout=_CHANNEL_START_TIMEOUT)
            except TimeoutError:
                logger.warning(
                    "Channel '%s' did not start within %.0fs — skipping (boot continues).",
                    label,
                    _CHANNEL_START_TIMEOUT,
                )
            except Exception as exc:
                logger.warning("Channel '%s' failed to start: %s", label, exc)

    async def _start_channel_on_restart(self, channel: Any, ch_name: str, agent_name: str) -> None:
        """Start a single channel attached during a single-agent restart.

        Bounded + isolated like ``_start_channels`` so a slow or unreachable
        channel can't hang the restart. Only used after the initial boot's
        one-time ``_start_channels()`` pass (see the ``_server_started`` guard
        at the call site) — mirrors ``_boot_platform_adapter``'s restart path.
        """
        try:
            await asyncio.wait_for(channel.start(), timeout=_CHANNEL_START_TIMEOUT)
        except TimeoutError:
            logger.warning(
                "Channel '%s' for agent '%s' did not start within %.0fs after restart "
                "— messages will not be delivered until the agent is restarted again.",
                ch_name,
                agent_name,
                _CHANNEL_START_TIMEOUT,
            )
        except Exception as exc:
            logger.warning(
                "Channel '%s' for agent '%s' failed to start after restart: %s",
                ch_name,
                agent_name,
                exc,
            )

    def _is_attended(self) -> bool:
        """True when started in an interactive terminal (foreground ``serve``);
        False for the detached daemon (``server start``), which has no TTY.

        Gates two behaviours: an interactive vault passphrase prompt and the
        console confirmation line — both only make sense when a human is watching.
        """
        try:
            return bool(sys.stdin and sys.stdin.isatty())
        except Exception:
            return False

    def _boot_console_notice(self, markup: str) -> None:
        """Print a one-line boot status to the console. Best-effort: console
        rendering must never break the boot."""
        try:
            from rich.console import Console

            Console().print(markup)
        except Exception:
            pass

    def _enroll_mcp_relay_key(self) -> None:
        """Self-migrate the MCP relay credential to Ed25519 (best-effort, once).

        Kept as a thin wrapper so boot never depends on it: the enroller already
        swallows its own failures, and this catches anything it somehow doesn't.
        See :mod:`olano.core.mcp_relay_enroll` for why this lives on the boot
        path: a managed box must be able to re-key by pressing Update in the
        cloud console, with no SSH.
        """
        try:
            from olano.core.mcp_relay_enroll import maybe_enroll_relay_key

            maybe_enroll_relay_key()
        except Exception as exc:  # pragma: no cover - boot must not depend on this
            logger.debug("MCP relay key enrollment skipped: %s", exc)

    def _unlock_vault_for_boot(self) -> None:
        """Unlock the credentials vault before any agent/channel boots.

        Channel and tool secrets written as ``${NAME}`` resolve vault → os.environ
        (see ``olano.vault.resolver``). If the vault is still locked when an
        agent's channel boots, a token like ``${TELEGRAM_BOT_TOKEN}`` falls back to
        whatever the launching shell exported — typically a stale value — so the
        bot polls the wrong account and looks dead behind a green "Live" badge.

        Historically ``set_vault()`` was only ever called by the dashboard's HTTP
        unlock endpoint, which runs *after* boot. Unlocking here closes that race
        for every entry point that boots a ``RuntimeServer``.

        Resolution: ``OLANO_VAULT_KEY`` → keyring → (attended only) an
        interactive passphrase prompt. Outcomes:

        - **unlocked** → log + (attended) a console confirmation line.
        - **locked, vault exists** → an actionable warning; the boot continues so
          the in-daemon dashboard can still unlock it (``${NAME}`` falls back to
          ``os.environ`` meanwhile). If ``LIONCLAW_REQUIRE_VAULT=1`` (opt-in,
          headless production), refuse to start instead — ``sys.exit(1)``.
        - **no vault DB** → fresh install, nothing to unlock; stay silent.
        """
        from olano.vault.resolver import get_vault
        from olano.vault.runtime import unlock_vault_for_session

        if get_vault() is not None:
            return  # already unlocked in this process (e.g. dashboard got there first)

        attended = self._is_attended()
        try:
            # Attended foreground `serve` may prompt for the passphrase; the
            # detached daemon (no TTY) never blocks on input.
            result = unlock_vault_for_session(allow_prompt=attended)
        except Exception as exc:  # a vault hiccup must never abort the boot
            logger.warning("Vault unlock at boot raised (continuing locked): %s", exc)
            return

        if result.vault is not None:
            logger.info(
                "Vault unlocked at boot (mode=%s) — ${NAME} secrets resolve from the vault.",
                result.mode,
            )
            if attended:
                self._boot_console_notice(
                    f"[green]✓ Vault unlocked[/green] [dim](mode={result.mode})[/dim]"
                )
            return

        if not result.db_existed:
            return  # fresh install — nothing to unlock; stay silent

        detail = f": {result.error}" if result.error else ""
        warning = (
            f"Vault is LOCKED at boot (mode={result.mode}{detail}). Channel/tool secrets "
            "written as ${NAME} fall back to os.environ until the vault is unlocked, so a "
            "bot token referenced that way may resolve to a stale value. Set "
            "OLANO_VAULT_KEY (or re-sync the keyring) and restart, or unlock via the "
            "dashboard."
        )

        # Opt-in strict mode: refuse to run half-broken (headless production where
        # an orchestrator should restart/alert). OFF by default so a locked vault
        # never blocks access to the in-daemon dashboard that unlocks it.
        if os.environ.get("LIONCLAW_REQUIRE_VAULT") == "1":
            logger.critical(
                "Vault is LOCKED at boot and LIONCLAW_REQUIRE_VAULT=1 — refusing to start "
                "(mode=%s%s).",
                result.mode,
                detail,
            )
            self._boot_console_notice(
                "[red]✗ Vault is locked and LIONCLAW_REQUIRE_VAULT=1 — refusing to start.[/red]"
            )
            sys.exit(1)

        logger.warning(warning)
        if attended:
            self._boot_console_notice(f"[yellow]⚠ {warning}[/yellow]")

    def _flag_locked_vault(self) -> None:
        """Alert when boot finished with agents whose secrets never resolved.

        This is the difference between "my bot is broken" and "the vault is
        locked": the agents are up, they just can't reach their credentials, and
        one unlock fixes all of them at once.
        """
        stuck = sorted(self._agents_with_unresolved_secrets)
        if not stuck:
            self._notify("resolve", "vault_locked")
            return
        try:
            from olano.vault.resolver import get_vault

            locked = get_vault() is None
        except Exception:
            locked = False
        if not locked:
            return  # unlocked but still unresolved — the per-connector alert says why
        self._notify(
            "notify",
            kind="vault_locked",
            level="warn",
            title="Vault is locked",
            body=(
                f"{len(stuck)} agent(s) booted without their credentials: "
                f"{', '.join(stuck)}. Unlock the vault and they reconnect "
                "automatically — no restart needed."
            ),
            dedupe_key="vault_locked",
            action={"label": "Unlock the vault", "href": "/vault"},
        )

    @staticmethod
    def _notify(op: str, *args: Any, **kwargs: Any) -> None:
        """Raise or clear an operator notification without ever raising.

        The notification store is a convenience surface — a failure there must
        not take down a boot, a tick, or an unlock.
        """
        try:
            from olano.runtime import notifications

            getattr(notifications, "notify" if op == "notify" else "resolve")(*args, **kwargs)
        except Exception:  # pragma: no cover - notifications are best-effort
            logger.debug("Notification %s failed", op, exc_info=True)

    async def reboot_unresolved_secret_agents(self) -> list[str]:
        """Re-boot agents whose channel/tool secrets were unresolved at boot.

        ``set_vault()`` makes the vault available but does not touch already-running
        channels — a channel that booted with a literal ``${NAME}`` (vault locked)
        keeps the stale value until it is rebuilt. Calling this right after a
        mid-session unlock (the dashboard's ``POST /api/vault/unlock``) re-resolves
        those agents automatically, so users no longer have to restart them by hand.

        Returns the names actually rebooted.
        """
        pending = sorted(self._agents_with_unresolved_secrets)
        rebooted: list[str] = []
        if pending:
            # Whatever the outcome, the "vault is locked" alert is stale the
            # moment we get here — this only runs after a successful unlock.
            self._notify("resolve", "vault_locked")
        for name in pending:
            # Clear first; a fresh _boot_agent re-adds the agent only if its
            # secrets are STILL unresolved, so this self-corrects.
            self._agents_with_unresolved_secrets.discard(name)
            if name not in self._runners:
                continue  # not running → it'll resolve cleanly on its next boot
            try:
                logger.info(
                    "Vault unlocked — rebooting agent '%s' to pick up resolved secrets.",
                    name,
                )
                await self._stop_agent(name)
                await self._boot_agent(name)
                rebooted.append(name)
            except Exception as exc:
                logger.error("Vault-unlock reboot of agent '%s' failed: %s", name, exc)
        return rebooted

    async def _start_api(self) -> None:
        try:
            import uvicorn

            from olano.runtime.api import create_api, install_log_capture
        except ImportError:
            logger.debug("FastAPI/uvicorn not available — dashboard API disabled")
            return

        home_config = load_home_config()
        port = int(os.environ.get("LIONCLAW_RUNTIME_PORT", str(home_config.runtime_api_port)))

        # Pre-bind probe with backoff. The kernel can hold the port in
        # TIME_WAIT for up to ~60s after a SIGKILL of the previous daemon —
        # without this loop, the API silently fails to start and the daemon
        # runs port-less (visible only as a swallowed traceback in the log
        # and an HTTP 'connection refused' for downstream tools like the
        # dashboard, which then naively boots a competing RuntimeServer on
        # the same port and produces a cascade of Telegram 409 conflicts,
        # WhatsApp session-lock errors, etc.).
        if not await self._wait_for_port_free(port, timeout=20.0):
            logger.error(
                "Runtime API could NOT start: port %d is still in use after 20s. "
                "Another process is bound there. Check `lsof -nP -iTCP:%d -sTCP:LISTEN` "
                "and either stop it or set LIONCLAW_RUNTIME_PORT to a free port.",
                port,
                port,
            )
            return

        api_app = create_api(self)
        install_log_capture()
        config = uvicorn.Config(
            api_app,
            host="0.0.0.0",
            port=port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        # Suppress uvicorn installing its own signal handlers — we drive
        # shutdown from RuntimeServer so the whole stack tears down in order.
        server.install_signal_handlers = lambda: None  # type: ignore[attr-defined,method-assign]
        self._uvicorn_server = server

        async def _serve_with_visible_errors() -> None:
            try:
                await server.serve()
            except asyncio.CancelledError:
                raise
            except SystemExit as exc:
                # uvicorn calls sys.exit(1) on bind failure — surface it as a
                # olano ERROR so it's actionable instead of buried in stderr.
                logger.error(
                    "Runtime API uvicorn exited with code %s — port %d may be unavailable.",
                    exc.code,
                    port,
                )
            except Exception:
                logger.exception("Runtime API task crashed (port %d)", port)

        self._background_tasks.append(
            asyncio.create_task(_serve_with_visible_errors(), name="runtime_api")
        )
        logger.info("Runtime API started on port %d", port)

    @staticmethod
    async def _wait_for_port_free(port: int, timeout: float = 20.0) -> bool:
        """Return ``True`` once *port* is bindable on 0.0.0.0, retrying with
        exponential backoff up to *timeout* seconds.

        The probe opens a real socket with ``SO_REUSEADDR`` and immediately
        closes it on success — a small TOCTOU window remains before uvicorn
        binds for real, but in practice it's negligible and far better than
        silently failing.
        """
        import socket

        deadline = asyncio.get_event_loop().time() + timeout
        delay = 0.25
        while True:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("0.0.0.0", port))
            except OSError:
                sock.close()
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    return False
                logger.info(
                    "Runtime API port %d busy — retrying in %.1fs (%.1fs left)",
                    port,
                    min(delay, remaining),
                    remaining,
                )
                await asyncio.sleep(min(delay, remaining))
                delay = min(delay * 2, 4.0)
                continue
            sock.close()
            return True

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        logger.info("Runtime shutting down...")

        # Tell uvicorn to wind down early so in-flight requests can drain
        # while we tear down the rest of the stack.
        if self._uvicorn_server is not None:
            try:
                self._uvicorn_server.should_exit = True
            except Exception:
                pass

        for svc in self._self_learning:
            try:
                svc.stop()
            except Exception as exc:
                logger.debug("Self-learning stop error: %s", exc)

        # Close open browser sessions so their sign-ins are saved and any live
        # browser containers are removed with the process that owned them.
        try:
            from olano.browser import loop as browser_loop
            from olano.browser import sessions as browser_sessions

            if browser_sessions.all_sessions():
                await browser_loop.run_async(browser_sessions.close_all(), timeout_s=30)
        except Exception as exc:
            logger.debug("Browser session shutdown error: %s", exc)

        if getattr(self, "_settings_poller", None) is not None:
            try:
                self._settings_poller.stop()
            except Exception as exc:
                logger.debug("Settings poller stop error: %s", exc)

        if getattr(self, "_usage_reporter", None) is not None:
            try:
                self._usage_reporter.stop()
            except Exception as exc:
                logger.debug("Usage reporter stop error: %s", exc)

        if getattr(self, "_rotation_reporter", None) is not None:
            try:
                self._rotation_reporter.stop()
            except Exception as exc:
                logger.debug("Rotation reporter stop error: %s", exc)

        if getattr(self, "_disk_reporter", None) is not None:
            try:
                self._disk_reporter.stop()
            except Exception as exc:
                logger.debug("Disk reporter stop error: %s", exc)

        if getattr(self, "_host_agent_sync", None) is not None:
            try:
                self._host_agent_sync.stop()
            except Exception as exc:
                logger.debug("Host agent sync stop error: %s", exc)

        if getattr(self, "_catalog_sync", None) is not None:
            try:
                self._catalog_sync.stop()
            except Exception as exc:
                logger.debug("Catalog sync stop error: %s", exc)

        try:
            from olano.revisions.worker import get_revisions_worker

            worker = get_revisions_worker(start=False)
            if worker is not None:
                worker.stop(flush=True)  # pending debounced changes commit now
        except Exception as exc:
            logger.debug("Revisions worker stop error: %s", exc)

        try:
            from olano.push.dispatcher import set_push_dispatcher

            set_push_dispatcher(None)
        except Exception as exc:
            logger.debug("Push dispatcher stop error: %s", exc)

        try:
            self.webhook_dispatcher.stop()
        except Exception as exc:
            logger.debug("Webhook dispatcher stop error: %s", exc)

        # Flush queued Langfuse spans (5 s cap). A no-op when nothing was
        # traced: no client exists and the SDK was never imported.
        try:
            from olano.observability.tracing import shutdown_all

            await asyncio.to_thread(shutdown_all, 5.0)
        except Exception as exc:
            logger.debug("Langfuse shutdown error: %s", exc)

        # Stop channels and platforms in PARALLEL with a single 5s budget —
        # the previous serial loop with per-adapter timeouts could compound
        # to minutes if several adapters were misbehaving (Telegram polling,
        # Discord gateway, etc).  Running them concurrently caps the total
        # teardown wait at the slowest adapter, not the sum.
        teardown: list[asyncio.Task[Any]] = []
        for channel in self._channels:
            teardown.append(
                asyncio.create_task(
                    self._safe_call(
                        channel.stop(), label=f"channel:{getattr(channel, 'name', '?')}"
                    )
                )
            )
        for platform_adapter in self._platforms:
            teardown.append(
                asyncio.create_task(
                    self._safe_call(
                        platform_adapter.disconnect(),
                        label=f"platform:{getattr(platform_adapter, 'platform', '?')}",
                    )
                )
            )
        if teardown:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*teardown, return_exceptions=True),
                    timeout=5,
                )
            except TimeoutError:
                logger.warning(
                    "Channel/platform teardown exceeded 5s — cancelling remaining %d task(s)",
                    sum(1 for t in teardown if not t.done()),
                )
                for task in teardown:
                    if not task.done():
                        task.cancel()

        try:
            self.scheduler.stop()
        except Exception as exc:
            logger.debug("Scheduler stop error: %s", exc)

        # Stop every page server with its keeper (SIGTERM, then SIGKILL after
        # a grace period) so nothing a page started outlives the runtime.
        try:
            from olano.pages.services import stop_manager

            await asyncio.wait_for(stop_manager(), timeout=40)
        except Exception as exc:
            logger.debug("Page supervisor stop error: %s", exc)

        for task in self._background_tasks:
            if not task.done():
                task.cancel()
        if self._background_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._background_tasks, return_exceptions=True),
                    timeout=3,
                )
            except TimeoutError:
                logger.debug("Background tasks did not cancel within 3s — abandoning")
        self._background_tasks.clear()
        self._acp_sites.clear()

        # Release persistence pools/connections (Postgres connection pool, cached
        # SQLite connections) now that all writers are stopped but the loop is
        # still healthy — an async Postgres pool must close on the loop it opened
        # on. Idempotent and a no-op for the SQLite default beyond closing conns.
        from olano.core.checkpoints import aclose_checkpointers
        from olano.core.stores import aclose_stores

        if self.mcp_server is not None:
            await self._safe_call(self.mcp_server.stop(), label="mcp_server")
        if self.a2a_server is not None:
            await self._safe_call(self.a2a_server.stop(), label="a2a_server")
        await self._safe_call(aclose_checkpointers(), label="checkpointers")
        await self._safe_call(aclose_stores(), label="stores")

        # Cancel anything else still pending on the loop (PTB internal tasks,
        # stray httpx connections, etc.) so asyncio.run() can exit promptly
        # instead of stalling on orphan coroutines.
        current = asyncio.current_task()
        leftover = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        for task in leftover:
            task.cancel()
        if leftover:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*leftover, return_exceptions=True),
                    timeout=2,
                )
            except TimeoutError:
                logger.debug(
                    "%d task(s) still pending after cancel — abandoning",
                    sum(1 for t in leftover if not t.done()),
                )

        logger.info("Runtime stopped")

    async def _safe_call(self, coro, *, label: str) -> None:
        """Run a teardown coroutine, swallowing exceptions and logging them.

        Used by ``stop()`` so a misbehaving adapter can't take down the
        rest of the shutdown sequence.  Returns when the coroutine
        completes or raises — never raises itself.
        """
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("%s teardown error: %s", label, exc)

    def _agent_is_hidden(self, name: str) -> bool:
        """Whether ``name``'s on-disk config opts into ``hidden: true``.

        Fails open (returns False) on an unreadable/broken config so the agent
        still reaches ``_boot_agent``, which surfaces the real error instead of
        being silently skipped here.
        """
        try:
            return load_agent_config(name).hidden is True
        except Exception:
            return False

    def _should_boot_on_start(self, name: str) -> bool:
        """Whether an agent should boot during ``start()``.

        - Explicitly named agents (``--agent X``) always boot — the user asked
          for that agent by name, overriding both guards below.
        - Hidden/internal agents (``hidden: true``, e.g. the per-provider test
          agents) are **not** booted by the long-running runtime: they exist for
          pytest and one-shot ``olano --agent <name> -n "..."`` runs (which
          build a single agent directly, bypassing this loop), so booting them in the daemon
          would burn resources and keep them reachable for no reason. Opt a dev
          runtime back in with ``LIONCLAW_BOOT_HIDDEN_AGENTS=1``.
        - Agents the user explicitly paused stay offline across restarts.
        """
        if self._explicit_agents:
            return True
        if not _boot_hidden_agents_enabled() and self._agent_is_hidden(name):
            return False
        return not is_agent_paused(name)

    def _resolve_deployment_boot_set(self) -> set[str] | None:
        """Which agents the deployment settings allow to boot, or ``None`` for
        no cap (unmanaged installs, or no ``max_agents`` limit).

        Deterministic over-cap policy (deployment-settings design record, §5): the
        home default agent always boots first, then lexicographic order until
        **N total**. The remainder land in ``self._blocked_agents`` with a
        reason, so the dashboard can show *why* an agent is offline instead of
        silently dropping it. In quarantine ``max_agents`` is 1, so exactly
        the default agent comes up (the recovery surface).
        """
        self._blocked_agents.clear()
        try:
            from olano.deployment import resolver as dep_resolver

            status = dep_resolver.deployment_status()
        except Exception:
            logger.exception("deployment settings resolution failed at boot")
            return None
        if status.posture == "open":
            # Say so explicitly. An unrestricted posture on a box that was meant
            # to be managed is the single most consequential misconfiguration
            # here, so it must never be inferred from silence in the log.
            logger.info(
                "Deployment posture: open (unmanaged install — no entitlement "
                "limits, agents may use the host shell). This is expected for "
                "self-hosted installs only."
            )
            return None
        logger.info(
            "Deployment posture: %s (source=%s plan=%s revision=%s%s)",
            status.posture,
            status.source,
            status.plan or "-",
            status.policy_revision if status.policy_revision is not None else "-",
            f" error={status.error}" if status.error else "",
        )

        eligible = [n for n in self.agent_names if self._should_boot_on_start(n)]
        if not status.settings.feature_enabled("father_agent") and "agentfather" in eligible:
            eligible.remove("agentfather")
            self._blocked_agents["agentfather"] = (
                "blocked by deployment settings (FatherAgent disabled)"
            )

        max_agents = status.settings.resources.max_agents
        if max_agents is None:
            return set(eligible)

        try:
            default_agent = load_home_config().default_agent
        except Exception:
            default_agent = None
        ordered = sorted(eligible)
        if default_agent in ordered:
            ordered.remove(default_agent)
            ordered.insert(0, default_agent)
        return set(ordered[:max_agents])

    def _boot_lock(self, name: str) -> asyncio.Lock:
        """The per-agent lifecycle lock (created on first use)."""
        lock = self._boot_locks.get(name)
        if lock is None:
            lock = asyncio.Lock()
            self._boot_locks[name] = lock
        return lock

    def agent_boot_in_flight(self, name: str) -> bool:
        """True while a boot (or stop) for `name` holds the lifecycle lock.

        Lets the API answer a duplicate start request immediately with
        "already starting" instead of parking it behind a multi-minute boot.
        """
        lock = self._boot_locks.get(name)
        return bool(lock and lock.locked())

    async def _boot_agent(self, name: str) -> None:
        """Boot one agent, at most once at a time.

        The lock makes concurrent callers queue instead of racing; whoever gets
        in second finds the agent already in `_runners` and returns without
        building a second copy of it.
        """
        async with self._boot_lock(name):
            if name in self._runners:
                logger.info("Agent '%s' is already running — skipping duplicate boot.", name)
                return
            # Profile the whole boot. Collection is unconditional (a handful of
            # perf_counter calls); LIONCLAW_BOOT_PROFILE=1 additionally logs the
            # per-phase tree. See olano/core/boot_profile.py.
            with profile_boot(name):
                await self._boot_agent_locked(name)
        # A booted peer changes every other running agent's roster section.
        # Nudge the prompt refresh sweep so their prompts pick the newcomer up
        # (no-op during the initial boot storm — the sweep's first periodic
        # tick reconciles the whole roster once, after every agent is up).
        if not self._agents_booting:
            self.request_prompt_refresh(None)

    async def _boot_agent_locked(self, name: str) -> None:
        # Snapshot the lists so we can detect what THIS boot added and track
        # them for per-agent teardown. We can't tag adapters retroactively
        # because they're handed off to subgraphs; tracking by index-delta
        # is the least-invasive option.
        prior_channels = list(self._channels)
        prior_platforms = list(self._platforms)
        prior_self_learning = list(self._self_learning)
        prior_acp = list(self._acp_servers)
        prior_bg = list(self._background_tasks)
        try:
            with boot_phase("config"):
                config = await asyncio.to_thread(load_agent_config, name)
                workspace = get_workspace(config.workspace)

            # deepagents warns about a spec-violating skill name once per skill
            # per boot and keeps loading it, so the only symptom is a log line
            # nobody reads. Fix every illegal name a legal folder can absorb
            # first (reloading the config if a disabled_skills entry moved),
            # then put what is left on the bell with the one-click repair
            # attached, off the loop, since both re-parse every SKILL.md.
            if await asyncio.to_thread(_align_skill_names_at_boot, name, config, workspace):
                config = await asyncio.to_thread(load_agent_config, name)
            await asyncio.to_thread(_report_skill_spec_violations, name, config, workspace)

            # Snapshot the stable prompt sections from the same inputs this
            # build is about to read (cheap: file reads + peer registry).
            # Taken BEFORE the build so anything that moves mid-build — a
            # config save, a peer landing in the registry — reads as a
            # mismatch on the next sweep tick and triggers one corrective
            # hot-swap, instead of being silently absorbed into a snapshot
            # the compiled graph never actually saw.
            prompt_state = await asyncio.to_thread(compute_prompt_state, config, workspace)

            # Register in registry with starting status
            entry = AgentEntry(
                name=name,
                description=config.description,
                emoji=config.emoji,
                model=config.model,
                capabilities=config.tools,
                channels=[(c.type if hasattr(c, "type") else c) for c in config.channels],
                trust_level=config.security.trust_level,
                status="starting",
            )
            self.registry.register(entry)
            # Broadcast the "starting" transition too, so a dashboard open
            # across a restart shows each agent moving configured → starting →
            # running as the boot loop walks the roster, instead of a static
            # list that only corrects itself on a manual reload.
            self._set_agent_status(name, "starting")

            # Build agent graph. Bracket it with an RSS sample so the memory
            # report can attribute an approximate per-agent cost (the compiled
            # graph: model client + tools + MCP schemas + subagents + middleware).
            # Noisy (shared allocator, any concurrent boot) and excludes the
            # embedding model / Chroma index (lazy on first RAG use) — directional,
            # not exact.
            from olano.admin.memory import current_rss_mb

            _rss_before = current_rss_mb()
            with boot_phase("build"):
                agent = await build_agent_async(config, include_mcp=bool(config.mcp_servers))

            # The build just decided, per server, whether it has a usable grant.
            # Ring here rather than waiting for the health sweep: the sweep is
            # 90s away at best and switched off entirely on some boxes, and an
            # agent that boots without its MCP tools is already answering
            # requests wrongly. An agent with no servers still reports (an
            # empty list), which clears alerts for servers removed since.
            from olano.core.builder import get_mcp_summary

            _report_mcp_auth_required(name, get_mcp_summary(name) if config.mcp_servers else [])

            runner = AgentRunner(agent, config)
            runner._prompt_state = prompt_state
            self._runners[name] = runner
            _rss_after = current_rss_mb()
            if _rss_before is not None and _rss_after is not None:
                runner._boot_rss_mb = round(max(0.0, _rss_after - _rss_before), 1)

            # Memory
            with boot_phase("memory"):
                md_mem = MarkdownMemory(workspace.root)
                rag_mem: RAGMemory | None = None
                if config.memory.rag:
                    rag_mem = RAGMemory(
                        persist_dir=workspace.root / "rag",
                        collection_name=config.memory.rag_collection,
                    )

            # Self-learning — the always-on agentic distillation loop. Gated by the
            # self_learning feature AND the master autonomy switch (manual-only mode
            # leaves cognition to the dashboard "Think" button). Its cadence follows
            # the effective intensity preset.
            from olano.cognition.orchestrator import distill_interval_seconds
            from olano.config.system_settings import (
                autonomy_enabled,
                effective_intensity,
                feature_enabled,
            )

            if (
                config.memory.self_learning
                # Per-customer agents must never run shared self-learning: it
                # distills every thread's session into one MEMORY.md, which
                # would leak one customer's data into another's context.
                and not config.memory.per_customer
                and feature_enabled("self_learning", config)
                and autonomy_enabled(config)
            ):
                interval = distill_interval_seconds(effective_intensity(config))
                svc = SelfLearningService(
                    agent_config=config,
                    workspace_root=workspace.root,
                    markdown_memory=md_mem,
                    rag_memory=rag_mem,
                    interval_seconds=interval,
                )
                self._self_learning.append(svc)

            # Scheduler
            with boot_phase("scheduler"):
                self.scheduler.register_agent(config, runner, workspace_root=workspace.root)

            # Outbound webhooks — register the agent's hooks so subscribed
            # events start firing immediately.
            with boot_phase("webhooks"):
                self.webhook_dispatcher.register_agent(name, list(config.webhooks))

            # Channels — customer-facing entry points. A hidden/internal agent
            # (config.hidden, e.g. a provider test agent) must never be reachable
            # by a customer, so skip binding any of its channels/platforms. The
            # agent still boots and answers direct CLI/API/test-harness calls by
            # name, so internal testing is unaffected.
            channel_entries = list(config.channels)
            if config.hidden and channel_entries:
                logger.info(
                    "Agent '%s' is hidden (internal/test) — not starting %d customer-facing "
                    "channel(s).",
                    name,
                    len(channel_entries),
                )
                channel_entries = []

            # A fresh boot re-evaluates this agent's secret resolution from
            # scratch; the _flag_unresolved_secrets check below re-adds it only
            # if a placeholder is STILL unresolved (drives the auto-reboot in
            # reboot_unresolved_secret_agents once the vault is unlocked).
            self._agents_with_unresolved_secrets.discard(name)
            self._skipped_channels.pop(name, None)

            # Timed by hand rather than with a `with` block: the loop body is a
            # few hundred lines and re-indenting it to profile it would bury the
            # measurement in an unreviewable diff.
            _channels_t0 = time.perf_counter()
            for ch_entry in channel_entries:
                from olano.config.agent_config import ChannelConfig
                from olano.config.channel_secrets import hydrate_channel_credentials
                from olano.vault.resolver import resolve_config_dict

                if isinstance(ch_entry, ChannelConfig):
                    ch_name = ch_entry.type
                    ch_cfg = {k: v for k, v in ch_entry.to_dict().items() if k != "type"}
                else:
                    ch_name = ch_entry
                    ch_cfg = {}

                # Resolve ${vault:NAME} AND bare ${NAME} references in the
                # channel config before handing it to the adapter. The agent-
                # config loader interpolates ${ENV_VAR} placeholders from
                # os.environ only; since secrets are no longer hydrated into the
                # environment, a token written as ${TELEGRAM_TOKEN_KEVIN} would
                # otherwise reach the client verbatim and be rejected. Passing
                # the agent name scopes lookups to its per-agent vault override
                # (falling back to the common value), with the vault taking
                # priority over os.environ.
                ch_cfg = await asyncio.to_thread(resolve_config_dict, ch_cfg, agent=name)

                # Credentials are NOT in the config — every connector has one
                # canonical vault key (telegram → TELEGRAM_BOT_TOKEN), so the
                # token is put back here, resolved per-agent override → common
                # → env. Must run before the collision checks below, which
                # compare the real token across agents.
                resolution = await asyncio.to_thread(
                    hydrate_channel_credentials, ch_name, ch_cfg, agent=name
                )
                if not resolution.ok:
                    # No credential anywhere: skip just this connector (the
                    # agent itself still boots and answers CLI/API/dashboard),
                    # say exactly which key to set, and register the agent for
                    # the auto-reboot that runs when the vault is unlocked.
                    self._agents_with_unresolved_secrets.add(name)
                    self._skipped_channels.setdefault(name, {})[ch_name] = (
                        _missing_credentials_reason(resolution.missing)
                    )
                    _report_missing_channel_credentials(name, ch_name, resolution.missing)
                    continue
                _clear_missing_channel_credentials(name, ch_name)

                # Belt-and-suspenders: warn loudly if the resolved config
                # still contains an unresolved vault placeholder (e.g. the
                # vault is locked or the entry is missing). Saves users
                # debugging silent 401/Unauthorized errors from upstream.
                if _flag_unresolved_secrets(ch_cfg, agent=name, channel=ch_name):
                    self._agents_with_unresolved_secrets.add(name)

                # Run the token + resource collision checks on BOTH legacy
                # (`telegram`) and platform-adapter (`platform:telegram`)
                # configs so the diagnostics fire regardless of which
                # integration path the agent is configured to use.
                _ch_kind_for_collision = (
                    ch_name.split(":", 1)[1] if ch_name.startswith("platform:") else ch_name
                )
                _check_token_collision(_ch_kind_for_collision, ch_cfg, name)
                _check_resource_collision(_ch_kind_for_collision, ch_cfg, name)

                if ch_name.startswith("platform:"):
                    platform_name = ch_name.split(":", 1)[1]
                    try:
                        await self._boot_platform_adapter(platform_name, ch_cfg, runner, name)
                    except Exception as exc:
                        logger.warning(
                            "Could not load platform '%s' for agent '%s': %s",
                            platform_name,
                            name,
                            exc,
                        )
                    continue

                if ch_name in DEPRECATED_CHANNELS:
                    logger.warning(
                        "Agent '%s' uses the deprecated '%s' connector (duplicated "
                        "implementation). It still works, but switch the channel "
                        "type to '%s' to get the maintained adapter.",
                        name,
                        ch_name,
                        DEPRECATED_CHANNELS[ch_name],
                    )

                try:
                    ch_cls = _load_channel_class(ch_name)
                    ch = ch_cls(ch_cfg)
                    # Stamp the owning agent on the classic-channel instance
                    # so the outbound ChannelRegistry can route by
                    # (agent, channel_type) just like platform adapters.
                    ch.agent_name = name
                    # Also expose the agent's config so channel-level command
                    # handlers (e.g. Telegram /start) can read monetization
                    # settings without a runtime back-reference.
                    ch.agent_config = runner.config

                    async def make_handler(
                        r: AgentRunner = runner,
                        agent_name: str = name,
                        channel_name: str = ch_name,
                        channel: Any = ch,
                        allowed_users: frozenset[str] = _normalize_allowed_users(
                            ch_cfg.get("allowed_users")
                        ),
                        # `_sender_is_trusted` is documented to take the union
                        # of both lists — `owner_users` names owners WITHOUT
                        # closing the bot, so an open bot can still have one.
                        # The platform-adapter path unions them; this one only
                        # ever built `allowed_users`, so union it here for the
                        # ownership answer. Deliberately not rewired into the
                        # `SenderInfo` below: that value also drives `/olano`
                        # owner commands, and widening those is a separate call.
                        trusted_users: frozenset[str] = _normalize_allowed_users(
                            ch_cfg.get("allowed_users")
                        )
                        | _normalize_allowed_users(ch_cfg.get("owner_users")),
                    ):
                        async def handler(msg: Any) -> str:
                            # The classic channel bus's twin of the platform
                            # adapter's `is_trusted`, hoisted above the context
                            # push so it can be carried: self-configuration must
                            # mean the same thing on both messenger paths.
                            _meta = getattr(msg, "metadata", None) or {}
                            _trusted = _sender_is_trusted(
                                str(getattr(msg, "user_id", "") or ""),
                                str(_meta.get("username") or "").lstrip("@").lower(),
                                trusted_users,
                            )
                            with push_request_context(
                                source="channel",
                                channel=channel_name,
                                user_id=str(getattr(msg, "user_id", "unknown")),
                                session_id=str(getattr(msg, "thread_id", "")),
                                authenticated=True,
                                is_owner=False,
                                is_group=_is_group_message(msg),
                                # Lets a proactive send with target="owner" land
                                # back in this chat (platforms/targets.py).
                                metadata={
                                    "chat_id": str(getattr(msg, "chat_id", "") or ""),
                                    "platform_user_id": str(getattr(msg, "user_id", "") or ""),
                                    AGENT_ADMIN_KEY: _trusted,
                                },
                            ):
                                await self.event_bus.emit_simple(
                                    "message.received",
                                    source=channel_name,
                                    agent=agent_name,
                                    text=msg.text[:100],
                                )

                                # Per-agent / per-platform inbound rate limit
                                # (external ingress only — scheduled/heartbeat/
                                # autonomous turns bypass this path).
                                from olano.security.message_rate_limit import (
                                    check_message_rate_limit,
                                    rate_limit_notice,
                                )

                                rl_decision = check_message_rate_limit(
                                    agent_name, channel_name, r.config
                                )
                                if not rl_decision.allowed:
                                    await self.event_bus.emit_simple(
                                        "rate_limited",
                                        source=channel_name,
                                        agent=agent_name,
                                        retry_after=rl_decision.retry_after,
                                    )
                                    return rate_limit_notice(rl_decision)

                                # Size cap + regex silence rules — the classic
                                # channel bus's twin of the platform-adapter
                                # gate below. Same position (above the command
                                # dispatcher and the model), same contract: ""
                                # means send nothing.
                                from olano.security import reply_policy as _rp

                                _verdict = await _rp.ascreen_inbound(
                                    getattr(msg, "text", "") or "",
                                    _rp.resolve_policy(r.config),
                                )
                                if _verdict.blocked:
                                    logger.info(
                                        "[%s] message_policy dropped a %s message (%s)",
                                        agent_name,
                                        channel_name,
                                        _verdict.reason,
                                    )
                                    await self.event_bus.emit_simple(
                                        "message_policy",
                                        source=channel_name,
                                        agent=agent_name,
                                        action=_verdict.action,
                                        reason=_verdict.reason,
                                    )
                                    return _verdict.notice
                                if _verdict.action == "truncate":
                                    msg.text = _verdict.text

                                metadata = getattr(msg, "metadata", None) or {}
                                recipient = str(
                                    metadata.get("chat_id")
                                    or metadata.get("recipient")
                                    or getattr(msg, "user_id", "")
                                )

                                base_thread_id = str(msg.thread_id)

                                # Slash command? Resolve it (per the agent's
                                # command config) instead of sending the literal
                                # text to the model.
                                from olano.platforms.inbound import (
                                    SenderInfo,
                                    get_session_settings,
                                    maybe_handle_command,
                                )

                                uid = str(getattr(msg, "user_id", "") or "")
                                uname = str(metadata.get("username") or "").lstrip("@").lower()
                                sender = SenderInfo(
                                    user_id=uid,
                                    username=uname,
                                    chat_id=recipient,
                                    channel=channel_name,
                                    is_trusted=_sender_is_trusted(uid, uname, allowed_users),
                                    allowlist_configured=bool(allowed_users),
                                )

                                # Monetization self-serve (/subscribe, /billing,
                                # /trial) is resolved BEFORE the paywall gate and
                                # before generic command dispatch, so a blocked
                                # user can still reach the command that unblocks
                                # them (and these aren't built-in commands).
                                mon_cfg = getattr(r.config, "monetization", None)
                                if mon_cfg is not None and getattr(mon_cfg, "enabled", False):
                                    try:
                                        from olano.monetization.commands import (
                                            handle_monetization_command,
                                        )

                                        mon_reply = await handle_monetization_command(
                                            msg.text or "",
                                            agent=agent_name,
                                            channel_name=channel_name,
                                            channel=channel,
                                            recipient=recipient,
                                            config=r.config,
                                            is_owner=sender.is_trusted,
                                        )
                                    except Exception as exc:
                                        logger.debug("monetization command failed: %s", exc)
                                        mon_reply = None
                                    if mon_reply is not None:
                                        return mon_reply

                                outcome = await maybe_handle_command(
                                    msg.text or "",
                                    runner=r,
                                    config=r.config,
                                    base_thread_id=base_thread_id,
                                    sender=sender,
                                )
                                if outcome.handled and outcome.run_prompt is None:
                                    await self.event_bus.emit_simple(
                                        "message.sent",
                                        source=agent_name,
                                        channel=channel_name,
                                        length=len(outcome.reply),
                                    )
                                    return outcome.reply

                                # Entitlement gate: an untrusted end-user of a
                                # monetized agent must hold an active
                                # subscription or a live trial. Owners
                                # (is_trusted) always bypass and are never
                                # charged. Failures fail open (the gate itself
                                # fails open on no-identity / errors) so a
                                # misconfig never hard-blocks users.
                                if (
                                    mon_cfg is not None
                                    and getattr(mon_cfg, "enabled", False)
                                    and not sender.is_trusted
                                ):
                                    try:
                                        from olano.monetization.gate import evaluate_access

                                        decision = evaluate_access(
                                            agent_name, channel_name, r.config
                                        )
                                    except Exception as exc:
                                        logger.warning(
                                            "Entitlement gate error for agent=%s: %s",
                                            agent_name,
                                            exc,
                                        )
                                        decision = None
                                    if decision is not None and not decision.allowed:
                                        try:
                                            from olano.monetization.service import (
                                                build_subscription_offer,
                                            )

                                            offer = await build_subscription_offer(
                                                agent_name,
                                                channel_name,
                                                sender.user_id,
                                                r.config,
                                            )
                                            if hasattr(channel, "send_subscription_offer"):
                                                await channel.send_subscription_offer(
                                                    recipient, offer
                                                )
                                            elif offer.stripe_url:
                                                return (
                                                    f"{decision.message}\n\n"
                                                    f"Subscribe here: {offer.stripe_url}"
                                                )
                                        except Exception as exc:
                                            logger.warning(
                                                "Subscription offer failed for agent=%s: %s",
                                                agent_name,
                                                exc,
                                            )
                                        return decision.message
                                    # First contact (trial just started) → send a
                                    # one-time welcome explaining the trial + price
                                    # + how to pay, then let the model answer their
                                    # actual message. Best-effort; never blocks.
                                    if decision is not None and decision.reason == "trial_started":
                                        try:
                                            from olano.monetization.service import welcome_text

                                            wt = welcome_text(
                                                agent_name,
                                                channel_name,
                                                r.config,
                                                user_id=sender.user_id,
                                            )
                                            send_text = getattr(channel, "send_text", None)
                                            if callable(send_text):
                                                await send_text(recipient, wt)
                                            elif hasattr(channel, "send_with_retry"):
                                                await channel.send_with_retry(recipient, wt)
                                        except Exception as exc:
                                            logger.debug(
                                                "Welcome message failed for agent=%s: %s",
                                                agent_name,
                                                exc,
                                            )

                                approval_handler = None
                                try:
                                    approval_handler = await channel.make_approval_handler(
                                        recipient, agent_name
                                    )
                                except Exception as exc:
                                    logger.debug(
                                        "Channel '%s' approval handler unavailable: %s",
                                        channel_name,
                                        exc,
                                    )

                                settings = get_session_settings(r, base_thread_id)
                                verbosity = settings["verbosity"]
                                think_level = settings["think_level"]

                                # Broadcast to the dashboard's SSE stream so
                                # the playground / Activity feed reflect work
                                # triggered by channels (Telegram, Discord,
                                # Slack, …) — not just the dashboard chat.
                                from olano.platforms.progress_reporter import (
                                    make_channel_reporter,
                                )
                                from olano.platforms.verbosity import (
                                    dialect_for_channel,
                                    render_events,
                                )
                                from olano.runtime.api import _push_event as _api_push
                                from olano.tools.talk_to import current_agent as _ca

                                _api_push(
                                    "agent_activity", {"agent": agent_name, "status": "thinking"}
                                )
                                _ca_token = _ca.set(agent_name)
                                _dialect = dialect_for_channel(channel_name)
                                # Classic channels only send, so progress is
                                # appended as occasional messages (opt-in per
                                # channel via SUPPORTS_PROGRESS).
                                progress = make_channel_reporter(
                                    channel,
                                    recipient,
                                    verbosity=verbosity,
                                    dialect=_dialect,
                                )
                                # Custom command runs its prompt; otherwise the
                                # user's text — enveloped when the sender is
                                # not the configured owner (see
                                # security/untrusted_ingress).
                                if outcome.run_prompt is not None:
                                    inbound_text = outcome.run_prompt
                                else:
                                    from olano.security.untrusted_ingress import (
                                        envelope_inbound_text,
                                    )

                                    inbound_text = envelope_inbound_text(
                                        msg.text or "",
                                        is_trusted=sender.is_trusted,
                                        allowlist_configured=sender.allowlist_configured,
                                    )
                                if think_level and think_level != "off" and inbound_text:
                                    inbound_text = f"[think:{think_level}] {inbound_text}"
                                collected: list[tuple[str, Any]] = []
                                progress.start()
                                try:
                                    async for event_type, payload in r.astream_events(
                                        inbound_text,
                                        thread_id=settings["active_thread"],
                                        approval_handler=approval_handler,
                                    ):
                                        collected.append((event_type, payload))
                                        progress.feed(event_type, payload)
                                finally:
                                    await progress.finish()
                                    try:
                                        _ca.reset(_ca_token)
                                    except Exception:
                                        pass
                                    _api_push(
                                        "agent_activity", {"agent": agent_name, "status": "idle"}
                                    )
                                # Declined under `ignore_rules` — asked before
                                # `render_events` (see `declined_reply`); the
                                # platform-adapter handler below is the twin.
                                _declined = _rp.declined_reply(
                                    collected, _rp.resolve_policy(r.config)
                                )
                                if _declined is not None:
                                    await self.event_bus.emit_simple(
                                        "message_policy",
                                        source=channel_name,
                                        agent=agent_name,
                                        action="ignore",
                                        reason="ignore_rules",
                                    )
                                    return _declined
                                response = render_events(
                                    collected,
                                    verbosity,
                                    dialect=_dialect,
                                    streamed_progress=progress.delivered,
                                )
                                await self.event_bus.emit_simple(
                                    "message.sent",
                                    source=agent_name,
                                    channel=channel_name,
                                    length=len(response),
                                )
                                return response

                        return handler

                    ch.set_handler(await make_handler())
                    self._channels.append(ch)
                    # Register for outbound delivery so notification_tools
                    # and PULSE can route ``(agent, channel)`` messages
                    # back through this adapter. Classic BaseChannel and
                    # BaseChannelAdapter expose overlapping APIs — only
                    # register if the adapter actually supports it.
                    if hasattr(ch, "send_text"):
                        try:
                            from olano.platforms import get_channel_registry

                            get_channel_registry().register(name, ch_name, ch)
                        except Exception as exc:
                            logger.debug(
                                "Channel '%s' could not register for outbound delivery: %s",
                                ch_name,
                                exc,
                            )
                    logger.info("Channel '%s' attached to agent '%s'", ch_name, name)

                    # During the initial boot, starting is deferred to the
                    # one-time ``_start_channels()`` pass (which runs after the
                    # REST API binds). On a *single-agent restart* that pass
                    # never runs again, so start the channel here — otherwise it
                    # is attached but its poller/listener never starts and
                    # inbound messages are silently queued forever (e.g. Telegram
                    # ``getUpdates`` is never called).
                    if getattr(self, "_server_started", False):
                        await self._start_channel_on_restart(ch, ch_name, name)
                except Exception as e:
                    logger.warning(
                        "Could not load channel '%s' for agent '%s': %s", ch_name, name, e
                    )
            if channel_entries:
                record_phase(
                    f"channels ({len(channel_entries)})", time.perf_counter() - _channels_t0
                )

            # ACP server (`acp_enabled` in agent.json5 / dashboard → Advanced).
            #
            # Constructing ACPServer registers the agent in the process-global
            # ACP registry, and every ACP site routes /acp/agents/<name>/… by
            # that registry — so agents sharing a port share ONE site and the
            # whole fleet is reachable through it. Only the first agent on a
            # given port binds the socket; the rest just register. Binding twice
            # would raise "address already in use" inside a background task
            # nobody awaits, which is how a second ACP-enabled agent used to go
            # silently unserved.
            if config.acp_enabled:
                with boot_phase("acp"):
                    from olano.acp.server import ACPServer

                    acp = ACPServer(agent, name)
                    self._acp_servers.append((acp, config.acp_port))
                    if config.acp_port in self._acp_sites:
                        logger.info(
                            "ACP: agent '%s' joins the server already listening on port %d",
                            name,
                            config.acp_port,
                        )
                    else:
                        task = asyncio.create_task(
                            self._run_acp_server(acp, name, config.acp_port),
                            name=f"acp_{config.acp_port}",
                        )
                        self._acp_sites[config.acp_port] = task
                        self._background_tasks.append(task)

            # Inbound MCP (`external_access.mcp` in agent.json5 / dashboard →
            # External access): publish the agent as an ask_<name> tool on the
            # shared endpoint. The endpoint itself is home-wide (config.yaml →
            # external_access.mcp).
            if config.external_access.mcp and self.mcp_server is not None:
                with boot_phase("mcp_expose"):
                    try:
                        self.mcp_server.register_agent(name, runner)
                    except Exception as exc:  # never let the tool surface break a boot
                        logger.error("MCP server: could not expose agent '%s': %s", name, exc)
            # Inbound A2A: an Agent Card + JSON-RPC endpoint for this agent.
            if config.external_access.a2a and self.a2a_server is not None:
                with boot_phase("a2a_expose"):
                    try:
                        self.a2a_server.register_agent(name, runner)
                    except Exception as exc:
                        logger.error("A2A server: could not expose agent '%s': %s", name, exc)

            self._set_agent_status(name, "running")
            # Record exactly which adapters/services this agent owns so we
            # can tear them down individually later.
            self._agent_resources[name] = {
                "channels": [c for c in self._channels if c not in prior_channels],
                "platforms": [p for p in self._platforms if p not in prior_platforms],
                "self_learning": [s for s in self._self_learning if s not in prior_self_learning],
                "acp_servers": [a for a in self._acp_servers if a not in prior_acp],
                # The ACP site is owned by the runtime, not by the agent that
                # happened to start it: it serves every agent registered on that
                # port, so stopping its starter must not take the others' endpoint
                # down with it. `_stop_agent` closes it only when the last agent
                # on the port leaves.
                "background": [
                    t
                    for t in self._background_tasks
                    if t not in prior_bg and t not in self._acp_sites.values()
                ],
            }
            logger.info(
                "Agent '%s' booted [model=%s trust=%d]",
                name,
                config.model,
                config.security.trust_level,
            )
            if config.webhooks:
                await self.event_bus.emit_simple(
                    "agent.started", source=name, agent=name, model=config.model
                )

        except Exception as e:
            logger.error("Failed to boot agent '%s': %s", name, e)
            self._set_agent_status(name, "error", error=str(e))

    async def _run_acp_server(self, acp: Any, name: str, port: int) -> None:
        """Serve one agent's ACP site, turning a bind failure into a log line.

        The task is created and never awaited, so an exception here would only
        surface as "Task exception was never retrieved" at interpreter exit (or
        not at all). A port already held by another process is the expected
        failure and deserves an actionable message: the agent stays up, only its
        ACP endpoint is missing.
        """
        try:
            await acp.run_http(port=port)
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            logger.error(
                "ACP server for agent '%s' could not bind port %d: %s — "
                "pick a free port in the agent's Advanced settings (acp_port), "
                "or leave several agents on one port to share a single server. "
                "The agent is running; only its ACP endpoint is unavailable.",
                name,
                port,
                exc,
            )
        except Exception as exc:
            logger.error("ACP server for agent '%s' stopped: %s", name, exc)

    def _set_agent_status(self, name: str, status: str, error: str = "") -> None:
        """Record an agent's lifecycle status *and* tell connected dashboards.

        The registry is in-process state; `GET /api/agents` reads it per request.
        Without a broadcast, a status the runtime changes on its own — every
        agent transition during boot, notably — is invisible to an already-open
        dashboard until something else happens to make it refetch. That is why
        the sidebar agent picker could sit on "offline" for agents that had
        finished booting minutes earlier: the picker (currentAgent.tsx) refreshes
        on `agents_update`, and only the API's own handlers were ever emitting
        one. The Overview grid appeared to work only because it separately
        sniffs boot lines out of the log stream.

        Status changes driven by an API handler broadcast there too; a duplicate
        frame is harmless (clients coalesce and refetch once).
        """
        self.registry.update_status(name, status, error=error)
        try:
            from olano.runtime.api import _broadcast_roster_changed

            _broadcast_roster_changed(name)
        except Exception:  # pragma: no cover - the API may not be up yet
            logger.debug("roster broadcast skipped for '%s' (%s)", name, status, exc_info=True)

    async def _stop_agent(self, name: str) -> None:
        """Tear down a single booted agent without touching the others.

        Stops the agent's channels and platform adapters, cancels its
        background tasks, deregisters its scheduler jobs, and removes the
        runner from `_runners`. Idempotent on already-stopped agents.

        Shares `_boot_agent`'s per-agent lock: a stop issued while that agent is
        still booting waits for the boot to finish and then tears it down, so
        "stop" during a slow boot no longer no-ops and leaves the agent running.
        """
        async with self._boot_lock(name):
            await self._stop_agent_locked(name)
        # A stopped peer leaves every other running agent's roster section —
        # nudge the prompt refresh sweep so their prompts drop it.
        if not self._agents_booting and not self._stopped:
            self.request_prompt_refresh(None)

    async def _stop_agent_locked(self, name: str) -> None:
        if name not in self._runners and name not in self._agent_resources:
            return
        self._set_agent_status(name, "stopping")

        # Drop this agent's adapters from the outbound registries first,
        # so any in-flight notification call resolves to "no adapter"
        # instead of mid-shutdown send attempts.
        try:
            from olano.platforms import get_channel_registry, get_platform_registry

            get_channel_registry().unregister_agent(name)
            get_platform_registry().unregister_agent(name)
        except Exception as exc:
            logger.debug("Registry cleanup error for '%s': %s", name, exc)

        # Fire a final agent.stopped webhook, then stop firing this agent's
        # webhooks until it boots again.
        try:
            if self.webhook_dispatcher.webhooks_for(name):
                await self.event_bus.emit_simple("agent.stopped", source=name, agent=name)
            self.webhook_dispatcher.unregister_agent(name)
        except Exception as exc:
            logger.debug("Webhook cleanup error for '%s': %s", name, exc)

        # Release any port / session-path / token claims so the next
        # boot doesn't trip the collision warning against this agent's
        # own previous registration.
        _clear_resource_owners_for_agent(name)

        resources = self._agent_resources.pop(name, {})

        teardown: list[asyncio.Task[Any]] = []
        for channel in resources.get("channels", []) or []:
            teardown.append(
                asyncio.create_task(
                    self._safe_call(
                        channel.stop(), label=f"channel:{getattr(channel, 'name', '?')}"
                    )
                )
            )
            try:
                self._channels.remove(channel)
            except ValueError:
                pass
        for platform_adapter in resources.get("platforms", []) or []:
            teardown.append(
                asyncio.create_task(
                    self._safe_call(
                        platform_adapter.disconnect(),
                        label=f"platform:{getattr(platform_adapter, 'platform', '?')}",
                    )
                )
            )
            try:
                self._platforms.remove(platform_adapter)
            except ValueError:
                pass

        if teardown:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*teardown, return_exceptions=True),
                    timeout=5,
                )
            except TimeoutError:
                logger.warning("Stop timed out for agent '%s'", name)

        for svc in resources.get("self_learning", []) or []:
            try:
                svc.stop()
            except Exception as exc:
                logger.debug("self-learning stop error for '%s': %s", name, exc)
            try:
                self._self_learning.remove(svc)
            except ValueError:
                pass

        cancelled_bg: list[asyncio.Task[Any]] = []
        for task in resources.get("background", []) or []:
            if not task.done():
                task.cancel()
                cancelled_bg.append(task)
            try:
                self._background_tasks.remove(task)
            except ValueError:
                pass
        if cancelled_bg:
            # Give the cancellations a moment to actually unwind. `cancel()` only
            # schedules the CancelledError, so without this a task's cleanup
            # (notably the ACP site releasing its socket) still hasn't run when a
            # restart re-boots the agent and tries to bind the same port.
            await asyncio.wait(cancelled_bg, timeout=5)

        for acp in resources.get("acp_servers", []) or []:
            try:
                self._acp_servers.remove(acp)
            except ValueError:
                pass
            # Close the site once the last agent on that port is gone; while any
            # remain it keeps serving them.
            port = acp[1] if isinstance(acp, tuple) else None
            if port is not None and not any(p == port for _s, p in self._acp_servers):
                task = self._acp_sites.pop(port, None)
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.wait([task], timeout=5)
                try:
                    self._background_tasks.remove(task)
                except ValueError:
                    pass
        # Idempotent: a stopped agent must not stay callable from outside.
        for srv, label in ((self.mcp_server, "MCP"), (self.a2a_server, "A2A")):
            if srv is not None:
                try:
                    srv.unregister_agent(name)
                except Exception as exc:
                    logger.debug("%s server: unregister '%s' failed: %s", label, name, exc)
        if resources.get("acp_servers"):
            # The registry is process-global and a sibling agent's site routes by
            # it, so dropping the task is not enough — unregister the name or a
            # stopped agent stays callable over another agent's ACP port.
            from olano.acp.server import unregister_agent_runner

            unregister_agent_runner(name)

        try:
            self.scheduler.unregister_agent(name)
        except Exception as exc:
            logger.debug("scheduler unregister error for '%s': %s", name, exc)

        self._runners.pop(name, None)
        self._set_agent_status(name, "stopped")
        logger.info("Agent '%s' stopped", name)

    async def _boot_platform_adapter(
        self,
        platform_name: str,
        cfg: dict[str, Any],
        runner: AgentRunner,
        agent_name: str,
    ) -> None:
        """Instantiate a ``BasePlatformAdapter`` and wire it to *runner*."""
        from olano.platforms import PlatformConfig, get_platform_registry
        from olano.platforms.config import Platform

        adapter_cls, platform_enum_name = _load_platform_class(platform_name)
        platform_enum = Platform[platform_enum_name]

        cfg = _fold_credentials_into_extra(platform_name, cfg)
        platform_config = PlatformConfig.from_dict({"enabled": True, **cfg})

        # Pass agent_name to the constructor when supported so adapters
        # with per-agent default ports/paths can compute them eagerly.
        # Adapters whose __init__ doesn't accept agent_name fall back to
        # the legacy signature and have agent_name stamped on after.
        try:
            adapter = adapter_cls(platform_config, agent_name=agent_name)
        except TypeError:
            adapter = adapter_cls(platform_config)
            adapter.agent_name = agent_name
        else:
            # Idempotent guard in case the adapter doesn't store the arg.
            adapter.agent_name = agent_name

        # Adapters that serve an API rather than a chat surface run turns on
        # the runner directly (the ``handler`` below is a chat pipeline —
        # slash commands, progress views, a single rendered reply — none of
        # which an OpenAI-shaped request wants). Duck-typed: only
        # ``platform:api_server`` implements it today.
        if hasattr(adapter, "set_agent_runner"):
            adapter.set_agent_runner(runner)

        # Two lists feed sender trust: ``allowed_users`` (which on some
        # adapters — Telegram — also gates who may message the bot at all)
        # and ``owner_users``, which ONLY designates owners: it never closes
        # the bot, so an open bot can still have a real owner instead of an
        # operator-authored prompt rule the model has to interpret.
        #
        # The business WhatsApp connectors (Twilio, Meta Cloud API) are the
        # exception: their ``allowed_users`` is a list of *customers* allowed
        # to write in, so on them only ``owner_users`` confers trust. Using
        # the allowlist for trust contradicted the promise that a customer can
        # never reach owner-only tools: on Meta (senders arrive as
        # ``+6591234567``, matching an entry exactly) every allowed customer
        # was an owner; on Twilio (``whatsapp:+6591234567``) none matched
        # until the phone-number matching below, which must not change that.
        allowed_users = _normalize_allowed_users(platform_config.extra.get("allowed_users"))
        owner_users = _normalize_allowed_users(platform_config.extra.get("owner_users"))
        if platform_enum.value in _CUSTOMER_ALLOWLIST_PLATFORMS:
            trusted_users = owner_users
        else:
            trusted_users = allowed_users | owner_users
        # Whether the operator named anyone at all: decides whether a sender
        # who is not trusted is treated as an outsider (untrusted envelope).
        allowlist_configured = bool(allowed_users | owner_users)

        async def handler(event: Any) -> str:
            source = event.source
            user_id = str(getattr(source, "user_id", "") or "unknown")
            handle = str(getattr(source, "username", "") or "").lstrip("@").lower()
            display_name = str(getattr(source, "user_name", "") or "").lstrip("@").lower()
            thread_id = source.thread_id or source.chat_id
            # Trust keys ONLY on stable identifiers (numeric id / @handle) via
            # _sender_is_trusted — never the display name, which any group
            # member can set to the owner's name. This same value gates HITL
            # auto-approval, ctx.is_owner, and /olano below; a looser
            # display_name match here once made auto-approval spoofable.
            # The three WhatsApp connectors report a sender's number in three
            # shapes (QR: a JID or @lid with the phone JID in user_id_alt;
            # Twilio: whatsapp:+E164; Meta: +E164 or a business-scoped id), and owners are typed
            # as phone numbers. Only these pass a phone for digit matching.
            phone = ""
            if platform_enum.value == "whatsapp":
                phone = str(getattr(source, "user_id_alt", "") or "") or user_id
            elif platform_enum.value in ("whatsapp_twilio", "whatsapp_meta_api"):
                phone = user_id
            is_trusted = _sender_is_trusted(user_id, handle, trusted_users, phone=phone)

            # How a gated tool call in this turn gets its decision:
            #   * owner + ``chat_approvals`` on: a YES/NO prompt in this chat
            #     (platforms/chat_approval.py);
            #   * owner otherwise: approved without asking, as before;
            #   * anyone else: no handler, so the runner defers it to the
            #     dashboard Approvals queue.
            # Every outcome lands in the approval record (security/hitl.py).
            approval_handler = None
            if is_trusted:
                from olano.platforms.chat_approval import (
                    chat_approvals_enabled,
                    make_chat_approval_handler,
                    sender_identities,
                )

                if chat_approvals_enabled(platform_config.extra):
                    approval_handler = make_chat_approval_handler(
                        adapter,
                        agent_name=agent_name,
                        platform=platform_enum.value,
                        chat_id=str(source.chat_id or user_id),
                        approvers=sender_identities(user_id, handle, phone),
                        timeout_seconds=int(
                            getattr(runner.config.security, "hitl_timeout_seconds", 300) or 300
                        ),
                    )
                else:

                    async def approval_handler(
                        actions: list[dict[str, Any]],
                    ) -> list[dict[str, Any]]:
                        from olano.core import runner as _runner_mod
                        from olano.security import hitl
                        from olano.security.request_context import current_origin

                        # A browser submit/purchase/send is never approved
                        # on the sender's behalf: it goes to the Approvals
                        # inbox (runner._always_asks_human).
                        decisions: list[dict[str, Any]] = []
                        for action in actions:
                            if _runner_mod._always_asks_human(action):
                                decisions.append(
                                    await _runner_mod._defer_to_inbox(
                                        action, runner.config, current_origin(), "high"
                                    )
                                )
                            else:
                                decisions.append({})
                        logger.info(
                            "Platform '%s' auto-approving %d HITL action(s) for trusted user %s",
                            platform_enum.value,
                            sum(1 for d in decisions if not d),
                            user_id,
                        )
                        for action, decided in zip(actions, decisions, strict=True):
                            if decided:
                                continue
                            hitl.record_decision(
                                agent_name=agent_name,
                                action=str(action.get("name") or "tool"),
                                approved=True,
                                decided_by=f"{platform_enum.value}:{user_id}",
                                via="trusted-sender",
                                note="approved without asking: the sender is on the "
                                "channel's owner list and chat_approvals is off",
                                args=action.get("args"),
                                risk=str(action.get("risk") or ""),
                            )
                        return [d or {"type": "approve"} for d in decisions]

            with push_request_context(
                source="platform",
                channel=platform_enum.value,
                user_id=user_id,
                session_id=str(thread_id or ""),
                authenticated=is_trusted,
                is_owner=is_trusted,
                is_group=(getattr(source, "chat_type", "dm") != "dm"),
                # Carry the platform sender so a shared agent can render the
                # active-human block (multi-user mode) and tools can attribute.
                # ``chat_id`` is what proactive sends resolve a bare
                # ``target="owner"`` to, so an agent asked to push a file back
                # into the conversation it is already in lands in the right chat
                # (see platforms/targets.py).
                metadata={
                    "name": str(getattr(source, "user_name", "") or "") or handle or user_id,
                    "username": handle,
                    "platform_user_id": user_id,
                    "channel": platform_enum.value,
                    "chat_id": str(getattr(source, "chat_id", "") or ""),
                    # Self-configuration from a messenger rides the same trust
                    # signal, for the same reason HITL auto-approval does: on
                    # this surface the allowlist IS the statement of who set the
                    # bot up. A stranger in a group cannot rewrite its prompt or
                    # wire it to a new MCP server; the person on the list can,
                    # which is what keeps "pair yourself with WhatsApp" working
                    # in chat (self_config_tools._ownership_gate).
                    AGENT_ADMIN_KEY: is_trusted,
                },
            ):
                await self.event_bus.emit_simple(
                    "message.received",
                    source=platform_enum.value,
                    agent=agent_name,
                    text=(event.text or "")[:100],
                )

                # Context-only event (e.g. the owner speaking as themselves in
                # a Telegram Business chat): fold it into the conversation's
                # history so the agent's next turn sees it — without running
                # the agent, rate-limiting, or sending any reply. The adapter
                # composed self-describing text; an empty note is a no-op.
                if getattr(event, "record_only", False):
                    note = (event.text or "").strip()
                    if note:
                        runner.record_context_note(str(thread_id or user_id), note)
                    return ""

                # Per-agent / per-platform inbound rate limit (external ingress
                # only — scheduled/heartbeat/autonomous turns bypass this path).
                from olano.security.message_rate_limit import (
                    check_message_rate_limit,
                    rate_limit_notice,
                )

                rl_decision = check_message_rate_limit(
                    agent_name, platform_enum.value, runner.config
                )
                if not rl_decision.allowed:
                    await self.event_bus.emit_simple(
                        "rate_limited",
                        source=platform_enum.value,
                        agent=agent_name,
                        retry_after=rl_decision.retry_after,
                    )
                    return rate_limit_notice(rl_decision)

                # Size cap + regex silence rules (agent.json5 → message_policy).
                # Above the command dispatcher and the model alike: a rejected
                # message must cost nothing, which it only does if nothing has
                # run yet. Screens the sender's own text — the [from …] prefix
                # and the untrusted envelope are added further down, and a cap
                # that counted them would depend on the sender's display name.
                from olano.security import reply_policy as _rp

                _verdict = await _rp.ascreen_inbound(
                    event.text or "", _rp.resolve_policy(runner.config)
                )
                if _verdict.blocked:
                    logger.info(
                        "[%s] message_policy dropped a %s message (%s)",
                        agent_name,
                        platform_enum.value,
                        _verdict.reason,
                    )
                    await self.event_bus.emit_simple(
                        "message_policy",
                        source=platform_enum.value,
                        agent=agent_name,
                        action=_verdict.action,
                        reason=_verdict.reason,
                    )
                    # "" is how this seam says "send nothing" — see
                    # BasePlatformAdapter._process_message_background, which
                    # treats an empty handler result as a completed turn with
                    # no delivery. A rejection still carries its notice.
                    return _verdict.notice
                if _verdict.action == "truncate":
                    logger.info(
                        "[%s] message_policy truncated a %s message (%s)",
                        agent_name,
                        platform_enum.value,
                        _verdict.reason,
                    )
                    event.text = _verdict.text

                # Per-chat state (verbosity, thinking level, active thread) is
                # keyed by the deterministic chat-derived id so it survives a
                # /new that swaps the streaming thread.
                base_thread_id = str(thread_id or user_id)

                # Slash command? Resolve it (e.g. /help, /model, /verbosity, or
                # a custom command) per the agent's command config instead of
                # forwarding the literal text to the model.
                from olano.platforms.inbound import (
                    SenderInfo,
                    get_session_settings,
                    maybe_handle_command,
                )

                # Owner gating for /olano uses the same strict identity check
                # as HITL auto-approval above (_sender_is_trusted): stable
                # identifiers only, never the freely-settable display name.
                sender = SenderInfo(
                    user_id=user_id,
                    username=handle,
                    display_name=display_name,
                    chat_id=str(source.chat_id or user_id),
                    channel=platform_enum.value,
                    is_trusted=is_trusted,
                    allowlist_configured=allowlist_configured,
                )

                # Monetization self-serve (/subscribe, /billing, /trial) is
                # resolved BEFORE the paywall gate and before generic command
                # dispatch, so a blocked user can still reach the command that
                # unblocks them. Mirrors the classic-channel path above.
                mon_cfg = getattr(runner.config, "monetization", None)
                if mon_cfg is not None and getattr(mon_cfg, "enabled", False):
                    try:
                        from olano.monetization.commands import (
                            handle_monetization_command,
                        )

                        mon_reply = await handle_monetization_command(
                            event.text or "",
                            agent=agent_name,
                            channel_name=platform_enum.value,
                            channel=adapter,
                            recipient=sender.chat_id,
                            config=runner.config,
                            is_owner=sender.is_trusted,
                        )
                    except Exception as exc:
                        logger.debug("monetization command failed: %s", exc)
                        mon_reply = None
                    if mon_reply is not None:
                        return mon_reply

                outcome = await maybe_handle_command(
                    event.text or "",
                    runner=runner,
                    config=runner.config,
                    base_thread_id=base_thread_id,
                    sender=sender,
                )
                if outcome.handled and outcome.run_prompt is None:
                    await self.event_bus.emit_simple(
                        "message.sent",
                        source=agent_name,
                        channel=platform_enum.value,
                        length=len(outcome.reply),
                    )
                    return outcome.reply

                # Entitlement gate: an untrusted end-user of a monetized agent
                # must hold an active subscription or a live trial. Owners
                # (is_trusted) always bypass and are never charged. Failures
                # fail open so a misconfig never hard-blocks users. Mirrors
                # the classic-channel path above.
                if (
                    mon_cfg is not None
                    and getattr(mon_cfg, "enabled", False)
                    and not sender.is_trusted
                ):
                    try:
                        from olano.monetization.gate import evaluate_access

                        decision = evaluate_access(agent_name, platform_enum.value, runner.config)
                    except Exception as exc:
                        logger.warning("Entitlement gate error for agent=%s: %s", agent_name, exc)
                        decision = None
                    if decision is not None and not decision.allowed:
                        try:
                            from olano.monetization.service import (
                                build_subscription_offer,
                            )

                            offer = await build_subscription_offer(
                                agent_name,
                                platform_enum.value,
                                sender.user_id,
                                runner.config,
                            )
                            if hasattr(adapter, "send_subscription_offer"):
                                await adapter.send_subscription_offer(sender.chat_id, offer)
                            elif offer.stripe_url:
                                return f"{decision.message}\n\nSubscribe here: {offer.stripe_url}"
                        except Exception as exc:
                            logger.warning(
                                "Subscription offer failed for agent=%s: %s", agent_name, exc
                            )
                        return decision.message
                    # First contact (trial just started) → one-time welcome
                    # explaining the trial + price + how to pay, then let the
                    # model answer their actual message. Best-effort.
                    if decision is not None and decision.reason == "trial_started":
                        try:
                            from olano.monetization.service import welcome_text

                            wt = welcome_text(
                                agent_name,
                                platform_enum.value,
                                runner.config,
                                user_id=sender.user_id,
                            )
                            await adapter.send_text(wt, sender.chat_id)
                        except Exception as exc:
                            logger.debug("Welcome message failed for agent=%s: %s", agent_name, exc)

                settings = get_session_settings(runner, base_thread_id)
                verbosity = settings["verbosity"]
                think_level = settings["think_level"]

                # Broadcast agent activity for the dashboard playground /
                # Activity feed when work is triggered by a platform
                # adapter (Discord / Slack / WhatsApp / …).
                from olano.platforms.progress_reporter import make_platform_reporter
                from olano.platforms.verbosity import (
                    dialect_for_platform,
                    render_events,
                )
                from olano.runtime.api import _push_event as _api_push
                from olano.tools.talk_to import current_agent as _ca

                _api_push("agent_activity", {"agent": agent_name, "status": "thinking"})
                _ca_token = _ca.set(agent_name)
                _dialect = dialect_for_platform(platform_enum.value)
                # Live progress: the plan + tool calls stream to the chat while
                # the agent works, so a long multi-tool turn shows what it is
                # doing instead of a silent "Thinking…" bubble.
                progress = make_platform_reporter(
                    adapter, event, verbosity=verbosity, dialect=_dialect
                )
                collected: list[tuple[str, Any]] = []
                if outcome.run_prompt is not None:
                    # Custom command: run its owner-authored prompt verbatim
                    # (no sender prefix — it's a template, not the user's words).
                    inbound_text = outcome.run_prompt
                else:
                    # Prepend a "[from @handle (Display Name, id=…)]" tag so the
                    # agent sees who sent the message — essential in groups, and
                    # useful in DMs for natural address. Skipped when the source
                    # carries no identity info (synthetic/internal events).
                    # Non-owner senders (allowlist configured, sender not on it)
                    # additionally get their text wrapped in the untrusted
                    # envelope — the [from …] tag stays OUTSIDE it, runtime-
                    # authored and unforgeable (see security/untrusted_ingress).
                    from olano.platforms.reply_context import render_reply_context
                    from olano.security.untrusted_ingress import (
                        envelope_inbound_text,
                    )

                    sender_prefix = source.format_sender_prefix()
                    # In groups/channels, also name the current chat WITH its
                    # id — so "remind this group every Friday" gives the agent
                    # a concrete address to store, with zero lookups.
                    chat_prefix = source.format_chat_prefix()
                    if chat_prefix:
                        sender_prefix = (
                            f"{sender_prefix} {chat_prefix}" if sender_prefix else chat_prefix
                        )
                    body = envelope_inbound_text(
                        event.text or "",
                        is_trusted=sender.is_trusted,
                        allowlist_configured=sender.allowlist_configured,
                    )
                    inbound_text = f"{sender_prefix}: {body}" if sender_prefix and body else body
                    # A person replying to an older message means "about THIS
                    # one" — without it the agent reads a bare answer with no
                    # referent. Adapters have always captured the quoted text;
                    # this is where it finally reaches the model. Above the
                    # body, so the referent is established before the words
                    # about it. Quoted text is sender-selected (and in groups
                    # usually authored by someone else), so the renderer
                    # envelopes it under the same rule as the body.
                    reply_block = render_reply_context(
                        event,
                        is_trusted=sender.is_trusted,
                        allowlist_configured=sender.allowlist_configured,
                    )
                    if reply_block:
                        inbound_text = (
                            f"{reply_block}\n{inbound_text}" if inbound_text else reply_block
                        )
                if think_level and think_level != "off" and inbound_text:
                    inbound_text = f"[think:{think_level}] {inbound_text}"
                # Fold inbound attachments (voice → transcript, images → saved
                # to the workspace uploads/ dir + path note, documents →
                # extracted text) into the payload. Custom commands run a fixed
                # template, so they never pull in the sender's media. Returns
                # the plain string unchanged when there's no usable media. Bind
                # the agent's credential overlay for the duration —
                # transcription resolves per-agent STT keys (config.env →
                # vault → os.environ) exactly like a tool call.
                if outcome.run_prompt is None:
                    from olano.platforms.inbound import build_agent_message
                    from olano.tools._credentials import agent_runtime_context

                    with agent_runtime_context(agent_name, runner.config.env):
                        model_input: Any = await build_agent_message(
                            inbound_text, event, workspace_root=runner.workspace.root
                        )
                else:
                    model_input = inbound_text
                progress.start()
                try:
                    async for event_type, payload in runner.astream_events(
                        model_input,
                        thread_id=settings["active_thread"],
                        approval_handler=approval_handler,
                    ):
                        collected.append((event_type, payload))
                        progress.feed(event_type, payload)
                finally:
                    # Settle the progress view before the answer is sent, so
                    # the chat never shows a stale checklist beside the reply.
                    await progress.finish()
                    try:
                        _ca.reset(_ca_token)
                    except Exception:
                        pass
                    _api_push("agent_activity", {"agent": agent_name, "status": "idle"})
                # The model declined under `message_policy.ignore_rules` —
                # asked BEFORE `render_events`, which would otherwise turn a
                # text-less turn into "(done — N tool call(s))" (see
                # `declined_reply`). "" is how this seam says "send nothing".
                _declined = _rp.declined_reply(collected, _rp.resolve_policy(runner.config))
                if _declined is not None:
                    await self.event_bus.emit_simple(
                        "message_policy",
                        source=platform_enum.value,
                        agent=agent_name,
                        action="ignore",
                        reason="ignore_rules",
                    )
                    return _declined
                response = render_events(
                    collected,
                    verbosity,
                    dialect=_dialect,
                    streamed_progress=progress.delivered,
                )
                await self.event_bus.emit_simple(
                    "message.sent",
                    source=agent_name,
                    channel=platform_enum.value,
                    length=len(response),
                )
                return response

        adapter.set_message_handler(handler)
        self._platforms.append(adapter)
        get_platform_registry().register(agent_name, platform_enum, adapter)
        # Also register for outbound delivery so notification tools and PULSE
        # can route ``(agent, channel)`` envelopes back through this adapter
        # (BasePlatformAdapter provides the send_text/send_media shims) —
        # mirrors the classic-channel registration in _boot_agent above.
        try:
            from olano.platforms import get_channel_registry

            get_channel_registry().register(agent_name, platform_enum.value, adapter)
        except Exception as exc:
            logger.debug(
                "Platform '%s' could not register for outbound delivery: %s",
                platform_enum.value,
                exc,
            )
        logger.info("Platform '%s' attached to agent '%s'", platform_enum.value, agent_name)

        # Connect the adapter immediately.  During the initial boot this is a
        # no-op (the outer ``start()`` loop calls connect() on all adapters
        # after _boot_agent returns), but on a *single-agent restart* via
        # ``/api/agents/{name}/restart`` the outer loop never runs again, so
        # without this call the adapter's inbound listener (poll_task etc.)
        # is never started and messages from the platform are silently dropped.
        if getattr(self, "_server_started", False):
            try:
                connected = await adapter.connect()
                if not connected:
                    logger.warning(
                        "Platform '%s' connect returned False after restart of agent '%s' — "
                        "messages from this platform will not be delivered until the agent "
                        "is restarted again or the full server is restarted.",
                        platform_enum.value,
                        agent_name,
                    )
            except Exception as exc:
                logger.warning(
                    "Platform '%s' connect() raised during restart of agent '%s': %s",
                    platform_enum.value,
                    agent_name,
                    exc,
                )

    # ─────────────────────────────────── PULSE delivery ──────────────────────

    async def _pulse_delivery_loop(self) -> None:
        """
        Background loop that delivers pending PULSE items to channels.
        Runs every 5 minutes, only if agent has pulse.in_channels=True.
        """
        await asyncio.sleep(30)  # Wait for channels to stabilize
        while True:
            try:
                await self._deliver_pulse_items()
            except asyncio.CancelledError:
                break  # shutdown — exit quietly, no error log
            except Exception as e:
                logger.debug("PULSE delivery loop error: %s", e)
            await asyncio.sleep(300)  # Every 5 minutes

    async def _memory_sampler_loop(self) -> None:
        """Log a cheap ``mem-sample`` line each cycle while the monitor is on, so
        leak-vs-baseline growth is visible in the runtime log over time.

        Gated by ``self._memory_monitor.enabled`` (flipped live via the toggle),
        so an idle loop just sleeps — no measurable cost — until turned on. The
        sample itself is ``/proc`` + ``len()`` only (no gc/tracemalloc).
        """
        from olano.admin.memory import sample_memory_line

        await asyncio.sleep(30)  # let agents stabilize before the first sample
        while True:
            mon = self._memory_monitor
            try:
                if mon.enabled and mon.interval_seconds > 0:
                    sample_memory_line(self, logger)
            except asyncio.CancelledError:
                break  # shutdown — exit quietly, no error log
            except Exception as e:
                logger.debug("memory sampler error: %s", e)
            await asyncio.sleep(max(30, mon.interval_seconds or 600))

    async def _disk_monitor_loop(self) -> None:
        """Measure disk usage on an interval, alert on threshold transitions,
        and run the allow-listed cleanup actions when usage crosses the cleanup
        threshold (safe actions by default; see ``config.yaml → disk_monitor``).

        Settings are re-read every tick so dashboard/CLI changes apply within
        one interval without a restart. The measurement + any subprocesses run
        in a worker thread — never on the event loop.
        """
        if self._disk_monitor.env_disabled:
            return  # LIONCLAW_NO_DISK_MONITOR=1 — no loop at all this boot

        from olano.config.system_settings import load_disk_monitor_settings

        try:
            from olano.runtime.api import _push_event as _disk_push
        except Exception:  # pragma: no cover - defensive
            _disk_push = None  # type: ignore[assignment]

        await asyncio.sleep(60)  # let boot (and its housekeeping pass) settle
        interval_minutes = 30
        while True:
            try:
                settings = load_disk_monitor_settings()
                interval_minutes = settings.check_interval_minutes
                if settings.enabled:
                    await asyncio.to_thread(self._disk_tick_once, settings, _disk_push)
            except asyncio.CancelledError:
                break  # shutdown — exit quietly, no error log
            except Exception as e:
                logger.debug("disk monitor error: %s", e)
            await asyncio.sleep(max(300, interval_minutes * 60))

    def _disk_tick_once(self, settings: Any, push: Any) -> None:
        """One synchronous monitor cycle (runs inside ``asyncio.to_thread``)."""
        from olano.config.home import get_olano_home
        from olano.runtime import disk_monitor as dm

        home = get_olano_home()
        spath = dm.state_path(home)

        def _alert(kind: str, payload: dict[str, Any]) -> None:
            message = str(payload.get("message") or f"disk {kind}")
            if kind in ("warn", "critical"):
                logger.warning("%s", message)
            else:
                logger.info("%s", message)
            if push is not None:
                try:
                    push("disk_cleanup" if kind == "cleanup" else "disk_pressure", payload)
                except Exception:  # pragma: no cover - SSE must never break the tick
                    pass
            # Threshold transitions belong in the bell: a full disk stops
            # writes everywhere and nobody reads the log until it does. One
            # dedupe key per level, cleared when usage recovers.
            try:
                from olano.runtime import notifications

                if kind in ("warn", "critical"):
                    notifications.notify(
                        kind="disk_pressure",
                        level="error" if kind == "critical" else "warn",
                        title=f"Disk usage {kind}",
                        body=message,
                        dedupe_key="disk_pressure",
                        action={"label": "Open Overview", "href": "/overview"},
                    )
                elif kind == "ok":
                    notifications.resolve("disk_pressure")
            except Exception:  # pragma: no cover - never break the tick
                logger.debug("Could not raise the disk notification", exc_info=True)

        def _guarded_cleanup(**kwargs: Any) -> dm.CleanupReport | None:
            # Single cleanup slot shared with the manual "clean now" endpoint.
            if not self._disk_monitor.try_begin_cleanup():
                logger.info("disk cleanup skipped: another cleanup is already running")
                return None
            try:
                return dm.run_cleanup(**kwargs)
            finally:
                self._disk_monitor.end_cleanup()

        def _start_scan() -> bool:
            # Daily directory-size scan — runs on its own niced daemon thread
            # (disk_scan holds the single-scan slot), so the tick never blocks
            # on the walk. The SSE frame lets the dashboard refresh in place.
            from olano.runtime import disk_scan

            def _on_done(result: dict[str, Any]) -> None:
                if push is not None:
                    try:
                        push("disk_scan", result)
                    except Exception:  # pragma: no cover - SSE must never break the scan
                        pass

            return disk_scan.spawn_scan(home, settings.scan, on_done=_on_done)

        state = dm.load_state(spath)
        state, snapshot, report = dm.disk_monitor_tick(
            settings=settings,
            state=state,
            now=datetime.now(timezone.utc),
            home=home,
            alert_fn=_alert,
            cleanup_fn=_guarded_cleanup,
            scan_fn=_start_scan,
        )
        dm.save_state(spath, state)
        if snapshot is not None:
            self._disk_monitor.last_snapshot = snapshot.to_dict()
        if report is not None:
            self._disk_monitor.last_report = report.to_dict()

    async def _deliver_pulse_items(self) -> None:
        """Deliver pending PULSE items to configured channels for each agent."""
        for agent_name, runner in self._runners.items():
            config = runner.config
            if not config.pulse.enabled or not config.pulse.in_channels:
                continue

            workspace = get_workspace(config.workspace)
            try:
                from olano.pulse.delivery import PulseDelivery
                from olano.pulse.engine import PulseEngine

                engine = await asyncio.to_thread(PulseEngine, config, workspace.state_dir)
                items = await asyncio.to_thread(
                    engine.get_deliverable, max_items=config.pulse.max_items_per_session
                )
                if not items:
                    continue

                delivery = PulseDelivery(engine)

                # Deliver to all channels for this agent
                delivered = await delivery.deliver_to_channels(self.channel_registry, config)
                if delivered:
                    logger.info(
                        "PULSE: delivered %d items across configured channels for %s",
                        delivered,
                        agent_name,
                    )
            except Exception as e:
                logger.debug("PULSE: delivery failed for agent '%s': %s", agent_name, e)

    # ─────────────────────────────────── Cognition drainer ───────────────────

    async def _warm_catalog_snapshots(self) -> None:
        """Stage 1: load the disk-cached catalogs into memory. Milliseconds.

        Every catalog with a persisted snapshot (`<home>/catalog/`) can answer
        correctly from the first request without rebuilding anything, so this
        is cheap enough to await before booting agents. On a cold install the
        snapshots are missing and each read falls through to stage 2's rebuild
        — still correct, just not yet fast.

        Best-effort by construction: each registry's read path already degrades
        to bundled data rather than raising.
        """

        def _load() -> None:
            from olano.archetypes.catalog_index import get_archetype_catalog
            from olano.catalog import models_registry
            from olano.core import mcp_registry

            get_archetype_catalog()
            models_registry.get_model_catalog()
            mcp_registry.get_directory()

        try:
            await asyncio.to_thread(_load)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("Catalog snapshot warm-up skipped: %s", e)

    async def _warm_catalog_caches(self) -> None:
        """Stage 2: pay every remaining cold-cache cost, off the request path.

        Builds the ToolkitAgent index and the tool registry and pre-runs the
        per-model reasoning probes, so the first `/api/integrations`,
        `/api/catalog/tools` and `/api/catalog/providers` requests hit warm
        caches. Runs concurrently with agent boot; the module-level caches it
        fills are all idempotent, so racing a request that builds the same
        thing wastes work but cannot corrupt it. Best-effort: any failure is
        logged at debug and left for the on-demand path to retry.
        """

        def _warm() -> None:
            from olano.archetypes.catalog_index import get_archetype_catalog
            from olano.catalog import models_registry
            from olano.core import mcp_registry
            from olano.subagents.registry import list_toolkit_agents
            from olano.tools.registry import list_tool_names

            # Cheap when stage 1 already loaded a valid snapshot; this is what
            # compiles and persists it on a cold install.
            get_archetype_catalog()
            # The Langfuse SDK (~0.2 s, only when tracing is on), so the first
            # traced turn does not import it on the event loop.
            from olano.observability.tracing import prewarm_if_enabled

            prewarm_if_enabled()
            list_toolkit_agents(catalog_only=True)
            # Load the synced cloud catalogs (models + MCP directory) from disk
            # into the in-process caches — on a cold install this may do one
            # cloud fetch, here in the warm thread instead of a request path.
            models_registry.get_model_catalog()
            mcp_registry.get_directory()
            # Registering every tool means importing every tool module — ~2.5s
            # on a cold process, and boot only imports the modules the booted
            # agents actually use. Without this the bill lands on whoever opens
            # the dashboard's Chat or Tools view first after a restart, on the
            # event loop, stalling every other request behind it.
            list_tool_names()
            # The full /api/catalog/providers payload is dominated on a cold
            # process by the reasoning-capability probes — one langchain chat
            # class instantiated per model name (models/reasoning.py:_probe,
            # lru_cached). Pay them here so the first Chat / Providers /
            # agent-edit view after a restart renders its model pickers in
            # ~0.1s instead of several seconds. Probe exactly the model names
            # the payload assembles: catalog chat models + each provider's
            # static default list (dynamic-discovery providers — ollama,
            # codex, litellm — are probed lazily; their listing can shell out
            # or hit the network, which a warm thread shouldn't).
            from olano.models import PROVIDERS
            from olano.models.reasoning import reasoning_effort_info

            for entry in models_registry.get_model_catalog().get("models", []):
                if entry.get("modality") != "chat":
                    continue
                provider = str(entry.get("provider") or "")
                model = str(entry.get("model") or "")
                if provider and model:
                    reasoning_effort_info(f"{provider}:{model}")
            for key, info in PROVIDERS.items():
                for m in info.default_models:
                    reasoning_effort_info(f"{key}:{m}")

            # Which managed groups the gateway actually serves. One request, and
            # the only place it is paid: the diagnostics endpoint reads the cache
            # so a dashboard load costs nothing. Warming it here is also what
            # gets the WARNING into the boot log on a box whose plan includes a
            # service the gateway does not route — the failure that was
            # previously visible only as an unreadable error in someone's chat.
            from olano.deployment import gateway_probe

            gateway_probe.probe()

        try:
            # Run in a thread — the work is sync, CPU/IO bound, and we don't want
            # to block the event loop while it parses files / builds the index.
            await asyncio.to_thread(_warm)
            logger.debug("Catalog caches warmed (archetypes + integrations + tools).")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("Catalog cache warm-up skipped: %s", e)

    async def _warm_thread_summaries(self) -> None:
        """Summarize every agent's transcripts for the thread list, off the request path.

        The summaries are kept on disk (``runtime/thread_summaries``), so on a
        box that has run this release before the walk reads only transcripts
        that changed while the runtime was down, and the first list request
        after a restart reads none. On the first boot of this release it reads
        the whole history once, here, instead of in the first request a phone
        makes. One agent at a time, in a thread, concurrent with the agent boot.
        """

        def _warm() -> tuple[int, int]:
            from olano.config.home import get_olano_paths
            from olano.runtime import thread_summaries

            sessions = get_olano_paths().sessions_dir
            if not sessions.is_dir():
                return (0, 0)
            agents = read = 0
            for directory in sorted(p for p in sessions.iterdir() if p.is_dir()):
                agents += 1
                read += thread_summaries.warm_directory(directory)
            return (agents, read)

        try:
            agents, read = await asyncio.to_thread(_warm)
            logger.info("Thread summaries warmed: %d agent(s), %d transcript(s) read", agents, read)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("Thread summary warm-up skipped: %s", e)

    async def _cognition_drainer_loop(self) -> None:
        """Run queued ``run_worker`` pulse actions and post follow-up outcome pulses.

        Approving an actionable pulse from a sync surface (dashboard) enqueues
        a job; this loop runs the worker (budget/timeout guarded) and, on success,
        adds a FINDING pulse linking the artifact with a one-click undo action.
        """
        await asyncio.sleep(30)  # let agents stabilize
        wake = self._cognition_drainer_wake
        while True:
            # Cleared BEFORE the drain: an approval that lands while a drain
            # is running sets it again, and the wait below returns at once
            # instead of parking that job until the next 30s tick.
            wake.clear()
            try:
                await self._drain_cognition_jobs()
            except asyncio.CancelledError:
                break  # shutdown, exit quietly with no error log
            except Exception as e:
                logger.debug("Cognition drainer error: %s", e)
            # Every 30s, or at once when an approval queued a job: the person
            # who pressed Approve is watching for it to start.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout=30)

    def wake_cognition_drainer(self) -> None:
        """Run queued Pulse worker jobs now rather than on the next 30s tick."""
        self._cognition_drainer_wake.set()

    async def _drain_cognition_jobs(self) -> None:
        import json

        from olano.cognition import jobs as cortex_jobs
        from olano.cognition.pulse_agent import run_pulse_worker_action
        from olano.pulse.engine import PulseEngine
        from olano.pulse.models import PulseLane, PulsePriority, PulseType

        def _list_jobs(jobs_dir: Path) -> list[Path]:
            return sorted(jobs_dir.glob("*.json")) if jobs_dir.exists() else []

        def _write_job(path: Path, descriptor: dict[str, Any]) -> None:
            path.write_text(json.dumps(descriptor, indent=2), encoding="utf-8")

        async def _run_one(
            config: Any, workspace: Any, descriptor: dict[str, Any], action: dict[str, Any]
        ) -> tuple[Any, str]:
            # A Cortex job, so an approved task is listed in the Runs panel
            # with every step its worker takes, and a promotion approved from
            # Pulse shows on its card like one started from the card.
            promote = str(action.get("type") or "") == "cortex_promote"
            goal = str(action.get("goal") or "").strip()
            async with cortex_jobs.REGISTRY.track(
                agent=config.name,
                workspace_root=workspace.root,
                kind="promote" if promote else "pulse_task",
                target=str((action.get("item_id") if promote else descriptor.get("item_id")) or ""),
                title=(
                    f"Promote (approved): {action.get('item_id')}"
                    if promote
                    else f"Approved task: {goal[:80] or 'worker'}"
                ),
                source="approved",
                detail={"pulse_item_id": descriptor.get("item_id")},
            ) as job_scope:
                outcome, backup_job = await run_pulse_worker_action(config, workspace.root, action)
                job_scope.complete(
                    outcome=outcome.status,
                    summary=outcome.summary or "",
                    error=outcome.error or "",
                    result={"artifacts": outcome.artifacts, "backup_job": backup_job},
                )
                return outcome, backup_job

        for runner in list(self._runners.values()):
            config = runner.config
            workspace = get_workspace(config.workspace)
            jobs_dir = workspace.state_dir / "state" / "cognition" / "jobs"

            for job_file in await asyncio.to_thread(_list_jobs, jobs_dir):
                try:
                    descriptor = json.loads(
                        await asyncio.to_thread(job_file.read_text, encoding="utf-8")
                    )
                except Exception:
                    continue
                if descriptor.get("status") != "queued":
                    continue

                descriptor["status"] = "running"
                await asyncio.to_thread(_write_job, job_file, descriptor)

                action = descriptor.get("action") or {}
                # Its own task, so the job's Stop (which cancels the task the
                # job runs in) ends this one run and not the drainer loop that
                # hosts every agent's approved work until the next restart.
                child = asyncio.get_running_loop().create_task(
                    _run_one(config, workspace, descriptor, action),
                    name=f"cognition-job:{config.name}:{descriptor.get('id')}",
                )
                try:
                    outcome, backup_job = await child
                except asyncio.CancelledError:
                    me = asyncio.current_task()
                    if me is not None and me.cancelling():
                        # Shutdown interrupted this job: mark it cancelled (not a
                        # spurious "error") so it isn't left stuck "running", then
                        # let cancellation propagate to end the drainer loop. The
                        # write stays inline: a task already being cancelled can't
                        # count on completing another await.
                        descriptor["status"] = "cancelled"
                        descriptor["error"] = "Interrupted by shutdown"
                        job_file.write_text(json.dumps(descriptor, indent=2), encoding="utf-8")
                        raise
                    # Only the job was stopped (POST /api/cortex/jobs/{id}/stop).
                    logger.info("Cognition drainer: job %s stopped", descriptor.get("id"))
                    descriptor["status"] = "cancelled"
                    descriptor["error"] = "Stopped from the dashboard"
                    await asyncio.to_thread(_write_job, job_file, descriptor)
                    continue
                except Exception as e:
                    logger.error("Cognition drainer: job %s failed: %s", descriptor.get("id"), e)
                    descriptor["status"] = "error"
                    descriptor["error"] = str(e)
                    await asyncio.to_thread(_write_job, job_file, descriptor)
                    continue

                descriptor["status"] = "done"
                descriptor["artifacts"] = outcome.artifacts
                await asyncio.to_thread(_write_job, job_file, descriptor)

                # Post a follow-up outcome pulse with a one-click undo.
                try:
                    engine = await asyncio.to_thread(PulseEngine, config, workspace.state_dir)
                    undo_action = (
                        {"type": "restore_backup", "job_id": backup_job} if backup_job else None
                    )
                    title = (action.get("goal") or "Task complete")[:80]
                    await asyncio.to_thread(
                        engine.add,
                        content=outcome.summary or "Done.",
                        source="cognition",
                        type=PulseType.FINDING,
                        priority=PulsePriority.NORMAL,
                        lane=PulseLane.PULSE,
                        title=f"Done: {title}",
                        why_now="A background worker you approved finished.",
                        suggested_next_step="Review the artifact; use pulse_undo to revert.",
                        references=outcome.artifacts,
                        action=undo_action,
                        dedup_window_hours=0.0,
                    )
                except Exception as e:
                    logger.debug("Cognition drainer: follow-up pulse failed: %s", e)

                # Live push so an open dashboard surfaces the result the moment
                # the task finishes (not just on its next poll of the queue).
                try:
                    from olano.runtime.api import _push_event as _api_push

                    _api_push(
                        "worker_done",
                        {
                            "agent": config.name,
                            "item_id": descriptor.get("item_id"),
                            "title": (action.get("goal") or "Task complete")[:80],
                            "summary": outcome.summary or "Done.",
                            "references": outcome.artifacts,
                        },
                    )
                except Exception as e:
                    logger.debug("Cognition drainer: worker_done push failed: %s", e)

    def request_prompt_refresh(self, names: list[str] | None = None) -> None:
        """Nudge the prompt refresh sweep to check *names* (``None`` = every
        running agent) on its next debounce window instead of waiting for the
        periodic tick. Cheap and non-blocking; safe from any handler."""
        if names:
            self._prompt_refresh_pending.update(names)
        else:
            self._prompt_refresh_pending.add("*")
        self._prompt_refresh_wake.set()

    async def _hot_swap_agent(self, name: str, *, reason: str) -> dict[str, Any]:
        """Rebuild a running agent's graph from current on-disk config and swap
        it into the live runner — no restart, channels/scheduler untouched.

        The shared rebuild primitive behind ``reload_agent_mcp`` and the prompt
        refresh sweep. Serialized with boot/stop via the per-agent lifecycle
        lock, so a swap can never race a concurrent teardown. The runner's
        prompt-state snapshot is taken BEFORE the build (same reasoning as
        ``_boot_agent_locked``): anything that moves mid-build reads as a
        mismatch on the next sweep tick and converges with one more swap.
        """
        async with self._boot_lock(name):
            runner = self._runners.get(name)
            if runner is None:
                return {"ok": False, "running": False, "reason": "agent not running"}
            try:
                config = await asyncio.to_thread(load_agent_config, name)
                prompt_state = await asyncio.to_thread(compute_prompt_state, config)
                agent = await build_agent_async(config, include_mcp=bool(config.mcp_servers))
            except Exception as exc:
                logger.warning("agent rebuild (%s) failed for %s: %s", reason, name, exc)
                return {"ok": False, "running": True, "reason": str(exc)}
            runner.set_agent(agent, config=config, prompt_state=prompt_state)
            return {"ok": True, "running": True}

    async def _prompt_refresh_loop(self) -> None:
        """Detect stable prompt-input changes and hot-swap rebuilt graphs.

        Each tick (an event nudge + 5s debounce, or the periodic interval)
        every candidate agent's stable prompt sections are recomputed from
        disk + the live peer registry (cheap — file reads only, no MCP) and
        compared against the snapshot taken when its current graph was built.
        On mismatch the agent is rebuilt and hot-swapped; existing threads
        then receive a one-shot update notice on their next turn
        (``AgentRunner._prompt_change_notice``). Settings are re-read every
        tick so ``config.yaml → prompt_refresh`` changes apply within one
        interval without a restart. Rebuilds are serialized (one agent at a
        time) — a roster change after an agent boots legitimately rebuilds
        every peer once, and doing so one-by-one keeps the loop from stacking
        MCP handshakes.
        """
        from olano.config.system_settings import load_prompt_refresh_settings

        try:
            from olano.runtime.api import _push_event as _prompt_push
        except Exception:  # pragma: no cover - defensive
            _prompt_push = None  # type: ignore[assignment]

        settings = await asyncio.to_thread(load_prompt_refresh_settings)
        while True:
            try:
                # One settings read per tick (below, after the wake) serves both
                # that sweep and the next wait's interval.
                interval = max(60, settings.check_interval_minutes * 60)
                woke = True
                try:
                    await asyncio.wait_for(self._prompt_refresh_wake.wait(), timeout=interval)
                except TimeoutError:
                    woke = False
                if woke:
                    # Debounce: let a burst of nudges (config save + roster
                    # broadcast, several boots) coalesce into one sweep.
                    await asyncio.sleep(5)
                self._prompt_refresh_wake.clear()
                pending = self._prompt_refresh_pending
                self._prompt_refresh_pending = set()

                settings = await asyncio.to_thread(load_prompt_refresh_settings)
                if not settings.enabled or self._agents_booting:
                    continue

                if woke and "*" not in pending:
                    names = [n for n in pending if n in self._runners]
                else:
                    names = list(self._runners.keys())

                for name in names:
                    if self.agent_boot_in_flight(name):
                        continue
                    try:
                        await self._prompt_refresh_check(name, push_event=_prompt_push)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.debug("prompt refresh check failed for %s: %s", name, e)
            except asyncio.CancelledError:
                break  # shutdown — exit quietly, no error log
            except Exception as e:
                logger.debug("prompt refresh loop error: %s", e)

    async def _prompt_refresh_check(
        self, name: str, push_event: Any | None = None
    ) -> dict[str, Any]:
        """Compare one agent's on-disk stable sections against its running
        graph's snapshot; hot-swap on mismatch. Returns
        ``{ok, rebuilt, changed: [...]}`` (also used by the manual
        ``POST /api/agents/{name}/prompt/refresh`` endpoint)."""
        runner = self._runners.get(name)
        if runner is None:
            return {"ok": False, "rebuilt": False, "changed": [], "reason": "agent not running"}
        baseline = runner.prompt_state()
        baseline_fp = dict(getattr(baseline, "fingerprint", None) or {})
        if not baseline_fp:
            # Feature inert for this agent (non-`full` prompt mode, or the
            # snapshot compute failed) — never treat "unknown" as "changed".
            return {"ok": True, "rebuilt": False, "changed": []}
        config = await asyncio.to_thread(load_agent_config, name)
        fresh = await asyncio.to_thread(compute_prompt_state, config)
        if not fresh.fingerprint:
            return {"ok": True, "rebuilt": False, "changed": []}
        moved = sorted(
            key
            for key, digest in fresh.fingerprint.items()
            if baseline_fp.get(key, digest) != digest
        )
        added = sorted(key for key in fresh.fingerprint if key not in baseline_fp)
        changed = moved + added
        if not changed:
            return {"ok": True, "rebuilt": False, "changed": []}
        result = await self._hot_swap_agent(name, reason="prompt_refresh")
        if not result.get("ok"):
            return {
                "ok": False,
                "rebuilt": False,
                "changed": changed,
                "reason": result.get("reason", "rebuild failed"),
            }
        logger.info("prompt refresh: rebuilt %s (changed: %s)", name, ", ".join(changed))
        if push_event is not None:
            try:
                push_event("prompt_updated", {"agent": name, "changed": changed})
            except Exception:  # pragma: no cover - defensive
                pass
        return {"ok": True, "rebuilt": True, "changed": changed}

    async def _report_runtime_version(self) -> None:
        """One-shot: report this runtime's build identity to the control plane.

        Off the boot path (a task, and the POST runs in a thread) because a slow
        or unreachable control plane must cost nothing here. Best-effort by
        construction — the daily disk report carries the same field, so a lost
        boot report heals within a day without a restart.
        """
        from olano.deployment.version_reporter import report_runtime_version

        try:
            await asyncio.to_thread(report_runtime_version)
        except Exception:  # pragma: no cover - reporting must never break a boot
            logger.debug("runtime version report failed", exc_info=True)

    async def _gateway_enrollment_loop(self) -> None:
        """Own gateway-key enrollment, so no agent build ever waits on the cloud.

        Enrollment used to run inline in ``_build_gateway_model``: the first
        olano-tier model built on a keyless box made a blocking POST to the
        control plane, bounded at 10s. When that endpoint degraded, every agent
        build and every ``/model`` switch paid the timeout, and the retry cadence
        was whatever the build traffic happened to be.

        With this loop the build path is pure cache and the network call has
        exactly one owner. Pacing is NOT this loop's job — the module's
        exponential backoff (60s → 30 min) gates the real attempts, so a tight
        poll here costs nothing while the control plane is down.
        """
        from olano.deployment.gateway_key import (
            enroll_gateway_key,
            enroll_via_settings_channel,
            enrollment_due,
            enrollment_state,
            set_background_enrollment,
            wait_for_enrollment_request,
        )
        from olano.deployment.sources import deployment_token

        poll_seconds = 20.0

        if not await asyncio.to_thread(deployment_token):
            # Unattached box: there is no credential to enroll with, and
            # `_build_gateway_model` already raises an actionable error. Leave
            # the inline path in place rather than pretending to own it.
            return

        set_background_enrollment(True)
        try:
            while True:
                try:
                    # Non-blocking drain: tells us a build actually wanted a key
                    # since the last tick, and stops the flag latching forever.
                    wanted = wait_for_enrollment_request(0)
                    if enrollment_state() != "enrolled" and enrollment_due():
                        if wanted:
                            logger.debug(
                                "gateway enrollment: an agent build asked for a key; attempting"
                            )
                        key = await asyncio.to_thread(enroll_gateway_key)
                        if key is None and enrollment_state() != "enrolled":
                            # The direct POST failed and the box still has no
                            # key. The settings GET is the channel that stays up
                            # when that endpoint doesn't (workbuddy 2026-08-16:
                            # the POST 504'd for days while every settings poll
                            # succeeded) — ask it now instead of waiting up to
                            # 15 minutes for the ordinary poll. Adoption, when
                            # the control plane delivers, happens inside.
                            await asyncio.to_thread(enroll_via_settings_channel)
                except asyncio.CancelledError:
                    raise
                except Exception:  # pragma: no cover - the loop must not die
                    logger.debug("gateway enrollment sweep failed", exc_info=True)
                await asyncio.sleep(poll_seconds)
        finally:
            # Hand ownership back, so anything still running in this process
            # (a CLI path, a test) falls back to enrolling inline.
            set_background_enrollment(False)

    async def _selfhost_relay_loop(self) -> None:
        """Keep a self-hosted box enrolled with the MCP sign-in relay.

        Each tick is a cheap local check (``enrollment_due``: an env read, the
        memoised account key, one vault read); the network call happens only
        when the box has no current enrollment, when its daily refresh is due,
        or right after a key is pasted (``request_enrollment`` wakes the loop).
        Pacing on failure is the module's own exponential backoff, so a cloud
        outage costs one bounded POST per window. Never raises.
        """
        from olano.core import mcp_relay_selfhost

        poll_seconds = 60.0
        while True:
            try:
                if await asyncio.to_thread(mcp_relay_selfhost.enrollment_due):
                    await asyncio.to_thread(mcp_relay_selfhost.maybe_enroll)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - the loop must not die
                logger.debug("self-hosted relay enrollment sweep failed", exc_info=True)
            # Sleep on the loop (never a blocked thread, which would hold up
            # shutdown), waking early when a key was just pasted.
            waited = 0.0
            while waited < poll_seconds and not mcp_relay_selfhost.take_request():
                await asyncio.sleep(5.0)
                waited += 5.0

    async def run_checkpoint_gc(self, *, dry_run: bool = False) -> dict[str, Any] | None:
        """One checkpoint history sweep; the report, or ``None`` on sqlite.

        Shared by the loop below and the admin ``POST
        /api/diagnostics/checkpoints/gc`` route. The last report is kept on the
        runtime for ``GET /api/diagnostics/checkpoints``.
        """
        from olano.config.loader import list_agents
        from olano.config.system_settings import load_checkpoint_gc_settings
        from olano.core import checkpoint_gc
        from olano.core.checkpoints import aget_postgres_saver

        settings = await asyncio.to_thread(load_checkpoint_gc_settings)
        saver = await aget_postgres_saver()
        pool = getattr(saver, "conn", None) if saver is not None else None
        if pool is None:
            return None
        try:
            live_agents: set[str] | None = set(
                await asyncio.to_thread(list_agents, include_hidden=True)
            )
        except Exception:
            live_agents = None
        report = await checkpoint_gc.collect(
            pool,
            keep_min=settings.keep_min,
            dry_run=dry_run,
            max_seconds=settings.max_minutes * 60,
            saver=saver,
            live_agents=live_agents,
        )
        if report.compacted:
            logger.info(
                "checkpoint gc: wrote a snapshot for %d channel(s) that never had one: %s",
                len(report.compacted),
                ", ".join(report.compacted[:10]),
            )
        payload = report.as_dict()
        payload["finished_at"] = time.time()
        self._checkpoint_gc_last = payload
        freed = ""
        if report.bytes_before is not None and report.bytes_after is not None:
            freed = f", {(report.bytes_before - report.bytes_after) / 1e6:.0f} MB freed"
        logger.info(
            "checkpoint gc%s: %d of %d namespace(s) swept, %d checkpoint(s), %d write(s), "
            "%d blob(s) removed in %.1fs%s%s",
            " (dry run)" if dry_run else "",
            report.namespaces_swept,
            report.namespaces_scanned,
            report.checkpoints_removed,
            report.writes_removed,
            report.blobs_removed,
            report.seconds,
            freed,
            " (budget spent, continuing next pass)" if report.stopped_early else "",
        )
        return payload

    async def _demo_visitor_sweep_loop(self) -> None:
        """Hourly ``demo_access.asweep_visitors`` on the demo box; nothing elsewhere."""
        from olano.runtime import demo_access

        if demo_access.demo_account_email() is None:
            return
        while True:
            try:
                await demo_access.asweep_visitors()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.warning("demo visitor sweep failed", exc_info=True)
            await asyncio.sleep(3600)

    async def _checkpoint_gc_loop(self) -> None:
        """Sweep dead checkpoint history on a timer (``config.yaml → checkpoint_gc``).

        The first pass waits ``boot_delay_minutes`` after the agents boot so it
        never competes with the boot for the database; later passes run every
        ``interval_hours``. ``LIONCLAW_NO_CHECKPOINT_GC=1`` skips the loop for
        one boot. Postgres only: the sqlite backend returns at once.
        """
        if (os.environ.get("LIONCLAW_NO_CHECKPOINT_GC") or "").strip() in {"1", "true", "yes"}:
            logger.info("checkpoint gc disabled (LIONCLAW_NO_CHECKPOINT_GC)")
            return
        from olano.config.system_settings import load_checkpoint_gc_settings

        settings = await asyncio.to_thread(load_checkpoint_gc_settings)
        await asyncio.sleep(max(0, settings.boot_delay_minutes) * 60)
        while True:
            try:
                settings = await asyncio.to_thread(load_checkpoint_gc_settings)
                due = settings.enabled and not self._agents_booting
                if due and await self.run_checkpoint_gc() is None:
                    return  # sqlite: nothing to sweep, ever
            except asyncio.CancelledError:
                break
            except Exception:
                logger.warning("checkpoint gc pass failed", exc_info=True)
            await asyncio.sleep(max(1, settings.interval_hours) * 3600)

    async def _mcp_health_loop(self) -> None:
        """Keep MCP connections alive without a human in the loop.

        Each tick renews every stored OAuth grant that is close to expiry and
        re-connects servers whose last build left them timed-out or errored
        (with per-server exponential backoff). Both halves exist because the
        reactive paths are not enough on their own: a token is otherwise only
        renewed by the next request that needs one — which for an idle agent
        may be after the grant is unrecoverable — and a server that failed at
        boot is otherwise skipped for the whole process lifetime.

        Settings are re-read every tick so dashboard/CLI changes apply within
        one interval without a restart. ``LIONCLAW_NO_MCP_HEALTH=1`` skips the
        loop entirely for one boot.
        """
        if (os.environ.get("LIONCLAW_NO_MCP_HEALTH") or "").strip() in {"1", "true", "yes"}:
            logger.info("MCP health sweep disabled (LIONCLAW_NO_MCP_HEALTH)")
            return

        from olano.config.system_settings import load_mcp_health_settings
        from olano.core import mcp_health
        from olano.core.builder import get_mcp_summary

        try:
            from olano.runtime.api import _push_event as _mcp_push
        except Exception:  # pragma: no cover - defensive
            _mcp_push = None  # type: ignore[assignment]

        await asyncio.sleep(90)  # let the boot sweep finish before judging it
        while True:
            try:
                # Before the settings gate on purpose: the reap is housekeeping
                # for the containers the sessions below leave behind, and a
                # box that switched the health sweep off still leaks them.
                await self._reap_ephemeral_containers("mcp health tick")
                settings = await asyncio.to_thread(load_mcp_health_settings)
                if not settings.enabled or self._agents_booting:
                    await asyncio.sleep(max(60, settings.check_interval_minutes * 60))
                    continue

                entries: dict[str, list[dict[str, Any]]] = {}
                summaries: dict[str, list[dict[str, Any]]] = {}
                for name in list(self._runners.keys()):
                    if self.agent_boot_in_flight(name):
                        continue
                    try:
                        config = await asyncio.to_thread(load_agent_config, name)
                    except Exception as exc:
                        logger.debug("MCP health: config load failed for %s: %s", name, exc)
                        continue
                    if not config.mcp_servers:
                        # Nothing to maintain, but a server removed since the
                        # last report may still have an alert up: clear it.
                        _report_mcp_auth_required(name, [])
                        continue
                    entries[name] = [srv.model_dump() for srv in config.mcp_servers]
                    summaries[name] = get_mcp_summary(name)

                if entries:
                    reports = await mcp_health.maintain(
                        entries,
                        summaries,
                        refresh_tokens=settings.refresh_tokens,
                        refresh_window=settings.refresh_window_minutes * 60,
                        recover=settings.auto_reconnect,
                    )
                    self._mcp_health = {name: r.to_dict() for name, r in reports.items()}
                    self._mcp_health_checked_at = time.time()
                    await self._apply_mcp_health(reports, push_event=_mcp_push)
                else:
                    self._mcp_health = {}
                    self._mcp_health_checked_at = time.time()
                await asyncio.to_thread(_prune_orphaned_mcp_auth_alerts)

                await asyncio.sleep(max(60, settings.check_interval_minutes * 60))
            except asyncio.CancelledError:
                break  # shutdown — exit quietly, no error log
            except Exception as e:
                logger.debug("MCP health loop error: %s", e)
                await asyncio.sleep(300)

    async def _apply_mcp_health(
        self, reports: dict[str, Any], *, push_event: Any | None = None
    ) -> None:
        """Act on a sweep: settle the bell, then reconnect what asked for it.

        Reloads are serialized on purpose: each one rebuilds an agent's graph,
        and the recovery sweep can legitimately want several at once after a
        shared outage. The per-server backoff was already recorded when the
        report was built, so a reload that fails again simply waits longer next
        time. The notification pass runs over *every* report, including the
        agents with nothing to reconnect — that is what takes a row back down
        once a server is healthy again.
        """
        for name, report in reports.items():
            # Ring (and un-ring) before the reloads, on every report: the sweep
            # is the one pass that sees every configured server on every agent,
            # including the ones it has decided not to touch. A server it means
            # to reconnect is not yet a re-authorization case, and if the
            # reconnect below succeeds the resolve on the next tick takes the
            # row back off the bell.
            _report_mcp_auth_required(name, [s.to_dict() for s in report.servers])
            if not report.needs_reload:
                continue
            logger.info("MCP health: reconnecting %s (%s)", name, ", ".join(report.reload_urls[:3]))
            try:
                result = await self.reload_agent_mcp(name, reset_backoff=False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("MCP health: reconnect failed for %s: %s", name, exc)
                continue
            if not result.get("ok"):
                continue
            # Refresh the stored snapshot so the diagnostics endpoint reflects
            # the post-reload truth rather than the state that triggered it.
            from olano.core.builder import get_mcp_summary

            recovered = [
                row.get("url")
                for row in get_mcp_summary(name)
                if row.get("ok") and row.get("url") in set(report.reload_urls)
            ]
            if recovered:
                from olano.core import mcp_health

                for url in recovered:
                    mcp_health.reset_recovery_for(name, str(url))
                logger.info("MCP health: %s recovered %s", name, ", ".join(map(str, recovered)))
                if push_event is not None:
                    try:
                        push_event("mcp_recovered", {"agent": name, "servers": recovered})
                    except Exception:  # pragma: no cover - defensive
                        pass

    def mcp_health_snapshot(self) -> dict[str, Any]:
        """Last MCP health sweep, for ``GET /api/diagnostics/mcp``."""
        from olano.backends.lionclaw_docker import last_reap_report

        return {
            "checked_at": self._mcp_health_checked_at,
            "agents": dict(self._mcp_health),
            # What the ephemeral-container reaper last found and removed — on a
            # box nobody can SSH into, the only place a stdio-MCP leak is
            # visible before it fills the bridge.
            "ephemeral_containers": last_reap_report(),
        }

    async def _reap_ephemeral_containers(self, reason: str) -> None:
        """Remove tool-server containers no live session can own (boot + each health tick).

        Off the loop, never raises, and skippable for one boot with
        ``LIONCLAW_NO_EPHEMERAL_REAP=1`` — the module logs what it did.
        """
        if (os.environ.get("LIONCLAW_NO_EPHEMERAL_REAP") or "").strip() in {"1", "true", "yes"}:
            return
        try:
            from olano.backends.lionclaw_docker import reap_ephemeral_containers

            await asyncio.to_thread(reap_ephemeral_containers, reason=reason)
        except Exception as exc:
            logger.debug("ephemeral container reap (%s) skipped: %s", reason, exc)
        if reason == "boot":
            # Live browser containers: sessions are in-memory, so any that
            # survived the previous process belong to nobody. Boot only: on a
            # health tick a session can be mid-open (container up, session not
            # yet registered), and reaping it would cut that agent's browser.
            try:
                from olano.browser import sidecar

                if sidecar.docker_available():
                    await asyncio.to_thread(sidecar.reap_orphans)
            except Exception as exc:
                logger.debug("browser container reap skipped: %s", exc)

    async def reload_agent_mcp(self, name: str, *, reset_backoff: bool = True) -> dict[str, Any]:
        """Rebuild a running agent's MCP tools + system prompt and hot-swap them
        into the live runner — no full restart, channels/scheduler untouched.

        Picks up newly-authorized OAuth servers, enable/disable toggles, and
        servers that were unreachable at boot. Queues a one-shot online/offline
        note so an in-flight conversation learns its toolset changed on its next
        turn. Returns the post-reload per-server summary.

        *reset_backoff* clears the health sweep's per-server retry backoff, so a
        human-initiated reload is always immediate. The sweep's own reconnects
        pass False — they recorded that attempt when they planned it, and must
        not reset the schedule they are riding on.
        """
        from olano.core import mcp_health
        from olano.core.builder import get_mcp_summary
        from olano.core.mcp_setup import clear_boot_failures

        runner = self._runners.get(name)
        if runner is None:
            return {"ok": False, "running": False, "reason": "agent not running"}

        # A reload IS the retry. The boot failure memo exists to stop one dead
        # server costing every agent its own 35s discovery during a boot sweep —
        # never to argue with someone who just fixed the server and asked again.
        if dropped := clear_boot_failures():
            logger.debug("MCP reload for %s: dropped %d remembered failure(s)", name, dropped)
        if reset_backoff:
            mcp_health.reset_recovery(name)

        before = {s.get("url"): s for s in get_mcp_summary(name)}
        result = await self._hot_swap_agent(name, reason="mcp_reload")
        if not result.get("ok"):
            if result.get("running"):
                logger.warning("MCP reload failed for %s: %s", name, result.get("reason"))
            return result

        runner = self._runners.get(name) or runner
        after_summary = get_mcp_summary(name)
        after = {s.get("url"): s for s in after_summary}

        # The reload is what a human does right after clicking Authorize, so
        # settle the bell against its result rather than leaving a stale
        # "needs re-authorization" row up until the next sweep — a fix that
        # still shows as broken is indistinguishable from one that didn't work.
        _report_mcp_auth_required(name, after_summary)

        came_online = [
            s for url, s in after.items() if s.get("ok") and not before.get(url, {}).get("ok")
        ]
        went_offline = [
            before.get(url, s)
            for url, s in after.items()
            if not s.get("ok") and before.get(url, {}).get("ok")
        ]
        notice = _format_mcp_notice(came_online, went_offline)
        if notice:
            runner.set_mcp_notice(notice)
        logger.info(
            "MCP reload for %s: %d connected, %d came online, %d went offline",
            name,
            sum(1 for s in after_summary if s.get("ok")),
            len(came_online),
            len(went_offline),
        )
        return {"ok": True, "running": True, "servers": after_summary, "notice": notice}

    # ─────────────────────────────────── Accessors ───────────────────────────

    def get_runner(self, name: str) -> AgentRunner | None:
        return self._runners.get(name)

    def list_agents(self) -> list[dict]:
        return [e.to_dict() for e in self.registry.list_all()]


def _is_group_message(msg: Any) -> bool:
    raw = getattr(msg, "raw", None)
    if raw is None:
        return bool(getattr(msg, "metadata", {}).get("is_group", False))

    effective_chat = getattr(raw, "effective_chat", None)
    chat_type = getattr(effective_chat, "type", None)
    if chat_type in {"group", "supergroup"}:
        return True

    if getattr(raw, "guild", None) is not None:
        return True

    return isinstance(raw, dict) and raw.get("channel_type") in {"channel", "group", "mpim"}
