"""Tool, skill, command, channel and provider catalogs.

Split out of ``olano.runtime.api.create_api``; see ``olano.runtime.routes``.
"""

import asyncio
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from olano.deployment import enforcement as dep_enforcement
from olano.runtime import api as _api
from olano.runtime import auth as auth_layer

# What the Olano provider card says above its paste field on a self-hosted box.
# Lives here, not on `models.PROVIDERS`, because that note and the synced
# catalog's are what a MANAGED box's card shows, and those must not change.
SELF_HOSTED_OLANO_NOTE = (
    "Billed in olano credits. Needs a deployment connected to cloud.olano.ai, "
    "or an Olano API key (cloud.olano.ai → API keys) pasted on a self-hosted box."
)


def register_catalog_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach the catalog routes to ``app``."""
    # ── Catalog (read-only metadata for the dashboard pickers) ────────────────

    @app.get("/api/catalog/tools")
    async def catalog_tools() -> dict:
        from olano.tools.registry import (
            get_tool_description,
            get_tool_tags,
            list_bundles,
            list_tool_names,
            list_tool_tags,
        )
        from olano.tools.requirements import (
            BUNDLE_REQUIREMENTS,
            get_requirements,
        )

        bundles_map = list_bundles()

        # Build name -> [bundle names] for category derivation.
        name_to_bundles: dict[str, list[str]] = {}
        for bundle, names in bundles_map.items():
            for n in names:
                name_to_bundles.setdefault(n, []).append(bundle)

        cache = getattr(catalog_tools, "_cache", None)
        if cache is None:
            from olano.admin.integrations import PRIVILEGED_TOOLKITS, privileged_tool_names

            privileged_names = privileged_tool_names()
            # First build of a process imports every registered tool module
            # (~2.5s). The boot-time warmer in server.py normally absorbs that,
            # but a request can still land first — yield the loop so it doesn't
            # freeze chat streaming and every other request behind it.
            await asyncio.to_thread(list_tool_names)
            tools_list: list[dict] = []
            for name in list_tool_names():
                desc = get_tool_description(name)

                bundles = name_to_bundles.get(name, [])
                prefix = name.split("_", 1)[0] if "_" in name else None
                # Prefer a bundle whose name matches the tool's prefix exactly
                # (e.g. `gmail_send` → bundle `google`, `wallet_send` → bundle
                # `wallet_hot`). When the prefix matches a bundle name, use it;
                # otherwise pick the first bundle, then fall back to the prefix.
                category = None
                if prefix and prefix in {b for b in bundles}:
                    category = prefix
                elif bundles:
                    # If multiple bundles, prefer the shortest single-word one.
                    category = sorted(bundles, key=lambda b: (len(b), b))[0]
                elif prefix:
                    category = prefix
                else:
                    category = "other"

                tools_list.append(
                    {
                        "name": name,
                        "description": desc,
                        "category": category,
                        "bundles": bundles,
                        "tags": get_tool_tags(name),
                        "requirements": get_requirements(name, bundles),
                        # Fleet-scoped: this tool reads or acts across the whole
                        # deployment (the shared log, other agents' sessions and
                        # workspaces, the vault, cross-agent exec), not just the
                        # agent holding it. It listed here identically to
                        # `web_search`, which is how a fleet-wide log reader ends
                        # up on an ordinary agent by a stray click. The picker
                        # badges it; `_privileged_tools_denial` gates the agent-
                        # driven write. Same set both surfaces read.
                        "privileged": name in privileged_names,
                    }
                )
            # Include each bundle's requirements alongside its tools so the
            # dashboard can show "Add this toolkit" with a one-glance summary.
            bundles_payload: list[dict] = []
            for k, v in sorted(bundles_map.items()):
                bundles_payload.append(
                    {
                        "name": k,
                        "tools": v,
                        "requirements": BUNDLE_REQUIREMENTS.get(k, {}),
                        # The rail's "Add all in toolkit" grants every member at
                        # once, so the toolkit carries the flag as well as its rows.
                        "privileged": k in PRIVILEGED_TOOLKITS,
                    }
                )
            cache = {
                "tools": tools_list,
                "bundles": bundles_payload,
            }
            catalog_tools._cache = cache  # type: ignore[attr-defined]
        # Overlay the live vault/env status (NOT cached — secrets can change
        # between requests). We only return whether the secret exists, never
        # its value.
        env_status = _collect_secret_status(cache["tools"])
        install_status = _collect_install_status(cache["tools"], cache["bundles"])
        from olano.config.ops_defaults import (
            BUILTIN_DOCUMENT_TOOL_NAMES,
            UNIVERSAL_AGENT_TOOL_NAMES,
        )
        from olano.tools.registry import get_disabled_tools

        disabled = get_disabled_tools()
        # Browser tools bind only while config.yaml → browser turns them on;
        # say so on each row so the Tools tab never offers a dead grant.
        from olano.browser.policy import BROWSER_TOOL_NAMES, browser_availability

        browser = browser_availability()
        browser_note = (
            ""
            if browser.enabled
            else (browser.reason or "Browser control is off. Turn it on under Config → Browser.")
        )
        # Tools every agent gets auto-injected (universal social/cognitive fabric
        # + local-document readers). The Tools tab shows these as checked and,
        # when unchecked, records the removal in the agent's `disabled_tools`
        # (a plain `tools` edit cannot drop them — AgentConfig re-injects them).
        agent_defaults = sorted({*UNIVERSAL_AGENT_TOOL_NAMES, *BUILTIN_DOCUMENT_TOOL_NAMES})
        return {
            "tools": [
                {
                    **t,
                    "requirements": _annotate_status(
                        t.get("requirements", {}), env_status, install_status
                    ),
                    "disabled": t["name"] in disabled,
                    "unavailable": browser_note if t["name"] in BROWSER_TOOL_NAMES else "",
                }
                for t in cache["tools"]
            ],
            "bundles": [
                {
                    **b,
                    "requirements": _annotate_status(
                        b.get("requirements", {}), env_status, install_status
                    ),
                }
                for b in cache["bundles"]
            ],
            "env_status": env_status,
            "install_status": install_status,
            "disabled": sorted(disabled),
            "agent_defaults": agent_defaults,
            # Discovery labels (tag → tool names). Includes builder-injected
            # tools such as the per-customer memory surface, which have no
            # registry entry and so never appear in `tools` above.
            "tags": list_tool_tags(),
        }

    @app.post("/api/catalog/tools/reload")
    async def reload_catalog_tools() -> dict:
        """Drop the in-memory tool catalog cache so the next GET rebuilds it.

        The catalog (names, descriptions, bundles) is cached at module level on
        first read and never otherwise invalidated; this lets the dashboard force
        a fresh scan after toolkits change without restarting the runtime.
        """
        if getattr(catalog_tools, "_cache", None) is not None:
            catalog_tools._cache = None  # type: ignore[attr-defined]
        return {"ok": True, "reloaded": True}

    def _collect_secret_status(tools: list[dict]) -> dict[str, dict]:
        """Build {ENV_NAME: {in_env, in_vault, in_connector}} for every secret referenced.

        ``in_connector`` covers credentials a connected OAuth connector supplies
        live through ``get_credential`` (e.g. `MS365_ACCESS_TOKEN` once Microsoft
        365 is connected). Without it the catalog reported every brokered tool as
        "missing keys" even though it works — the broker resolves *above* the
        vault and the environment, so a name that is only ever brokered appears
        in neither list.
        """
        names: set[str] = set()
        for t in tools:
            for spec in (t.get("requirements", {}) or {}).get("env_vars", []) or []:
                n = spec.get("name", "")
                if n:
                    names.add(n)
        # Vault list (lock-tolerant: returns empty when locked).
        vault_names: set[str] = set()
        try:
            from olano.vault.resolver import get_vault

            v = get_vault()
            if v is not None:
                vault_names = {x.get("name", "") for x in v.list_names()}
        except Exception:
            vault_names = set()
        # Credential names a connected connector currently resolves. Same seam
        # get_credential uses, so this cannot drift from what the tools see.
        connector_names: set[str] = set()
        try:
            from olano.core.connectors import exposed_credential_names, resolve_exposed_credential

            for n in names & set(exposed_credential_names()):
                if resolve_exposed_credential(n):
                    connector_names.add(n)
        except Exception:
            connector_names = set()
        result: dict[str, dict] = {}
        for n in names:
            result[n] = {
                "in_env": bool(os.environ.get(n)),
                "in_vault": n in vault_names,
                "in_connector": n in connector_names,
            }
        return result

    def _annotate_status(
        req: dict,
        env_status: dict[str, dict],
        install_status: dict | None = None,
    ) -> dict:
        """Return req with each env_var/package spec gaining live status flags."""
        if not req:
            return req
        out = dict(req)
        env_vars = out.get("env_vars") or []
        out["env_vars"] = [
            {
                **spec,
                **env_status.get(
                    spec.get("name", ""),
                    {"in_env": False, "in_vault": False, "in_connector": False},
                ),
            }
            for spec in env_vars
        ]
        # Attach installed-status for packages/cli so the dashboard can color
        # the chip green when the dependency already exists.
        if install_status:
            for field, src in (
                ("packages", install_status.get("python", {})),
                ("npm_packages", install_status.get("npm", {})),
                ("cli", install_status.get("cli", {})),
            ):
                items = out.get(field) or []
                if items:
                    out[f"{field}_status"] = {x: bool(src.get(x)) for x in items}
        return out

    # Map PyPI package names to their import name when they differ. Most
    # packages match their PyPI name; this dict only carries the mismatches we
    # actually reference in `BUNDLE_REQUIREMENTS` / `TOOL_OVERRIDES`.
    _PY_IMPORT_ALIASES = {
        "tavily-python": "tavily",
        "google-api-python-client": "googleapiclient",
        "google-auth-oauthlib": "google_auth_oauthlib",
        "google-auth-httplib2": "google_auth_httplib2",
        "PyGithub": "github",
        "notion-client": "notion_client",
        "slack-sdk": "slack_sdk",
        "simple-salesforce": "simple_salesforce",
        "eth-account": "eth_account",
    }

    def _is_python_pkg_installed(name: str) -> bool:
        from importlib.util import find_spec

        candidates = [_PY_IMPORT_ALIASES.get(name), name, name.replace("-", "_")]
        for c in candidates:
            if not c:
                continue
            try:
                if find_spec(c) is not None:
                    return True
            except Exception:
                continue
        return False

    def _is_cli_installed(name: str) -> bool:
        # CLI entries are usually a single binary name (`ffmpeg`, `gcloud`).
        # If a "setup_commands"-style multi-word string slips in, only the
        # first token is meaningful for `which`.
        token = name.strip().split()[0] if name.strip() else ""
        if not token:
            return False
        from shutil import which

        return which(token) is not None

    def _collect_install_status(
        tools: list[dict], bundles: list[dict]
    ) -> dict[str, dict[str, bool]]:
        """Return {python: {pkg: bool}, npm: {pkg: bool}, cli: {name: bool}}."""
        py_pkgs: set[str] = set()
        npm_pkgs: set[str] = set()
        cli_names: set[str] = set()
        for source in (tools, bundles):
            for item in source:
                req = item.get("requirements") or {}
                py_pkgs.update(req.get("packages") or [])
                npm_pkgs.update(req.get("npm_packages") or [])
                cli_names.update(req.get("cli") or [])
        py = {p: _is_python_pkg_installed(p) for p in py_pkgs}
        # npm detection from a Python daemon is brittle and olano doesn't
        # currently declare any node deps, so leave it false until needed.
        npm = dict.fromkeys(npm_pkgs, False)
        cli = {c: _is_cli_installed(c) for c in cli_names}
        return {"python": py, "npm": npm, "cli": cli}

    @app.get("/api/catalog/tools/usage")
    async def catalog_tools_usage() -> dict:
        """Map every catalog tool to the agents/subagents that reference it.

        Returns: ``{"<tool>": [{"agent": str, "subagent": str | None}], …}``.
        Used by the dashboard's Tools Explorer to surface "this tool is wired
        into agents X, Y, Z".
        """
        # Members only learn the wiring of agents they can access.
        allowed = auth_layer.allowed_agent_names()

        def _collect() -> dict[str, list[dict]]:
            from olano.config.loader import list_agents

            usage: dict[str, list[dict]] = {}
            for name in list_agents():
                if allowed is not None and name not in allowed:
                    continue
                try:
                    data = _api._read_agent_config_cached(name)
                except Exception:
                    continue
                for tool_name in data.get("tools") or []:
                    usage.setdefault(str(tool_name), []).append({"agent": name, "subagent": None})
                for sub in data.get("subagents") or []:
                    if not isinstance(sub, dict):
                        continue
                    sub_name = sub.get("name") or "?"
                    for tool_name in sub.get("tools") or []:
                        usage.setdefault(str(tool_name), []).append(
                            {"agent": name, "subagent": sub_name}
                        )
            return usage

        return {"usage": await asyncio.to_thread(_collect)}

    @app.get("/api/catalog/tools/disabled")
    async def catalog_tools_disabled() -> dict:
        from olano.tools.registry import get_disabled_tools

        return {"disabled": sorted(get_disabled_tools())}

    @app.post("/api/catalog/tools/disabled")
    async def catalog_tools_disabled_set(body: dict) -> dict:
        """Body: ``{"name": "<tool>", "disabled": bool}``.

        Persists the choice and returns the new full list. Disabled tools are
        skipped at agent boot — running agents pick up the change after their
        next /new (or runtime restart).
        """
        from olano.tools.registry import set_tool_disabled

        name = (body or {}).get("name", "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="name is required")
        disabled = bool((body or {}).get("disabled"))
        result = set_tool_disabled(name, disabled)
        return {"disabled": sorted(result)}

    @app.post("/api/catalog/tools/disabled/bulk")
    async def catalog_tools_disabled_bulk(body: dict) -> dict:
        """Body: ``{"names": ["<tool>", ...], "disabled": bool}``.

        Applies the same disabled state to every name in one shot — used by
        the dashboard's toolkit-level "Enable/Disable all" buttons. Returns
        the new full disabled list.
        """
        from olano.tools.registry import set_tool_disabled

        names = (body or {}).get("names") or []
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise HTTPException(status_code=400, detail="names must be a list of strings")
        names = [n.strip() for n in names if n.strip()]
        if not names:
            raise HTTPException(status_code=400, detail="names cannot be empty")
        disabled = bool((body or {}).get("disabled"))
        result: set[str] = set()
        for n in names:
            result = set_tool_disabled(n, disabled)
        return {"disabled": sorted(result), "applied": names}

    def _skill_browse_context(agent: str | None) -> tuple[Any, Any]:
        """``(config, workspace)`` for the skill-browsing endpoints.

        The same resolution ``catalog_skills`` does: a known agent browses its
        own config + per-agent workspace; anything else (no ``?agent=``, or an
        agent whose config is gone) falls back to the default workspace with no
        config, which is exactly the unscoped listing's view. Returning the
        config is what lets ``browse_roots`` honour ``agent.json5 → skills``.
        """
        from olano.config.loader import find_agent_config_path, load_agent_config
        from olano.config.workspace import get_workspace

        if agent and find_agent_config_path(agent) is not None:
            cfg = load_agent_config(agent)
            return cfg, get_workspace(cfg.workspace)
        return None, get_workspace()

    @app.get("/api/catalog/skills")
    async def catalog_skills(agent: str | None = None) -> dict:
        from pathlib import Path

        from olano.config.workspace import get_workspace
        from olano.skills import browse as skill_browse
        from olano.skills.loader import discover_skills

        if agent:
            auth_layer.assert_agent_access(agent)

        def _scan() -> dict:
            # discover_skills recurses, so scanning a skills dir also surfaces the
            # nested skills/learned/<slug>/SKILL.md entries. Sources are listed
            # low→high priority and deduped last-source-wins: a workspace skill
            # shadows a managed one, and a learned skill (deeper under skills_dir)
            # shadows its top-level namesake.
            by_name: dict = {}

            # Agent-scoped: resolve exactly the dirs the CLI /skills uses, including
            # the agent's per-agent workspace (config.workspace) + config.skills.
            # Without this the listing falls back to the global default workspace,
            # which never contains an agent's per-agent learned skills — the chat
            # /skills slash command then renders "No skills available" even though
            # the agent has skills on disk.
            if agent:
                from olano.config.loader import find_agent_config_path, load_agent_config
                from olano.skills.loader import (
                    compute_quarantined_shadows,
                    load_skills_for_agent,
                )

                if find_agent_config_path(agent) is not None:
                    cfg = load_agent_config(agent)
                    ws = get_workspace(cfg.workspace)
                    # Degraded learned shadows are skipped by location so the
                    # original of the same name is what the catalog lists; they are
                    # returned separately so the UI can surface the quarantine.
                    quarantined = compute_quarantined_shadows(cfg, ws)
                    for d in load_skills_for_agent(cfg, ws):
                        for s in discover_skills(Path(d)):
                            name = s.get("name", "")
                            if name and s.get("location") not in quarantined:
                                by_name[name] = s
                    return {
                        # scope/editable/enabled per row so the dashboard can
                        # badge a managed (shared, read-only here) skill apart
                        # from the agent's own, and draw its on/off switch,
                        # without a request per skill.
                        "skills": skill_browse.annotate(list(by_name.values()), cfg, ws),
                        # The raw field, not just the per-row flag: it may still
                        # name a skill that has since been uninstalled, and the
                        # dashboard writes it back verbatim when toggling.
                        "disabled_skills": list(cfg.disabled_skills or []),
                        "quarantined": [
                            {"location": loc, **info} for loc, info in sorted(quarantined.items())
                        ],
                    }

            # Global fallback (no agent, or unknown agent): managed + default workspace.
            ws = get_workspace()
            for skill_dir in [ws.managed_skills_dir, ws.skills_dir]:
                if skill_dir.exists():
                    for s in discover_skills(skill_dir):
                        name = s.get("name", "")
                        if name:
                            by_name[name] = s
            return {
                "skills": skill_browse.annotate(list(by_name.values()), None, ws),
                "disabled_skills": [],
            }

        # Off the event loop: this walks every skills dir and parses the YAML
        # frontmatter of each SKILL.md — a couple hundred milliseconds that the
        # chat view asks for on every mount and agent switch.
        return await asyncio.to_thread(_scan)

    def _resolve_browsable_skill(location: str, agent: str | None) -> Any:
        """Resolve a ``SKILL.md`` location to its ``SkillRef``, or raise 403/404.

        The path half of the gate for the three skill-browsing endpoints below;
        the caller half (``assert_agent_access``) is asserted on the event loop
        before each handler dispatches here, per the convention the rest of this
        module follows. A location must sit under a directory the agent already
        loads skills from — a managed ``<home>/skills`` folder qualifies (which
        is the point: the Files endpoints cannot reach it), an arbitrary path
        does not.
        """
        from olano.skills import browse as skill_browse

        cfg, ws = _skill_browse_context(agent)
        try:
            return skill_browse.resolve_skill(location, cfg, ws)
        except skill_browse.SkillAccessError as e:
            raise HTTPException(status_code=403, detail=str(e))
        except skill_browse.SkillNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.get("/api/catalog/skills/detail")
    async def catalog_skill_detail(location: str, agent: str | None = None) -> dict:
        """One skill: its SKILL.md, the files bundled with it, and its scope.

        ``location`` is the absolute ``SKILL.md`` path from
        ``GET /api/catalog/skills``. The response says where the skill lives
        (``scope``), whether the dashboard may edit it (``editable`` — true only
        inside the agent's workspace, where ``/api/files`` can write) and, when
        it is editable, the ``workspace_path`` the Files view deep-links to.
        """
        from olano.skills import browse as skill_browse
        from olano.skills.loader import discover_skills

        auth_layer.assert_agent_access(agent or _api._default_agent_name())

        def _detail() -> dict:
            from olano.skills.loader import (
                effective_tool_names,
                gated_skill_reason,
                is_skill_disabled,
            )

            cfg, _ = _skill_browse_context(agent)
            ref = _resolve_browsable_skill(location, agent)
            listing = skill_browse.list_skill_files(ref)
            # Re-read the skill's own metadata (tier, trigger, spec_error …)
            # from its folder rather than trusting anything the caller sent.
            # discover_skills visits shallowest-first and prunes anything nested
            # inside a found skill, so the first entry is this skill — a nested
            # example SKILL.md can never be mistaken for it.
            found = discover_skills(ref.directory)
            meta: dict = found[0] if found else {}
            return {
                "skill": {
                    **meta,
                    "name": meta.get("name") or ref.name,
                    "location": str(ref.skill_md),
                    "directory": str(ref.directory),
                    "source_dir": str(ref.source_dir),
                    "scope": ref.scope,
                    "editable": ref.editable,
                    "workspace_path": ref.workspace_path,
                    # Same answer the list row carries, re-derived here so the
                    # modal never has to trust what opened it.
                    "enabled": not is_skill_disabled(meta.get("name") or ref.name, cfg),
                    "gated": gated_skill_reason(meta, effective_tool_names(cfg)),
                },
                **listing,
                "content": skill_browse.read_skill_file(ref, "SKILL.md"),
            }

        # Walks the skill folder and reads SKILL.md — blocking disk work.
        return await asyncio.to_thread(_detail)

    @app.get("/api/catalog/skills/file")
    async def catalog_skill_file(location: str, path: str, agent: str | None = None) -> dict:
        """One file from inside a skill folder, for the detail view's preview.

        ``path`` is relative to the skill folder. Binary and image files come
        back as metadata with a null ``content``; the raw endpoint below serves
        their bytes.
        """
        from olano.skills import browse as skill_browse

        auth_layer.assert_agent_access(agent or _api._default_agent_name())

        def _read() -> dict:
            ref = _resolve_browsable_skill(location, agent)
            try:
                return skill_browse.read_skill_file(ref, path)
            except skill_browse.SkillAccessError as e:
                raise HTTPException(status_code=403, detail=str(e))
            except skill_browse.SkillNotFoundError as e:
                raise HTTPException(status_code=404, detail=str(e))

        return await asyncio.to_thread(_read)

    @app.get("/api/catalog/skills/file-raw")
    async def catalog_skill_file_raw(location: str, path: str, agent: str | None = None) -> Any:
        """Raw bytes of a file inside a skill folder, with a guessed mime type.

        The image previews in the skill detail view point here, so a skill's
        example screenshots render natively instead of through a base64 round
        trip (mirrors ``/api/files-raw`` for the workspace).
        """
        import mimetypes

        from starlette.responses import FileResponse

        from olano.skills import browse as skill_browse

        auth_layer.assert_agent_access(agent or _api._default_agent_name())

        def _resolve() -> Path:
            ref = _resolve_browsable_skill(location, agent)
            try:
                target = skill_browse.skill_file_path(ref, path)
            except skill_browse.SkillAccessError as e:
                raise HTTPException(status_code=403, detail=str(e))
            if not target.exists():
                raise HTTPException(status_code=404, detail=f"File not found: {path}")
            if not target.is_file():
                raise HTTPException(status_code=400, detail=f"Not a file: {path}")
            return target

        target = await asyncio.to_thread(_resolve)
        mime, _ = mimetypes.guess_type(str(target))
        return FileResponse(str(target), media_type=mime or "application/octet-stream")

    @app.post("/api/skills/repair-names")
    async def repair_skill_names_endpoint(body: dict) -> dict:
        """Make every skill's name agree with its folder.

        Body: ``{"agent": str?, "apply": bool?}``. Defaults to a **dry run** —
        ``apply: false`` returns exactly what applying would do, so the operator
        sees the changes before any happen. ``agent`` also scans every skill dir
        that agent loads from; omit it for the managed ``<home>/skills`` dir only.

        A legal name keeps its name and the folder moves; an illegal name
        (spaces, capitals) takes its folder's name. Collisions and learned forks
        are reported untouched (see ``repair_skill_names``). When an applied
        repair leaves nothing behind, the agent's alert is cleared at once
        instead of waiting for its next boot.
        """
        from olano.admin.skills import repair_skill_names

        body = body or {}
        agent = body.get("agent") or None
        if agent:
            auth_layer.assert_agent_admin(str(agent))
        else:
            auth_layer.assert_admin()
        apply = bool(body.get("apply", False))
        # Walks every skills dir and re-parses each SKILL.md, then renames — all
        # blocking disk work.
        result = await asyncio.to_thread(
            repair_skill_names, str(agent) if agent else None, apply=apply
        )
        if apply and result.get("count"):
            _api._push_event("skills_repaired", {"agent": agent, "count": result["count"]})
        if apply and agent and not result.get("skipped"):
            remaining = await asyncio.to_thread(repair_skill_names, str(agent), apply=False)
            if not remaining.get("count") and not remaining.get("skipped"):
                from olano.runtime import notifications

                notifications.resolve(f"skills_spec_violation:{agent}")
        return result

    @app.get("/api/catalog/commands")
    async def catalog_commands() -> dict:
        """The built-in chat slash-command catalog for the agent Commands editor.

        Returns every built-in command grouped, with its default chat exposure
        (sensitive ⇒ off). The editor overlays the agent's saved
        ``commands.enabled`` deviations on top of these defaults.
        """
        from olano.config.commands_catalog import COMMAND_GROUPS, COMMAND_SPECS

        return {
            "groups": [{"id": gid, "label": label} for gid, label in COMMAND_GROUPS],
            "commands": [
                {
                    "name": s.name,
                    "description": s.description,
                    "group": s.group,
                    "sensitive": s.sensitive,
                    "default_exposed": s.default_exposed,
                    "aliases": list(s.aliases),
                }
                for s in COMMAND_SPECS
            ],
        }

    @app.get("/api/catalog/channels")
    async def catalog_channels() -> dict:
        """List every connector the runtime can boot: legacy ``CHANNEL_MAP`` rows
        plus ``platform:<name>`` Hermes-style adapters from ``PLATFORM_MAP``."""
        from olano.platforms.catalog import build_platform_catalog

        return {"channels": build_platform_catalog()}

    def _catalog_providers_payload(basic: bool = False) -> dict:
        """Assemble the provider catalog. Blocking — call it via ``to_thread``.

        ``basic=True`` drops the per-model payload (``models`` + ``model_meta``)
        and all the work behind it. That work dominates the response on a cold
        process: the reasoning probe imports a langchain provider package and
        instantiates a chat class *per model name* to read its profile, and
        ollama discovery shells out to ``ollama list`` (up to its 5s timeout
        when the binary is installed but the daemon isn't answering). Callers
        that only render the provider list and its key/auth state — the
        onboarding picker — shouldn't wait on any of it.
        """
        from olano.catalog import models_registry as model_catalog
        from olano.models import PROVIDERS, get_provider_models, is_provider_available
        from olano.models.codex_auth import auth_status
        from olano.models.reasoning import reasoning_effort_info, resolve_reasoning_effort

        # Synced cloud catalog (bundled/disk/cloud — never a network wait here):
        # supplies the per-provider model lists plus capability/pricing/context
        # metadata the hardcoded PROVIDERS dict never had.
        catalog = model_catalog.get_model_catalog()
        cat_providers = {p.get("key"): p for p in catalog.get("providers", [])}
        cat_chat: dict[str, list[dict]] = {}
        cat_by_id: dict[str, dict] = {}
        if not basic:
            from olano.models.olano_gateway import is_olano_utility_model

            for entry in catalog.get("models", []):
                entry_id = str(entry.get("id") or "")
                cat_by_id[entry_id] = entry
                # Utility groups (olano:vision, olano:olano-image) are priced
                # and routed in the cloud catalog like any other row, but the
                # product calls them on the agent's behalf — they must never
                # reach a model picker, whatever modality the row carries.
                if entry.get("modality") == "chat" and not is_olano_utility_model(entry_id):
                    cat_chat.setdefault(str(entry.get("provider") or ""), []).append(entry)
        # Providers whose model list is discovered live (installed ollama tags,
        # the Codex account's models, the user's own proxy) — the catalog only
        # enriches those, it must not inject models the instance doesn't have.
        dynamic_list_providers = {"ollama", "openai-codex", "litellm"}

        # A key counts as "set" if it's in the process env OR stored in the vault
        # (the vault is hydrated into env lazily at first model build, so env
        # alone undercounts configured keys). Lock-tolerant: empty when locked.
        vault_names: set[str] = set()
        try:
            from olano.vault.resolver import get_vault

            v = get_vault()
            if v is not None:
                vault_names = {x.get("name", "") for x in v.list_names()}
        except Exception:
            vault_names = set()

        def _key_set(env_var: str | None) -> bool:
            if env_var is None:
                return True
            return bool(os.environ.get(env_var)) or env_var in vault_names

        result = []
        for key, info in PROVIDERS.items():
            auth = None
            if key == "openai-codex":
                raw_status = auth_status()
                auth = {
                    "type": "device_code",
                    "connected": bool(raw_status.get("logged_in")),
                    "status": "connected" if raw_status.get("logged_in") else "not_connected",
                    "error": raw_status.get("error"),
                    "auth_store": raw_status.get("auth_store"),
                    "token_preview": raw_status.get("token_preview"),
                    "expires_soon": raw_status.get("expires_soon"),
                }
            elif key == "olano":
                # env_var is None, so the generic `env_set` below reads True —
                # but Olano tiers authenticate with the per-deployment gateway
                # key (enrollable over the deployment token), and an unattached
                # install can only error at first use. Publish an `auth` object
                # (the picker prefers it over env_set, see catalog.ts
                # providerConnected) with the real state.
                # THREE states, not two. The old
                # `bool(gateway_key() or deployment_token())` read "connected"
                # for a box that is attached but holds no gateway key — the
                # exact state where every olano tier 401s, and the exact state a
                # human opens this card to diagnose (incident 2026-08-14).
                from olano.deployment.gateway_key import enrollment_state

                olano_state = enrollment_state()
                auth = {
                    "type": "deployment_token",
                    # Stays "can this box use olano at all" — an attached box
                    # can, because enrollment self-heals. `providerUsable()` in
                    # the dashboard gates the model pickers on this, so
                    # narrowing it to `enrolled` would hide every olano tier
                    # mid-enrollment, including from agents already set to one.
                    # `status` carries the degraded signal instead.
                    "connected": olano_state != "unattached",
                    "status": {
                        "enrolled": "connected",
                        "unenrolled": "degraded",
                        "unattached": "not_connected",
                    }[olano_state],
                    "error": {
                        "enrolled": None,
                        "unenrolled": (
                            "Attached to cloud.olano.ai, but this box has no gateway key "
                            "yet — Olano models will fail until enrollment succeeds. "
                            "Retrying automatically; see Diagnostics → Olano gateway."
                        ),
                        "unattached": "Olano models need a deployment attached to cloud.olano.ai",
                    }[olano_state],
                }
                # A self-hosted (open) box can run the tiers on a pasted
                # account API key (Providers → Olano). Two keys, ADDED only on
                # that posture, so a managed deployment's payload stays
                # byte-identical: `source` says which family serves the key
                # and `manageable` that the card may connect/remove one.
                try:
                    from olano.deployment import resolver as dep_resolver
                    from olano.deployment.gateway_key import (
                        account_key_rejected,
                        gateway_key_source,
                    )

                    if dep_resolver.deployment_status().posture == "open":
                        auth["source"] = gateway_key_source()
                        auth["manageable"] = True
                        if olano_state == "unattached":
                            auth["error"] = (
                                "Olano models need an Olano API key (cloud.olano.ai → API "
                                "keys) pasted here, or a deployment attached to cloud.olano.ai."
                            )
                        elif auth["source"] == "account" and account_key_rejected():
                            auth["status"] = "degraded"
                            auth["error"] = (
                                "The Olano API key was refused by the gateway (revoked on "
                                "cloud.olano.ai, or pasted incorrectly). Paste a valid key."
                            )
                except Exception:  # pragma: no cover - defensive
                    pass
            models: list[str] = []
            if not basic:
                local_models = get_provider_models(key)
                if key in dynamic_list_providers:
                    models = list(local_models)
                else:
                    # Catalog order first (it carries tiers/recommended sorting),
                    # then any local-only extras so a catalog lag never hides a
                    # model the install already knows about.
                    models = [str(e.get("model")) for e in cat_chat.get(key, [])]
                    models.extend(m for m in local_models if m not in models)
                if key == "olano":
                    # Retired tiers still resolve for agents already configured
                    # with them, but no picker may offer them — filtered here
                    # rather than in the dashboard because the cloud catalog can
                    # list a tier this build has since withdrawn.
                    from olano.models.olano_gateway import tier_offered

                    models = [m for m in models if tier_offered(m)]
            # Per-model metadata: reasoning-effort capability from langchain's
            # model profiles (models/reasoning.py — authoritative for the class
            # we actually build), plus capabilities/pricing/context from the
            # synced cloud catalog. Only models with something to say get an
            # entry, so the UI shows pickers/badges exactly when valid.
            model_meta: dict[str, dict] = {}
            for m in models:
                meta: dict = {}
                effort_info = reasoning_effort_info(f"{key}:{m}")
                if effort_info:
                    # default_effort = what an unset agent value means for this
                    # model (None → "leave the model's thinking alone").
                    meta["reasoning_efforts"] = effort_info["levels"]
                    meta["default_effort"] = resolve_reasoning_effort(f"{key}:{m}", None)
                entry = cat_by_id.get(f"{key}:{m}")
                if entry:
                    pricing = None
                    if entry.get("priceInput") is not None:
                        pricing = {
                            "input": entry.get("priceInput"),
                            "output": entry.get("priceOutput"),
                            "cache_read": entry.get("priceCacheRead"),
                            "cache_write": entry.get("priceCacheWrite"),
                        }
                    meta.update(
                        {
                            "display_name": entry.get("displayName"),
                            "description": entry.get("description"),
                            "tier": entry.get("tier"),
                            "recommended": entry.get("recommended"),
                            "default_for_provider": entry.get("defaultForProvider"),
                            "deprecated": entry.get("deprecated"),
                            "eol_date": entry.get("eolDate"),
                            "context_window": entry.get("contextWindow"),
                            "max_output_tokens": entry.get("maxOutputTokens"),
                            "capabilities": {
                                "vision": entry.get("vision"),
                                "audio_input": entry.get("audioInput"),
                                "image_gen": entry.get("imageGen"),
                                "tts": entry.get("tts"),
                                "stt": entry.get("stt"),
                                "reasoning": entry.get("reasoning"),
                            },
                            "pricing": pricing,
                        }
                    )
                if meta:
                    model_meta[m] = meta
            cat_provider = cat_providers.get(key) or {}
            result.append(
                {
                    "key": key,
                    "name": info.name,
                    "package": info.package,
                    "env_var": info.env_var,
                    "available": is_provider_available(key),
                    "env_set": _key_set(info.env_var),
                    "models": models,
                    "model_meta": model_meta,
                    # The catalog's note for olano ("needs a deployment connected
                    # to cloud.olano.ai") is false exactly where the card offers
                    # a paste field, so a SELF-HOSTED box takes the sentence
                    # below; a managed box keeps the synced note byte for byte.
                    "notes": (
                        SELF_HOSTED_OLANO_NOTE
                        if key == "olano" and auth and auth.get("manageable")
                        else cat_provider.get("notes") or info.notes
                    ),
                    "docs_url": cat_provider.get("docsUrl"),
                    "console_url": cat_provider.get("consoleUrl"),
                    "auth": auth,
                }
            )
        # Deployment entitlements: when model selection or the plan's BYOK
        # entitlement is off, the catalog is still visible but marked locked
        # so the UI can disable key entry / model pickers. `byok_locked` is
        # surfaced separately so the Providers view can say "upgrade for BYOK"
        # instead of a generic lock.
        byok_locked = not dep_enforcement.feature_enabled("byok")
        locked = not dep_enforcement.feature_enabled("model_selection") or byok_locked
        if locked:
            for entry in result:
                entry["locked"] = True
        # `default:*` aliases — the deployment-default picker options. Served in
        # BOTH shapes (one cached config read, no per-model work, so the basic
        # path stays cheap): the frontend needs the alias→target map to label
        # the options, gate usability on the target's provider, and resolve the
        # context bar without re-implementing the backend's resolution rules.
        model_aliases = []
        try:
            from olano.models.aliases import ALIAS_TIERS, alias_targets

            targets = alias_targets()
            model_aliases = [
                {"value": f"default:{tier}", "tier": tier, "target": targets.get(tier)}
                for tier in ALIAS_TIERS
            ]
        except Exception:  # a broken config block must not take the catalog down
            _api.logger.debug("model_aliases: resolution failed", exc_info=True)
        return {
            "providers": result,
            "model_aliases": model_aliases,
            "model_selection_locked": locked,
            "byok_locked": byok_locked,
            # Echoed back so a caller can tell a basic payload from a full one
            # without inferring it from empty model lists.
            "fields": "basic" if basic else "full",
            # Where the model data came from + its cloud revision, for the
            # Providers view's freshness hint and debugging stale catalogs.
            "catalog": {
                "revision": catalog.get("revision", 0),
                "source": catalog.get("source"),
            },
        }

    @app.get("/api/catalog/providers")
    async def catalog_providers(fields: str = "full") -> dict:
        """List LLM providers. ``?fields=basic`` omits ``models``/``model_meta``.

        Off the event loop either way: assembling the full payload blocks for
        seconds on a cold process, and doing that inline stalls every other
        request on the server for the duration.
        """
        return await asyncio.to_thread(_catalog_providers_payload, fields == "basic")

    @app.get("/api/catalog/status")
    async def catalog_sync_status() -> dict:
        """Per-catalog sync state: revision, data source, last fetch time."""
        from olano.archetypes import catalog_index
        from olano.catalog import (
            agents_registry,
            connectors_registry,
            credits_registry,
            skills_registry,
        )
        from olano.catalog import models_registry as model_catalog
        from olano.catalog import store as catalog_store
        from olano.config.system_settings import load_catalog_sync_settings
        from olano.core import mcp_registry

        settings = load_catalog_sync_settings()
        mcp_state = catalog_store.state_entry("mcp")
        return {
            "enabled": settings.enabled,
            "manifest_poll_minutes": settings.manifest_poll_minutes,
            "catalogs": {
                "models": model_catalog.catalog_status(),
                "mcp": {
                    "source": mcp_registry.get_directory().get("source"),
                    "fetched_at": mcp_state.get("fetched_at"),
                },
                "credits": credits_registry.catalog_status(),
                # Official-connector clients (public ids only): how many
                # providers offer one-click "via Olano's app" connections on
                # this box. `official_count: 0` with source `bundled` is the
                # signature of a box that never synced the connectors catalog.
                "connectors": connectors_registry.catalog_status(),
                # Local, not cloud-synced: reports where the compiled archetype
                # catalog is served from plus whether the canonical pack is on
                # disk, so "the Expert list only has five entries" is
                # diagnosable without shell access to the box.
                "archetypes": catalog_index.status(),
                # The cloud-synced archetype pack that feeds the above: how many
                # archetypes were materialized to disk and at what revision.
                # `archetypes: 0` here with a low `specialists` count above is
                # the signature of a box that never received the pack.
                "agents": agents_registry.catalog_status(),
                # The common skills pack, token-gated on the cloud. `source:
                # none` with `token: true` is a box whose deployment token the
                # cloud is rejecting; `token: false` is an unmanaged box, which
                # only ever gets the pack by git.
                "skills": skills_registry.catalog_status(),
            },
        }

    @app.post("/api/catalog/refresh")
    async def catalog_sync_refresh() -> dict:
        """Force a full catalog sync now (all catalogs, revision check skipped)."""
        from olano.archetypes import catalog_index
        from olano.catalog import sync as catalog_sync

        report = await asyncio.to_thread(catalog_sync.sync_once, True, _api._push_event)
        # The archetype catalog is local, so it has no revision to diff — an
        # explicit refresh must re-read the files rather than trust the
        # fingerprint throttle.
        await asyncio.to_thread(catalog_index.get_archetype_catalog, True)
        report["catalogs"]["archetypes"] = {"status": "rebuilt"}
        _api._push_event("catalog_updated", {"catalog": "archetypes"})
        return report
