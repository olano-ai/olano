"""Move an installer-placed AgentFather from the deep tier onto the fast one.

Until now the conductor installed on ``default:pro`` (0011 put it on
``olano:pro``, 0012 moved that to the alias) while every other agent got
``default:turbo``. The deep tier made every one of AgentFather's turns —
conversational, tool-driven, gated by an approval card — wait on reasoning the
task did not need, and that latency is the first thing a new owner meets.
``installer.MANAGED_CONDUCTOR_MODEL`` is ``default:turbo`` now; this moves the
AgentFathers already on disk, under the rule 0011 and 0012 established: only a
model the installer provably wrote is rewritten.

Rewritten when the model is exactly one of the two values the installers ever
wrote for a conductor — the 0012 alias ``default:pro`` or the 0011 tier
``olano:pro`` (a box that skipped 0012 somehow, or a pre-alias managed install).
Anything else — a concrete provider model, ``default:max``, an empty key — was
picked by the owner or is somebody else's migration, and is left alone. Runs on
every posture, like 0012: the alias resolves per box. Only conductor-provenance
agents are touched (``archetype: agentfather``, or the by-name fallback for a
config old enough to predate the field). Idempotent: an agent already on
``default:turbo`` is left byte-for-byte alone, and only files that actually
change are rewritten (comments are dropped on those, as in 0003-0012).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ID = "0020_agentfather_turbo"

# Frozen on purpose — a migration is a historical record. The live constants
# may move again; this one must keep repairing exactly the boxes it targets.
_INSTALLER_CONDUCTOR_MODELS = frozenset({"default:pro", "olano:pro"})
_TARGET = "default:turbo"


def _conductor_slug(data: dict[str, Any], dir_name: str) -> str | None:
    """The conductor archetype this agent was installed from, or None.

    Same resolution as 0011/0012: explicit provenance first, then the
    by-name fallback for an AgentFather old enough to predate ``archetype``.
    """
    from olano.archetypes.installer import CONDUCTOR_ARCHETYPES

    for key in ("archetype", "template", "toolkit_agent"):
        value = str(data.get(key) or "").strip()
        if value:
            return value.lower() if value.lower() in CONDUCTOR_ARCHETYPES else None
    for candidate in (str(data.get("name") or ""), dir_name):
        if candidate.strip().lower() in CONDUCTOR_ARCHETYPES:
            return candidate.strip().lower()
    return None


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

        if _conductor_slug(data, config_path.parent.name) is None:
            continue

        current = str(data.get("model") or "").strip()
        if current == _TARGET:
            continue  # already there
        if current not in _INSTALLER_CONDUCTOR_MODELS:
            continue  # somebody picked this brain on purpose — leave it

        data["model"] = _TARGET
        write_agent_config_text(config_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        logger.info("%s: %s moved from '%s' to '%s'", ID, config_path.parent.name, current, _TARGET)
