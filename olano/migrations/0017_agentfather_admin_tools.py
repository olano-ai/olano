"""Grant the user-admin, disk, and skill-repair tools to existing AgentFathers.

The human-user tools (``olano_list_users`` … ``olano_access_overview``) let the
deployment's conductor invite humans, issue password-reset links + QR images,
flip admin/member roles, and assign owners/members to agents — the dashboard
Admin section's operations, from chat, owner-only and HITL-gated.
``olano_disk_report``/``olano_disk_monitor`` and ``olano_repair_skill_names``
wrap admin-layer functions that until now only the dashboard and CLI reached.

They ship in the archetype template; AgentFathers created earlier have an
explicit ``tools`` list without them.

The ``system_prompt`` is patched too, with two exact-match, idempotent edits
(prior art: 0010/0016 rewrite prompts): the "non-secret runtime settings"
sentence is extended to describe the full config.yaml coverage + honest
``applied``/``ignored`` reporting, and the team-accounts operating rule (never
pass a password through chat — relay the invite link + QR) is inserted before
the "Diagnose before guessing" bullet on a stock prompt, or appended when an
operator customized it away. A prompt already mentioning ``olano_create_user``
is left alone. The fragments are frozen copies of the archetype text — a
migration is a historical record, never regenerated from live code.

Same shape and guarantees as migrations 0003/0004/0005/0015: idempotent per
tool, respects ``disabled_tools`` opt-outs, only touches AgentFather configs,
and rewrites serialize as plain JSON (comments dropped only on files that
actually change). Includes the surrogate repair on the payload and the
``isinstance(data, dict)`` guard that 0010/0013 learned the hard way.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

ID = "0017_agentfather_admin_tools"

# tool name → the tool it should sit next to (falls back to append).
_TOOLS: tuple[tuple[str, str], ...] = (
    ("olano_list_users", "olano_update_home_config"),
    ("olano_get_user", "olano_list_users"),
    ("olano_create_user", "olano_get_user"),
    ("olano_reset_user_password", "olano_create_user"),
    ("olano_set_user_role", "olano_reset_user_password"),
    ("olano_update_user", "olano_set_user_role"),
    ("olano_delete_user", "olano_update_user"),
    ("olano_grant_agent_access", "olano_delete_user"),
    ("olano_revoke_agent_access", "olano_grant_agent_access"),
    ("olano_access_overview", "olano_revoke_agent_access"),
    ("olano_repair_skill_names", "olano_install_skill"),
    ("olano_disk_report", "olano_memory_monitor"),
    ("olano_disk_monitor", "olano_disk_report"),
)

# ── Frozen prompt fragments (decoded — real newlines, exactly what a stock
# pre-0017 agent.json5 carries after JSON decoding, and what the 0017-era
# archetype ships). Never regenerate these from the live archetype.

_OLD_CONFIG_SENTENCE = "For non-secret runtime settings\n    use `olano_update_home_config`.\n"
_NEW_CONFIG_SENTENCE = (
    "For non-secret runtime settings\n"
    "    use `olano_update_home_config` — it patches every config.yaml block\n"
    "    (speech, cognition, disk_monitor, magic_links, issues, …) as well as the\n"
    "    root keys, and reports honestly: check `ignored` in its result before\n"
    "    telling anyone a setting changed.\n"
)
_PROMPT_ANCHOR = "\n  • Diagnose before guessing."
_TEAM_BULLET = (
    "  • Team accounts: manage this deployment's humans with the user tools.\n"
    "    `olano_create_user` INVITES — never set, ask for, or accept a password in\n"
    "    chat: relay the single-use link + QR image it returns, and the person sets\n"
    "    their own password in the browser. `olano_reset_user_password` re-issues\n"
    "    that link for an existing user (lost password, or an expired invite).\n"
    "    `olano_set_user_role` makes someone admin or member,\n"
    "    `olano_grant_agent_access` / `olano_revoke_agent_access` decide which\n"
    "    humans may use (member) or manage (owner) which agent, and\n"
    "    `olano_access_overview` shows the whole grid. Every mutation is owner-only\n"
    "    and surfaces an approval card."
)


def _patch_prompt(prompt: str) -> tuple[str, list[str]]:
    """The two prompt edits, each exact-match and naturally idempotent.

    Returns ``(new_prompt, notes)`` — an unchanged prompt comes back with no
    notes. An operator-customized prompt degrades gracefully: a rewritten
    config sentence simply doesn't match (skip), and the team bullet appends
    at the end when the stock anchor is gone. A prompt that already mentions
    ``olano_create_user`` is left entirely alone.
    """
    notes: list[str] = []
    out = prompt
    if _OLD_CONFIG_SENTENCE in out:
        out = out.replace(_OLD_CONFIG_SENTENCE, _NEW_CONFIG_SENTENCE, 1)
        notes.append("config-coverage sentence")
    if "olano_create_user" not in out:
        if _PROMPT_ANCHOR in out:
            out = out.replace(_PROMPT_ANCHOR, "\n" + _TEAM_BULLET + _PROMPT_ANCHOR, 1)
        else:
            out = out + ("" if out.endswith("\n") else "\n") + _TEAM_BULLET + "\n"
        notes.append("team-accounts rule")
    return out, notes


def _is_agentfather(data: dict, dir_name: str) -> bool:
    return "agentfather" in {
        str(data.get("name") or "").lower(),
        str(data.get("archetype") or "").lower(),
        dir_name.lower(),
    }


def _repair_surrogates(text: str) -> str:
    """Recombine UTF-16 surrogate pairs into the characters they encode.

    ``_load_json5`` yields lone surrogates for ``\\ud83e\\udd81``-style escapes
    when a file takes the json5-fallback parse (unquoted keys, single quotes),
    and ``ensure_ascii=False`` passes them through — ``write_text`` then raises
    ``UnicodeEncodeError`` and the failed migration re-runs (and re-fails) on
    every boot, blocking all later migrations. Same frozen copy as 0010/0013 —
    a migration is a historical record, never regenerated from live code.
    """
    try:
        return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    except Exception:
        return text


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
        if not isinstance(data, dict):
            continue  # a top-level array is not an agent config
        if not _is_agentfather(data, config_path.parent.name):
            continue
        tools = data.get("tools")
        if not isinstance(tools, list):
            continue
        disabled = set(data.get("disabled_tools") or [])
        added: list[str] = []
        for tool_name, anchor in _TOOLS:
            if tool_name in tools or tool_name in disabled:
                continue  # present, or the owner explicitly opted out
            if anchor in tools:
                tools.insert(tools.index(anchor) + 1, tool_name)
            else:
                tools.append(tool_name)
            added.append(tool_name)
        if added:
            data["tools"] = tools

        prompt_notes: list[str] = []
        prompt = data.get("system_prompt")
        if isinstance(prompt, str) and prompt:
            patched, prompt_notes = _patch_prompt(prompt)
            if prompt_notes:
                data["system_prompt"] = patched

        if not added and not prompt_notes:
            continue
        payload = _repair_surrogates(json.dumps(data, indent=2, ensure_ascii=False)) + "\n"
        write_agent_config_text(config_path, payload)
        logger.info(
            "%s: applied %s to %s",
            ID,
            ", ".join(added + [f"prompt: {n}" for n in prompt_notes]),
            config_path.parent.name,
        )
