"""Edit-prompt — change an agent's standing instructions from a phone.

The Tier-3 "scoped micro-form" of the auth-handshake design: a non-technical
owner gets a PIN-gated page with one textarea — the agent's current
``system_prompt`` — edits it, saves, and the running agent hot-swaps within the
prompt-refresh window. No dashboard session, no other surface of the deployment
is reachable from the link.

Writes go through :func:`olano.admin.agents.update_agent`, the same
validated merge path the ``olano_update_agent`` admin tool uses — the result
is re-validated as a full ``AgentConfig`` before touching disk, so a bad edit
fails loudly on the page instead of corrupting ``agent.json5``.

Extensibility: what the form edits is a *target* in :data:`_TARGETS`
(``system_prompt`` today). A future target (``AGENTS.md``, ``SOUL.md``) adds a
loader/saver pair there — the page, mint surfaces, and this adapter's protocol
methods don't change.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from olano.magic_links.flows import (
    APPLY_SELF,
    STATUS_DONE,
    BaseFlow,
    FlowField,
    register,
)
from olano.magic_links.store import LinkRecord

logger = logging.getLogger(__name__)

#: Hard cap on the edited text. Far above any sane prompt, far below anything
#: that could balloon the config file or the model context by accident.
MAX_PROMPT_CHARS = 40_000


def _load_config(agent: str) -> Any:
    from olano.config.loader import load_agent_config

    return load_agent_config(agent)


def _load_system_prompt(agent: str) -> str:
    return str(_load_config(agent).system_prompt or "")


def _save_system_prompt(agent: str, value: str) -> None:
    from olano.admin import agents as admin_agents

    admin_agents.update_agent(agent, {"system_prompt": value})


@dataclass(frozen=True)
class _Target:
    """One editable thing. ``load`` returns the current text; ``save`` persists
    a validated replacement (raising ``ValueError``/``Exception`` on refusal)."""

    key: str
    title: str  # page heading suffix
    description: str  # shown under the heading
    load: Callable[[str], str]
    save: Callable[[str, str], None]


_TARGETS: dict[str, _Target] = {
    "system_prompt": _Target(
        key="system_prompt",
        title="instructions",
        description=(
            "These are the standing instructions the agent reads at the start of "
            "every conversation — its role, rules, and priorities. Edit and save; "
            "the running agent picks the change up within a couple of minutes."
        ),
        load=_load_system_prompt,
        save=_save_system_prompt,
    ),
}


def _request_prompt_refresh(agent: str) -> None:
    """Nudge the runtime's prompt-refresh sweep (no-op outside the daemon)."""
    try:
        from olano.tools.talk_to import get_bound_runtime

        runtime = get_bound_runtime()
        if runtime is not None:
            runtime.request_prompt_refresh([agent])
    except Exception:  # pragma: no cover - a nudge must never fail the save
        logger.debug("edit-prompt: prompt refresh nudge failed", exc_info=True)


class EditPromptFlow(BaseFlow):
    id = "edit-prompt"
    label = "Edit instructions"
    kind = "form"
    # `_request_prompt_refresh` nudges the runtime sweep, which hot-swaps a
    # rebuilt graph — the flow's own result text already promises no restart.
    applies = APPLY_SELF

    def validate(self, params: dict[str, Any]) -> dict[str, Any]:
        agent = str(params.get("agent") or "").strip()
        if not agent:
            raise ValueError("An agent name is required for an edit-prompt link.")
        target = str(params.get("target") or "system_prompt").strip()
        if target not in _TARGETS:
            known = ", ".join(sorted(_TARGETS))
            raise ValueError(f"Unknown edit target '{target}'. Available: {known}.")
        try:
            _load_config(agent)
        except Exception as exc:
            raise ValueError(f"Agent '{agent}' was not found: {exc}") from exc
        return {"agent": agent, "target": target, "service": "edit-prompt"}

    def descriptor(self, link: LinkRecord) -> dict[str, Any]:
        base = super().descriptor(link)
        agent = str(link.params.get("agent") or "")
        target = _TARGETS.get(str(link.params.get("target") or "system_prompt"))
        if target is None:  # pragma: no cover - validate() refused it at mint
            return base
        base["service"] = {"id": "edit-prompt", "name": agent}
        base["title"] = f"Edit {agent}'s {target.title}"
        base["instructions"] = target.description
        base["cta"] = "Save changes"
        try:
            current = target.load(agent)
        except Exception as exc:
            logger.warning("edit-prompt: could not load current value: %s", exc)
            current = ""
        base["fields"] = [
            FlowField(
                name="value",
                label=f"{agent} — {target.title}",
                multiline=True,
                value=current,
                placeholder="Describe the agent's role, rules and priorities…",
                help=(
                    "Plain text. Saving replaces the current instructions; "
                    "workspace files (memory, persona notes) are unaffected."
                ),
            ).public()
        ]
        return base

    def submit(self, link: LinkRecord, fields: dict[str, str]) -> dict[str, Any]:
        agent = str(link.params.get("agent") or "")
        target = _TARGETS.get(str(link.params.get("target") or "system_prompt"))
        if target is None:
            raise ValueError("This edit link's target is no longer available.")
        value = str(fields.get("value") or "").strip()
        if not value:
            raise ValueError(
                "The instructions can't be empty. To start from scratch, write "
                "the new instructions here instead of clearing them."
            )
        if len(value) > MAX_PROMPT_CHARS:
            raise ValueError(
                f"That's too long ({len(value):,} characters — the limit is "
                f"{MAX_PROMPT_CHARS:,}). Trim it down and save again."
            )
        try:
            target.save(agent, value)
        except ValueError:
            raise
        except Exception as exc:
            logger.warning("edit-prompt: save failed for %s: %s", agent, exc)
            raise ValueError(
                "Could not save the new instructions. Ask the operator to check the server logs."
            ) from exc
        _request_prompt_refresh(agent)
        return {
            "status": STATUS_DONE,
            "result": (
                f"{agent}'s {target.title} were updated. The running agent picks "
                "them up automatically — no restart needed."
            ),
        }


register(EditPromptFlow())
