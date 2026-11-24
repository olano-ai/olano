"""Move every channel credential out of ``agent.json5`` and into the vault.

Connectors now resolve their own credential by convention — ``telegram`` reads
``TELEGRAM_BOT_TOKEN``, ``slack`` reads ``SLACK_BOT_TOKEN`` / ``SLACK_APP_TOKEN``,
scoped per agent (``config/channel_secrets.py``). A config therefore has nothing
to say about credentials, and the two shapes installs carry today are both
obsolete:

* a **literal token** — the plain-text credential this whole mechanism exists to
  get rid of. Stored as a per-agent override, then removed from the file.
* a ``${vault:TELEGRAM_BOT_TOKEN}`` / ``${TELEGRAM_BOT_TOKEN}`` **reference to
  the canonical key** — already vaulted, and now redundant. Removed.

A reference to a *different* key (``${vault:MARISOL_TELEGRAM_BOT_TOKEN}``) is a
deliberate choice — the value lives under a name the convention won't find — so
it is left exactly as written and keeps resolving through the explicit-value
path.

Idempotent: a second run finds no credential fields. Safe when the vault is
locked — literals are left in place and reported, so nothing is lost and the
next run (or ``olano vault absorb-channel-secrets``) finishes the job.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

ID = "0007_channel_credentials_to_vault"


def apply(home: Path) -> None:
    from olano.config.channel_secrets import absorb_channel_secrets
    from olano.config.loader import _load_json5, write_agent_config_text

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
        if not isinstance(data.get("channels"), list):
            continue

        try:
            report = absorb_channel_secrets(agent, data)
        except Exception as exc:  # a vault hiccup must not abort the upgrade
            logger.warning("%s: could not process %s: %s", ID, config_path, exc)
            continue

        if not report.changed:
            if report.left_in_config:
                logger.warning(
                    "%s: vault unavailable — %d credential(s) stay in %s as plain text. "
                    "Unlock the vault and run `olano vault absorb-channel-secrets`.",
                    ID,
                    len(report.left_in_config),
                    config_path,
                )
            continue

        write_agent_config_text(config_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        moved = ", ".join(sorted({k for _, _, k in report.vaulted}))
        dropped = ", ".join(sorted({k for _, _, k in report.stripped}))
        logger.info(
            "%s: %s — %s%s%s",
            ID,
            agent,
            f"vaulted {moved}" if moved else "",
            "; " if moved and dropped else "",
            f"dropped redundant {dropped}" if dropped else "",
        )
