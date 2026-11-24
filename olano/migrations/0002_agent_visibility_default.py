"""Propagate the ``agent_visibility`` default ("none") to existing agents.

``agent_visibility`` (added alongside the awareness/roster rework) defaults to
``{mode: "none", agents: []}`` — the safe default where an agent is told about
no other agents except the ones it can already reach (talk_to allow-list +
manager / direct reports / coworkers). An absent field already resolves to that
default at load time, so this migration does not change behaviour; it writes the
value **explicitly** into every existing ``agent.json5`` that has not set it, so
the safe default is visible and auditable in the config the operator reads (and
survives future default changes).

Idempotent: an agent that already has an ``agent_visibility`` key — for any
mode, including a deliberate ``all`` / ``list`` — is left untouched. Like every
programmatic ``agent.json5`` write in the codebase (dashboard Config tab, the
``0001`` migration, ``olano_update_agent``), the rewrite serializes as plain
JSON, so ``//`` comments are dropped **only** on a file that actually needed the
change.

This runs from the one canonical propagation hook — :func:`run_pending_migrations`
via :func:`olano.update.auto.run_startup_upgrades`, invoked on every runtime
boot (so every ``deploy/*`` restart picks it up) and by ``olano update`` /
the ``/platform update`` chat command.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

ID = "0002_agent_visibility_default"


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
        if "agent_visibility" in data:
            continue  # the owner (or a prior run) already set it — respect it
        data["agent_visibility"] = {"mode": "none", "agents": []}
        write_agent_config_text(config_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        logger.info("%s: set agent_visibility=none for %s", ID, config_path.parent.name)
