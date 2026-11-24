"""Move installer-written agent models onto the `default:*` aliases.

The `default:turbo` / `default:pro` aliases resolve at build time to whatever
the box's ``default_models:`` config block (or built-in target) points at, so
an agent carrying the alias re-points live when the deployment retargets a
tier. New installs get the aliases from ``installer.default_archetype_model``
and ``api._default_agent_model``; this moves the agents already on disk, under
0011's rule: the conductor gets the deep tier, every other agent installed
from an archetype, a creation template or a toolkit gets the fast one.

Unlike 0011 this runs on UNMANAGED boxes too (no ``applicable`` gate): the
alias is safe everywhere, because resolution forks per posture — Olano tiers
on a managed box, the BYOK targets elsewhere.

An agent's model is rewritten only when it is provably installer-written:

* **empty** — the toolkit-agent installer wrote no ``model`` key, so the agent
  ran on ``AgentConfig``'s default;
* **the frozen pack literal** ``anthropic:claude-sonnet-4-6`` — what every
  archetype in the pack declared when this migration was written (0011's
  ``_shipped_model`` read the *live* template, which goes stale the moment the
  pack itself moves to aliases — hence frozen literals here);
* **the tier 0011 set** (``olano:turbo`` / ``olano:pro`` per role) — an
  installer-written value by definition, and behavior-identical to the alias
  on the managed boxes 0011 ran on.

Anything else was picked by the owner and is left alone. Only
archetype/template/toolkit-provenance agents are touched, resolved exactly as
0011 resolved them. Idempotent: an agent already on its alias is left
byte-for-byte alone, and only files that actually change are rewritten
(comments are dropped on those, as in migrations 0003-0011).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ID = "0012_default_model_aliases"

# Frozen on purpose — a migration is a historical record. These are the values
# the installers wrote before the aliases existed; the live constants may move
# again, and this migration must keep repairing exactly the boxes it targets.
_SHIPPED_PACK_MODEL = "anthropic:claude-sonnet-4-6"
_0011_CONDUCTOR_MODEL = "olano:pro"
_0011_ARCHETYPE_MODEL = "olano:turbo"
_ALIAS_CONDUCTOR_MODEL = "default:pro"
_ALIAS_ARCHETYPE_MODEL = "default:turbo"


def _archetype_slug(data: dict[str, Any], dir_name: str) -> str | None:
    """The archetype, built-in template or toolkit this agent was installed from.

    Same resolution as 0011: explicit provenance fields first, then the
    conductor-by-name fallback for an AgentFather old enough to predate the
    ``archetype`` field.
    """
    for key in ("archetype", "template", "toolkit_agent"):
        value = str(data.get(key) or "").strip()
        if value:
            return value
    from olano.archetypes.installer import CONDUCTOR_ARCHETYPES

    for candidate in (str(data.get("name") or ""), dir_name):
        if candidate.strip().lower() in CONDUCTOR_ARCHETYPES:
            return candidate.strip().lower()
    return None


def apply(home: Path) -> None:
    from olano.archetypes.installer import CONDUCTOR_ARCHETYPES
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

        slug = _archetype_slug(data, config_path.parent.name)
        if not slug:
            continue

        conductor = slug.lower() in CONDUCTOR_ARCHETYPES
        target = _ALIAS_CONDUCTOR_MODEL if conductor else _ALIAS_ARCHETYPE_MODEL
        tier_0011 = _0011_CONDUCTOR_MODEL if conductor else _0011_ARCHETYPE_MODEL

        current = str(data.get("model") or "").strip()
        if current == target:
            continue  # already on the alias
        if current not in ("", _SHIPPED_PACK_MODEL, tier_0011):
            continue  # somebody picked this brain on purpose — leave it

        data["model"] = target
        write_agent_config_text(config_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        logger.info("%s: %s moved from '%s' to '%s'", ID, config_path.parent.name, current, target)
