"""Agent roster, org chart and agent config CRUD.

Split out of ``olano.runtime.api.create_api``; see ``olano.runtime.routes``.
"""

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from olano.config.agent_state import is_agent_paused
from olano.config.loader import _load_json5
from olano.deployment import enforcement as dep_enforcement
from olano.deployment import guard as dep_guard
from olano.runtime import api as _api
from olano.runtime import auth as auth_layer


def register_agents_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach the agents routes to ``app``."""
    # Closures defined by an earlier route module (see routes/__init__.py).
    _agent_avatar_url = closures["_agent_avatar_url"]
    _agent_sprites_urls = closures["_agent_sprites_urls"]

    # ── Agents ─────────────────────────────────────────────────────────────────

    @app.get("/api/agents")
    async def list_agents_api() -> list:
        result = await asyncio.to_thread(_agents_roster)
        # Members see only the agents they own or were granted access to.
        return auth_layer.filter_visible_agents(result)

    def _agents_roster() -> list:
        from olano.config.loader import list_agents

        result = []
        seen: set[str] = set()
        # Snapshot: this runs in a worker thread while the loop keeps booting
        # and stopping agents (`_runners[name] = …` / `.pop(…)`), and the
        # per-agent disk work below yields the GIL on every entry — iterating
        # the live dict raises "changed size during iteration" mid-roster.
        for name, runner in list(runtime._runners.items()):
            cfg = runner.config
            # Hidden/internal agents (e.g. provider test agents) never appear in
            # the dashboard roster. Mark as seen so the disk-only pass below
            # doesn't resurrect them.
            if _api._agent_is_hidden(cfg):
                seen.add(name)
                continue
            channels: list = []
            for ch in cfg.channels or []:
                channels.append(ch if isinstance(ch, str) else getattr(ch, "type", "?"))
            sub_count = len(cfg.subagents or [])
            entry = runtime.registry.get(name) if hasattr(runtime, "registry") else None
            result.append(
                {
                    "name": name,
                    "display_name": getattr(cfg, "display_name", "") or "",
                    "title": _api._agent_title(name, getattr(cfg, "org", None)),
                    "emoji": cfg.emoji,
                    "model": cfg.model,
                    "channels": channels,
                    "description": cfg.description,
                    "subagent_count": sub_count,
                    "tool_count": len(cfg.tools or []),
                    "mcp_count": len(cfg.mcp_servers or []),
                    "skill_count": len(cfg.skills or []),
                    "running": True,
                    "paused": is_agent_paused(name),
                    "color": getattr(cfg, "color", None),
                    "avatar_url": _agent_avatar_url(name) or getattr(cfg, "avatar_url", None),
                    "sprites": _agent_sprites_urls(name) or getattr(cfg, "sprites", None),
                    "status": (entry.status if entry else "running"),
                    "error": (entry.error if entry else ""),
                    "blocked": False,
                    "blocked_reason": "",
                }
            )
            seen.add(name)

        # Add disk-only agents (not yet booted) so newly created ones appear
        for name in list_agents():
            if name in seen:
                continue
            try:
                data = _api._read_agent_config_cached(name)
            except Exception:
                continue
            # Hidden/internal agents stay out of the dashboard roster.
            if _api._agent_is_hidden(data):
                continue
            channels = []
            for ch in data.get("channels", []) or []:
                channels.append(ch if isinstance(ch, str) else ch.get("type", "?"))
            entry = runtime.registry.get(name) if hasattr(runtime, "registry") else None
            # If the registry has the agent in error/stopping state, surface that.
            status = "configured"
            err = ""
            if entry:
                status = entry.status or "configured"
                err = entry.error or ""
            paused = is_agent_paused(name)
            if paused and status == "configured":
                status = "paused"
            blocked_reason = getattr(runtime, "_blocked_agents", {}).get(name, "")
            if blocked_reason and status == "configured":
                status = "blocked"
            result.append(
                {
                    "name": data.get("name", name),
                    # Raw dict, not a validated AgentConfig — mirror the
                    # model's never-empty coercion or booted and on-disk rows
                    # disagree about the same agent.
                    "display_name": data.get("display_name") or data.get("name", name),
                    "title": _api._agent_title(name, data.get("org")),
                    "emoji": data.get("emoji", "🦁"),
                    "model": data.get("model", ""),
                    "channels": channels,
                    "description": data.get("description", ""),
                    "subagent_count": len(data.get("subagents", []) or []),
                    "tool_count": len(data.get("tools", []) or []),
                    "mcp_count": len(data.get("mcp_servers", []) or []),
                    "skill_count": len(data.get("skills", []) or []),
                    "running": False,
                    "paused": paused,
                    "color": data.get("color"),
                    "avatar_url": _agent_avatar_url(name) or data.get("avatar_url"),
                    "sprites": _agent_sprites_urls(name) or data.get("sprites"),
                    "status": status,
                    "error": err,
                    "blocked": bool(blocked_reason),
                    "blocked_reason": blocked_reason,
                }
            )

        return result

    # ── Org chart ───────────────────────────────────────────────────────────────

    @app.get("/api/org-chart")
    async def org_chart() -> dict[str, Any]:
        """Org chart graph: humans, agents, and the relationships between them.

        Composes three independent relationship sources into one graph the
        dashboard Org Chart view renders:

        * **Humans → agents** (access): the per-agent owner/member grants from
          the user store (plus the global ``is_admin`` role). This is the
          existing human access-control model.
        * **Agents → agents** (talk): explicit ``talk_to_agent`` allow-lists
          from each agent's ``org`` config (``input_agents`` / ``output_agents``).
        * **Manager → report** (hierarchy): the ``org.manager`` / ``org.reports``
          reporting lines among top-level peer agents.

        Each agent record also carries ``channels``: one ``{type, status,
        detail}`` row per configured messaging connector, from in-memory
        runtime state only (``runtime/channel_status.py``), never a network
        probe, because this endpoint draws the whole fleet at once.

        Access scoping mirrors ``/api/agents``: admins (and single-user installs)
        see the whole org; a non-admin member sees only the agents they can
        access, the agent↔agent edges among those, and their own access edges.
        """
        from olano.config import org_store
        from olano.config.agent_config import OrgConfig
        from olano.config.loader import list_agents, load_agent_config
        from olano.runtime.channel_status import channel_states
        from olano.users.store import get_user_store

        viewer = auth_layer.current_user()
        is_admin = bool(getattr(viewer, "is_admin", False))
        multi_user = auth_layer.multi_user_enabled()
        resources_by_agent = getattr(runtime, "_agent_resources", None)
        skipped_by_agent = getattr(runtime, "_skipped_channels", None)

        # All agent names (booted + on disk), deduped.
        all_names: list[str] = list(runtime._runners.keys())
        for name in list_agents():
            if name not in all_names:
                all_names.append(name)

        def _agent_record(name: str) -> dict[str, Any] | None:
            runner = runtime._runners.get(name)
            cfg = getattr(runner, "config", None)
            if cfg is None:
                try:
                    cfg = load_agent_config(name)
                except Exception:
                    return None
            # Hidden/internal agents (e.g. the repo's test agents) are excluded
            # from the chart just like the /api/agents roster; edges touching
            # them drop out with the record since they never enter `visible`.
            if _api._agent_is_hidden(cfg):
                return None
            # Org placement is governed by the central organization.yaml store
            # when it has an entry; otherwise fall back to the agent's already
            # loaded org block (booted runner or agent.json5) — not a fresh disk
            # read — so unmigrated agents still chart correctly.
            central = org_store.get_org_if_present(name)
            org = central if central is not None else (getattr(cfg, "org", None) or OrgConfig())
            subs = [getattr(s, "name", "") for s in (getattr(cfg, "subagents", None) or [])]
            channels = channel_states(
                getattr(cfg, "channels", None) or [],
                running=runner is not None,
                resources=(
                    resources_by_agent.get(name) if isinstance(resources_by_agent, dict) else None
                ),
                skipped=skipped_by_agent.get(name) if isinstance(skipped_by_agent, dict) else None,
            )
            return {
                "name": name,
                "display_name": getattr(cfg, "display_name", "") or "",
                "emoji": getattr(cfg, "emoji", "🦁"),
                "color": getattr(cfg, "color", None),
                "description": (getattr(cfg, "description", "") or "").strip(),
                "running": runner is not None,
                "title": org.title,
                "manager": org.manager,
                "reports": list(org.reports or []),
                "coworkers": list(org.coworkers or []),
                "input_agents": list(org.input_agents or []),
                "output_agents": list(org.output_agents or []),
                "input_mode": org.effective_mode("input"),
                "output_mode": org.effective_mode("output"),
                "autogenerate": bool(org.autogenerate),
                "subagents": [s for s in subs if s],
                "channels": channels,
            }

        agents: list[dict[str, Any]] = []
        for name in all_names:
            rec = _agent_record(name)
            if rec is not None:
                agents.append(rec)

        # Access scoping for non-admin members.
        store = get_user_store()
        if multi_user and viewer is not None and not is_admin:
            allowed = set(store.list_agent_names(viewer))
            agents = [a for a in agents if a["name"] in allowed]

        visible = {a["name"] for a in agents}
        # Case-insensitive map from any listed name to the canonical agent name.
        name_by_norm = {a["name"].strip().casefold(): a["name"] for a in agents}

        def _resolve(name: str | None) -> str | None:
            return name_by_norm.get((name or "").strip().casefold())

        # Members: scrub raw org relation fields so a visible record can't name
        # non-granted agents (the edges below are already built visible-only,
        # but the per-record manager/reports/… strings would still leak names).
        if multi_user and viewer is not None and not is_admin:
            for a in agents:
                a["manager"] = a["manager"] if _resolve(a["manager"]) in visible else None
                for field in ("reports", "coworkers", "input_agents", "output_agents"):
                    a[field] = [n for n in a[field] if _resolve(n) in visible]

        # ── Agent ↔ agent talk edges (explicit allow-lists only) ──────────────
        talk_seen: set[tuple[str, str]] = set()
        talk_edges: list[dict[str, str]] = []

        def _add_talk(src: str | None, dst: str | None) -> None:
            s, d = _resolve(src), _resolve(dst)
            if not s or not d or s == d:
                return
            key = (s, d)
            if key in talk_seen:
                return
            talk_seen.add(key)
            talk_edges.append({"from": s, "to": d})

        for a in agents:
            # Autogenerate agents derive messaging from their org line, which is
            # already drawn as manager/coworker edges — don't double-draw.
            if a.get("autogenerate"):
                continue
            for peer in a["output_agents"]:
                _add_talk(a["name"], peer)
            for peer in a["input_agents"]:
                _add_talk(peer, a["name"])

        # ── Manager → report hierarchy edges ──────────────────────────────────
        mgr_seen: set[tuple[str, str]] = set()
        manager_edges: list[dict[str, str]] = []

        def _add_mgr(manager: str | None, report: str | None) -> None:
            m, r = _resolve(manager), _resolve(report)
            if not m or not r or m == r:
                return
            key = (m, r)
            if key in mgr_seen:
                return
            mgr_seen.add(key)
            manager_edges.append({"manager": m, "report": r})

        for a in agents:
            _add_mgr(a["manager"], a["name"])
            for rep in a["reports"]:
                _add_mgr(a["name"], rep)

        # ── Co-worker edges (mutual, undirected) ──────────────────────────────
        cow_seen: set[tuple[str, str]] = set()
        coworker_edges: list[dict[str, str]] = []

        def _add_cow(x: str | None, y: str | None) -> None:
            p, q = _resolve(x), _resolve(y)
            if not p or not q or p == q:
                return
            key = (p, q) if p < q else (q, p)
            if key in cow_seen:
                return
            cow_seen.add(key)
            coworker_edges.append({"a": key[0], "b": key[1]})

        for a in agents:
            for cow in a.get("coworkers") or []:
                _add_cow(a["name"], cow)

        # ── Humans + access edges ─────────────────────────────────────────────
        humans: list[dict[str, Any]] = []
        access_edges: list[dict[str, str]] = []

        def _human_record(
            uid: str,
            name: str,
            email: str,
            admin: bool,
            rows: list[dict],
            title: str = "",
        ) -> None:
            grants = [
                {"agent": row["agent_name"], "role": row.get("role", "member")}
                for row in rows
                if row.get("agent_name") in visible
            ]
            humans.append(
                {
                    "id": uid,
                    "name": name or email or "User",
                    "email": email,
                    "title": (title or "").strip(),
                    "is_admin": admin,
                    "agents": grants,
                }
            )
            for g in grants:
                access_edges.append({"user_id": uid, "agent": g["agent"], "role": g["role"]})

        if not multi_user:
            # Single-user install: one implicit owner with admin access to all.
            _human_record(
                "owner",
                "Owner",
                getattr(viewer, "email", "") or "",
                True,
                [{"agent_name": n, "role": "admin"} for n in visible],
            )
        elif is_admin:
            for u in store.db.list_users():
                explicit = store.db.list_agents_for_user(u.id)
                if u.is_admin:
                    # Admins implicitly reach every agent (see users.store.can_chat /
                    # list_agent_names). Materialise that as access edges to all visible
                    # agents, keeping any explicit per-agent role (e.g. owner) and
                    # defaulting the rest to "admin".
                    by_agent = {r["agent_name"]: r.get("role", "member") for r in explicit}
                    rows = [{"agent_name": n, "role": by_agent.get(n, "admin")} for n in visible]
                else:
                    rows = explicit
                _human_record(u.id, u.name, u.email, u.is_admin, rows, getattr(u, "title", ""))
        elif viewer is not None:
            # A non-admin member sees only themselves on the people axis.
            _human_record(
                viewer.id,
                viewer.name,
                viewer.email,
                viewer.is_admin,
                store.db.list_agents_for_user(viewer.id),
                getattr(viewer, "title", ""),
            )

        return {
            "multi_user": multi_user,
            "humans": humans,
            "agents": agents,
            "edges": {
                "access": access_edges,
                "talk": talk_edges,
                "manager": manager_edges,
                "coworker": coworker_edges,
            },
        }

    @app.get("/api/agents/{name}/org")
    async def get_agent_org(name: str) -> dict:
        """Canonical org block for an agent (central store, agent.json5 fallback)."""
        from olano.config import org_store

        auth_layer.assert_agent_access(name)
        try:
            _api._agent_config_path(name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
        return {"name": name, "org": org_store.entry_for(name)}

    @app.put("/api/agents/{name}/org")
    async def update_agent_org(name: str, body: _api.OrgUpdateBody) -> dict:
        """Persist an agent's org block to the central organization.yaml and
        reciprocally patch its counterparts (manager↔reports, co-workers)."""
        from olano.config import org_store
        from olano.config.agent_config import OrgConfig

        try:
            _api._agent_config_path(name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
        try:
            new_org = OrgConfig(
                title=body.title,
                manager=(body.manager or None),
                reports=body.reports,
                coworkers=body.coworkers,
                input_mode=body.input.mode,
                input_agents=body.input.agents,
                output_mode=body.output.mode,
                output_agents=body.output.agents,
                autogenerate=body.autogenerate,
                mutating_from_manager_only=body.mutating_from_manager_only,
            )
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Invalid org config: {exc}")
        try:
            _, touched = org_store.update_org(name, new_org)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        _api._broadcast_roster_changed(name)
        for other in touched:
            _api._broadcast_roster_changed(other)
        return {
            "ok": True,
            "name": name,
            "org": org_store.entry_for(name),
            "touched": sorted(touched),
        }

    # ── Agent CRUD ────────────────────────────────────────────────────────────

    @app.get("/api/agents/{name}")
    async def get_agent(name: str) -> dict:
        from olano.config.loader import find_agent_config_path

        user = auth_layer.assert_agent_access(name)
        path = find_agent_config_path(name)
        if path is None:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        def _load() -> dict:
            text = path.read_text(encoding="utf-8")
            data = _load_json5(text)
            data = _api._resolve_subagents_for_display(data or {})
            # The AgentConfig view backs the tool resolution below; the raw
            # parse above stays the payload (this endpoint returns the file
            # as-is). One parse each instead of the former three.
            try:
                cfg = _api._load_agent_config_cached(name)
            except Exception:
                cfg = None
            # Display-only effective toolset: agent.json5 only lists *extra* tools, so
            # the raw `tools` field is empty for most agents even though every agent
            # runs with the universal + ops defaults (merged by AgentConfig) plus the
            # deepagents built-ins. Surface that resolved list separately — without
            # mutating `tools` — so listings like the dashboard `/tools` command match
            # what the agent actually has (and reports via `whoami`).
            data["tools_resolved"] = _api._resolve_effective_tool_names(name, data, cfg)
            data["internal_tools"] = _api._resolve_internal_tools(name, cfg)
            # Display-only mirror of `org.title` (central store first): the
            # header draws it under the name, and the raw `org` block below
            # may be shadowed by organization.yaml. Never written back — it
            # is in _IMMUTABLE_AGENT_FIELDS.
            data["title"] = _api._agent_title(name, data.get("org"))
            return data

        data = await asyncio.to_thread(_load)
        # Members never see the credential overlay values (config.env may hold
        # secrets); config mutations are admin-gated anyway.
        if not getattr(user, "is_admin", False):
            data.pop("env", None)
        # Per-customer isolation IS customer-service mode
        # (AgentConfig._sync_customer_service_mode) — but this endpoint returns
        # the raw file, and an agent created from the customer-service
        # archetype (or a hand-edited config) sets memory.per_customer without
        # ever writing the switch. Mirror it into the payload so the dashboard
        # toggle reads ON for those agents instead of showing "off" while the
        # agent plainly behaves otherwise.
        if (data.get("memory") or {}).get("per_customer"):
            cs_block = dict(data.get("customer_service") or {})
            if not cs_block.get("enabled"):
                cs_block["enabled"] = True
                data["customer_service"] = cs_block
        # Same reason, for the prompt extension: a blank field on a multi-user
        # agent runs with the support-desk default
        # (AgentConfig._sync_customer_service_mode), so show that text in the
        # editor rather than an empty box the agent isn't actually using.
        cs_block = dict(data.get("customer_service") or {})
        if cs_block.get("enabled") and not (cs_block.get("prompt_extension") or "").strip():
            from olano.config.customer_service import resolve_prompt_extension

            cs_block["prompt_extension"] = resolve_prompt_extension(None)
            data["customer_service"] = cs_block
        data["_running"] = name in runtime._runners
        # Stamp the media URLs the same way the roster does. The config stores
        # them bare, and a bare URL still works — it just revalidates on every
        # use instead of being cached outright. Both are display-only mirrors
        # (`_IMMUTABLE_AGENT_FIELDS`), so the typed editor never writes these back.
        stamped_avatar = _agent_avatar_url(name)
        if stamped_avatar:
            data["avatar_url"] = stamped_avatar
        stamped_sprites = _agent_sprites_urls(name)
        if stamped_sprites:
            data["sprites"] = stamped_sprites
        return data

    @app.get("/api/agents/{name}/context")
    async def get_agent_context(name: str) -> dict:
        """Estimated context-window breakdown by category for the chat /context view.

        Static footprint only (system prompt, tool schemas, subagents, memory
        files, skill summaries); the live conversation fill is derived on the
        client from the last turn's token usage.
        """
        from olano.config.loader import find_agent_config_path

        auth_layer.assert_agent_access(name)
        if find_agent_config_path(name) is None:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
        from olano.runtime.context_report import build_context_report

        try:
            # Config parse + skills walk + tool resolution (first import ~0.35s).
            return await asyncio.to_thread(build_context_report, name)
        except Exception as exc:  # never 500 a read-only overview
            _api.logger.debug("context report for %s failed: %s", name, exc)
            raise HTTPException(status_code=500, detail="Failed to build context report")

    async def _welcome_new_agent(name: str) -> bool:
        """Bring a freshly-provisioned agent onto the team and tell everyone.

        Three things happen so a new teammate is immediately first-class for
        both humans and agents:

        1. **Auto-boot** the agent into the running runtime (best-effort) so it
           is "online" and reachable via ``talk_to_agent``. Booting also nudges
           the prompt refresh sweep (see ``RuntimeServer._boot_agent``), which
           rebuilds every peer's prompt so the newcomer lands in their roster
           block — running conversations learn about it via the one-shot
           prompt-update notice on their next turn.
        2. **Notify humans** with an ``agent_joined`` stream frame so the
           dashboard can pop a welcome toast (see ``ui/toast`` + the listener in
           ``core/currentAgent``).
        3. **Notify agents** by appending a dated welcome line to every *other*
           running agent's daily memory log, which is bootstrapped back into
           their system prompt — so the announcement persists in their context
           rather than evaporating like an in-memory bus message would.

        Always finishes by broadcasting ``agents_update`` so the sidebar agent
        picker and Agents view refresh live. Returns ``True`` if the agent
        booted. Never raises — provisioning already succeeded by the time we get
        here, so any failure here is logged, not surfaced.
        """
        booted = False
        try:
            if name not in runtime._runners:
                await runtime._boot_agent(name)
            booted = name in runtime._runners
        except Exception as exc:  # boot is best-effort; the agent still exists on disk
            _api.logger.warning("Auto-boot of new agent '%s' failed: %s", name, exc)

        # Pull emoji/description for a friendlier announcement (fall back gracefully).
        emoji, description = "🦁", ""
        try:
            cfg = _api._read_agent_config(name)
            emoji = (cfg.get("emoji") or "🦁").strip() or "🦁"
            description = (cfg.get("description") or "").strip()
        except Exception:
            pass

        # 2. Humans — a dedicated frame the dashboard turns into a welcome toast.
        _api._push_event(
            "agent_joined",
            {"name": name, "emoji": emoji, "description": description, "online": booted},
        )

        # 3. Agents — durable note in each running peer's daily memory log.
        try:
            from olano.config.workspace import get_workspace
            from olano.memory.markdown_memory import MarkdownMemory

            desc_part = f" — {description}" if description else ""
            note = (
                f"{emoji} New teammate joined the team: **{name}**{desc_part}. "
                f'Reach them directly via `talk_to_agent(agent_name="{name}", message="…")`.'
            )
            for peer, peer_runner in list(runtime._runners.items()):
                if peer == name:
                    continue
                try:
                    peer_cfg = getattr(peer_runner, "config", None)
                    ws_root = get_workspace(getattr(peer_cfg, "workspace", None)).root
                    MarkdownMemory(ws_root).append_daily(note)
                except Exception as exc:
                    _api.logger.debug("Could not announce new teammate to '%s': %s", peer, exc)
        except Exception as exc:
            _api.logger.debug("Teammate announcement to agents skipped: %s", exc)

        if booted:
            _api._push_event("agent_started", {"name": name})
        _api._broadcast_roster_changed(name)
        return booted

    @app.post("/api/agents")
    async def create_agent(body: _api.AgentCreateRequest) -> dict:
        """Provision a new agent with the same shape as `olano setup`.

        Writes ``agent.json5`` (not legacy YAML), points the agent at a
        per-agent workspace under ``~/.olano/agents/<name>/workspace/``,
        and runs ``scaffold_agent_workspace`` so SOUL.md / IDENTITY.md /
        AGENTS.md / TOOLS.md / system files / memories/ etc. all land in
        place — same as the CLI's `agents create` flow.

        With an ``archetype`` in the body the installer does that instead (the
        same path ``POST /api/archetypes/install`` takes), so the two ways to
        get an agent from this endpoint share one implementation.

        Admins reach this exactly as they always have. A non-admin reaches it
        only where ``self_serve_agents.enabled`` is on, and then inside that
        block's quota and archetype allow-list — see the guard below.
        """
        # ── name validation ─────────────────────────────────────────────────
        # The name becomes a filesystem path (`agents_dir / name`, then a
        # recursive mkdir and a workspace scaffold), so it must be a slug and
        # nothing else. This was implicitly safe while the endpoint was
        # admin-only and the dashboard's AGENT_NAME_RE did the checking
        # client-side; self-serve opened it to members, for whom a name like
        # `../../../../tmp/evil` would have written an agent tree at an
        # attacker-chosen path. Server-side, for every caller, admins
        # included — a client regex is a courtesy, never a boundary.
        from olano.archetypes.installer import scaffold_agent_workspace
        from olano.config.agent_names import (
            is_valid_agent_name,
            mint_agent_name,
            taken_agent_names,
        )
        from olano.config.home import get_olano_paths

        requested_name = (body.name or "").strip()
        display_name = (body.display_name or "").strip()
        if requested_name:
            if not is_valid_agent_name(requested_name):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "invalid_agent_name: lowercase letters, digits, dashes and "
                        "underscores only, starting with a letter or digit (max 64)."
                    ),
                )
            agent_name = requested_name
        else:
            # No handle chosen — mint one from the display name, the way a
            # human's username is minted from their name. Both dashboards
            # take this path: the person types "Hillary" and gets `hillary`
            # (or `hillary-2` if that folder exists), never a field they must
            # fill in with a slug. An archetype create with no display name
            # falls back to the archetype's slug, as the installer always did.
            if not display_name and not body.archetype:
                raise HTTPException(
                    status_code=422,
                    detail="display_name_required: give the agent a name to mint its handle from.",
                )
            agent_name = await asyncio.to_thread(
                lambda: mint_agent_name(
                    display_name, taken_agent_names(), fallback=body.archetype or "agent"
                )
            )

        # ── self-serve guard ────────────────────────────────────────────────
        # A non-admin can only reach this handler at all while self-serve
        # creation is on (the default) — `runtime/auth.py:_member_forbidden`
        # opens exactly this one path for them. It is re-checked here anyway:
        # a permission enforced only in middleware is one refactor away from
        # not being enforced, and the quota and archetype allow-list need the
        # request body regardless.
        #
        # Admins skip the entire block, and so does the implicit owner of a
        # single-user install (`_OWNER.is_admin`) — neither has ever been
        # subject to the switch, the quota, or the allow-list.
        creator = auth_layer.current_user()
        if creator is not None and not creator.is_admin:
            from olano.config.system_settings import (
                load_self_serve_agent_settings,
                self_serve_archetype_allowed,
            )
            from olano.users.store import get_user_store

            self_serve = load_self_serve_agent_settings()
            if not self_serve.enabled:
                raise HTTPException(status_code=403, detail="self_serve_agents_disabled")
            if not self_serve_archetype_allowed(body.archetype, settings=self_serve):
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"archetype_not_allowed: this deployment allows "
                        f"{', '.join(self_serve.allowed_archetypes)}"
                    ),
                )
            # Quota counts what this human OWNS (agent_access role='owner'),
            # not what they were granted access to — being invited to someone
            # else's agent must not consume your own allowance.
            owned = await asyncio.to_thread(
                lambda: sum(
                    1
                    for row in get_user_store().db.list_agents_for_user(creator.id)
                    if row.get("role") == "owner"
                )
            )
            if owned >= self_serve.max_per_user:
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"self_serve_quota_exceeded: you already own {owned} "
                        f"agent(s) and the limit is {self_serve.max_per_user}."
                    ),
                )
            # Some tools only an administrator may hand out: the fleet-scoped
            # admin toolkit, and the Twilio template admin toolkit (every agent
            # on that account shares its templates). Editing an agent's tools is
            # admin-only already, so a member's own create is the one route that
            # could name them; the set is the one AgentFather's config writes are
            # gated on (`olano_admin._privileged_tools_denial`).
            from olano.admin.integrations import (
                operator_granted_tool_names,
                privileged_tool_names,
            )

            admin_only = sorted(
                set(body.tools) & (privileged_tool_names() | operator_granted_tool_names())
            )
            if admin_only:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"admin_only_tools: only an administrator can give an agent "
                        f"{', '.join(admin_only)}. Create the agent without them and "
                        "ask an administrator to add them on its Tools tab."
                    ),
                )

        # Refuse if agent.json5 already exists (local shadow or home).
        try:
            _api._agent_config_path(agent_name)
            raise HTTPException(status_code=409, detail=f"Agent '{agent_name}' already exists")
        except FileNotFoundError:
            pass

        # Deployment entitlements: agent cap + FatherAgent toggle. (The write
        # itself re-checks under the interprocess lock below.)
        dep_guard.reject(dep_enforcement.can_create_agent())
        if agent_name == "agentfather":
            dep_guard.reject(dep_enforcement.feature_decision("father_agent", label="FatherAgent"))

        if body.archetype:
            # Archetype create: the template *is* the agent, so the installer
            # owns the config write and the workspace scaffold — the same code
            # path `POST /api/archetypes/install` takes, rather than a second
            # copy of it here. Free-form fields the archetype decides for
            # itself are refused rather than silently dropped.
            _api._validate_brain(body.model)
            clashes = [
                field
                for field, value in (
                    ("system_prompt", body.system_prompt),
                    ("tools", body.tools),
                )
                if value
            ]
            if clashes:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"An archetype create cannot also set {', '.join(clashes)} — "
                        "the archetype defines them. Create the agent, then edit it."
                    ),
                )
            from olano.archetypes.installer import install_archetype

            try:
                installed = await asyncio.to_thread(
                    install_archetype,
                    name=body.archetype,
                    agent_name=agent_name,
                    model=body.model or None,
                    overwrite=False,
                    # The person's name for the agent and its position — the
                    # simple shell's "New bot" used to drop both, so "Hillary"
                    # went by "Recruitment Specialist" in her own prompt.
                    display_name=display_name or None,
                    title=(body.title or "").strip() or None,
                )
            except FileNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except FileExistsError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except dep_enforcement.DeploymentLimitExceeded as exc:
                _api.logger.warning("archetype create denied: %s", exc.decision.code)
                dep_guard.reject(exc.decision)
            # Same ownership + announcement tail as the from-scratch path below.
            auth_layer.grant_owner_on_create(agent_name)
            _api._push_event("agent_installed", {"name": agent_name, "archetype": body.archetype})
            online = await _welcome_new_agent(agent_name)
            return {
                "ok": True,
                "path": str(installed),
                "name": agent_name,
                "online": online,
            }

        agents_dir = get_olano_paths().agents_dir
        agent_dir = agents_dir / agent_name
        agent_dir.mkdir(parents=True, exist_ok=True)
        config_path = agent_dir / "agent.json5"
        workspace_root = agent_dir / "workspace"

        # Translate the dashboard's friendly backend shorthand into the modern
        # block form expected by AgentConfig.
        backend_block = _api._BACKEND_STRING_MAP.get(body.backend) or {
            "type": "local_shell",
            "root_dir": "workspace",
            "virtual_mode": False,
        }

        model = body.model or _api._default_agent_model()
        _api._validate_brain(model)
        label = display_name or agent_name
        template: dict = {
            "name": agent_name,
            "display_name": display_name,
            "description": body.description or f"{label} agent",
            "emoji": body.emoji or "🦁",
            "model": model,
            "system_prompt": body.system_prompt
            or (
                f"You are {label}, a Olano agent. Be helpful, concise, and "
                "honest about what tools and context you do or do not have."
            ),
            "tools": body.tools or [],
            "subagents": [],
            "skills": [],
            "mcp_servers": [],
            "channels": [],
            "schedule": [],
            "memory": {
                "markdown": True,
                "rag": False,
                "rag_collection": f"lionclaw_{agent_name}",
                "self_learning": True,
                "self_learning_interval": 3600,
            },
            "backend": backend_block,
            "security": {"profile": "safe", "trust_level": 2},
            "pulse": {"enabled": True},
            "workspace": str(workspace_root),
        }
        # Auto-assign a fresh accent color if the client didn't provide one,
        # avoiding hues already taken by booted agents so the gallery stays
        # visually varied.
        if body.color:
            template["color"] = body.color
        else:
            template["color"] = _api._pick_unused_agent_color(runtime)
        # The position, in the field every surface reads it from.
        if (body.title or "").strip():
            template["org"] = {"title": body.title.strip()}

        # Write agent.json5 first so the workspace scaffolder can read it back.
        # count → decide → write runs under the deployment write lock so two
        # concurrent creates cannot jointly exceed the agent cap. The lock can
        # spin up to 10s and the count guard parses every agent.json5, so the
        # whole sequence runs off the event loop.
        from olano.deployment.locks import deployment_write_lock

        def _locked_write() -> None:
            from olano.config.loader import write_agent_config_text

            with deployment_write_lock():
                decision = dep_enforcement.validate_config_write(agent_name, None, template)
                dep_guard.reject(decision)
                write_agent_config_text(config_path, _api._serialize_agent_config(template))
                dep_enforcement.invalidate_counts()

        async with _api._config_write_lock():
            await asyncio.to_thread(_locked_write)

        # Bootstrap the per-agent workspace (SOUL.md, IDENTITY.md, AGENTS.md,
        # USER.md, TOOLS.md, system/, memories/, skills/, ...). This is the
        # same scaffolder `olano agents create --from-template` uses.
        try:
            await asyncio.to_thread(
                scaffold_agent_workspace, agent_name, template, workspace_root, overwrite=False
            )
        except Exception as exc:
            _api.logger.warning("Workspace scaffold for '%s' failed: %s", agent_name, exc)

        # Skills directory next to agent.json5 (matches install_agent_template).
        (agent_dir / "skills").mkdir(exist_ok=True)

        # In multi-user mode, the admin who created the agent becomes its owner.
        auth_layer.grant_owner_on_create(agent_name)

        # Boot the agent, tell humans (toast) and agents (memory log), and
        # broadcast the roster change so the picker refreshes live.
        online = await _welcome_new_agent(agent_name)
        return {"ok": True, "path": str(config_path), "name": agent_name, "online": online}

    @app.post("/api/agents/generate-prompt")
    async def generate_agent_prompt(body: _api.PromptGenerateRequest) -> dict:
        """Draft an optimized system prompt for an agent or subagent via an LLM.

        Powers the "Generate with AI" button in the create/edit agent + subagent
        forms. Takes the agent's name, description, and selected tools and asks a
        model to write a focused, second-person system prompt. The agent need not
        exist yet, so this is keyed off the request body, not a config on disk.
        """
        from langchain_core.messages import HumanMessage, SystemMessage

        name = (body.name or "").strip()
        description = (body.description or "").strip()
        tools = [t.strip() for t in (body.tools or []) if t and t.strip()]
        if not name and not description:
            raise HTTPException(
                status_code=400,
                detail="Provide a name or description before generating a prompt.",
            )

        # Resolve which model writes the prompt: the one the user picked, else the
        # parent agent's model (subagent "inherit"), else the platform default.
        model_str = (body.model or "").strip()
        if not model_str and body.agent:
            try:
                parent = await asyncio.to_thread(_api._read_agent_config_cached, body.agent)
                model_str = (parent.get("model") or "").strip()
            except Exception:
                model_str = ""
        if not model_str:
            model_str = "anthropic:claude-sonnet-4-6"

        is_sub = body.kind == "subagent"
        role = (
            "subagent (a specialized worker an orchestrating parent agent delegates to)"
            if is_sub
            else "autonomous AI agent"
        )
        tool_line = (
            ", ".join(tools) if tools else "(none specified — assume general reasoning only)"
        )

        meta_system = (
            "You are an expert prompt engineer who writes system prompts for "
            f"{role}s. Given an agent's name, role description, and the tools it "
            "can call, write the system prompt that agent will run under.\n\n"
            "Rules:\n"
            '- Write in the second person ("You are …").\n'
            "- Open by establishing the agent's identity and core purpose from its name and description.\n"
            "- Spell out its responsibilities and the kinds of tasks it handles.\n"
            "- When tools are listed, explain when and how to use them well; never invent tools that aren't listed.\n"
            "- Give guidance on working style, tone, and how to handle uncertainty or missing information.\n"
            "- Be specific and actionable — no generic filler.\n"
            "- Keep it focused: roughly 120–320 words.\n"
            "- Output ONLY the system prompt text. No preamble, no commentary, no "
            "markdown code fences, no 'System prompt:' heading."
        )
        meta_human = (
            f"Agent name: {name or '(unnamed)'}\n"
            f"Role / description: {description or '(none given — infer a sensible role from the name)'}\n"
            f"Available tools: {tool_line}\n\n"
            "Write the system prompt."
        )

        try:
            from olano.models import build_model

            model = build_model(model_str)
            result = await model.ainvoke(
                [SystemMessage(content=meta_system), HumanMessage(content=meta_human)]
            )
            # Bare model call outside any AgentRunner turn — ledger the spend.
            from olano.analytics.usage_recorder import record_result_usage

            record_result_usage("system", model_str, result, origin="prompt_gen")
        except Exception as exc:
            _api.logger.warning("Prompt generation via '%s' failed: %s", model_str, exc)
            raise HTTPException(status_code=502, detail=f"Prompt generation failed: {exc}") from exc

        text = _api._ai_message_text(getattr(result, "content", result)).strip()
        # Strip an accidental ```…``` fence if the model wrapped its answer anyway.
        if text.startswith("```"):
            text = text.strip("`").strip()
            first_nl = text.find("\n")
            if first_nl != -1 and " " not in text[:first_nl].strip():
                text = text[first_nl + 1 :].strip()
        if not text:
            raise HTTPException(status_code=502, detail="Model returned an empty prompt.")
        return {"system_prompt": text, "model": model_str}

    # Fields the typed config editor must never write: the agent's identity /
    # on-disk key (`name`), the derived workspace path, install provenance, and
    # the display-only mirrors the read endpoint injects. Avatar/sprite media and
    # renames are handled by their own dedicated endpoints.
    _IMMUTABLE_AGENT_FIELDS = frozenset(
        {
            "name",
            "workspace",
            "archetype",
            "avatar_url",
            "sprites",
            "tools_resolved",
            "subagents_resolved",
            "title",  # resolved mirror of org.title; edited via PUT …/org
            "_running",
        }
    )

    # Config fields that already have a richer, dedicated editor elsewhere in the
    # agent detail view. The schema-driven "Advanced" panel uses this to point the
    # user at the specialized tab instead of rendering a raw generic control.
    _MANAGED_AGENT_FIELDS = {
        "tools": "tools",
        "subagents": "subagents",
        "channels": "connectors",
        "mcp_servers": "mcp",
        "webhooks": "webhooks",
        "commands": "commands",
        "monetization": "monetization",
        "org": "org",
        # Peer visibility is edited in the Talk to Agent (org) tab's Visibility
        # section, so defer to it rather than a generic control.
        "agent_visibility": "org",
        "external_access": "external",  # the External access tab (inbound MCP / A2A)
        "skills": "skills",
        "schedule": "overview",
        "memory": "overview",
        "security": "overview",
        "cognition": "overview",
        # Customer-service mode has a dedicated Overview card with the toggle,
        # the prompt-extension editor and the suggested-text copy button; a
        # generic two-field control would lose the explanation operators need.
        "customer_service": "overview",
        # Message policy has a dedicated Multi-user card: the fields interact
        # (the oversize action only means anything with a limit set, the
        # ignore notice only with something that ignores), and a generic
        # object editor would show all seven flat with none of that.
        "message_policy": "multiuser",
        "heartbeat": "overview",
        # The Gmail watch card shows the Google connection and the watch's last
        # check next to its fields; a bare object editor would hide both.
        "gmail_watch": "overview",
        # Tracing to Langfuse has a dedicated Overview card: three-state fields
        # with "Follow Config", the full-capture warning and what is in effect.
        "observability": "overview",
        "backend": "overview",
        "model": "overview",
        "reasoning_effort": "overview",
        "system_prompt": "overview",
        "description": "overview",
        "color": "overview",
        "emoji": "overview",
        # Editable in the Overview identity card next to `description`, which
        # explains the interaction with the immutable `name`; a generic string
        # control in Advanced would lose that.
        "display_name": "overview",
    }

    def _live_sync_commands(name: str, merged: Any) -> None:
        """Apply a saved ``commands`` block to the live runtime — no Restart.

        Two halves, and both are needed. The gate in
        ``platforms.inbound.maybe_handle_command`` reads ``runner.config``, the
        object captured at boot, so a disk-only write left every chat surface
        on the pre-edit exposure map until someone restarted the agent — the
        Commands tab appeared to do nothing. And the menus connectors publish
        (Telegram's ``/`` list, Discord's command tree) are built from the same
        config, so they have to be re-published or they would advertise
        commands the freshly-updated gate now refuses.

        Surgical on purpose: only ``commands`` is swapped. It feeds no prompt,
        tool binding or graph — it is re-read per turn — so unlike the model or
        backend it needs no rebuild. Channels share the config object, so the
        in-place assignment reaches every adapter at once.

        The re-publish runs as a background task: it makes a network call per
        connected adapter, and a slow Telegram must not hold up the save.
        """
        running = runtime._runners.get(name)
        if running is not None:
            try:
                running.config.commands = merged.commands
            except Exception as exc:
                _api.logger.debug("Command config refresh skipped for '%s': %s", name, exc)

        from olano.platforms.registry import republish_command_menus

        try:
            asyncio.get_running_loop().create_task(republish_command_menus(name))
        except RuntimeError:  # pragma: no cover - no loop (sync test caller)
            _api.logger.debug("No running loop; skipped command menu re-publish for '%s'", name)

    def _live_sync_cognition(name: str, merged: Any) -> None:
        """Apply a saved cognition block to the live runtime — no Restart needed.

        Two targeted syncs, both best-effort:

        1. Refresh the running agent's cached ``runner.config.cognition`` so the
           live Cortex reads reflect the new per-agent overrides immediately.
           ``GET /api/cognition/status`` (and the pulse/drainer loops + manual
           "Run now") resolve a *running* agent from ``runner.config`` — the
           in-memory config captured at boot (see ``_resolve_agent_for_pulse``) —
           so a disk-only write left them all on the stale pre-edit config and
           every "This agent" toggle in the dashboard snapped back / froze.
           Channels share the same config object, so the in-place assignment
           propagates everywhere at once. Deliberately surgical: only
           ``cognition`` is swapped — the built graph's model/tools/backend keep
           their restart-to-apply semantics.
        2. Re-register the agent's cognition cron jobs so toggling Cortex off
           pauses its background work (and cadence edits reschedule) live.
        """
        running = runtime._runners.get(name)
        if running is not None:
            try:
                running.config.cognition = merged.cognition
            except Exception as exc:
                _api.logger.debug("Cognition cache refresh skipped for '%s': %s", name, exc)
        try:
            from olano.scheduler.cron import get_active_scheduler

            sched = get_active_scheduler()
            if sched is not None:
                sched.resync_agent_cognition_jobs(merged)
        except Exception as exc:
            _api.logger.debug("Cognition live-sync skipped for '%s': %s", name, exc)

    @app.put("/api/agents/{name}")
    async def update_agent(name: str, body: _api.AgentUpdateRequest) -> dict:
        try:
            data = await _api._read_agent_config_for_update(name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        # Merge every field the client explicitly sent. A field present in the
        # patch is applied as-is — including an explicit `null`, which clears an
        # override (e.g. top_level_memory_tools: null → restore the default) —
        # so the schema-driven editor can both set and reset any field. Only the
        # identity/derived fields in the denylist are never patchable this way;
        # renames and avatar/sprite management have their own endpoints.
        old_model = data.get("model")
        # Effective customer-service state, not just the raw switch: an agent
        # created from the customer-service archetype (or hand-edited) runs in
        # CS mode via memory.per_customer with no customer_service block at
        # all, and the transition logic below must fire for it too.
        old_customer_service_enabled = bool(
            (data.get("customer_service") or {}).get("enabled")
        ) or bool((data.get("memory") or {}).get("per_customer"))
        patch = body.model_dump(exclude_unset=True)
        for key, value in patch.items():
            if key in _IMMUTABLE_AGENT_FIELDS:
                continue
            data[key] = value

        # The switch is the operator-facing control, and it must work in BOTH
        # directions. AgentConfig._sync_customer_service_mode mirrors
        # memory.per_customer back into the switch (per-customer isolation IS
        # customer-service mode), so a PUT that only sets enabled=false on an
        # agent whose file carries per_customer=true would validate — and
        # behave — as still enabled. When the request explicitly turns the
        # switch off and doesn't itself set memory.per_customer (a deliberate
        # choice we respect), clear the mirror too so "off" means off.
        cs_patch = patch.get("customer_service")
        mem_patch = patch.get("memory")
        if (
            isinstance(cs_patch, dict)
            and cs_patch.get("enabled") is False
            and not (isinstance(mem_patch, dict) and "per_customer" in mem_patch)
            and (data.get("memory") or {}).get("per_customer")
        ):
            mem = dict(data.get("memory") or {})
            mem["per_customer"] = False
            data["memory"] = mem

        # Deployment entitlements: switching models is a BYOK/model-selection
        # capability that a plan may disable.
        if data.get("model") != old_model:
            dep_guard.reject(
                dep_enforcement.feature_decision("model_selection", label="model selection")
            )

        # customer_service.enabled flipping (either direction) changes which
        # tools _build_memory_tools actually constructs (shared MEMORY.md tools
        # vs. per-customer remember/recall/recall_all/forget_me) — see
        # AgentConfig._sync_customer_service_mode. top_level_memory_tools (and
        # its rag/graph siblings) are a *name allow-list* filtered against
        # whatever gets built (core/builder._select_tool_subset); left over from
        # before the switch, they silently zero out the newly-built tools
        # because none of the old names match the new set. Reset them to the
        # default (no filter) on a transition, unless the same request also set
        # one of them explicitly — that's a deliberate operator choice, not a
        # stale leftover.
        new_customer_service_enabled = bool(
            (data.get("customer_service") or {}).get("enabled")
        ) or bool((data.get("memory") or {}).get("per_customer"))
        if new_customer_service_enabled != old_customer_service_enabled:
            for field_name in (
                "top_level_memory_tools",
                "top_level_rag_tools",
                "top_level_graph_tools",
            ):
                if field_name not in patch:
                    data[field_name] = None

        # Coerce shorthand backend strings ("filesystem+shell") into the block
        # form expected by AgentConfig before the validator sees them.
        _api._normalize_backend(data)
        # Canonicalize verbosity aliases so the file matches what the UI shows.
        _api._normalize_default_verbosity(data)
        # Drop display-only `from_builtin` subagent mirrors surfaced on GET.
        _api._collapse_subagent_refs(data)

        # Validate the merged config against the schema. Rejecting on the API
        # boundary keeps a malformed PUT from corrupting agent.json5 on disk.
        try:
            from olano.config.agent_config import AgentConfig

            merged = AgentConfig(**data)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Invalid agent config: {exc}")

        # A multi-user agent whose extension is blank runs with the support-desk
        # default (AgentConfig._sync_customer_service_mode). Persist what it
        # runs with, so agent.json5 and the raw config editor say the same thing
        # as the system prompt — a file that reads blank while the agent carries
        # scope rules is the kind of mismatch operators debug for an hour. Only
        # a blank field is ever written; operator wording is untouched here.
        if merged.customer_service.enabled:
            cs_block = dict(data.get("customer_service") or {})
            if not (cs_block.get("prompt_extension") or "").strip():
                cs_block["prompt_extension"] = merged.customer_service.prompt_extension
                data["customer_service"] = cs_block

        await _api._write_agent_config_async(name, data)

        # Heartbeat settings are scheduler-level (not baked into the built agent
        # graph), so unlike most config edits they apply live — no Restart needed.
        if body.heartbeat is not None:
            try:
                from olano.config.system_settings import (
                    effective_heartbeat_enabled,
                    effective_heartbeat_interval,
                )
                from olano.scheduler.cron import get_active_scheduler

                sched = get_active_scheduler()
                if sched is not None:
                    sched.set_heartbeat_options(
                        name,
                        enabled=effective_heartbeat_enabled(merged),
                        interval_minutes=effective_heartbeat_interval(merged),
                    )
            except Exception as exc:
                _api.logger.debug("Heartbeat live-sync skipped for '%s': %s", name, exc)

        # Schedule edits also apply live — reregister the agent's config-schedule
        # jobs so a save from the Config editor takes effect without a Restart.
        if body.schedule is not None:
            try:
                from olano.scheduler.cron import get_active_scheduler

                sched = get_active_scheduler()
                if sched is not None:
                    sched.resync_agent_config_schedule(merged)
            except Exception as exc:
                _api.logger.debug("Schedule live-sync skipped for '%s': %s", name, exc)
            _api._push_event("scheduler_update", {"action": "updated", "id": f"cfg:{name}"})

        # The Gmail watch is a scheduler job, not part of the built graph, so
        # its settings apply live too.
        if body.gmail_watch is not None:
            try:
                from olano.scheduler.cron import get_active_scheduler

                sched = get_active_scheduler()
                if sched is not None:
                    sched.resync_agent_gmail_watch(merged)
            except Exception as exc:
                _api.logger.debug("Gmail watch live-sync skipped for '%s': %s", name, exc)

        # Cognition edits (per-agent Cortex enable / feature / autonomy / intensity)
        # apply live too. The `cortex_*` chat tools are baked into the built
        # graph, so those change on next build.
        if body.cognition is not None:
            _live_sync_cognition(name, merged)

        # Command exposure + custom commands apply live: the inbound gate reads
        # the running config, and the connectors' published menus are rebuilt
        # from it, so a Commands-tab save takes effect without a Restart.
        if body.commands is not None:
            _live_sync_commands(name, merged)

        _api._broadcast_roster_changed(name)
        # Prompt inputs may have moved (system_prompt, customer_service
        # extension, subagents, org/visibility, tools) — nudge the prompt
        # refresh sweep so a running agent hot-swaps within seconds instead
        # of waiting for the periodic tick (or a restart).
        try:
            nudge = getattr(runtime, "request_prompt_refresh", None)
            if nudge is not None:
                nudge([name])
        except Exception:  # pragma: no cover - defensive
            pass
        return {"ok": True, "name": name}

    @app.patch("/api/agents/{name}/cognition")
    async def patch_agent_cognition(name: str, body: dict) -> dict:
        """Merge a **delta** into one agent's ``cognition`` block.

        Body is just the keys that changed — ``{"idea_generation": true}``, or
        ``{"idea_generation": null}`` to clear the override and go back to
        inheriting the home-wide default.

        This exists because the dashboard's knobs had no way to say "change this
        one thing". They read the agent's whole config, merged one field
        client-side and PUT the block back, which cost a 125 ms
        ``GET /api/agents/{name}`` per click and — worse — lost writes: two
        toggles clicked in quick succession both read the pre-write state, and
        the second PUT reverted the first one's field. Server-side merging under
        the agent's read-modify-write gate makes concurrent patches of different
        keys commute, which is the only fix that doesn't depend on the client
        being slow enough.

        Deliberately narrower than ``PUT /api/agents/{name}`` in three ways:

        * it emits ``cognition_updated``, not ``agents_update`` — the roster did
          not change, and the broadcast made every open dashboard tab refetch the
          whole agent list on every knob click;
        * it nudges the prompt-refresh sweep only when ``enabled`` actually
          flips, since that is the one cognition field feeding the prompt (it
          gates the ``cortex_*`` tools);
        * it rejects unknown keys instead of writing them, so a typo can't
          silently become a dead field in agent.json5.

        Returns the agent's fresh overrides + resolved values so the caller can
        reconcile optimistic state without a follow-up read.
        """
        from olano.config.agent_config import AgentConfig, CognitionConfig
        from olano.config.system_settings import (
            FEATURES,
            auto_approve_enabled,
            autonomy_enabled,
            cortex_enabled,
            effective_intensity,
            effective_worker_budget,
            feature_enabled,
            load_system_settings,
        )

        auth_layer.assert_agent_access(name)
        patch = dict(body or {})
        if not patch:
            raise HTTPException(status_code=400, detail="empty cognition patch")
        unknown = sorted(set(patch) - set(CognitionConfig.model_fields))
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"unknown cognition field(s): {', '.join(unknown)}",
            )

        try:
            data = await _api._read_agent_config_for_update(name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        raw_block = data.get("cognition")
        if raw_block is not None and not isinstance(raw_block, dict):
            # A hand-edited agent.json5 whose `cognition` isn't a mapping. Say so
            # rather than raising out of `dict(...)` as a 500.
            raise HTTPException(
                status_code=422,
                detail=f"agent.json5 `cognition` must be an object, got {type(raw_block).__name__}",
            )
        block = dict(raw_block or {})
        was_enabled = block.get("enabled")
        for key, value in patch.items():
            # An explicit null clears the override: drop the key entirely rather
            # than storing `null`, so agent.json5 reads as "this agent has no
            # opinion" instead of carrying a field that looks set but isn't.
            if value is None:
                block.pop(key, None)
            else:
                block[key] = value
        data["cognition"] = block

        try:
            merged = AgentConfig(**data)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Invalid cognition config: {exc}")

        await _api._write_agent_config_async(name, data)
        _live_sync_cognition(name, merged)
        _api._push_event("cognition_updated", {"agent": name, "fields": sorted(patch)})

        if block.get("enabled") != was_enabled:
            try:
                nudge = getattr(runtime, "request_prompt_refresh", None)
                if nudge is not None:
                    nudge([name])
            except Exception:  # pragma: no cover - defensive
                pass

        def _echo() -> dict:
            settings = load_system_settings()
            cog = merged.cognition
            budget_steps, budget_timeout = effective_worker_budget(merged, settings=settings)
            return {
                "ok": True,
                "name": name,
                "overrides": {
                    "enabled": getattr(cog, "enabled", None),
                    "autonomous": getattr(cog, "autonomous", None),
                    "auto_approve": getattr(cog, "auto_approve", None),
                    "intensity": getattr(cog, "intensity", None),
                    "worker_max_iterations": getattr(cog, "worker_max_iterations", None),
                    "worker_timeout_seconds": getattr(cog, "worker_timeout_seconds", None),
                    "features": {f: getattr(cog, f, None) for f in FEATURES},
                },
                "resolved": {
                    "enabled": cortex_enabled(merged, settings=settings),
                    "autonomous": autonomy_enabled(merged, settings=settings),
                    "auto_approve": auto_approve_enabled(merged, settings=settings),
                    "intensity": effective_intensity(merged, settings=settings),
                    "worker_max_iterations": budget_steps,
                    "worker_timeout_seconds": budget_timeout,
                    "features": {
                        f: feature_enabled(f, merged, settings=settings) for f in FEATURES
                    },
                },
            }

        return await asyncio.to_thread(_echo)

    @app.get("/api/agents/{name}/config/raw")
    async def get_agent_raw_config(name: str) -> dict:
        """Return the verbatim ``agent.json5`` text — the source of truth.

        This is intentionally NOT the enriched ``GET /api/agents/{name}`` payload:
        no injected/computed fields (``tools_resolved``, ``subagents_resolved``,
        ``_running``), no re-serialization. The Config tab loads and edits this
        directly, so what the user sees and saves is byte-for-byte the file.
        """
        from olano.config.loader import find_agent_config_path

        # Raw agent.json5 can carry credential values (config.env) — reading it
        # is a management surface: admin or the agent's owner only.
        auth_layer.assert_agent_admin(name)
        dep_guard.reject(
            dep_enforcement.feature_decision("agent_config_raw", label="raw config editor")
        )
        path = find_agent_config_path(name)
        if path is None:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
        return {"content": path.read_text(encoding="utf-8"), "format": "json5"}

    @app.put("/api/agents/{name}/config/raw")
    async def put_agent_raw_config(name: str, request: Request) -> dict:
        """Overwrite ``agent.json5`` with the raw text from the Config editor.

        Validates that the text parses as JSON5 and satisfies ``AgentConfig``
        before touching disk, then writes the bytes **verbatim** — comments, key
        order and whitespace are preserved exactly as typed. Because the editor's
        text is the source of truth, there is nothing to strip or normalize: the
        enriched fields only ever live on the read-side ``GET /api/agents/{name}``.
        """
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid request body: {exc}")
        content = body.get("content") if isinstance(body, dict) else None
        if not isinstance(content, str):
            raise HTTPException(
                status_code=400, detail='Body must be {"content": "<agent.json5 text>"}'
            )

        from olano.config.loader import find_agent_config_path

        path = find_agent_config_path(name)
        if path is None:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        # Validate before writing: must parse as JSON5 and satisfy the schema.
        # json5 parsing is pure-Python and CPU-bound (~0.6s on a large config).
        try:
            parsed = await asyncio.to_thread(_load_json5, content)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Invalid JSON5: {exc}")
        if not isinstance(parsed, dict):
            raise HTTPException(status_code=422, detail="Config must be a JSON object")
        cfg_name = parsed.get("name")
        if cfg_name is not None and cfg_name != name:
            raise HTTPException(
                status_code=422,
                detail=f"`name` in the config ({cfg_name!r}) must match the agent "
                f"({name!r}) — renaming via the raw editor is not supported.",
            )
        try:
            from olano.config.agent_config import AgentConfig

            merged = AgentConfig.model_validate({**parsed, "name": name})
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Invalid agent config: {exc}")

        dep_guard.reject(
            dep_enforcement.feature_decision("agent_config_raw", label="raw config editor")
        )

        # A literal bot token typed into the raw editor gets the same treatment
        # as one typed into the connector form — moved to the vault, replaced by
        # a ${vault:KEY} reference. Done as a text substitution rather than a
        # re-serialize, so the comments and layout this editor exists to
        # preserve survive it.
        from olano.config.channel_secrets import absorb_channel_secrets_in_text
        from olano.deployment.locks import deployment_write_lock

        # The editor's text IS the file — write it verbatim (no re-serialization).
        # The raw editor bypasses _write_agent_config, so the deployment-limit
        # guard (count → decide → write, under the interprocess lock) runs here.
        # One thread hop for the vault absorb + old-config parse + locked write:
        # the lock can spin up to 10s and the count guard re-parses configs.
        def _absorb_and_locked_write() -> Any:
            from olano.config.loader import write_agent_config_text

            new_content, report = absorb_channel_secrets_in_text(name, content, parsed)
            with deployment_write_lock():
                old_raw: dict | None = None
                try:
                    old_raw = _load_json5(path.read_text(encoding="utf-8"))
                except Exception:
                    old_raw = {}
                if (old_raw or {}).get("model") != parsed.get("model"):
                    dep_guard.reject(
                        dep_enforcement.feature_decision("model_selection", label="model selection")
                    )
                dep_guard.reject(dep_enforcement.validate_config_write(name, old_raw, parsed))
                write_agent_config_text(path, new_content)
                dep_enforcement.invalidate_counts()
            return report

        async with _api._config_write_lock():
            secret_report = await asyncio.to_thread(_absorb_and_locked_write)
        # Cognition applies live regardless of which editor saved it — same as the
        # typed PUT above. Cheap + idempotent, so no need to diff the block first.
        _live_sync_cognition(name, merged)
        _api._broadcast_roster_changed(name)
        # Same nudge as the typed PUT: raw edits can change any prompt input.
        try:
            nudge = getattr(runtime, "request_prompt_refresh", None)
            if nudge is not None:
                nudge([name])
        except Exception:  # pragma: no cover - defensive
            pass
        return {
            "ok": True,
            "name": name,
            "vaulted": [{"channel": c, "field": f, "key": k} for c, f, k in secret_report.vaulted],
            "vault_locked": not secret_report.vault_available,
        }

    @app.get("/api/agents/{name}/config/schema")
    async def get_agent_config_schema(name: str) -> dict:
        """JSON Schema for ``agent.json5`` (``AgentConfig``) + editor metadata.

        Powers the dashboard's schema-driven "Advanced" config panel: the
        frontend renders a control for EVERY field the schema declares, so a new
        ``AgentConfig`` field becomes editable in the UI automatically — no
        per-field UI work, and nothing is ever locked behind the raw-JSON tab.
        The metadata tells the panel which fields to hide (``read_only`` —
        identity/derived) and which already have a dedicated, richer tab
        (``managed`` → tab id) so it can defer to the specialized editor.
        """
        from olano.config.loader import find_agent_config_path

        if find_agent_config_path(name) is None:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        from olano.config.agent_config import AgentConfig

        return {
            "schema": AgentConfig.model_json_schema(),
            "read_only": sorted(_IMMUTABLE_AGENT_FIELDS),
            "managed": dict(_MANAGED_AGENT_FIELDS),
        }

    @app.get("/api/customer-service/defaults")
    async def get_customer_service_defaults() -> dict:
        """Help text + the suggested prompt extensions for multi-user mode.

        Served rather than duplicated in the frontend so the wording has one
        source of truth (``config/customer_service.py``).

        Two variants ship: ``multi_user`` (privacy rules only — correct for any
        agent) and ``support`` (privacy plus support-desk scope guardrails).
        ``default_prompt_extension_variant`` names the one that fills a **blank**
        field when multi-user mode is on, so the dashboard preselects it instead
        of hard-coding which is which; text an operator wrote is never
        overwritten by anything here.
        ``suggested_prompt_extension`` (singular) keeps serving the support
        variant so an older dashboard build sees no change.
        """
        from olano.config.customer_service import (
            CUSTOMER_SERVICE_HELP,
            CUSTOMER_SERVICE_TOOLS,
            DEFAULT_PROMPT_EXTENSION_VARIANT,
            SUGGESTED_PROMPT_EXTENSION,
            SUGGESTED_PROMPT_EXTENSIONS,
        )

        return {
            "help": CUSTOMER_SERVICE_HELP,
            "tools": list(CUSTOMER_SERVICE_TOOLS),
            "suggested_prompt_extension": SUGGESTED_PROMPT_EXTENSION,
            "suggested_prompt_extensions": dict(SUGGESTED_PROMPT_EXTENSIONS),
            "default_prompt_extension_variant": DEFAULT_PROMPT_EXTENSION_VARIANT,
        }

    @app.delete("/api/agents/{name}")
    async def delete_agent(name: str) -> dict:
        import shutil

        # Stop the runner first so it doesn't keep serving requests against
        # a directory we're about to remove. Without this, the agent stays
        # listed as "running" in the API and the dashboard card persists.
        if name in runtime._runners or name in runtime._agent_resources:
            try:
                await runtime._stop_agent(name)
            except Exception as exc:
                _api.logger.warning("Stop during delete failed for '%s': %s", name, exc)

        try:
            path = _api._agent_config_path(name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
        agent_dir = path.parent
        # An agent dir can hold a multi-GB workspace — rmtree inline would
        # freeze the loop (and every channel) for minutes.
        await asyncio.to_thread(shutil.rmtree, agent_dir, ignore_errors=True)
        # Its checkpoint history goes with it: the rows are keyed by the
        # agent's prefix and nothing could ever load them again. Best-effort,
        # like the grants below.
        try:
            from olano.core.checkpoints import adelete_agent_threads

            await adelete_agent_threads(name)
        except Exception:
            _api.logger.debug(
                "Failed to purge checkpoint history for deleted agent '%s'", name, exc_info=True
            )
        # Drop the human↔agent access grants too, so they don't outlive the
        # agent — a stale grant on a recreated (or default-fallback) name would
        # otherwise leak access. Best-effort; single-user installs have none.
        try:
            from olano.users.store import get_user_store

            get_user_store().db.revoke_agent(name)
        except Exception:
            _api.logger.debug(
                "Failed to purge access grants for deleted agent '%s'", name, exc_info=True
            )
        # Nothing on the bell about a deleted agent can still be fixed, and its
        # producers (boot, the MCP sweep) no longer run for it to resolve them.
        from olano.runtime import notifications

        await asyncio.to_thread(notifications.forget_agent, name)
        _api._push_event("agent_deleted", {"name": name})
        _api._broadcast_roster_changed(name)
        return {"ok": True, "name": name}

    # Closures that later route modules bind (see routes/__init__.py).
    closures["_welcome_new_agent"] = _welcome_new_agent
