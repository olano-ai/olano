"""MCP OAuth client secrets live in the vault, never in ``agent.json5``.

The sibling of :mod:`olano.config.channel_secrets`, for the one MCP credential
that is a real secret: the **client secret** of an OAuth app the user created
for a server that refuses Dynamic Client Registration (HubSpot). It reaches a
config from four directions — the magic-link credentials form, the dashboard's
server editor and its Authorize dialog, AgentFather's ``olano_add_mcp_server``,
and the flow's own back-fill after a successful sign-in — and every one of them
used to leave it in plain text on disk, in every backup and every config
export, for as long as the server was configured.

Channels get a **canonical key per connector type** and the field is dropped
from the config. An MCP server has no type, so the key is derived from the
**server URL** — ``https://mcp.hubspot.com`` → ``MCP_OAUTH_CLIENT_SECRET_MCP_HUBSPOT_COM``
— stored as a **per-agent override** (two agents, two HubSpot apps, one name),
and the entry keeps an explicit ``${vault:KEY}`` reference, exactly the shape
MCP ``headers`` and stdio ``env`` already use for their credentials. Explicit
rather than by-convention because a server entry is edited by hand far more
than a channel entry is, and a visible reference is the reader's only clue
that the vault is involved.

Two halves, both owned here:

* **Write** (:func:`absorb_mcp_client_secrets`) — a literal secret in any
  ``mcp_servers`` entry is written to the vault and replaced by its reference.
  Wired into every config writer: ``loader.save_agent_config`` (the model
  path: ``mutate_agent_config``, ``admin.update_agent``, ``add_mcp_server``,
  the magic-link form), ``runtime/api._write_agent_config`` (the dashboard's
  raw-dict path) and ``mcp_setup._persist_oauth_auth_mode`` (the flow's
  back-fill). Idempotent: a reference is never absorbed again.
* **Read** — ``mcp_setup.resolve_client_credentials`` resolves the reference
  with per-agent precedence at every consumer (runtime connect, probe,
  interactive flow, the health sweep's token refresh), the way
  ``agent_secret_headers`` does for headers.

A locked vault is not an error and never loses data: the literal stays where it
was and is reported, so the next save (or migration ``0019``) finishes the job.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from olano.config.channel_secrets import is_reference

logger = logging.getLogger(__name__)

#: The config field this module owns.
SECRET_FIELD = "oauth_client_secret"

KEY_PREFIX = "MCP_OAUTH_CLIENT_SECRET_"

#: Vault names elsewhere (the secret-entry magic link) are capped at 64 chars;
#: a longer URL is truncated and disambiguated with a short digest.
MAX_KEY_LENGTH = 64


def _normalized(url: str) -> str:
    return str(url or "").strip().lower().rstrip("/")


def client_secret_key(url: str) -> str:
    """The vault key an MCP server's client secret lives under.

    Deterministic per URL (case and a trailing slash ignored), readable, and
    unique per server: host **and path** are both in the name, so two Composio
    instances on one host never share a secret. ``https://mcp.hubspot.com`` →
    ``MCP_OAUTH_CLIENT_SECRET_MCP_HUBSPOT_COM``; ``https://mcp.notion.com/mcp``
    → ``MCP_OAUTH_CLIENT_SECRET_MCP_NOTION_COM_MCP``.
    """
    normalized = _normalized(url)
    parsed = urlparse(normalized)
    stem = f"{parsed.hostname or ''}{parsed.path or ''}" if parsed.scheme else normalized
    body = re.sub(r"[^A-Z0-9]+", "_", stem.upper()).strip("_") or "SERVER"
    key = KEY_PREFIX + body
    if len(key) > MAX_KEY_LENGTH:
        digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:6].upper()
        key = key[: MAX_KEY_LENGTH - len(digest) - 1].rstrip("_") + "_" + digest
    return key


def reference_for(url: str) -> str:
    """The config value that stands in for a vaulted client secret."""
    return f"${{vault:{client_secret_key(url)}}}"


@dataclass
class McpSecretReport:
    """What one absorb pass did."""

    # (server url, vault key) pairs moved into the vault this pass.
    vaulted: list[tuple[str, str]] = field(default_factory=list)
    # Server urls whose literal secret stayed because the vault could not take it.
    left_in_config: list[str] = field(default_factory=list)
    vault_available: bool = True

    @property
    def changed(self) -> bool:
        return bool(self.vaulted)


def _literal_entries(data: dict[str, Any]) -> list[tuple[dict[str, Any], str, str]]:
    """``(entry, url, secret)`` for every server holding a literal secret."""
    servers = data.get("mcp_servers")
    if not isinstance(servers, list):
        return []
    found: list[tuple[dict[str, Any], str, str]] = []
    for entry in servers:
        if not isinstance(entry, dict):
            continue
        value = entry.get(SECRET_FIELD)
        url = str(entry.get("url") or "").strip()
        if not url or not isinstance(value, str) or not value.strip() or is_reference(value):
            continue
        found.append((entry, url, value.strip()))
    return found


def absorb_mcp_client_secrets(
    agent: str,
    data: dict[str, Any],
    vault: Any = None,
    *,
    dry_run: bool = False,
) -> McpSecretReport:
    """Move literal client secrets out of ``data["mcp_servers"]`` into the vault.

    Each literal is written as a **per-agent override** under
    :func:`client_secret_key` and the field is replaced by ``${vault:KEY}``
    in place. A value that is already a ``${...}`` reference — this module's
    own, or one the operator pointed elsewhere — is left exactly as written.

    Calling this twice is a no-op. A locked vault, or a write the vault
    refuses, leaves the literal untouched and reports it.
    """
    report = McpSecretReport()
    literals = _literal_entries(data)
    if not literals:
        return report
    if dry_run:
        report.vaulted = [(url, client_secret_key(url)) for _, url, _ in literals]
        return report

    if vault is None:
        from olano.config.channel_secrets import _resolve_vault

        vault = _resolve_vault()
    if vault is None:
        report.vault_available = False
        report.left_in_config = [url for _, url, _ in literals]
        logger.warning(
            "Vault unavailable — %d MCP client secret(s) for agent '%s' stay in "
            "agent.json5 as plain text. Unlock the vault and re-save to secure them.",
            len(literals),
            agent,
        )
        return report

    for entry, url, secret in literals:
        key = client_secret_key(url)
        try:
            vault.set(
                name=key,
                value=secret,
                description=f"OAuth client secret for MCP server {url} (agent '{agent}')",
                agent=agent,
            )
        except Exception as exc:
            logger.warning(
                "Could not vault %s for agent '%s' (%s) — leaving it in agent.json5.",
                key,
                agent,
                exc,
            )
            report.left_in_config.append(url)
            continue
        entry[SECRET_FIELD] = f"${{vault:{key}}}"
        report.vaulted.append((url, key))

    if report.vaulted:
        logger.info(
            "Vaulted %d MCP client secret(s) for agent '%s': %s",
            len(report.vaulted),
            agent,
            ", ".join(sorted(k for _, k in report.vaulted)),
        )
    return report


def absorb_mcp_client_secrets_quietly(agent: str, data: dict[str, Any]) -> McpSecretReport:
    """:func:`absorb_mcp_client_secrets` that never raises.

    For the config writers on the save path: a vault hiccup must cost the
    secret its vaulting (it stays in the file, reported), never the save.
    """
    try:
        return absorb_mcp_client_secrets(agent, data)
    except Exception:  # pragma: no cover - defensive
        logger.warning("MCP client-secret absorb failed for agent '%s'", agent, exc_info=True)
        return McpSecretReport(vault_available=False)
