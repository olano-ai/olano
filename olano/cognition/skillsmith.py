"""SkillSmith — validate, dedup, and polish a candidate auto-learned skill.

Runs a DEEP worker that reads existing skills, skips near-duplicates, and writes
a clean ``skills/learned/<slug>/SKILL.md`` with proper frontmatter. The caller
falls back to a direct write if this returns anything other than ``completed``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from olano.cognition.playbooks import SKILLSMITH_PLAYBOOK
from olano.cognition.worker import WorkerOutcome, run_worker

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig

logger = logging.getLogger(__name__)


async def run_skillsmith(
    parent_config: AgentConfig,
    workspace_root: Path,
    candidate: dict,
    *,
    budget_check: bool = True,
) -> WorkerOutcome:
    """Validate + polish ``candidate`` (a drafted skill dict) into a SKILL.md."""
    import re

    from olano.skills.naming import skill_slug

    raw_name = str(candidate.get("name") or "unnamed-skill")
    # camelCase splits into words first (GetCryptoPrice -> get-crypto-price);
    # skill_slug then guarantees the spec shape, so the folder and the `name`
    # it is told to write are a legal pair by construction.
    slug = skill_slug(re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", raw_name)) or "unnamed-skill"
    goal = (
        f"Validate and, if worth keeping, write a polished skill for the candidate below. "
        f"First scan skills/learned/ for near-duplicate folder names and skip if one exists. "
        f"Write the skill to skills/learned/{slug}/SKILL.md following the standard format:\n"
        f"  - YAML frontmatter with: name: {slug} (exactly the folder name), description, "
        f"trigger, tier, auto_learned, category\n"
        f"  - Markdown body with ## heading, description, **Trigger:**, **Steps:** list\n\n"
        f"CANDIDATE:\n{json.dumps(candidate, indent=2)}"
    )
    return await run_worker(
        parent_config=parent_config,
        workspace_root=workspace_root,
        playbook=SKILLSMITH_PLAYBOOK,
        goal=goal,
        tools_allow=["memory_read"],
        budget_check=budget_check,
        worker_label="skillsmith",
    )
