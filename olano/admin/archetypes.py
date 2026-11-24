"""Archetype discovery + scaffolding.

Wraps :mod:`olano.archetypes.installer` so AgentFather can recommend a
pre-built archetype and then install it as a real agent (config + workspace
scaffold) in one validated call.
"""

from __future__ import annotations

from typing import Any

from olano.archetypes.installer import (
    build_archetype_catalog,
    find_archetype_path,
    install_archetype,
    load_archetype_template,
)
from olano.archetypes.installer import (
    list_archetypes as _list_archetype_names,
)


def list_archetypes() -> dict[str, list[dict[str, Any]]]:
    """Return the archetype catalog split into ``specialists`` and ``teams``.

    Each entry carries ``name``, ``label``, ``description``, ``emoji``,
    ``category`` and ``model`` — enough for AgentFather to make a recommendation
    without loading every template.
    """
    return build_archetype_catalog()


def read_archetype(name: str) -> dict[str, Any]:
    """Return one archetype's full template, read-only, without installing it.

    ``list_archetypes`` only carries the picker fields (label, description,
    emoji, category), so it cannot answer "what tools/prompt/security does this
    archetype actually ship with?" — the template file lives under the package
    (or an external pack) and is deliberately outside every agent's filesystem
    reach. This is the read path for that question.

    Returns:
        ``config``    — the parsed template exactly as authored (system prompt,
                        tools, disabled_tools, memory, backend, security, …).
        ``tools``     — the tool list the installed agent ends up with: the
                        template's own ``tools`` merged with the universal
                        defaults every agent receives, minus ``disabled_tools``,
                        plus the deepagents built-ins. This is what the agent's
                        ``whoami`` would report, so it is the honest answer to
                        "what are this archetype's default tools?".
        ``auto_generated_tools`` — memory/RAG/graph tools the *builder* wires
                        from the memory config rather than from ``tools`` (e.g.
                        the per-customer ``remember`` / ``recall`` / ``forget_me``
                        surface). Present only when the template pins them via a
                        ``top_level_*_tools`` override; otherwise the defaults
                        for the configured memory mode apply.
    """
    path = find_archetype_path(name)
    if path is None:
        available = ", ".join(_list_archetype_names())
        raise FileNotFoundError(f"Archetype '{name}' not found. Available: {available}")

    template = load_archetype_template(name)
    result: dict[str, Any] = {
        "archetype": path.stem,
        "name": template.get("name") or path.stem,
        "path": str(path),
        "source": str(path.parent),
        "config": template,
    }
    result.update(_effective_tool_view(template))
    return result


def _effective_tool_view(template: dict[str, Any]) -> dict[str, Any]:
    """Resolve a template's effective tool surface (best-effort).

    Validation runs through :class:`AgentConfig` so the universal-defaults merge
    and ``disabled_tools`` filtering match what an install would really produce.
    A template that fails to validate still returns its raw ``tools`` list — a
    read tool must never fail just because a template is being edited.
    """
    from olano.config.agent_config import AgentConfig
    from olano.config.ops_defaults import resolve_effective_tool_names

    auto: dict[str, list[str]] = {}
    for field in ("top_level_memory_tools", "top_level_rag_tools", "top_level_graph_tools"):
        pinned = template.get(field)
        if pinned:
            auto[field] = list(pinned)

    try:
        config = AgentConfig(**template)
    except Exception as exc:
        return {
            "tools": list(template.get("tools") or []),
            "auto_generated_tools": auto,
            "tools_note": f"Template did not validate ({type(exc).__name__}: {exc}); "
            "showing the raw `tools` list only.",
        }

    return {
        "tools": resolve_effective_tool_names(config.tools, config.disabled_tools, config),
        "disabled_tools": list(config.disabled_tools),
        "auto_generated_tools": auto,
    }


def scaffold_from_archetype(
    archetype: str,
    *,
    as_name: str | None = None,
    overwrite: bool = False,
    model: str | None = None,
    display_name: str | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """Install ``archetype`` as a new agent (config + full workspace scaffold).

    ``display_name`` is what the agent is called ("Hillary"); with no
    ``as_name`` the permanent handle is minted from it (``hillary``,
    ``hillary-2`` on a collision) — the same rule every dashboard create
    follows. ``title`` is the agent's position; empty keeps the archetype's
    own label ("Recruitment Specialist"), so naming the install never costs
    the agent its role.

    Archetype templates hardcode a model (nine of eleven say
    ``anthropic:claude-sonnet-4-6``), which is the wrong default on an install
    with no Anthropic key. Two layers settle that when the caller passes no
    ``model``:

    * **Managed box** — :func:`default_archetype_model` answers with the tier
      this deployment actually bills in credits (``olano:pro`` for the
      conductor, ``olano:turbo`` for everyone else). Reported in ``model_note``
      so AgentFather can say what it installed rather than implying the
      archetype's own literal was used.
    * **Anywhere else** — the template's provider is checked, and if it is not
      connected the cheapest connected model is used instead.

    An explicit ``model`` is always honoured, warning included.
    """
    from olano.admin.agents import resolve_model
    from olano.archetypes.installer import default_archetype_model

    note: str | None = None
    if not model:
        managed = default_archetype_model(archetype)
        if managed:
            model = managed
            note = (
                "No model was specified — this deployment runs on Olano credits, "
                f"so '{archetype}' was installed on '{managed}'."
            )
        else:
            template_model = str(load_archetype_template(archetype).get("model") or "")
            _, note = resolve_model(template_model or None)
            if note and template_model:
                # The template's own model can't run here: swap in a working one
                # rather than scaffolding an agent that fails on its first turn.
                model, note = resolve_model(None)
                note = f"The '{archetype}' archetype ships with '{template_model}'. {note}"
    else:
        model, note = resolve_model(model)

    agent_name = (as_name or "").strip() or None
    if agent_name is None and (display_name or "").strip():
        from olano.config.agent_names import mint_agent_name, taken_agent_names

        agent_name = mint_agent_name(display_name, taken_agent_names(), fallback=archetype)

    path = install_archetype(
        archetype,
        agent_name=agent_name,
        overwrite=overwrite,
        model=model,
        display_name=display_name,
        title=title,
    )
    result: dict[str, Any] = {
        "archetype": archetype,
        "agent": agent_name or archetype,
        "path": str(path),
        "installed": True,
    }
    if note:
        result["model_note"] = note
    return result
