"""Agent-facing tools for the encrypted credentials vault.

These tools read/write the AES-256-GCM SQLite store at ~/.olano/vault/vault.db
(distinct from the knowledge-vault notes tools, which manipulate Markdown files).

The agent does NOT receive raw credential values:
- `secret_list` returns names + metadata only.
- `secret_get` returns a masked preview by default; full values are released
  only via the security middleware's owner-only HITL path.
- `secret_set` lets the user store a credential by name without exposing it
  to model context (the `value` argument is redacted by the security middleware).
"""

from __future__ import annotations

import logging

from langchain_core.tools import tool

from olano.security.tool_metadata import tool_metadata

logger = logging.getLogger(__name__)


def _live_vault():
    """Return the process-wide unlocked VaultStore, or None if not unlocked."""
    from olano.vault.resolver import get_vault

    return get_vault()


def _mask(value: str) -> str:
    if not value:
        return "****"
    if len(value) <= 4:
        return "****"
    return value[:4] + "*" * (len(value) - 4)


@tool_metadata(capability="read_only", risk="high", requires_hitl=True, owner_only=True)
@tool
def secret_get(name: str, reveal: bool = False) -> str:
    """Read a credential from the encrypted vault by name.

    Returns a masked preview by default (e.g. 'PKBM************'). Pass
    `reveal=True` to request the full value — that path is owner-only and
    requires HITL approval before the value reaches the agent.

    Use this for API keys, tokens, and other secrets stored via
    `olano vault set NAME` or the `secret_set` tool.
    """
    from olano.vault.reserved import RUNTIME_SECRET_REFUSAL, is_runtime_secret

    if is_runtime_secret(name):
        return f"'{name}': {RUNTIME_SECRET_REFUSAL}"
    vault = _live_vault()
    if vault is None:
        return (
            "Credentials vault is not unlocked in this session. "
            "Run `olano vault init` to create it, then set OLANO_VAULT_KEY "
            "or install the `keyring` package so the chat session can unlock it."
        )
    try:
        value = str(vault.get(name))
    except Exception as exc:
        return f"Could not read secret '{name}': {exc}"

    if reveal:
        return value
    return f"'{name}' (masked): {_mask(value)} — pass reveal=True to fetch the full value."


@tool_metadata(capability="read_only", risk="low", requires_hitl=False, owner_only=False)
@tool
def secret_list(category: str | None = None, agent: str | None = None) -> str:
    """List names of credentials in the encrypted vault (no values returned).

    Use this to check whether a given credential (e.g. ALPACA_PAPER_TRADING_API_KEY)
    is already stored before asking the user to share it.

    Pass `agent` to list that agent's **per-agent overrides** instead of the
    shared values — that is how you verify a connector credential landed on the
    right agent (e.g. `secret_list(agent="support-bot")` after storing its
    TELEGRAM_BOT_TOKEN).
    """
    vault = _live_vault()
    if vault is None:
        return (
            "Credentials vault is not unlocked in this session. "
            "Run `olano vault status` to inspect it."
        )
    from olano.vault.reserved import without_runtime_secrets

    try:
        entries = without_runtime_secrets(
            vault.list_overrides(agent=agent) if agent else vault.list_names(category=category)
        )
    except Exception as exc:
        return f"Could not list secrets: {exc}"
    if not entries:
        if agent:
            return f"No per-agent credential overrides stored for '{agent}'."
        scope = f" in category '{category}'" if category else ""
        return f"No credentials stored{scope}."
    lines = []
    for entry in entries:
        scope = f"agent:{agent}" if agent else f"category: {entry.get('category', 'general')}"
        line = f"- {entry['name']}  ({scope}"
        if entry.get("last_used"):
            line += f", last used {entry['last_used'][:10]}"
        line += ")"
        lines.append(line)
    return "\n".join(lines)


@tool_metadata(capability="mutating", risk="high", requires_hitl=True, owner_only=True)
@tool
def secret_set(
    name: str,
    value: str,
    description: str = "",
    category: str = "general",
    agent: str = "",
) -> str:
    """Store a credential in the encrypted vault.

    Prefer asking the user to run `olano vault set NAME` themselves so the
    plaintext never enters the chat transcript. Only call this tool when the
    user has explicitly pasted a credential they want stored — and even then,
    the security middleware will redact the value from logs and require
    owner-only HITL approval before it reaches the vault.

    `agent` stores a **per-agent override** instead of the shared value. Use it
    for anything one agent must not share with another — above all connector
    credentials: every agent reads the same key name (TELEGRAM_BOT_TOKEN), and
    the per-agent override is what makes them different bots. Two agents left
    on one shared token authenticate as the same bot and one of them silently
    stops receiving messages.
    """
    vault = _live_vault()
    if vault is None:
        scope = f" --agent {agent}" if agent else ""
        return (
            "Credentials vault is not unlocked. Ask the user to run "
            f"`olano vault set {name}{scope}` from a terminal instead."
        )
    try:
        vault.set(name, value, description=description, category=category, agent=agent or None)
    except Exception as exc:
        return f"Could not store secret '{name}': {exc}"
    if agent:
        return f"Stored '{name}' as a per-agent override for '{agent}'."
    return f"Stored '{name}' in the credentials vault (category: {category})."


_SECRET_TOOL_NAMES = ["secret_get", "secret_list", "secret_set"]


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
