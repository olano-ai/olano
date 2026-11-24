"""Built-in skill descriptions stay within the Agent Skills spec limit.

The condition this guards: a ``SKILL.md`` whose frontmatter ``description``
runs over the spec's 1024 characters. deepagents keeps the first 1024 and
drops the rest from what the model sees when deciding whether to use the
skill, and logs a warning on **every agent boot** (``olano-cloud`` shipped at
1061 and warned on each build). The cut lands on the tail, which is where the
"use when ..." triggers and the "never quote prices" rule live, so the model
loses exactly the part that makes the description useful.

Same anti-drift discipline as ``test_skill_name_spec.py``: the limit is read
from deepagents when it is installed, so an upstream tightening goes red here
rather than silently truncating a customer's prompt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import olano.skills
from olano.skills.loader import discover_skills

_SPEC_LIMIT = 1024  # https://agentskills.io/specification
_BUILT_IN = Path(olano.skills.__file__).parent / "built_in"


def _description_limit() -> int:
    upstream = pytest.importorskip("deepagents.middleware.skills")
    limit = int(getattr(upstream, "MAX_SKILL_DESCRIPTION_LENGTH", _SPEC_LIMIT))
    assert limit == _SPEC_LIMIT, "deepagents moved the description limit; update this pin"
    return limit


def _built_in_skills() -> list[dict]:
    skills = discover_skills(_BUILT_IN)
    assert skills, f"no built-in skills discovered under {_BUILT_IN}"
    return skills


@pytest.mark.parametrize("skill", _built_in_skills(), ids=lambda s: str(s["name"]))
def test_built_in_skill_description_within_spec_limit(skill: dict) -> None:
    limit = _description_limit()
    description = str(skill["description"])
    assert description, f"{skill['location']} has no description"
    assert len(description) <= limit, (
        f"{skill['location']}: description is {len(description)} characters, over the "
        f"Agent Skills limit of {limit}; deepagents truncates it on every boot"
    )
