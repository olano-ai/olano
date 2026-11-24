"""Subagent Store registry — P6.17/P6.18/P6.19/P6.20.

Pre-built subagent definitions live in `system/subagents/`.  User overrides and
custom subagents live in `extensions/subagents/`.  Resolution order:
  1. extensions/subagents/<name>.json5  (user overrides, highest priority)
  2. system/subagents/<name>.json5      (built-in definitions)

P6.18 override fields (`extra_tools`, `denied_tools`, `model`, `system_prompt`,
`mcp_servers`) are merged when an extensions definition extends a system one.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from olano.config.json5_fast import loads as _loads_json5

logger = logging.getLogger(__name__)

_SYSTEM_SUBAGENTS_DIR = Path(__file__).parent.parent.parent / "system" / "subagents"
_EXTENSIONS_SUBAGENTS_DIRNAME = "extensions/subagents"


@dataclass
class SubagentDefinition:
    """A pre-built subagent definition from the store."""

    name: str
    description: str = ""
    category: str | None = None
    system_prompt: str = ""
    model: str | None = None
    tools: list[str] = field(default_factory=list)
    extra_tools: list[str] = field(default_factory=list)
    denied_tools: list[str] = field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    credential_requirements: list[str] = field(default_factory=list)
    source_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "system_prompt": self.system_prompt[:120] + "..."
            if len(self.system_prompt) > 120
            else self.system_prompt,
            "model": self.model,
            "tools": self.tools,
            "extra_tools": self.extra_tools,
            "denied_tools": self.denied_tools,
            "mcp_servers": self.mcp_servers,
            "tags": self.tags,
            "credential_requirements": self.credential_requirements,
        }

    def to_subagent_config(self, enabled: bool = True) -> Any:
        """Convert to a SubAgentConfig instance."""
        from olano.config.agent_config import SubAgentConfig

        effective_tools = [t for t in (self.tools + self.extra_tools) if t not in self.denied_tools]
        return SubAgentConfig(
            name=self.name,
            description=self.description,
            system_prompt=self.system_prompt,
            model=self.model,
            tools=effective_tools,
            enabled=enabled,
        )


def _load_json5_file(path: Path) -> dict[str, Any]:
    """Parse a subagent definition file, or ``{}`` on any parse failure.

    Uses the shared loader (:mod:`olano.config.json5_fast`), which is
    string-literal aware — the regex fallback this used to carry corrupted any
    value containing ``//`` (e.g. ``"http://host"``).
    """
    try:
        data = _loads_json5(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_definition(data: dict[str, Any], source_path: str = "") -> SubagentDefinition:
    return SubagentDefinition(
        name=data.get("name", ""),
        description=data.get("description", ""),
        category=data.get("category"),
        system_prompt=data.get("system_prompt", ""),
        model=data.get("model"),
        tools=data.get("tools") or [],
        extra_tools=data.get("extra_tools") or [],
        denied_tools=data.get("denied_tools") or [],
        mcp_servers=data.get("mcp_servers") or [],
        tags=data.get("tags") or [],
        credential_requirements=data.get("credential_requirements") or [],
        source_path=source_path,
    )


class SubagentStore:
    """Registry for built-in and user-defined subagent definitions.

    Resolution order (highest priority first):
      1. extensions/subagents/ (workspace-local user overrides)
      2. system/subagents/     (built-in definitions shipped with olano)
    """

    def __init__(self, workspace_root: Path | None = None) -> None:
        self._workspace_root = workspace_root or self._find_workspace()
        self._cache: dict[str, SubagentDefinition] | None = None

    # ── discovery ─────────────────────────────────────────────────────────────

    def _find_workspace(self) -> Path:
        import os

        from olano.config.workspace import get_workspace

        try:
            return get_workspace().root
        except Exception:
            try:
                from olano.config.home import get_olano_paths

                return get_olano_paths().legacy_workspace_root
            except Exception:
                return Path(os.environ.get("HOME", ".")) / "olano" / "workspace"

    def _system_dir(self) -> Path:
        return _SYSTEM_SUBAGENTS_DIR

    def _extensions_dir(self) -> Path:
        return self._workspace_root / _EXTENSIONS_SUBAGENTS_DIRNAME

    def _load_all(self) -> dict[str, SubagentDefinition]:
        definitions: dict[str, SubagentDefinition] = {}

        # Load system definitions first (lowest priority)
        sys_dir = self._system_dir()
        if sys_dir.exists():
            for path in sorted(sys_dir.glob("*.json5")):
                try:
                    data = _load_json5_file(path)
                    defn = _parse_definition(data, source_path=str(path))
                    if defn.name:
                        definitions[defn.name.lower()] = defn
                except Exception as exc:
                    logger.debug("SubagentStore: failed to load %s: %s", path, exc)

        # Load YAML definitions too
        if sys_dir.exists():
            import yaml

            for path in sorted(sys_dir.glob("*.yaml")):
                try:
                    data = yaml.safe_load(path.read_text()) or {}
                    defn = _parse_definition(data, source_path=str(path))
                    if defn.name:
                        definitions[defn.name.lower()] = defn
                except Exception as exc:
                    logger.debug("SubagentStore: failed to load %s: %s", path, exc)

        # Load extensions (highest priority — overrides system definitions)
        ext_dir = self._extensions_dir()
        if ext_dir.exists():
            for path in sorted(ext_dir.glob("*.json5")):
                try:
                    data = _load_json5_file(path)
                    defn = _parse_definition(data, source_path=str(path))
                    if defn.name:
                        # Merge with system definition if extends one
                        base_key = defn.name.lower()
                        if base_key in definitions:
                            base = definitions[base_key]
                            defn = _merge_override(base, defn)
                        definitions[base_key] = defn
                except Exception as exc:
                    logger.debug("SubagentStore: failed to load extension %s: %s", path, exc)

        return definitions

    def _get_cache(self) -> dict[str, SubagentDefinition]:
        if self._cache is None:
            self._cache = self._load_all()
        return self._cache

    def invalidate_cache(self) -> None:
        self._cache = None

    # ── public API ────────────────────────────────────────────────────────────

    def get(self, name: str) -> SubagentDefinition | None:
        return self._get_cache().get(name.lower())

    def list_all(
        self, category: str | None = None, tags: list[str] | None = None
    ) -> list[SubagentDefinition]:
        all_defns = list(self._get_cache().values())
        if category:
            all_defns = [d for d in all_defns if d.category == category]
        if tags:
            tag_set = {t.lower() for t in tags}
            all_defns = [d for d in all_defns if tag_set & {t.lower() for t in d.tags}]
        return sorted(all_defns, key=lambda d: d.name)

    def search(self, query: str) -> list[SubagentDefinition]:
        q = query.lower()
        return [
            d
            for d in self._get_cache().values()
            if q in d.name.lower()
            or q in d.description.lower()
            or q in (d.category or "").lower()
            or any(q in t.lower() for t in d.tags)
        ]

    def save_user_subagent(
        self, defn: SubagentDefinition, workspace_root: Path | None = None
    ) -> Path:
        """Save a user-defined subagent to extensions/subagents/.

        Used by P6.19 creation wizard.
        """
        ext_dir = (workspace_root or self._workspace_root) / _EXTENSIONS_SUBAGENTS_DIRNAME
        ext_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", defn.name)
        path = ext_dir / f"{safe_name}.json5"
        data = {
            "name": defn.name,
            "description": defn.description,
            "category": defn.category,
            "system_prompt": defn.system_prompt,
            "model": defn.model,
            "tools": defn.tools,
            "tags": defn.tags,
        }
        # Write as JSON (valid JSON5)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        self.invalidate_cache()
        return path

    def clone(self, source_name: str, new_name: str) -> SubagentDefinition | None:
        """Clone an existing subagent definition with a new name (P6.19)."""
        source = self.get(source_name)
        if source is None:
            return None
        import dataclasses

        cloned = dataclasses.replace(source, name=new_name, source_path="")
        return cloned


# ── helpers ───────────────────────────────────────────────────────────────────


def _merge_override(base: SubagentDefinition, override: SubagentDefinition) -> SubagentDefinition:
    """Merge P6.18 override fields from `override` onto `base`."""
    import dataclasses

    merged_tools = [
        t for t in (base.tools + override.extra_tools) if t not in override.denied_tools
    ]
    return dataclasses.replace(
        base,
        description=override.description or base.description,
        system_prompt=override.system_prompt or base.system_prompt,
        model=override.model or base.model,
        tools=merged_tools,
        extra_tools=override.extra_tools,
        denied_tools=override.denied_tools,
        mcp_servers=override.mcp_servers or base.mcp_servers,
        tags=list(dict.fromkeys(base.tags + override.tags)),
        source_path=override.source_path,
    )
