"""Archetype and integration catalogs and installs, MCP directory.

Split out of ``olano.runtime.api.create_api``; see ``olano.runtime.routes``.
"""

import asyncio
from typing import Any, Optional

from fastapi import FastAPI, HTTPException

from olano.deployment import enforcement as dep_enforcement
from olano.deployment import guard as dep_guard
from olano.runtime import api as _api


def register_archetypes_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach the archetypes routes to ``app``."""
    # Closures defined by an earlier route module (see routes/__init__.py).
    _welcome_new_agent = closures["_welcome_new_agent"]

    # ── Archetype catalog ─────────────────────────────────────────────────────

    @app.get("/api/archetypes")
    async def list_archetypes_api(refresh: bool = False) -> dict:
        """Return all available archetypes split into specialists vs teams.

        Specialists are leaf-node archetypes (no `subagents`) suitable for
        installing as standalone agents or composing into teams via
        `from_archetype`. Teams supervise multiple subagents.

        Served from the compiled catalog index (fingerprint-cached in memory
        and on disk), in a thread: with the canonical `olano-agents` pack
        installed a cold rebuild opens 202 files, and this used to run inline
        on the event loop. `refresh=true` forces a rebuild from source.
        """
        from olano.archetypes.catalog_index import get_archetype_catalog

        return await asyncio.to_thread(get_archetype_catalog, refresh)

    @app.get("/api/archetypes/{archetype_name}")
    async def get_archetype_detail(archetype_name: str) -> dict:
        """One archetype's template detail — the install preview.

        Powers the dashboard's per-member model table on team installs: the
        top-level model plus every member (archetype refs, inline subagents and
        builtin integrations) with its ref, effective default model and source,
        resolved through the same `_resolve_subagents_for_display` seam the
        agent detail view uses — prompts included, so the picker can show what
        each member runs before installing.
        """
        from olano.archetypes.installer import load_archetype_template

        def _collect() -> dict:
            template = load_archetype_template(archetype_name)
            view = _api._resolve_subagents_for_display(dict(template))
            members = [
                {
                    "ref": s.get("ref"),
                    "name": s.get("name"),
                    "description": s.get("description", ""),
                    "model": s.get("model"),
                    "source": s.get("source"),
                    "emoji": s.get("emoji"),
                }
                for s in view.get("subagents_resolved", [])
            ]
            return {
                "name": archetype_name,
                "label": template.get("label") or template.get("name") or archetype_name,
                "description": template.get("description", ""),
                "model": template.get("model"),
                "emoji": template.get("emoji"),
                "subagents": members,
            }

        try:
            return await asyncio.to_thread(_collect)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    def _resolve_install_name(
        agent_name: Optional[str], display_name: Optional[str], fallback: str
    ) -> Optional[str]:
        """The handle an install lands under.

        An explicit ``agent_name`` is validated and used verbatim (CLI, API
        clients). Otherwise, given a display name, one is minted from it —
        slug plus ``-2``… on a collision — which is the dashboard's path
        since the handle field became read-only. With neither, ``None`` keeps
        the installer's own default (the archetype / toolkit slug), 409 and
        all, for the callers that always relied on it.
        """
        from olano.config.agent_names import (
            is_valid_agent_name,
            mint_agent_name,
            taken_agent_names,
        )

        explicit = (agent_name or "").strip()
        if explicit:
            if not is_valid_agent_name(explicit):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "invalid_agent_name: lowercase letters, digits, dashes and "
                        "underscores only, starting with a letter or digit (max 64)."
                    ),
                )
            return explicit
        desired = (display_name or "").strip()
        if not desired:
            return None
        return mint_agent_name(desired, taken_agent_names(), fallback=fallback)

    @app.post("/api/archetypes/install")
    async def install_archetype_api(body: _api.ArchetypeInstallRequest) -> dict:
        """Install an archetype as a new agent.

        Wraps `olano.archetypes.installer.install_archetype`. The archetype
        is resolved across bundled, external, and user-configured search dirs.
        """
        from olano.archetypes.installer import install_archetype

        _api._validate_brain(body.model, *body.subagent_models.values())
        agent_name = await asyncio.to_thread(
            _resolve_install_name, body.agent_name, body.display_name, body.archetype
        )

        # Config write + workspace scaffold — all blocking disk work.
        try:
            path = await asyncio.to_thread(
                install_archetype,
                name=body.archetype,
                agent_name=agent_name,
                model=body.model,
                subagent_models=body.subagent_models or None,
                overwrite=body.overwrite,
                display_name=body.display_name,
                title=body.title,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ValueError as exc:
            # e.g. subagent_models naming a member the archetype doesn't have
            raise HTTPException(status_code=422, detail=str(exc))
        except dep_enforcement.DeploymentLimitExceeded as exc:
            # Caught here because the blanket handler below would otherwise eat
            # it before the app-level handler ever sees it.
            _api.logger.warning("archetype install denied: %s", exc.decision.code)
            dep_guard.reject(exc.decision)
        except Exception as exc:
            _api.logger.exception("Archetype install failed")
            raise HTTPException(status_code=500, detail=str(exc))

        installed_name = agent_name or body.archetype
        _api._push_event("agent_installed", {"name": installed_name, "archetype": body.archetype})
        online = await _welcome_new_agent(installed_name)
        return {"ok": True, "name": installed_name, "path": str(path), "online": online}

    # ── Integrations catalog (ToolkitAgents) ──────────────────────────────────

    @app.get("/api/integrations")
    async def list_integrations_api() -> dict:
        """Return ToolkitAgents that should appear in the dashboard picker.

        Hides infrastructure toolkits (time, vision, talk_to, etc.) that are
        callable as subagents but not meant for the catalog UI.

        Runs in a thread: the first call in a process builds the ToolkitAgent
        index, which imports `olano.tools.registry` (and langchain with it) —
        seconds of blocking work that must not land on the event loop.
        """

        def _collect() -> dict:
            from olano.subagents.registry import (
                effective_security,
                list_toolkit_agents,
            )

            items: list[dict] = []
            for defn in list_toolkit_agents(catalog_only=True):
                sec = effective_security(defn)
                items.append(
                    {
                        "name": defn.name,
                        "toolkit": defn.toolkit,
                        "label": defn.name,
                        "description": defn.description,
                        "emoji": defn.emoji,
                        "category": defn.category,
                        "tags": defn.tags,
                        "credential_env_vars": defn.credential_env_vars,
                        "writes": bool(sec.hitl_write_tools),
                    }
                )
            return {"integrations": items, "count": len(items)}

        return await asyncio.to_thread(_collect)

    @app.get("/api/integrations/{name}")
    async def get_integration_api(name: str) -> dict:
        """Return the full def for one ToolkitAgent (prompt + tools + security)."""
        from olano.subagents.registry import (
            _expand_tools,
            effective_security,
            load_toolkit_agent,
        )

        try:
            defn = load_toolkit_agent(name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

        sec = effective_security(defn)
        return {
            "name": defn.name,
            "toolkit": defn.toolkit,
            "description": defn.description,
            "emoji": defn.emoji,
            "category": defn.category,
            "system_prompt": defn.system_prompt,
            "tools": _expand_tools(defn, None),
            "credential_env_vars": defn.credential_env_vars,
            "tags": defn.tags,
            "security": sec.model_dump(),
        }

    @app.post("/api/integrations/install")
    async def install_integration_api(body: _api.IntegrationInstallRequest) -> dict:
        """Install a ToolkitAgent as a top-level agent."""
        from olano.subagents.installer import install_toolkit_agent_as_top_level

        agent_name = await asyncio.to_thread(
            _resolve_install_name, body.agent_name, body.display_name, body.toolkit_agent
        )

        # Config write + workspace scaffold — all blocking disk work.
        try:
            path = await asyncio.to_thread(
                install_toolkit_agent_as_top_level,
                body.toolkit_agent,
                install_name=agent_name,
                extra_tools=body.extra_tools,
                disabled_tools=body.disabled_tools,
                model=body.model,
                overwrite=body.overwrite,
                display_name=body.display_name,
                title=body.title,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except Exception as exc:
            _api.logger.exception("Integration install failed")
            raise HTTPException(status_code=500, detail=str(exc))

        installed_name = agent_name or body.toolkit_agent
        _api._push_event(
            "agent_installed",
            {"name": installed_name, "toolkit_agent": body.toolkit_agent},
        )
        online = await _welcome_new_agent(installed_name)
        return {"ok": True, "name": installed_name, "path": str(path), "online": online}

    # ── MCP server directory (cloud catalog) ──────────────────────────────────

    @app.get("/api/mcp/directory")
    async def mcp_directory_api(refresh: bool = False) -> dict:
        """Return the MCP server directory for the "Add MCP server" modal.

        Server-side proxy for the cloud registry (GET {CLOUD}/api/v1/mcp): fetched
        here (same-origin, so the SPA needs no CORS or base-URL config), cached
        with a short TTL, and backed by a bundled snapshot when the cloud is
        unreachable — the modal always has a list to show. Response:
        ``{"presets": [...], "examples": [...], "categories": [...],
        "source": "cloud"|"cache"|"bundled"}``. `refresh=true` bypasses the cache.
        """
        import asyncio

        from olano.core import mcp_registry

        return await asyncio.to_thread(mcp_registry.get_directory, refresh)
