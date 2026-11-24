"""Move archetype-installed agents on a managed box onto the Olano tiers.

Managed cloud provisioning runs ``olano quick-setup --agent agentfather``, and
quick-setup installs an archetype with **no** model — so whatever the archetype
file declared landed in ``agent.json5`` verbatim. For AgentFather that is
``anthropic:claude-sonnet-4-6``: a brain the box has no key for, on a
deployment whose whole billing story is credits through the Olano gateway.
Every managed box provisioned before ``default_archetype_model()`` existed is
sitting on one.

``installer.default_archetype_model`` fixes new installs. This fixes the ones
already on disk, under the same rule: the conductor gets the deep tier, every
other agent installed from an archetype, a creation template or a toolkit gets
the fast one.

Scope is deliberately narrow — three conditions, all required:

* **Managed box only.** An unmanaged install has no gateway to bill through, so
  rewriting it to ``olano:*`` would replace a working brain with one that can
  only 401. Same predicate (``managed_olano_default``) as the install path.
* **Installed-from-a-template agents only** — ``archetype``, ``template`` or
  ``toolkit_agent`` provenance. An agent the owner wrote from scratch is not
  this migration's business.
* **The model must still be the one that template shipped**, or absent entirely
  (the toolkit-agent installer wrote no ``model`` key, so those agents ran on
  ``AgentConfig``'s Anthropic default). That is the whole test for "the owner
  never chose this" — if the two differ by so much as a tier, somebody picked it
  on purpose and it is left alone. AgentFather is the one case that also accepts
  the frozen literal below, because its config predates the ``archetype`` field
  on some boxes and the pack copy of a template can move.

Idempotent: an agent already on its target tier is left byte-for-byte alone,
and only files that actually change are rewritten (comments are dropped on
those, as in migrations 0003-0010).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ID = "0011_managed_archetype_model_defaults"


def applicable() -> bool:
    """Only a managed box has Olano tiers to move onto.

    This is a gate for the RUNNER, not a guard inside :func:`apply`: returning
    False means "skip without recording", so a box whose first boot resolved
    unmanaged — the deployment manifest landing a beat after the runtime came
    up — retries on the boot where the posture flips instead of burning the
    migration forever (which stranded that box's agents on the BYOK literal
    with nothing left to repair them).
    """
    from olano.config.system_settings import managed_olano_default

    return managed_olano_default()


# What the AgentFather template shipped with when this migration was written.
# Frozen on purpose: a migration is a historical record, and the template on
# disk is exactly the thing that may since have moved (the pack copy outranks
# the in-package one, and either can be edited).
_SHIPPED_CONDUCTOR_MODEL = "anthropic:claude-sonnet-4-6"


def _archetype_slug(data: dict[str, Any], dir_name: str) -> str | None:
    """The archetype, built-in template or toolkit this agent was installed from."""
    for key in ("archetype", "template", "toolkit_agent"):
        value = str(data.get(key) or "").strip()
        if value:
            return value
    # An AgentFather old enough to predate the `archetype` field still answers
    # to its name — every other agent needs the explicit provenance.
    from olano.archetypes.installer import CONDUCTOR_ARCHETYPES

    for candidate in (str(data.get("name") or ""), dir_name):
        if candidate.strip().lower() in CONDUCTOR_ARCHETYPES:
            return candidate.strip().lower()
    return None


def _shipped_model(slug: str) -> str | None:
    """The model ``slug``'s template declares, or None if it can't be read."""
    from olano.archetypes.installer import load_agent_template, load_archetype_template

    for loader in (load_archetype_template, load_agent_template):
        try:
            return str(loader(slug).get("model") or "") or None
        except Exception:
            continue
    return None


def apply(home: Path) -> None:
    from olano.archetypes.installer import CONDUCTOR_ARCHETYPES, default_archetype_model
    from olano.config.loader import _load_json5, write_agent_config_text
    from olano.config.system_settings import managed_olano_default

    if not managed_olano_default():
        return  # unmanaged: no gateway to bill through, nothing to move onto

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

        target = default_archetype_model(slug)
        current = str(data.get("model") or "").strip()
        if not target or current == target:
            continue

        # "" = no `model` key at all, i.e. the agent has been running on
        # `AgentConfig`'s Anthropic default — the toolkit-agent installer wrote
        # configs like that. Nobody chose that either.
        allowed = {"", _shipped_model(slug)}
        if slug.lower() in CONDUCTOR_ARCHETYPES:
            allowed.add(_SHIPPED_CONDUCTOR_MODEL)
        if current not in {m for m in allowed if m is not None}:
            continue  # somebody picked this brain on purpose — leave it

        data["model"] = target
        write_agent_config_text(config_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        logger.info("%s: %s moved from '%s' to '%s'", ID, config_path.parent.name, current, target)
