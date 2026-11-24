"""Move the ``handshake:`` config block to ``magic_links:`` and rename its tool.

The feature is called **magic links** now. Two things on a deployed box still
carry the old name:

* ``config.yaml`` holds the settings under ``handshake:``. Both keys are read
  (``load_magic_link_settings`` falls back), so nothing breaks without this
  migration — but leaving two spellings around means a hand-edit of the key the
  operator can see may not be the key a later dashboard save rewrites. Moving
  it once keeps a single source of truth.
* ``agent.json5`` files list the status tool as ``handshake_status``.
  ``registry._ALIASES`` resolves that name, so those agents keep working
  either way; rewriting the configs means a human reading one sees the name
  the tool actually has, and the alias stays for configs nobody migrated.

``system_prompt`` gets the same word-boundary rewrite for the same reason as
migration 0010: AgentFather's prompt names the tool, and a prompt that teaches
a stale name teaches the model to guess.

Idempotent: a home with neither the old key nor the old tool name is left
byte-for-byte alone, and files are only rewritten when something changed.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ID = "0013_magic_links_rename"

# Frozen snapshot of this rename — never regenerate from the live registry.
LEGACY_CONFIG_KEY = "handshake"
CONFIG_KEY = "magic_links"
RENAMES: dict[str, str] = {"handshake_status": "magic_link_status"}

_PROMPT_RE = re.compile(r"(?<![\w])(" + "|".join(map(re.escape, RENAMES)) + r")(?![\w])")

# Keys whose list values hold OUR tool names.
_TOOL_LIST_KEYS = ("tools", "disabled_tools")


def _rename_list(values: list[Any]) -> tuple[list[Any], bool]:
    out: list[Any] = []
    changed = False
    for v in values:
        if isinstance(v, str) and v in RENAMES:
            out.append(RENAMES[v])
            changed = True
        else:
            out.append(v)
    return out, changed


def _walk(node: Any) -> bool:
    """Rewrite tool names in place. Returns True when anything changed."""
    changed = False
    if isinstance(node, dict):
        for key, value in node.items():
            # An MCP server's disabled_tools are that server's tool names.
            if key == "mcp_servers":
                continue
            if key in _TOOL_LIST_KEYS and isinstance(value, list):
                node[key], hit = _rename_list(value)
                changed |= hit
            elif key == "system_prompt" and isinstance(value, str):
                new = _PROMPT_RE.sub(lambda m: RENAMES[m.group(1)], value)
                if new != value:
                    node[key] = new
                    changed = True
            else:
                changed |= _walk(value)
    elif isinstance(node, list):
        for item in node:
            changed |= _walk(item)
    return changed


def _migrate_config_key(home: Path) -> None:
    """Move ``handshake:`` to ``magic_links:``, preserving every other key.

    An already-migrated home (or one carrying both, where the new key is
    authoritative) drops the stale legacy block rather than merging it — the
    loader already prefers ``magic_links:``, so keeping the old one around
    would only be a second spelling of a value nothing reads.
    """
    import yaml

    config_path = home / "config.yaml"
    if not config_path.is_file():
        return
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("%s: skipping unparseable %s: %s", ID, config_path, exc)
        return
    if not isinstance(raw, dict) or LEGACY_CONFIG_KEY not in raw:
        return
    legacy = raw.pop(LEGACY_CONFIG_KEY)
    if CONFIG_KEY not in raw and isinstance(legacy, dict):
        raw[CONFIG_KEY] = legacy
    try:
        config_path.write_text(
            yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("%s: could not rewrite %s: %s", ID, config_path, exc)
        return
    logger.info("%s: moved %s: to %s: in config.yaml", ID, LEGACY_CONFIG_KEY, CONFIG_KEY)


def _repair_surrogates(text: str) -> str:
    """Recombine UTF-16 surrogate pairs into the characters they encode.

    ``_load_json5`` yields lone surrogates for ``\\ud83e\\udd81``-style escapes,
    which ``ensure_ascii=False`` then cannot encode. AgentFather's prompt
    carries emoji, so this is a live path (see migration 0010).
    """
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def apply(home: Path) -> None:
    from olano.config.loader import _load_json5, write_agent_config_text

    _migrate_config_key(home)

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
            continue
        if not _walk(data):
            continue
        payload = _repair_surrogates(json.dumps(data, indent=2, ensure_ascii=False)) + "\n"
        write_agent_config_text(config_path, payload)
        logger.info("%s: renamed magic-link tool names in %s", ID, config_path.parent.name)
