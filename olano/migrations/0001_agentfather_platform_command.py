"""Enable the owner-only /platform command on existing AgentFather agents.

Freshly scaffolded AgentFathers get ``commands: { enabled: { platform: true } }``
from the archetype template, but AgentFathers created *before* the command
existed never received it — and because ``platform`` is a sensitive command
(default off), those agents reply "🔒 /platform is disabled" until the toggle
is set. This migration adds the toggle to every home AgentFather that hasn't
made an explicit choice yet.

Idempotent: an agent whose ``commands.enabled`` already contains a ``platform``
key (true OR false — the owner decided) is left untouched, as is every
non-AgentFather agent. Note the rewrite serializes the config as plain JSON,
the same convention as every programmatic ``agent.json5`` write (dashboard
Config tab, ``olano_update_agent``), so ``//`` comments in a hand-edited
file are dropped on the rewritten file only when a change is actually needed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

ID = "0001_agentfather_platform_command"


def _is_agentfather(data: dict, dir_name: str) -> bool:
    return "agentfather" in {
        str(data.get("name") or "").lower(),
        str(data.get("archetype") or "").lower(),
        dir_name.lower(),
    }


def apply(home: Path) -> None:
    from olano.config.loader import _load_json5, write_agent_config_text

    agents_dir = home / "agents"
    if not agents_dir.is_dir():
        return
    for config_path in sorted(agents_dir.glob("*/agent.json5")):
        try:
            data = _load_json5(config_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            logger.warning("%s: skipping unparseable %s: %s", ID, config_path, exc)
            continue
        if not _is_agentfather(data, config_path.parent.name):
            continue
        commands = data.get("commands")
        if not isinstance(commands, dict):
            commands = {}
        enabled = commands.get("enabled")
        if not isinstance(enabled, dict):
            enabled = {}
        if "platform" in enabled:
            continue  # the owner already made an explicit choice — respect it
        enabled["platform"] = True
        commands["enabled"] = enabled
        data["commands"] = commands
        write_agent_config_text(config_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        logger.info("%s: enabled /platform for %s", ID, config_path.parent.name)
