"""Turn on fleet filesystem access for existing AgentFather agents.

AgentFather's shell and file tools reached only its own workspace, so operating
the deployment meant reading other agents' files one at a time through the admin
tools. ``fleet_access`` (``security/fleet_access.py``) opens every agent's
directory and the common skills read-write and the logs read-only. It ships in
the archetype template; AgentFathers created earlier have no such key.

Two edits, both idempotent and both only on AgentFather configs:

* ``fleet_access: true`` when the key is absent. An explicit ``false`` is the
  owner's choice and is left alone.
* The installer-written "Filesystem boundaries" paragraph that told the model
  never to reach another agent's workspace from its shell is replaced by the
  template's current wording, only when it is present verbatim. A prompt the
  owner rewrote is left alone; the Workspace Environment block the prompt
  assembler generates states the access either way and says it supersedes the
  older instruction.

Rewrites serialize as plain JSON (comments dropped only on files that actually
change), with the surrogate repair and the ``isinstance(data, dict)`` guard of
0026.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

ID = "0027_agentfather_fleet_access"

# Frozen on purpose: a migration is a historical record. This is the paragraph
# the template carried before fleet access, and its replacement.
_OLD_PARAGRAPH = (
    "      - Your shell and file tools reach ONLY your own workspace (plus the\n"
    "        shared folder under `shared/` and read-only skill sources). Other\n"
    "        agents' directories (`~/.olano/agents/<name>/...`) are\n"
    "        runtime-owned and intentionally invisible to you \u2014 a `Permission\n"
    "        denied` or `No such file` there is the isolation boundary working,\n"
    "        not something to work around. Never try to reach another agent's\n"
    "        workspace from your shell, and never weaken permissions to get in.\n"
    "        Your cross-agent access is TOOLS, not the filesystem: inspect with\n"
    "        `olano_list_agent_workspace` / `olano_read_agent_file`, write\n"
    "        a file with `olano_write_agent_file`, install whole skills with\n"
    "        `olano_install_skill`, and RUN a command in another agent's\n"
    "        workspace with `olano_run_agent_command`.\n"
)
_NEW_PARAGRAPH = (
    "      - With fleet access on (your Workspace Environment section says so and\n"
    "        lists the paths), your shell and file tools reach every agent's\n"
    "        directory under the home `agents/` folder (read-write), the common\n"
    "        skills folder (read-write) and the deployment logs (read-only). Use\n"
    "        `grep`, `find` and `tail` there directly to investigate. Change an\n"
    "        agent's settings with `olano_update_agent` rather than editing its\n"
    "        `agent.json5` by hand (it validates and applies live), and remember\n"
    "        that a file you write into another agent's workspace is something\n"
    "        that agent will read and act on. Without fleet access, other\n"
    "        agents' directories are invisible to your shell: inspect with\n"
    "        `olano_list_agent_workspace` / `olano_read_agent_file`, write a\n"
    "        file with `olano_write_agent_file`, and install whole skills with\n"
    "        `olano_install_skill`. Either way, RUN a command as another agent\n"
    "        (in its own sandbox, with its packages) with\n"
    "        `olano_run_agent_command`.\n"
)


def _is_agentfather(data: dict, dir_name: str) -> bool:
    return "agentfather" in {
        str(data.get("name") or "").lower(),
        str(data.get("archetype") or "").lower(),
        dir_name.lower(),
    }


def _repair_surrogates(text: str) -> str:
    """Recombine UTF-16 surrogate pairs into the characters they encode (frozen
    copy of the helper 0010/0013/0015/0019 carry)."""
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
        changed: list[str] = []
        if "fleet_access" not in data:
            data["fleet_access"] = True
            changed.append("fleet_access")
        prompt = data.get("system_prompt")
        if isinstance(prompt, str) and _OLD_PARAGRAPH in prompt:
            data["system_prompt"] = prompt.replace(_OLD_PARAGRAPH, _NEW_PARAGRAPH)
            changed.append("system_prompt")
        if not changed:
            continue
        payload = _repair_surrogates(json.dumps(data, indent=2, ensure_ascii=False)) + "\n"
        write_agent_config_text(config_path, payload)
        logger.info("%s: updated %s on %s", ID, ", ".join(changed), config_path.parent.name)
