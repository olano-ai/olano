"""Move every MCP OAuth client secret out of ``agent.json5`` and into the vault.

The sibling of ``0007`` for the one MCP credential that is a real secret: the
client secret of an OAuth app created for a server that refuses Dynamic Client
Registration (HubSpot). Every writer now vaults it on save
(``config/mcp_secrets.py``), storing it as a per-agent override under a key
named after the server (``MCP_OAUTH_CLIENT_SECRET_MCP_HUBSPOT_COM``) and
leaving a ``${vault:KEY}`` reference on the entry — the shape MCP ``headers``
and ``env`` credentials already take. This sweep does the same for the entries
that were written before that.

Idempotent: a reference is never absorbed twice. Safe when the vault is locked —
literals are left in place and reported, and the next save of that agent's
config finishes the job.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

ID = "0019_mcp_client_secrets_to_vault"


def apply(home: Path) -> None:
    from olano.config.loader import _load_json5, write_agent_config_text
    from olano.config.mcp_secrets import absorb_mcp_client_secrets

    agents_dir = home / "agents"
    if not agents_dir.is_dir():
        return

    for config_path in sorted(agents_dir.glob("*/agent.json5")):
        agent = config_path.parent.name
        try:
            data = _load_json5(config_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            logger.warning("%s: skipping unparseable %s: %s", ID, config_path, exc)
            continue
        if not isinstance(data.get("mcp_servers"), list):
            continue

        try:
            report = absorb_mcp_client_secrets(agent, data)
        except Exception as exc:  # a vault hiccup must not abort the upgrade
            logger.warning("%s: could not process %s: %s", ID, config_path, exc)
            continue

        if not report.changed:
            if report.left_in_config:
                logger.warning(
                    "%s: vault unavailable — %d MCP client secret(s) stay in %s as plain "
                    "text. Unlock the vault and re-save the agent to secure them.",
                    ID,
                    len(report.left_in_config),
                    config_path,
                )
            continue

        write_agent_config_text(config_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        logger.info(
            "%s: %s — vaulted %s",
            ID,
            agent,
            ", ".join(sorted(key for _, key in report.vaulted)),
        )
