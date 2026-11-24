"""ToolkitAgent registry — discovery, loading, and subagent resolution.

Resolution order for a name:
  1. Hand-authored ``definitions/*.json5`` whose ``name`` field matches.
  2. Auto-generated ``ToolkitAgentDef`` synthesized from a toolkit in
     ``olano.tools.registry:_TOOLKITS``.

Names are matched case-insensitively with non-alphanumeric characters
stripped, so ``"AlpacaAgent"``, ``"alpaca_agent"``, and ``"alpaca"`` all
resolve to the same def.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

from olano.config.agent_config import SubAgentConfig
from olano.config.json5_fast import loads as _loads_json5
from olano.subagents import _defaults
from olano.subagents._schema import ToolkitAgentDef, ToolkitAgentOverride

logger = logging.getLogger(__name__)

_DEFINITIONS_DIR = Path(__file__).parent / "definitions"


# ── JSON5 loading ──────────────────────────────────────────────────────────


def _load_json5(text: str) -> dict[str, Any]:
    """Parse a ToolkitAgent definition file (shared loader — see json5_fast)."""
    return cast("dict[str, Any]", _loads_json5(text))


# ── Index ──────────────────────────────────────────────────────────────────

_INDEX: dict[str, ToolkitAgentDef] | None = None  # key = normalized name


def _normalize(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _scan_definitions() -> dict[str, ToolkitAgentDef]:
    out: dict[str, ToolkitAgentDef] = {}
    if not _DEFINITIONS_DIR.exists():
        return out
    for path in sorted(_DEFINITIONS_DIR.glob("*.json5")):
        try:
            data = _load_json5(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Skipping malformed subagent def %s: %s", path, exc)
            continue
        try:
            defn = ToolkitAgentDef.model_validate(data)
        except Exception as exc:
            logger.warning("Invalid ToolkitAgentDef in %s: %s", path, exc)
            continue
        out[_normalize(defn.name)] = defn
        # Also index by toolkit slug so `builtin_subagents: ["stripe"]` resolves.
        out.setdefault(_normalize(defn.toolkit), defn)
        for alias in defn.aliases:
            out.setdefault(_normalize(alias), defn)
    return out


def _auto_generate(toolkit: str) -> ToolkitAgentDef | None:
    """Synthesize a ToolkitAgentDef from a registered toolkit."""
    from olano.tools.registry import _TOOLKITS

    if toolkit not in _TOOLKITS:
        return None
    name = _defaults.default_agent_name(toolkit)
    return ToolkitAgentDef(
        name=name,
        toolkit=toolkit,
        description=f"Specialist for the '{toolkit}' toolkit.",
        emoji=_defaults.default_emoji(toolkit),
        system_prompt=_defaults.default_system_prompt(toolkit, name),
        default_tools="all",
        security=None,  # registry applies _defaults.default_security() at resolve time
        category=_defaults.default_category(toolkit),
        catalog_visible=_defaults.default_catalog_visible(toolkit),
        credential_env_vars=_defaults.default_credential_env_vars(toolkit),
        tags=[toolkit],
    )


def _ensure_index() -> dict[str, ToolkitAgentDef]:
    global _INDEX
    if _INDEX is not None:
        return _INDEX

    from olano.tools.registry import _TOOLKITS

    idx: dict[str, ToolkitAgentDef] = {}
    # Auto-generate from every registered toolkit first…
    for toolkit in _TOOLKITS:
        defn = _auto_generate(toolkit)
        if defn is None:
            continue
        idx[_normalize(defn.name)] = defn
        idx.setdefault(_normalize(toolkit), defn)
    # …then let hand-authored defs override.
    for key, defn in _scan_definitions().items():
        idx[key] = defn
        idx[_normalize(defn.name)] = defn

    _INDEX = idx
    return idx


def invalidate_cache() -> None:
    """Drop the cached index — call after writing a new definition file."""
    global _INDEX
    _INDEX = None


# ── Public API ─────────────────────────────────────────────────────────────


def list_toolkit_agents(*, catalog_only: bool = False) -> list[ToolkitAgentDef]:
    """Return every distinct ToolkitAgentDef known to the registry.

    ``catalog_only=True`` filters to defs with ``catalog_visible=True`` —
    use this for the dashboard / public picker.
    """
    idx = _ensure_index()
    seen: set[str] = set()
    out: list[ToolkitAgentDef] = []
    for defn in idx.values():
        if defn.name in seen:
            continue
        if catalog_only and not defn.catalog_visible:
            continue
        seen.add(defn.name)
        out.append(defn)
    out.sort(key=lambda d: (d.category, d.name))
    return out


def load_toolkit_agent(name: str) -> ToolkitAgentDef:
    """Resolve a name (agent name, toolkit slug, or alias) to a def."""
    idx = _ensure_index()
    defn = idx.get(_normalize(name))
    if defn is None:
        available = ", ".join(sorted({d.name for d in idx.values()}))
        raise KeyError(f"Unknown ToolkitAgent '{name}'. Available: {available}")
    return defn


def resolve_for_toolkit(toolkit: str) -> ToolkitAgentDef:
    """Reverse lookup: a toolkit slug → the ToolkitAgentDef bound to it."""
    return load_toolkit_agent(toolkit)


def _expand_tools(defn: ToolkitAgentDef, override: ToolkitAgentOverride | None) -> list[str]:
    """Compute the effective tool list for a def + optional override."""
    from olano.tools.registry import _TOOLKITS

    if defn.default_tools == "all":
        try:
            import importlib

            mod = importlib.import_module(_TOOLKITS[defn.toolkit])
            base = [t.name for t in mod.get_tools()]
        except Exception as exc:
            logger.warning(
                "Could not expand 'all' tools for toolkit '%s': %s — falling back to empty list",
                defn.toolkit,
                exc,
            )
            base = []
    else:
        base = list(defn.default_tools)

    if override is None:
        return base

    seen: set[str] = set()
    merged: list[str] = []
    for name in [*base, *override.extra_tools]:
        if name in override.disabled_tools or name in seen:
            continue
        seen.add(name)
        merged.append(name)
    return merged


def resolve_as_subagent(
    name: str,
    override: ToolkitAgentOverride | None = None,
) -> SubAgentConfig:
    """Resolve a ToolkitAgent name + optional override into a SubAgentConfig.

    Tools are expanded against the toolkit's live ``get_tools()`` so newly
    added tools propagate without touching defs.
    """
    defn = load_toolkit_agent(name)
    tools = _expand_tools(defn, override)
    model = override.model if override and override.model else None
    system_prompt = (
        override.system_prompt if override and override.system_prompt else defn.system_prompt
    )
    extension = (override.system_prompt_extension or "").strip() if override else ""
    if extension:
        # Owner-authored additions on top of the effective base prompt — the
        # def's prompt stays live (toolkit updates land), the additions ride
        # along. Same composition rule as archetype subagents
        # (AgentConfig._resolve_subagent_references).
        system_prompt = (
            f"{system_prompt.rstrip()}\n\n## Additional owner instructions\n{extension}"
        ).strip()
    return SubAgentConfig(
        name=defn.name,
        description=defn.description,
        system_prompt=system_prompt,
        model=model,
        tools=tools,
        enabled=True,
        system_prompt_extension=extension or None,
    )


def effective_security(defn: ToolkitAgentDef):
    """Return the SecurityConfig that should land in agent.json5 on install."""
    return defn.security if defn.security is not None else _defaults.default_security()
