"""Subagent management on an agent.

Split out of ``olano.runtime.api.create_api``; see ``olano.runtime.routes``.
"""

from typing import Any, Optional

from fastapi import FastAPI, HTTPException

from olano.runtime import api as _api


def register_subagents_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach the subagents routes to ``app``."""
    # ── Subagent CRUD ─────────────────────────────────────────────────────────

    @app.post("/api/agents/{name}/subagents/from-integration")
    async def add_subagent_from_integration(
        name: str, body: _api.SubagentFromIntegrationRequest
    ) -> dict:
        """Append a ToolkitAgent reference to the target agent's builtin_subagents.

        Body: ``{"toolkit_agent": "StripeAgent"}``, optionally with overrides
        (``model``, ``system_prompt_extension``, ``extra_tools``,
        ``disabled_tools``) — with any override present the entry is written in
        its dict form, else as the bare name. The AgentConfig validator expands
        the entry into a full SubAgentConfig at load time, so it shows up
        identically to a hand-written subagent.
        """
        toolkit_agent = body.toolkit_agent
        if not toolkit_agent:
            raise HTTPException(status_code=400, detail="`toolkit_agent` is required")
        _api._validate_brain(body.model)

        from olano.subagents.registry import load_toolkit_agent

        try:
            defn = load_toolkit_agent(toolkit_agent)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

        try:
            data = await _api._read_agent_config_for_update(name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        builtin = data.setdefault("builtin_subagents", []) or []

        def _entry_name(entry: Any) -> str:
            if isinstance(entry, dict):
                return str(entry.get("name", ""))
            return str(entry)

        for existing in builtin:
            if _entry_name(existing).lower() == defn.name.lower():
                raise HTTPException(
                    status_code=409,
                    detail=f"Integration '{defn.name}' is already a subagent of '{name}'.",
                )

        overrides = {
            k: v
            for k, v in {
                "model": (body.model or "").strip() or None,
                "system_prompt_extension": (body.system_prompt_extension or "").strip() or None,
                "extra_tools": body.extra_tools or None,
                "disabled_tools": body.disabled_tools or None,
            }.items()
            if v is not None
        }
        builtin.append({"name": defn.name, **overrides} if overrides else defn.name)
        data["builtin_subagents"] = builtin
        await _api._write_agent_config_async(name, data)
        return {"ok": True, "toolkit_agent": defn.name}

    @app.post("/api/agents/{name}/subagents/from-archetype")
    async def add_subagent_from_archetype(
        name: str, body: _api.SubagentFromArchetypeRequest
    ) -> dict:
        """Append a `from_archetype: <ref>` subagent block to an existing agent.

        Used by the dashboard's archetype picker to drop a specialist persona
        into a running team without copy-pasting the full system_prompt.
        Optional overrides (``model``, ``system_prompt_extension``, ``tools``)
        are written next to the ref — the AgentConfig validator merges them
        over the specialist's defaults, so the ref stays live.
        """
        archetype = body.archetype
        if not archetype:
            raise HTTPException(status_code=400, detail="`archetype` is required")
        _api._validate_brain(body.model)

        from olano.archetypes.installer import load_archetype_template

        try:
            specialist = load_archetype_template(archetype)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

        if specialist.get("subagents"):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Cannot add team archetype '{archetype}' as a subagent — "
                    "specialists referenced via `from_archetype` must be leaf nodes."
                ),
            )

        try:
            data = await _api._read_agent_config_for_update(name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        subagents = data.setdefault("subagents", []) or []

        # Avoid duplicates: skip if the same archetype is already referenced.
        for s in subagents:
            if isinstance(s, dict) and s.get("from_archetype") == archetype:
                raise HTTPException(
                    status_code=409,
                    detail=f"Subagent referencing '{archetype}' already exists",
                )

        stub: dict[str, Any] = {"from_archetype": archetype}
        model_text = (body.model or "").strip()
        if model_text:
            stub["model"] = model_text
        extension_text = (body.system_prompt_extension or "").strip()
        if extension_text:
            stub["system_prompt_extension"] = extension_text
        if body.tools is not None:
            stub["tools"] = body.tools
        subagents.append(stub)
        data["subagents"] = subagents
        await _api._write_agent_config_async(name, data)
        return {"ok": True, "archetype": archetype}

    @app.post("/api/agents/{name}/subagents")
    async def add_subagent(name: str, body: _api.SubagentCreateRequest) -> dict:
        try:
            data = await _api._read_agent_config_for_update(name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        subagents = data.setdefault("subagents", []) or []
        if any(s.get("name") == body.name for s in subagents):
            raise HTTPException(status_code=409, detail=f"Subagent '{body.name}' already exists")

        _api._validate_brain(body.model)
        sub = {
            "name": body.name,
            "description": body.description,
            "system_prompt": body.system_prompt,
            "tools": body.tools,
            "skills": body.skills,
        }
        if body.model:
            sub["model"] = body.model
        subagents.append(sub)
        data["subagents"] = subagents
        await _api._write_agent_config_async(name, data)
        return {"ok": True}

    def _is_catalog_sourced_ref(data: dict, ref: str) -> bool:
        """True when ``ref`` names a catalog-sourced subagent — an archetype
        stub in ``subagents`` or an entry in ``builtin_subagents``."""
        target = ref.strip().lower()
        for s in data.get("subagents", []) or []:
            if (
                isinstance(s, dict)
                and s.get("from_archetype")
                and (s["from_archetype"] == ref or str(s.get("name", "")).strip().lower() == target)
            ):
                return True
        from olano.subagents.registry import load_toolkit_agent

        for entry in data.get("builtin_subagents", []) or []:
            entry_ref = str(entry.get("name", "") if isinstance(entry, dict) else entry).strip()
            if not entry_ref:
                continue
            if entry_ref.lower() == target:
                return True
            try:
                if load_toolkit_agent(entry_ref).name.lower() == target:
                    return True
            except KeyError:
                pass
        return False

    @app.put("/api/agents/{name}/subagents/{sub_name}")
    async def update_subagent(name: str, sub_name: str, body: _api.SubagentCreateRequest) -> dict:
        try:
            data = await _api._read_agent_config_for_update(name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        _api._validate_brain(body.model)
        # A catalog-sourced subagent (archetype stub / builtin entry) is edited
        # through its OVERRIDES, never by full replacement — a rewrite here
        # would drop the `from_archetype` ref and freeze a stale copy of the
        # specialist's prompt into agent.json5.
        if _is_catalog_sourced_ref(data, sub_name):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"'{sub_name}' is a catalog-sourced subagent. Edit it via "
                    f"PUT /api/agents/{name}/subagents/{sub_name}/overrides so its "
                    "catalog reference stays live."
                ),
            )
        subagents = data.get("subagents", []) or []
        for i, s in enumerate(subagents):
            if s.get("name") == sub_name:
                updated = {
                    "name": body.name,
                    "description": body.description,
                    "system_prompt": body.system_prompt,
                    "tools": body.tools,
                    "skills": body.skills,
                }
                if body.model:
                    updated["model"] = body.model
                subagents[i] = updated
                data["subagents"] = subagents
                await _api._write_agent_config_async(name, data)
                return {"ok": True}

        raise HTTPException(status_code=404, detail=f"Subagent '{sub_name}' not found")

    @app.put("/api/agents/{name}/subagents/{ref}/overrides")
    async def update_subagent_overrides(
        name: str, ref: str, body: _api.SubagentOverridesRequest
    ) -> dict:
        """Replace a catalog-sourced subagent's override set.

        PUT semantics over the OVERRIDES, not the subagent: the stored entry is
        rewritten as its bare ref plus exactly the non-empty fields provided —
        an omitted/empty field clears that override back to the catalog
        default. The `from_archetype` ref (or builtin name) is never dropped,
        and resolved defaults are never written into agent.json5, so the
        subagent keeps tracking its archetype/toolkit.
        """
        _api._validate_brain(body.model)
        try:
            data = await _api._read_agent_config_for_update(name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        def _text(value: Optional[str]) -> Optional[str]:
            return (value or "").strip() or None

        # Archetype stubs first: matched by the `from_archetype` ref, the key
        # the dashboard routes cards by (`_resolve_subagents_for_display`).
        subagents = data.get("subagents", []) or []
        for i, s in enumerate(subagents):
            if not (isinstance(s, dict) and s.get("from_archetype") == ref):
                continue
            stub: dict[str, Any] = {"from_archetype": ref}
            model_text = _text(body.model)
            if model_text:
                stub["model"] = model_text
            if body.system_prompt is not None and body.system_prompt.strip():
                stub["system_prompt"] = body.system_prompt
            extension_text = _text(body.system_prompt_extension)
            if extension_text:
                stub["system_prompt_extension"] = extension_text
            if body.tools is not None:
                stub["tools"] = body.tools
            subagents[i] = stub
            data["subagents"] = subagents
            await _api._write_agent_config_async(name, data)
            return {"ok": True, "ref": ref, "source": "archetype"}

        # Then builtin entries (ToolkitAgents), matched like the DELETE below:
        # by the raw entry name or the resolved canonical ToolkitAgent name.
        from olano.subagents.registry import load_toolkit_agent

        target = ref.strip().lower()
        builtin = data.get("builtin_subagents", []) or []
        for i, entry in enumerate(builtin):
            entry_ref = str(entry.get("name", "") if isinstance(entry, dict) else entry).strip()
            if not entry_ref:
                continue
            matches = entry_ref.lower() == target
            if not matches:
                try:
                    matches = load_toolkit_agent(entry_ref).name.lower() == target
                except KeyError:
                    matches = False
            if not matches:
                continue
            overrides = {
                k: v
                for k, v in {
                    "model": _text(body.model),
                    "system_prompt": (body.system_prompt or "").strip() or None,
                    "system_prompt_extension": _text(body.system_prompt_extension),
                    "extra_tools": body.extra_tools or None,
                    "disabled_tools": body.disabled_tools or None,
                }.items()
                if v is not None
            }
            builtin[i] = {"name": entry_ref, **overrides} if overrides else entry_ref
            data["builtin_subagents"] = builtin
            await _api._write_agent_config_async(name, data)
            return {"ok": True, "ref": ref, "source": "builtin"}

        raise HTTPException(status_code=404, detail=f"Catalog-sourced subagent '{ref}' not found")

    @app.delete("/api/agents/{name}/subagents/{sub_name}")
    async def delete_subagent(name: str, sub_name: str) -> dict:
        try:
            data = await _api._read_agent_config_for_update(name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        # Try removing from the hand-written subagents list first. Match an
        # inline subagent by its `name`, or an archetype stub by the
        # `from_archetype` ref the dashboard surfaces as the card's delete key.
        subagents = data.get("subagents", []) or []
        new_list = [
            s
            for s in subagents
            if not (
                isinstance(s, dict)
                and (s.get("name") == sub_name or s.get("from_archetype") == sub_name)
            )
        ]
        removed = len(new_list) != len(subagents)

        if removed:
            data["subagents"] = new_list
            await _api._write_agent_config_async(name, data)
            return {"ok": True}

        # Fall through to builtin_subagents (ToolkitAgents + OpsManager). Match
        # by either the entry name (bare string or dict) or, for ToolkitAgents,
        # by the resolved canonical name so "AlpacaAgent" deletes an entry
        # written as "alpaca".
        target = sub_name.strip().lower()
        from olano.subagents.registry import load_toolkit_agent

        def _matches_builtin(entry: Any) -> bool:
            ref = entry.get("name") if isinstance(entry, dict) else str(entry)
            if not ref:
                return False
            if ref.strip().lower() == target:
                return True
            try:
                return load_toolkit_agent(ref).name.lower() == target
            except KeyError:
                return False

        builtin = data.get("builtin_subagents") or []
        new_builtin = [b for b in builtin if not _matches_builtin(b)]
        if len(new_builtin) != len(builtin):
            data["builtin_subagents"] = new_builtin
            await _api._write_agent_config_async(name, data)
            return {"ok": True}

        raise HTTPException(status_code=404, detail=f"Subagent '{sub_name}' not found")
