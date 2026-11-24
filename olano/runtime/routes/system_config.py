"""System deployment info and home-wide config.

Split out of ``olano.runtime.api.create_api``; see ``olano.runtime.routes``.
"""

import asyncio
import re
from typing import Any

from fastapi import FastAPI, HTTPException

from olano.deployment import enforcement as dep_enforcement
from olano.runtime import api as _api
from olano.runtime import auth as auth_layer


def _browser_status() -> dict[str, Any]:
    """Read-only companion of the ``browser`` block: what the box actually runs
    (after the plan, hardware and Docker gates) and the live image's state."""
    from olano.browser import sidecar
    from olano.browser.policy import (
        HEADLESS_MIN_MEM_BYTES,
        LIVE_MIN_CPUS,
        LIVE_MIN_MEM_BYTES,
        browser_availability,
    )

    return {
        "availability": browser_availability().to_dict(),
        "image": sidecar.image_status(),
        "requirements": {
            "headless_min_mem_bytes": HEADLESS_MIN_MEM_BYTES,
            "live_min_mem_bytes": LIVE_MIN_MEM_BYTES,
            "live_min_cpus": LIVE_MIN_CPUS,
        },
        "live_entitled": dep_enforcement.feature_enabled("browser_live")
        and dep_enforcement.feature_enabled("browser"),
    }


def register_system_config_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach the system config routes to ``app``."""
    # Closures defined by an earlier route module (see routes/__init__.py).
    _caps_cache = closures["_caps_cache"]

    @app.get("/api/system/playground")
    async def get_playground_setting() -> dict:
        """Report the home-wide Playground switch (config.yaml playground.enabled)."""
        from olano.config.system_settings import playground_enabled

        return {"enabled": playground_enabled()}

    @app.post("/api/system/playground")
    async def set_playground_setting(body: dict) -> dict:
        """Flip the home-wide Playground switch.

        Body: ``{"enabled": bool}``. When off, the dashboard hides the Playground
        and character generation skips the animation sprite sheets (the portrait
        is still generated)."""
        from olano.config.system_settings import (
            playground_enabled,
            set_playground_enabled,
        )

        enabled = bool((body or {}).get("enabled", False))
        set_playground_enabled(enabled)
        _caps_cache["expires"] = 0.0  # see the note in PUT /api/system/config
        return {"ok": True, "enabled": playground_enabled()}

    # ── Deployment settings (signed entitlements) — read-only admin surface ────
    # Admin-only via the ``/api/system`` prefix gate. Read-only by design: the
    # settings are signed by the olano control plane; nothing here can edit them.
    @app.get("/api/system/deployment")
    async def get_system_deployment() -> dict:
        """The deployment's entitlement posture, limits vs current usage, and
        feature/tab toggles — powers the Admin → Deployment card."""
        from olano.config.system_settings import OVERRIDABLE_USAGE_LIMITS
        from olano.deployment import resolver as dep_resolver
        from olano.deployment.schema import KNOWN_FEATURES, KNOWN_TABS

        # count_deployment_objects parses every agent.json5 when its cache is
        # cold (~3s fleet-wide), and the user count is a sqlite read.
        status = await asyncio.to_thread(dep_resolver.deployment_status)
        s = status.settings

        def _current_counts() -> tuple[Any, int | None]:
            try:
                counts = dep_enforcement.count_deployment_objects()
            except Exception:
                counts = None
            try:
                from olano.runtime.auth import get_user_store

                user_count: int | None = len(get_user_store().db.list_users())
            except Exception:
                user_count = None
            return counts, user_count

        counts, user_count = await asyncio.to_thread(_current_counts)

        resources = [
            {
                "key": "max_agents",
                "label": "Agents",
                "limit": s.resources.max_agents,
                "current": counts.agents if counts else None,
            },
            {
                "key": "max_connectors_total",
                "label": "Connectors (total)",
                "limit": s.resources.max_connectors_total,
                "current": counts.connectors_total if counts else None,
            },
            {
                "key": "max_connectors_per_agent",
                "label": "Connectors per agent",
                "limit": s.resources.max_connectors_per_agent,
                "current": (
                    max(counts.connectors_by_agent.values(), default=0) if counts else None
                ),
            },
            {
                "key": "max_mcp_servers_total",
                "label": "MCP servers (total)",
                "limit": s.resources.max_mcp_servers_total,
                "current": counts.mcp_total if counts else None,
            },
            {
                "key": "max_webhooks_total",
                "label": "Webhooks (total)",
                "limit": s.resources.max_webhooks_total,
                "current": counts.webhooks_total if counts else None,
            },
            {
                "key": "max_users",
                "label": "Dashboard users",
                "limit": s.resources.max_users,
                "current": user_count,
            },
        ]

        def _usage_current(key: str) -> int | None:
            # Filled by the durable usage counters; None renders as "—".
            try:
                from olano.deployment.usage_counters import current_usage_for_limit

                return current_usage_for_limit(key)
            except Exception:
                return None

        # Display labels, in the order the card lists them (deployment-wide
        # first, then per-agent) — the enforcement module orders them by metric.
        _USAGE_ROW_ORDER = (
            ("input_tokens_total", "Input tokens (total)"),
            ("input_tokens_per_agent", "Input tokens per agent"),
            ("output_tokens_total", "Output tokens (total)"),
            ("output_tokens_per_agent", "Output tokens per agent"),
            ("cost_total", "Cost (total)"),
            ("cost_per_agent", "Cost per agent"),
        )

        def _usage_rows() -> list[dict]:
            # The EFFECTIVE limits, not the artifact's — a card that quotes a
            # ceiling the runtime isn't enforcing is worse than no card. Each
            # row carries where its number came from so the UI can mark an
            # override and still show what it replaced.
            from olano.deployment.usage_budgets import effective_usage_limits

            by_key = {eff.key: eff for eff in effective_usage_limits(s.usage)}
            rows = []
            for key, label in _USAGE_ROW_ORDER:
                eff = by_key.get(key)
                if eff is None:  # pragma: no cover - the six keys are fixed
                    continue
                rows.append(
                    {
                        "key": key,
                        "label": label,
                        "limit": eff.limit.value,
                        "window": eff.limit.window,
                        "current": _usage_current(key),
                        "unit": eff.unit,
                        "source": eff.source,
                        "overridable": eff.key in OVERRIDABLE_USAGE_LIMITS,
                        "deployment_limit": eff.deployment_limit.value,
                    }
                )
            return rows

        usage = await asyncio.to_thread(_usage_rows)

        return {
            "posture": status.posture,
            "source": status.source,
            "mode": status.mode,
            "deployment_id": status.deployment_id,
            "plan": status.plan,
            "policy_revision": status.policy_revision,
            "issued_at": status.issued_at,
            "expires_at": status.expires_at,
            "last_refresh": status.last_refresh,
            "error": status.error,
            "resources": resources,
            "usage": usage,
            "features": {name: s.feature_enabled(name) for name in KNOWN_FEATURES},
            "tabs": {name: s.tab_enabled(name) for name in KNOWN_TABS},
            "credits_left": s.credits_left,
            "olano_model_credits": s.olano_model_credits,
            "blocked_agents": dict(getattr(runtime, "_blocked_agents", {}) or {}),
        }

    @app.post("/api/system/deployment/refresh")
    async def refresh_system_deployment() -> dict:
        """Force a settings refresh from the control plane (managed-cloud)."""
        from olano.deployment import resolver as dep_resolver

        status = await dep_resolver.refresh_async()
        return {
            "ok": True,
            "posture": status.posture,
            "source": status.source,
            "error": status.error,
        }

    # ── Global system config (config.yaml: HomeConfig + cognition models) ──────
    # Admin-only: the ``/api/system`` prefix is gated in the auth middleware when
    # multi-user is on; in single-owner mode the owner reaches it directly.
    @app.get("/api/system/config")
    async def get_system_config() -> dict:
        """Snapshot of the global config.yaml settings for the dashboard's Config
        section: the top-level HomeConfig keys plus the cognition model fields."""
        from olano.catalog import credits_registry
        from olano.config.home import get_olano_paths, load_home_config
        from olano.config.system_settings import (
            COST_DISPLAY_MODES,
            SPEECH_PROVIDERS,
            cost_display_locked,
            cost_display_mode,
            default_models_report,
            load_agent_autonomy_settings,
            load_browser_settings,
            load_checkpoint_gc_settings,
            load_context_window_settings,
            load_disk_monitor_settings,
            load_external_access_settings,
            load_group_thread_settings,
            load_magic_link_settings,
            load_mcp_health_settings,
            load_media_settings,
            load_mobile_app_settings,
            load_observability_settings,
            load_pages_settings,
            load_peer_thread_settings,
            load_rate_limit_settings,
            load_sandbox_settings,
            load_scheduler_settings,
            load_self_serve_agent_settings,
            load_speech_settings,
            load_system_settings,
            load_web_search_settings,
            playground_enabled,
        )
        from olano.magic_links.service import magic_links_available
        from olano.media_catalog import media_catalog
        from olano.runtime.disk_monitor import action_availability
        from olano.speech import speech_catalog

        hc = load_home_config()
        ss = load_system_settings()
        rl = load_rate_limit_settings()
        sp = load_speech_settings()
        md = load_media_settings()
        wss = load_web_search_settings()
        dk = load_disk_monitor_settings()
        hs = load_magic_link_settings()
        mh = load_mcp_health_settings()
        # Host detection shells out to `docker info`; it is memoised for five
        # minutes, but the first call after a restart must not run on the event
        # loop (the socket round-trip would stall every other request).
        sandbox_block = await asyncio.to_thread(_api._sandbox_config_block, load_sandbox_settings())
        # Reads the deployment artifact and the counters DB (sqlite) — off-loop
        # for the same reason.
        usage_budget_block = await asyncio.to_thread(_api._usage_budget_config_block)
        hs_ok, hs_reason = magic_links_available(hs)
        credit_rate = credits_registry.get_credit_rate()
        return {
            "default_agent": hc.default_agent,
            "preferred_model": hc.preferred_model,
            "runtime_api_port": hc.runtime_api_port,
            "dashboard_port": hc.dashboard_port,
            # The RESOLVED value (posture-aware default applied), not the raw
            # tri-state — the dashboard toggle must show what the runtime
            # actually enforces; writes still store an explicit choice.
            "multi_user_enabled": auth_layer.multi_user_enabled(),
            # Team-transparency: show every agent's work-highlights digest to all
            # humans (default off → members see only their granted agents').
            "highlights_all_agents": hc.highlights_all_agents,
            # The Playground (animated 2D world) and its slow sprite-sheet
            # generation: opt-in, home-wide. Off means the nav item, the route
            # and the sprite step all stay dormant.
            "playground_enabled": playground_enabled(),
            # Member bot creation (config.yaml self_serve_agents:) — whether a
            # non-admin may create their own agent through POST /api/agents
            # (default on), how many they may own, and which archetypes a
            # self-serve create may pick from (empty = any, including
            # from-scratch). Admins are never subject to any of it.
            "self_serve_agents": load_self_serve_agent_settings().model_dump(),
            # Chat self-configuration fleet defaults (config.yaml
            # agent_autonomy:, everything default on) — what agents may change
            # about themselves from a conversation. Per-agent agent.json5 →
            # autonomy overrides these; changes apply to the agent's next
            # self_* tool call, no restart.
            "agent_autonomy": load_agent_autonomy_settings().model_dump(),
            # Multi-bot group threads (config.yaml groups:) — the shared
            # conversation a roster of 2–6 agents holds. `enabled` draws the
            # whole surface (rail section, New group, the group info panel);
            # `autonomous` is the separate, still-off-by-default switch that
            # lets a reply naming another member by @name hand it the next
            # turn, bounded by max_hops and cascade_timeout_seconds.
            "groups": load_group_thread_settings().model_dump(),
            "extra_bind_roots": list(hc.extra_bind_roots),
            # Deployment-wide outbound-destination lists for the SSRF-gated
            # tools. Empty = every public destination; an agent's own
            # egress_allow replaces the allow list, its egress_deny adds to the
            # deny list (security/egress.py). Applies live: resolved per call.
            "egress_allow": list(hc.egress_allow),
            "egress_deny": list(hc.egress_deny),
            "archetype_dirs": list(hc.archetype_dirs),
            "fast_model": ss.fast_model,
            "deep_model": ss.deep_model,
            # The `default:*` alias targets (Config → Default models). Per tier:
            # `value` = the operator's explicit target ("" = inherit), `resolved`
            # = what the alias currently points at, `builtin` = this box's
            # fallback — so the card can say "inherited → Olano Turbo".
            "default_models": default_models_report(),
            # Provider-independent speech services (voice-note transcription /
            # voice replies). Disabled by default; the UI's provider dropdowns
            # come from speech_providers, and the model/voice dropdowns from
            # speech_catalog (per-provider curated options).
            "speech": {
                "stt": sp.stt.model_dump(),
                "tts": sp.tts.model_dump(),
            },
            "speech_providers": list(SPEECH_PROVIDERS),
            "speech_catalog": speech_catalog(),
            # The customer's per-feature SOURCE choice for vision, image and web
            # search (dashboard Config → Media & search). Stored values: "" =
            # auto (the runtime chain), "olano" = the managed service, or a
            # "provider:model" BYOK pin; web_search is "" | olano | tavily |
            # disabled. `media_catalog` is the live per-feature option list
            # (managed option only when entitled; BYOK models only for providers
            # whose key is present), the same "keyed providers → their models"
            # shape as the chat model dropdown — computed server-side because
            # image-generation models are not in /api/catalog/providers.
            "media": {"vision": md.vision, "image": md.image},
            "web_search": {"provider": wss.provider},
            "media_catalog": media_catalog(),
            # Home-wide inbound message rate-limit defaults (opt-in; empty =
            # unlimited). Windows with no cap are omitted so the UI's
            # empty-input model round-trips.
            "rate_limits": {
                "messages": rl.messages.model_dump(exclude_none=True),
                "per_platform": {
                    name: win.model_dump(exclude_none=True) for name, win in rl.per_platform.items()
                },
            },
            # Disk monitor thresholds + which allow-listed cleanup actions run
            # automatically (safe ones by default; docker_prune is opt-in).
            # Applies live — the monitor loop re-reads this every interval.
            "disk_monitor": dk.model_dump(),
            # Catalog for the UI checkboxes: name/description/safe flag, plus
            # whether the action can run on this host (binary present, etc.).
            "disk_cleanup_actions": action_availability(dk),
            # Local overrides of the deployment's TOKEN budgets, plus the
            # thresholds at which a filling budget raises a notification. The
            # rows carry each budget's live limit/usage so the form shows what
            # is actually enforced, not just what was typed. Cost budgets are
            # listed read-only: they bound olano's own upstream bill and are
            # not settable from the box.
            "usage_budgets": usage_budget_block,
            # Magic links (chat → browser page). `available` folds in whether
            # this deployment actually has a public origin to serve them from,
            # so the UI can explain a disabled state instead of offering a
            # button that always fails. Emitted under the legacy `handshake`
            # key too — an external script reading the config API predates the
            # rename and costs one dict entry to keep working.
            "magic_links": {
                **hs.model_dump(),
                "available": hs_ok,
                "unavailable_reason": hs_reason,
            },
            "handshake": {**hs.model_dump(), "available": hs_ok, "unavailable_reason": hs_reason},
            # MCP connection upkeep: renew OAuth grants before they lapse and
            # reconnect servers that dropped out. Applies live — the health loop
            # re-reads this every tick.
            "mcp_health": mh.model_dump(),
            # How much conversation two agents carry into each talk_to_agent
            # exchange, and the checkpoint history sweep. Both read live.
            "peer_threads": load_peer_thread_settings().model_dump(),
            "checkpoint_gc": load_checkpoint_gc_settings().model_dump(),
            # How late a scheduled task may still run, and whether a firing
            # that fell inside a restart is run at boot. Read at job
            # registration (a restart or a task save applies it).
            "scheduler": load_scheduler_settings().model_dump(),
            # What the Olano mobile app shows (tabs, Inbox sections, Settings
            # rows). Phones read it from GET /api/mobile/config at every launch
            # and return to the foreground, so it applies without a restart.
            "mobile_app": load_mobile_app_settings().model_dump(),
            "pages": load_pages_settings().model_dump(),
            "browser": load_browser_settings().model_dump(),
            # Opt-in Langfuse tracing. Keys are never here (reserved vault
            # rows); /api/observability/langfuse/status says where they come from.
            "observability": load_observability_settings().model_dump(),
            "browser_status": await asyncio.to_thread(_browser_status),
            # Context-window policy: what a compaction keeps / when it fires,
            # and the MCP result offload cap. Applies to agents at their next
            # build (restart / reload); per-agent agent.json5 overrides win.
            "context_window": load_context_window_settings().model_dump(),
            # Inbound surfaces for systems OUTSIDE the deployment (MCP endpoint,
            # per-agent A2A endpoints, Olano's own OAuth authorization server).
            # Both surfaces default off. Applies live: every request re-reads it.
            "external_access": load_external_access_settings().model_dump(),
            # Resource ceilings for each agent's sandbox container, plus the
            # detected host size and what the limits actually resolve to. The
            # computed halves are read-only (the PUT drops them) — they exist so
            # the card can explain "auto" instead of showing two empty fields.
            "sandbox": sandbox_block,
            # Cost display unit. `credits_per_usd` and the unit vocabulary are
            # read-only here — they come from the cloud catalog, not this file,
            # so a rate change never needs a config edit. `source` lets the UI
            # distinguish a synced rate from the bundled launch rate.
            "credits": {
                "display": cost_display_mode(),
                # Managed boxes: the display unit is signed policy; the Config
                # card renders read-only and the PUT below refuses writes.
                "display_locked": cost_display_locked(),
                "credits_per_usd": credit_rate.get("creditsPerUsd"),
                "unit_name": credit_rate.get("unitName"),
                "unit_name_plural": credit_rate.get("unitNamePlural"),
                "decimals": credit_rate.get("decimals"),
                "source": credit_rate.get("source"),
                "revision": credit_rate.get("revision"),
            },
            "cost_display_modes": list(COST_DISPLAY_MODES),
            "config_path": str(get_olano_paths().config_path),
            # Fields that only take effect after a runtime restart.
            "restart_required": ["runtime_api_port", "dashboard_port", "multi_user_enabled"],
        }

    @app.put("/api/system/config")
    async def update_system_config(body: dict) -> dict:
        """Persist changes to the global config.yaml. Accepts any subset of the
        fields returned by GET. HomeConfig fields are merged via update_home_config
        (which preserves the cognition block); fast_model/deep_model write into the
        cognition block. Port / multi-user changes need a runtime restart to apply.
        """
        from olano.config.home import update_home_config
        from olano.config.system_settings import set_cognition_model

        body = body or {}
        home_updates: dict = {}

        if "default_agent" in body:
            name = str(body["default_agent"] or "").strip()
            if not name:
                raise HTTPException(status_code=400, detail="default_agent cannot be empty")
            home_updates["default_agent"] = name
        if "preferred_model" in body:
            pm = body["preferred_model"]
            home_updates["preferred_model"] = (str(pm).strip() or None) if pm else None
        for port_field in ("runtime_api_port", "dashboard_port"):
            if port_field in body:
                try:
                    port = int(body[port_field])
                except (TypeError, ValueError) as exc:
                    raise HTTPException(
                        status_code=400, detail=f"{port_field} must be an integer"
                    ) from exc
                if not (1 <= port <= 65535):
                    raise HTTPException(status_code=400, detail=f"{port_field} must be 1–65535")
                home_updates[port_field] = port
        if "multi_user_enabled" in body:
            home_updates["multi_user_enabled"] = bool(body["multi_user_enabled"])
        if "highlights_all_agents" in body:
            home_updates["highlights_all_agents"] = bool(body["highlights_all_agents"])
        if "playground_enabled" in body:
            # Its own config.yaml block, so it travels with set_playground_enabled
            # rather than the home-config merge above.
            from olano.config.system_settings import set_playground_enabled

            set_playground_enabled(bool(body["playground_enabled"]))
            # /api/status caches capabilities for 5s. The dashboard re-reads
            # them the moment this returns, so without dropping the cache the
            # nav could keep the old answer past the save that changed it.
            _caps_cache["expires"] = 0.0
        if "self_serve_agents" in body:
            # Its own config.yaml block; the writer round-trips it through the
            # settings model so the quota clamp and archetype-list coercion
            # apply on write exactly as they do on read.
            from olano.config.system_settings import set_self_serve_agent_settings

            ssa_body = body["self_serve_agents"] or {}
            if not isinstance(ssa_body, dict):
                raise HTTPException(status_code=400, detail="self_serve_agents must be an object")
            ssa_updates: dict = {}
            if "enabled" in ssa_body:
                ssa_updates["enabled"] = bool(ssa_body["enabled"])
            if "max_per_user" in ssa_body:
                try:
                    ssa_updates["max_per_user"] = int(ssa_body["max_per_user"])
                except (TypeError, ValueError) as exc:
                    raise HTTPException(
                        status_code=400,
                        detail="self_serve_agents.max_per_user must be an integer",
                    ) from exc
            if "allowed_archetypes" in ssa_body:
                value = ssa_body["allowed_archetypes"]
                if not isinstance(value, list):
                    raise HTTPException(
                        status_code=400,
                        detail="self_serve_agents.allowed_archetypes must be a list",
                    )
                ssa_updates["allowed_archetypes"] = [
                    str(x).strip() for x in value if str(x).strip()
                ]
            if ssa_updates:
                set_self_serve_agent_settings(ssa_updates)
                # The enabled flag rides /api/status capabilities (that is
                # what draws a member's New-bot button), so the save must
                # outrun the 5s cache the same way playground_enabled does.
                _caps_cache["expires"] = 0.0
        if "agent_autonomy" in body:
            # Fleet defaults for chat self-configuration. Booleans only; the
            # writer drops unknown keys, so a stale dashboard build cannot
            # invent a capability. No capability cache to drop — the self_*
            # tools re-read the block on every call.
            from olano.config.system_settings import (
                AgentAutonomySettings,
                set_agent_autonomy_settings,
            )

            aa_body = body["agent_autonomy"] or {}
            if not isinstance(aa_body, dict):
                raise HTTPException(status_code=400, detail="agent_autonomy must be an object")
            aa_updates = {
                k: bool(v) for k, v in aa_body.items() if k in AgentAutonomySettings.model_fields
            }
            if aa_updates:
                set_agent_autonomy_settings(aa_updates)
        if "groups" in body:
            # Multi-bot group threads. The writer round-trips the merged block
            # through GroupThreadSettings, so the roster bounds and both
            # cascade limits are clamped on write exactly as on read — this
            # handler only has to reject the wrong *type*, never police ranges.
            from olano.config.system_settings import (
                GroupThreadSettings,
                set_group_thread_settings,
            )

            gt_body = body["groups"] or {}
            if not isinstance(gt_body, dict):
                raise HTTPException(status_code=400, detail="groups must be an object")
            bool_fields = {"enabled", "autonomous"}
            int_fields = {"min_members", "max_members", "max_hops", "cascade_timeout_seconds"}
            gt_updates: dict = {}
            for key, value in gt_body.items():
                if key not in GroupThreadSettings.model_fields:
                    continue
                if key in bool_fields:
                    gt_updates[key] = bool(value)
                elif key in int_fields:
                    try:
                        gt_updates[key] = int(value)
                    except (TypeError, ValueError) as exc:
                        raise HTTPException(
                            status_code=400, detail=f"groups.{key} must be an integer"
                        ) from exc
            if gt_updates:
                set_group_thread_settings(gt_updates)
                # groups_enabled rides /api/status capabilities — that is what
                # draws every group surface in the simple shell — so the save
                # has to outrun the 5s cache the same way playground_enabled
                # does, or the rail keeps the old answer past the save.
                _caps_cache["expires"] = 0.0
        for list_field in ("extra_bind_roots", "archetype_dirs"):
            if list_field in body:
                value = body[list_field]
                if not isinstance(value, list):
                    raise HTTPException(status_code=400, detail=f"{list_field} must be a list")
                home_updates[list_field] = [str(x).strip() for x in value if str(x).strip()]
        from olano.security.egress import EgressRuleError, normalize_domains

        for list_field in ("egress_allow", "egress_deny"):
            if list_field in body:
                value = body[list_field]
                if not isinstance(value, list):
                    raise HTTPException(status_code=400, detail=f"{list_field} must be a list")
                # Strict here, lenient on the read: a dashboard save must never
                # persist something other than what was typed, while the hot
                # path that reads the file back must never raise.
                try:
                    home_updates[list_field] = normalize_domains(value, strict=True)
                except EgressRuleError as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"{list_field}: {exc}. Enter bare domain names or IPv4 "
                            "addresses, one per line, like api.github.com"
                        ),
                    ) from exc

        if home_updates:
            update_home_config(home_updates)

        if "fast_model" in body:
            fast = str(body["fast_model"] or "").strip() or None
            _api._validate_brain(fast)
            set_cognition_model("fast_model", fast)
        if "deep_model" in body:
            dm = body["deep_model"]
            deep = (str(dm).strip() or None) if dm else None
            _api._validate_brain(deep)
            set_cognition_model("deep_model", deep)

        if "default_models" in body:
            # The `default:*` alias targets. Accepts any subset of the tiers;
            # "" / null resets that tier to this box's built-in target.
            # set_default_model does the semantic validation (no alias-to-alias,
            # no utility groups, no unknown olano tier).
            from olano.config.system_settings import DEFAULT_MODEL_TIERS, set_default_model

            dm_body = body["default_models"] or {}
            if not isinstance(dm_body, dict):
                raise HTTPException(status_code=400, detail="default_models must be an object")
            unknown = [k for k in dm_body if k not in DEFAULT_MODEL_TIERS]
            if unknown:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown default-model tiers: {', '.join(sorted(unknown))}",
                )
            for tier in DEFAULT_MODEL_TIERS:
                if tier not in dm_body:
                    continue
                raw_value = dm_body[tier]
                value = str(raw_value or "").strip()
                try:
                    set_default_model(tier, value or None)
                except ValueError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc

        if "credits" in body:
            # Only `display` is writable — the rate and unit names are catalog
            # data. Accepting them here would let a deployment drift from the
            # published rate, which is exactly what syncing them prevents.
            from olano.config.system_settings import (
                cost_display_locked,
                set_cost_display_mode,
            )

            credits_body = body["credits"] or {}
            if not isinstance(credits_body, dict):
                raise HTTPException(status_code=400, detail="credits must be an object")
            if "display" in credits_body:
                # Managed boxes: the unit is signed deployment policy. Writing
                # config.yaml here would be dead config at best and, at worst,
                # look like a control the operator never actually had.
                if cost_display_locked():
                    raise HTTPException(
                        status_code=403,
                        detail=(
                            "Cost display is set by your plan at cloud.olano.ai "
                            "and cannot be changed from this dashboard."
                        ),
                    )
                try:
                    set_cost_display_mode(str(credits_body["display"] or ""))
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc

        if "rate_limits" in body:
            from olano.config.system_settings import set_rate_limits

            rl_body = body["rate_limits"] or {}
            if not isinstance(rl_body, dict):
                raise HTTPException(status_code=400, detail="rate_limits must be an object")
            messages = rl_body.get("messages") or {}
            per_platform = rl_body.get("per_platform") or {}
            if not isinstance(messages, dict) or not isinstance(per_platform, dict):
                raise HTTPException(
                    status_code=400,
                    detail="rate_limits.messages and per_platform must be objects",
                )

            def _validate_windows(windows: dict, ctx: str) -> None:
                for key, val in windows.items():
                    if val is None:
                        continue
                    if isinstance(val, bool) or not isinstance(val, int) or val < 0:
                        raise HTTPException(
                            status_code=400,
                            detail=f"{ctx}.{key} must be a non-negative integer or null",
                        )

            _validate_windows(messages, "rate_limits.messages")
            for pname, pwin in per_platform.items():
                if not isinstance(pwin, dict):
                    raise HTTPException(
                        status_code=400,
                        detail=f"rate_limits.per_platform.{pname} must be an object",
                    )
                _validate_windows(pwin, f"rate_limits.per_platform.{pname}")
            set_rate_limits(messages, per_platform)

        if "speech" in body:
            from olano.config.system_settings import (
                SPEECH_PROVIDERS,
                load_speech_settings,
                set_speech_settings,
            )

            sp_body = body["speech"] or {}
            if not isinstance(sp_body, dict):
                raise HTTPException(status_code=400, detail="speech must be an object")
            persisted = load_speech_settings()
            for section in ("stt", "tts"):
                sec = sp_body.get(section)
                if sec is None:
                    continue
                if not isinstance(sec, dict):
                    raise HTTPException(
                        status_code=400, detail=f"speech.{section} must be an object"
                    )
                provider = sec.get("provider")
                if provider is not None and provider not in SPEECH_PROVIDERS:
                    raise HTTPException(
                        status_code=400,
                        detail=f"speech.{section}.provider must be one of {SPEECH_PROVIDERS}",
                    )
                # Updates are merged into the persisted section, so a URL saved
                # earlier satisfies the custom-provider requirement.
                effective_url = str(sec.get("url", getattr(persisted, section).url) or "").strip()
                if provider == "custom" and not effective_url:
                    raise HTTPException(
                        status_code=400,
                        detail=f"speech.{section}.url is required for the custom provider",
                    )
            set_speech_settings(sp_body.get("stt"), sp_body.get("tts"))

        if "media" in body:
            from olano.config.system_settings import set_media_settings

            md_body = body["media"] or {}
            if not isinstance(md_body, dict):
                raise HTTPException(status_code=400, detail="media must be an object")
            for k in ("vision", "image"):
                if k in md_body and md_body[k] is not None and not isinstance(md_body[k], str):
                    raise HTTPException(status_code=400, detail=f"media.{k} must be a string")
            # No _reject_utility_brain here: "olano" (and olano:vision/olano:olano-image)
            # is the DELIBERATE selection of the managed service for that feature, not
            # a brain — the whole point of this control.
            set_media_settings(md_body.get("vision"), md_body.get("image"))

        if "web_search" in body:
            from olano.config.system_settings import (
                WEB_SEARCH_SOURCES,
                set_web_search_settings,
            )

            ws_body = body["web_search"] or {}
            if not isinstance(ws_body, dict):
                raise HTTPException(status_code=400, detail="web_search must be an object")
            provider = ws_body.get("provider")
            if provider is not None and str(provider).strip().lower() not in WEB_SEARCH_SOURCES:
                raise HTTPException(
                    status_code=400,
                    detail=f"web_search.provider must be one of {WEB_SEARCH_SOURCES}",
                )
            set_web_search_settings(provider)

        # `magic_links` is the current key; `handshake` is what clients written
        # before the rename send. The new name wins when a client sends both.
        if "magic_links" in body or "handshake" in body:
            from olano.config.system_settings import set_magic_link_settings

            key = "magic_links" if "magic_links" in body else "handshake"
            hs_body = body[key] or {}
            if not isinstance(hs_body, dict):
                raise HTTPException(status_code=400, detail=f"{key} must be an object")
            if "pin" in hs_body and str(hs_body["pin"]) not in ("required", "off"):
                raise HTTPException(
                    status_code=400, detail=f"{key}.pin must be 'required' or 'off'"
                )
            # `available`/`unavailable_reason` are read-only derived fields in the
            # GET payload; drop them so a round-tripped object doesn't 400.
            set_magic_link_settings(
                {k: v for k, v in hs_body.items() if k not in ("available", "unavailable_reason")}
            )

        if "mcp_health" in body:
            from olano.config.system_settings import (
                load_mcp_health_settings,
                set_mcp_health_settings,
            )

            mh_body = body["mcp_health"] or {}
            if not isinstance(mh_body, dict):
                raise HTTPException(status_code=400, detail="mcp_health must be an object")
            for key, lo, hi in (
                ("check_interval_minutes", 1, 24 * 60),
                ("refresh_window_minutes", 1, 12 * 60),
            ):
                if key in mh_body:
                    val = mh_body[key]
                    if isinstance(val, bool) or not isinstance(val, int) or not (lo <= val <= hi):
                        raise HTTPException(
                            status_code=400,
                            detail=f"mcp_health.{key} must be an integer {lo}–{hi}",
                        )
            # Checked against the merged result so a partial update can't leave a
            # window shorter than the tick — which would let a token be first
            # *noticed* after it had already lapsed, the exact gap the sweep exists
            # to close.
            persisted_mh = load_mcp_health_settings()
            eff_tick = mh_body.get("check_interval_minutes", persisted_mh.check_interval_minutes)
            eff_window = mh_body.get("refresh_window_minutes", persisted_mh.refresh_window_minutes)
            if isinstance(eff_tick, int) and isinstance(eff_window, int) and eff_window < eff_tick:
                raise HTTPException(
                    status_code=400,
                    detail="mcp_health.refresh_window_minutes must be >= check_interval_minutes",
                )
            set_mcp_health_settings(mh_body)

        if "peer_threads" in body:
            from olano.config.system_settings import set_peer_thread_settings

            pt_body = body["peer_threads"] or {}
            if not isinstance(pt_body, dict):
                raise HTTPException(status_code=400, detail="peer_threads must be an object")
            for key, lo, hi in (("max_age_days", 1, 365), ("max_turns", 10, 100_000)):
                if key in pt_body:
                    val = pt_body[key]
                    if isinstance(val, bool) or not isinstance(val, int) or not (lo <= val <= hi):
                        raise HTTPException(
                            status_code=400,
                            detail=f"peer_threads.{key} must be an integer {lo}–{hi}",
                        )
            if "rotate" in pt_body and not isinstance(pt_body["rotate"], bool):
                raise HTTPException(status_code=400, detail="peer_threads.rotate must be a boolean")
            set_peer_thread_settings(pt_body)

        if "scheduler" in body:
            from olano.config.system_settings import set_scheduler_settings

            sc_body = body["scheduler"] or {}
            if not isinstance(sc_body, dict):
                raise HTTPException(status_code=400, detail="scheduler must be an object")
            if "misfire_grace_minutes" in sc_body:
                val = sc_body["misfire_grace_minutes"]
                if isinstance(val, bool) or not isinstance(val, int) or not (1 <= val <= 1440):
                    raise HTTPException(
                        status_code=400,
                        detail="scheduler.misfire_grace_minutes must be an integer 1–1440",
                    )
            if "max_run_minutes" in sc_body:
                val = sc_body["max_run_minutes"]
                if isinstance(val, bool) or not isinstance(val, int) or not (0 <= val <= 1440):
                    raise HTTPException(
                        status_code=400,
                        detail="scheduler.max_run_minutes must be an integer 0–1440 (0 = no limit)",
                    )
            if "catch_up_on_boot" in sc_body and not isinstance(sc_body["catch_up_on_boot"], bool):
                raise HTTPException(
                    status_code=400, detail="scheduler.catch_up_on_boot must be a boolean"
                )
            set_scheduler_settings(sc_body)

        if "checkpoint_gc" in body:
            from olano.config.system_settings import set_checkpoint_gc_settings

            gc_body = body["checkpoint_gc"] or {}
            if not isinstance(gc_body, dict):
                raise HTTPException(status_code=400, detail="checkpoint_gc must be an object")
            for key, lo, hi in (
                ("interval_hours", 1, 24 * 7),
                ("boot_delay_minutes", 0, 24 * 60),
                ("keep_min", 1, 1000),
                ("max_minutes", 1, 24 * 60),
            ):
                if key in gc_body:
                    val = gc_body[key]
                    if isinstance(val, bool) or not isinstance(val, int) or not (lo <= val <= hi):
                        raise HTTPException(
                            status_code=400,
                            detail=f"checkpoint_gc.{key} must be an integer {lo}–{hi}",
                        )
            if "enabled" in gc_body and not isinstance(gc_body["enabled"], bool):
                raise HTTPException(
                    status_code=400, detail="checkpoint_gc.enabled must be a boolean"
                )
            set_checkpoint_gc_settings(gc_body)

        if "mobile_app" in body:
            from olano.config.system_settings import (
                MobileAppSettings,
                set_mobile_app_settings,
            )

            ma_body = body["mobile_app"] or {}
            if not isinstance(ma_body, dict):
                raise HTTPException(status_code=400, detail="mobile_app must be an object")
            nested = {"inbox", "settings_tab"}
            for key, value in ma_body.items():
                if key not in MobileAppSettings.model_fields:
                    raise HTTPException(
                        status_code=400, detail=f"mobile_app.{key} is not a setting"
                    )
                if key in nested:
                    if not isinstance(value, dict):
                        raise HTTPException(
                            status_code=400, detail=f"mobile_app.{key} must be an object"
                        )
                    group = MobileAppSettings.model_fields[key].annotation
                    allowed = set(getattr(group, "model_fields", {}))
                    for sub, sub_value in value.items():
                        if sub not in allowed:
                            raise HTTPException(
                                status_code=400, detail=f"mobile_app.{key}.{sub} is not a setting"
                            )
                        if not isinstance(sub_value, bool):
                            raise HTTPException(
                                status_code=400,
                                detail=f"mobile_app.{key}.{sub} must be true or false",
                            )
                elif not isinstance(value, bool):
                    raise HTTPException(
                        status_code=400, detail=f"mobile_app.{key} must be true or false"
                    )
            set_mobile_app_settings(ma_body)

        if "pages" in body:
            from olano.config.system_settings import PagesSettings, set_pages_settings

            pg_body = body["pages"] or {}
            if not isinstance(pg_body, dict):
                raise HTTPException(status_code=400, detail="pages must be an object")
            bounds = {
                "max_services_per_agent": (1, 50),
                "health_interval_seconds": (5, 3600),
                "request_timeout_seconds": (5, 3600),
                "max_request_body_mb": (1, 1024),
                "max_concurrent_requests": (1, 1024),
            }
            for key, value in pg_body.items():
                if key not in PagesSettings.model_fields:
                    raise HTTPException(status_code=400, detail=f"unknown pages setting: {key}")
                if key == "services_enabled":
                    if not isinstance(value, bool):
                        raise HTTPException(
                            status_code=400, detail="pages.services_enabled must be true or false"
                        )
                    continue
                lo, hi = bounds[key]
                if isinstance(value, bool) or not isinstance(value, int) or not (lo <= value <= hi):
                    raise HTTPException(
                        status_code=400, detail=f"pages.{key} must be an integer {lo}–{hi}"
                    )
            set_pages_settings(pg_body)

        if "observability" in body:
            from olano.config.system_settings import (
                LANGFUSE_CAPTURE_MODES,
                normalize_langfuse_host,
                set_langfuse_settings,
            )

            ob_body = body["observability"] or {}
            lf_body = ob_body.get("langfuse") if isinstance(ob_body, dict) else None
            if not isinstance(lf_body, dict):
                raise HTTPException(
                    status_code=400, detail="observability.langfuse must be an object"
                )
            if "capture" in lf_body and lf_body["capture"] not in LANGFUSE_CAPTURE_MODES:
                raise HTTPException(
                    status_code=400,
                    detail="observability.langfuse.capture must be 'metadata' or 'full'",
                )
            if "host" in lf_body:
                raw_host = str(lf_body["host"] or "").strip()
                if raw_host and not normalize_langfuse_host(raw_host):
                    raise HTTPException(
                        status_code=400,
                        detail="observability.langfuse.host must be an https:// address "
                        "(http:// only for localhost), or empty",
                    )
            if "sample_rate" in lf_body:
                rate = lf_body["sample_rate"]
                if (
                    isinstance(rate, bool)
                    or not isinstance(rate, int | float)
                    or not (0.0 <= float(rate) <= 1.0)
                ):
                    raise HTTPException(
                        status_code=400,
                        detail="observability.langfuse.sample_rate must be a number 0.0–1.0",
                    )
            for key in ("enabled", "trace_background"):
                if key in lf_body and not isinstance(lf_body[key], bool):
                    raise HTTPException(
                        status_code=400,
                        detail=f"observability.langfuse.{key} must be true or false",
                    )
            set_langfuse_settings(lf_body)

        if "browser" in body:
            from olano.browser.policy import enable_refusal
            from olano.config.system_settings import (
                BROWSER_MODES,
                BROWSER_PROVIDERS,
                load_browser_settings,
                set_browser_settings,
            )

            br_body = body["browser"] or {}
            if not isinstance(br_body, dict):
                raise HTTPException(status_code=400, detail="browser must be an object")
            if "mode" in br_body and br_body["mode"] not in BROWSER_MODES:
                raise HTTPException(
                    status_code=400, detail="browser.mode must be 'off', 'headless' or 'live'"
                )
            if "provider" in br_body and br_body["provider"] not in BROWSER_PROVIDERS:
                raise HTTPException(
                    status_code=400, detail="browser.provider must be 'local' or 'remote'"
                )
            if "timezone" in br_body:
                from olano.config.system_settings import is_iana_timezone

                tz = br_body["timezone"]
                if tz is None:
                    tz = ""
                if not isinstance(tz, str) or (tz.strip() and not is_iana_timezone(tz.strip())):
                    raise HTTPException(
                        status_code=400,
                        detail="browser.timezone must be empty or a time zone name like "
                        "Europe/Madrid",
                    )
                br_body = {**br_body, "timezone": tz.strip()}
            if "human_check" in br_body:
                from olano.config.system_settings import HUMAN_CHECK_MODES

                if br_body["human_check"] not in HUMAN_CHECK_MODES:
                    raise HTTPException(
                        status_code=400,
                        detail="browser.human_check must be 'wait', 'hold' or 'skip'",
                    )
            for key, lo, hi in (
                ("max_sessions", 1, 8),
                ("idle_timeout", 1, 240),
                ("human_check_wait", 1, 120),
            ):
                if key in br_body:
                    val = br_body[key]
                    if isinstance(val, bool) or not isinstance(val, int) or not (lo <= val <= hi):
                        raise HTTPException(
                            status_code=400,
                            detail=f"browser.{key} must be an integer {lo}–{hi}",
                        )
            before = load_browser_settings()
            mode = br_body.get("mode", before.mode)
            provider = br_body.get("provider", before.provider)
            # Turning the browser on (or onto a heavier mode or the local
            # provider) is checked against the plan, the hardware and the
            # memory free right now; staying where it is never re-checks, so a
            # busy moment cannot lock an operator out of editing the timeouts.
            if mode != "off" and (mode, provider) != (before.mode, before.provider):
                refusal = await asyncio.to_thread(enable_refusal, mode, provider)
                if refusal:
                    raise HTTPException(status_code=400, detail=refusal)
            set_browser_settings({k: v for k, v in br_body.items() if k not in ("status", "image")})
            if mode == "live" and provider == "local":
                from olano.browser import sidecar

                # "Install on enable": fetch the live browser image now, in the
                # background, rather than on the first agent that needs it.
                await asyncio.to_thread(sidecar.start_pull)
            if (mode, provider) != (before.mode, before.provider):
                # Sessions of the old kind close (sign-ins saved) instead of
                # waiting for the idle sweep.
                try:
                    from olano.browser import loop as browser_loop
                    from olano.browser import sessions as browser_sessions

                    if browser_sessions.all_sessions():
                        await browser_loop.run_async(browser_sessions.close_all(), timeout_s=60)
                except Exception as exc:  # pragma: no cover - best effort
                    _api.logger.debug("closing browser sessions after a mode change: %s", exc)

        if "context_window" in body:
            from olano.config.system_settings import (
                ContextWindowSettings,
                load_context_window_settings,
                set_context_window_settings,
            )

            cw_body = body["context_window"] or {}
            if not isinstance(cw_body, dict):
                raise HTTPException(status_code=400, detail="context_window must be an object")
            cw_unknown = set(cw_body) - set(ContextWindowSettings.model_fields)
            if cw_unknown:
                raise HTTPException(
                    status_code=400,
                    detail=f"context_window: unknown field(s) {', '.join(sorted(cw_unknown))}",
                )
            for key, frac_lo, frac_hi in (
                ("keep_fraction", 0.05, 0.8),
                ("trigger_fraction", 0.3, 0.98),
            ):
                if key in cw_body:
                    val = cw_body[key]
                    if (
                        isinstance(val, bool)
                        or not isinstance(val, int | float)
                        or not (frac_lo <= val <= frac_hi)
                    ):
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                f"context_window.{key} must be a number "
                                f"between {frac_lo} and {frac_hi}"
                            ),
                        )
            for key, lo, hi in (
                ("keep_tokens", 2_000, 2_000_000),
                ("trigger_tokens", 8_000, 4_000_000),
                ("mcp_result_token_limit", 0, 200_000),
            ):
                if key in cw_body:
                    val = cw_body[key]
                    if isinstance(val, bool) or not isinstance(val, int) or not (lo <= val <= hi):
                        raise HTTPException(
                            status_code=400,
                            detail=f"context_window.{key} must be an integer {lo}–{hi}",
                        )
            # Keep strictly below trigger, checked on the merged result so a
            # partial update cannot cross them (the model validator would
            # silently halve the keep, which is worse than a 400 here).
            merged = {**load_context_window_settings().model_dump(), **cw_body}
            if merged["keep_fraction"] >= merged["trigger_fraction"]:
                raise HTTPException(
                    status_code=400,
                    detail="context_window.keep_fraction must be below trigger_fraction",
                )
            if merged["keep_tokens"] >= merged["trigger_tokens"]:
                raise HTTPException(
                    status_code=400,
                    detail="context_window.keep_tokens must be below trigger_tokens",
                )
            set_context_window_settings(cw_body)

        if "external_access" in body:
            from olano.config.system_settings import set_external_access_settings

            ea_body = body["external_access"] or {}
            if not isinstance(ea_body, dict):
                raise HTTPException(status_code=400, detail="external_access must be an object")
            for sub in ("mcp", "a2a", "oauth", "jwt"):
                if sub in ea_body and not isinstance(ea_body[sub], dict | bool):
                    raise HTTPException(
                        status_code=400, detail=f"external_access.{sub} must be an object"
                    )
            ttl = ea_body.get("token_ttl_days")
            if ttl is not None and (
                isinstance(ttl, bool) or not isinstance(ttl, int) or not (0 <= ttl <= 3650)
            ):
                raise HTTPException(
                    status_code=400, detail="external_access.token_ttl_days must be 0–3650"
                )
            retention = ea_body.get("a2a_task_retention_days")
            if retention is not None and (
                isinstance(retention, bool)
                or not isinstance(retention, int)
                or not (1 <= retention <= 3650)
            ):
                raise HTTPException(
                    status_code=400,
                    detail="external_access.a2a_task_retention_days must be 1–3650",
                )
            oauth_body = ea_body.get("oauth")
            if isinstance(oauth_body, dict):
                for key, lo, hi in (
                    ("access_token_minutes", 5, 24 * 60),
                    ("refresh_token_days", 1, 365),
                ):
                    if key in oauth_body:
                        val = oauth_body[key]
                        if (
                            isinstance(val, bool)
                            or not isinstance(val, int)
                            or not (lo <= val <= hi)
                        ):
                            raise HTTPException(
                                status_code=400,
                                detail=f"external_access.oauth.{key} must be an integer {lo}–{hi}",
                            )
            set_external_access_settings(ea_body)

        if "sandbox" in body:
            from olano.config.backend_config import (
                SANDBOX_CPUS_MAX,
                SANDBOX_CPUS_MIN,
                normalize_sandbox_cpus,
                normalize_sandbox_memory,
            )
            from olano.config.system_settings import set_sandbox_settings

            sb_body = body["sandbox"] or {}
            if not isinstance(sb_body, dict):
                raise HTTPException(status_code=400, detail="sandbox must be an object")
            # `host`/`effective`/`env_overrides` are read-only derived fields in
            # the GET payload; drop them so a round-tripped object doesn't 400.
            sb_updates = {
                k: v for k, v in sb_body.items() if k not in ("host", "effective", "env_overrides")
            }

            # Only keys actually present are touched — an absent one means "no
            # change to this field", and writing a None for it would clear a
            # stored limit on any partial save. An empty string or an explicit
            # null clears the field back to automatic sizing. Anything else is
            # validated loudly here, where the YAML loader would coerce
            # silently: a dashboard save must never quietly apply a different
            # limit than the one that was typed.
            def _normalize_limit(key: str, normalize: Any, guidance: str) -> None:
                if key not in sb_updates:
                    return
                value = sb_updates[key]
                if value in ("", None):
                    sb_updates[key] = None
                    return
                try:
                    sb_updates[key] = normalize(value)
                except ValueError as e:
                    raise HTTPException(
                        status_code=400, detail=f"sandbox.{key}: {e}. {guidance}"
                    ) from e

            _normalize_limit(
                "memory",
                normalize_sandbox_memory,
                "Use a docker size string like '4g' or '2048m', or leave it empty for the "
                "automatic limit.",
            )
            _normalize_limit(
                "cpus",
                normalize_sandbox_cpus,
                f"Use a number of cores between {SANDBOX_CPUS_MIN} and {SANDBOX_CPUS_MAX} "
                "(e.g. 2), or leave it empty for the automatic limit.",
            )
            set_sandbox_settings(sb_updates)

        if "disk_monitor" in body:
            from olano.config.system_settings import (
                DISK_CLEANUP_ACTION_ALIASES,
                DISK_CLEANUP_ACTIONS,
                load_disk_monitor_settings,
                set_disk_monitor_settings,
            )

            dm_body = body["disk_monitor"] or {}
            if not isinstance(dm_body, dict):
                raise HTTPException(status_code=400, detail="disk_monitor must be an object")

            # PUT rejects loudly where the YAML loader coerces silently — a
            # dashboard save must never silently change what the user typed.
            def _require_ranged_int(container: dict, key: str, lo: int, hi: int, ctx: str) -> None:
                if key not in container:
                    return
                val = container[key]
                if isinstance(val, bool) or not isinstance(val, int) or not (lo <= val <= hi):
                    raise HTTPException(
                        status_code=400, detail=f"{ctx}.{key} must be an integer {lo}–{hi}"
                    )

            _require_ranged_int(dm_body, "warn_percent", 50, 99, "disk_monitor")
            _require_ranged_int(dm_body, "cleanup_percent", 50, 99, "disk_monitor")
            _require_ranged_int(dm_body, "check_interval_minutes", 5, 1440, "disk_monitor")
            if "path" in dm_body and not str(dm_body["path"] or "").strip():
                raise HTTPException(status_code=400, detail="disk_monitor.path cannot be empty")
            # Threshold ordering is checked against the merged result so a
            # partial update can't sneak cleanup below warn.
            persisted_dm = load_disk_monitor_settings()
            eff_warn = dm_body.get("warn_percent", persisted_dm.warn_percent)
            eff_cleanup = dm_body.get("cleanup_percent", persisted_dm.cleanup_percent)
            if (
                isinstance(eff_warn, int)
                and isinstance(eff_cleanup, int)
                and eff_cleanup < eff_warn
            ):
                raise HTTPException(
                    status_code=400,
                    detail="disk_monitor.cleanup_percent must be >= warn_percent",
                )
            cl_body = dm_body.get("cleanup")
            if cl_body is not None:
                if not isinstance(cl_body, dict):
                    raise HTTPException(
                        status_code=400, detail="disk_monitor.cleanup must be an object"
                    )
                _require_ranged_int(cl_body, "cooldown_minutes", 30, 10080, "disk_monitor.cleanup")
                _require_ranged_int(cl_body, "force_interval_days", 0, 90, "disk_monitor.cleanup")
                if "actions" in cl_body:
                    actions = cl_body["actions"]
                    if not isinstance(actions, list):
                        raise HTTPException(
                            status_code=400, detail="disk_monitor.cleanup.actions must be a list"
                        )
                    # A dashboard tab opened before an action rename may still
                    # post the legacy name — accept and store the current one.
                    actions = [DISK_CLEANUP_ACTION_ALIASES.get(str(a), str(a)) for a in actions]
                    unknown = [a for a in actions if a not in DISK_CLEANUP_ACTIONS]
                    if unknown:
                        raise HTTPException(
                            status_code=400, detail=f"unknown cleanup action: {unknown[0]}"
                        )
                    cl_body["actions"] = actions
                if "journald_vacuum_size" in cl_body:
                    size = str(cl_body["journald_vacuum_size"] or "").strip()
                    if not re.fullmatch(r"\d+[KMGT]?", size):
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                "disk_monitor.cleanup.journald_vacuum_size must be a size "
                                "like 200M or 1G"
                            ),
                        )
            sc_body = dm_body.get("scan")
            if sc_body is not None:
                if not isinstance(sc_body, dict):
                    raise HTTPException(
                        status_code=400, detail="disk_monitor.scan must be an object"
                    )
                _require_ranged_int(sc_body, "interval_hours", 1, 168, "disk_monitor.scan")
                _require_ranged_int(sc_body, "depth", 1, 4, "disk_monitor.scan")
                _require_ranged_int(sc_body, "agent_depth", 0, 4, "disk_monitor.scan")
                _require_ranged_int(sc_body, "time_budget_seconds", 30, 3600, "disk_monitor.scan")
                _require_ranged_int(sc_body, "throttle_ms", 0, 1000, "disk_monitor.scan")
                _require_ranged_int(sc_body, "max_entries", 50, 2000, "disk_monitor.scan")
            set_disk_monitor_settings(dm_body)

        if "usage_budgets" in body:
            from olano.config.system_settings import (
                OVERRIDABLE_USAGE_LIMITS,
                UNLIMITED_SPELLINGS,
                set_usage_budget_settings,
            )

            ub_body = body["usage_budgets"] or {}
            if not isinstance(ub_body, dict):
                raise HTTPException(status_code=400, detail="usage_budgets must be an object")

            updates: dict[str, Any] = {}
            for key in OVERRIDABLE_USAGE_LIMITS:
                if key not in ub_body:
                    continue
                raw = ub_body[key]
                # Three states, and the API keeps them apart exactly as the
                # config file does: null/"" clears the override (back to the
                # deployment ceiling), "unlimited" removes the ceiling, and a
                # non-negative integer replaces it. The loader coerces junk
                # silently; a dashboard save must not.
                if raw is None or (isinstance(raw, str) and not raw.strip()):
                    updates[key] = None
                    continue
                if isinstance(raw, str) and raw.strip().lower() in UNLIMITED_SPELLINGS:
                    updates[key] = "unlimited"
                    continue
                try:
                    value = int(str(raw).strip().replace("_", "").replace(",", ""))
                except (TypeError, ValueError) as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"usage_budgets.{key} must be a non-negative integer, "
                            f"'unlimited', or empty to inherit the deployment limit"
                        ),
                    ) from exc
                if value < 0:
                    raise HTTPException(
                        status_code=400, detail=f"usage_budgets.{key} cannot be negative"
                    )
                updates[key] = value
            # Cost budgets bound olano's own upstream bill and are signed, not
            # local. Say so rather than accepting a write that would be dropped.
            refused = [k for k in ub_body if k.startswith("cost_")]
            if refused:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"usage_budgets.{refused[0]} is not settable on this box — the cost "
                        f"budgets come from the deployment's signed settings. Token budgets "
                        f"({', '.join(OVERRIDABLE_USAGE_LIMITS)}) can be overridden here."
                    ),
                )
            if "alerts_enabled" in ub_body:
                updates["alerts_enabled"] = bool(ub_body["alerts_enabled"])
            if "alert_percents" in ub_body:
                pcts = ub_body["alert_percents"]
                if not isinstance(pcts, list):
                    raise HTTPException(
                        status_code=400, detail="usage_budgets.alert_percents must be a list"
                    )
                cleaned: list[int] = []
                for item in pcts:
                    if isinstance(item, bool) or not isinstance(item, int) or not (1 <= item <= 99):
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                "usage_budgets.alert_percents entries must be integers 1–99 "
                                "(100 is always alerted and needs no entry)"
                            ),
                        )
                    cleaned.append(item)
                updates["alert_percents"] = cleaned
            if updates:
                set_usage_budget_settings(updates)

        # Re-read so the client always renders persisted truth.
        return await get_system_config()

    # Container -> media type for the voice preview's response.
    _PREVIEW_MIME = {
        ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg",
        ".wav": "audio/wav",
        ".flac": "audio/flac",
        ".aac": "audio/aac",
        ".m4a": "audio/mp4",
    }
    # Long enough for the dashboard's two sample sentences in any language,
    # short enough that a preview can never become a bill.
    _PREVIEW_MAX_CHARS = 300
    _PREVIEW_DEFAULT_TEXT = "Hello! This is how your agent's voice replies will sound."

    @app.post("/api/system/config/speech/preview")
    async def preview_speech_voice(body: dict) -> Any:
        """Read a short sample aloud with the TTS settings in the Config form.

        Body: ``{"tts": {provider, model, voice, url, api_key_name}, "text": str}``.
        ``tts`` is merged over the saved ``speech.tts`` block for this one call
        and never persisted, so a voice can be heard before it is saved. Keys
        resolve the home-wide way (vault, then environment), the same as a turn
        with no per-agent override. It is a real synthesis call and bills like
        one. Returns the audio itself; nothing is written anywhere.

        Asks for mp3 (every browser plays it; the Ogg/Opus voice-bubble default
        is not playable in Safari) unless the provider takes no format at all
        (``qwen``, ``custom`` until opted in), which then picks its own.
        Errors: ``409`` ``{code: tts_disabled | tts_not_configured, message}``,
        ``502`` ``{code: tts_provider_error, message}``.
        """
        import tempfile
        from pathlib import Path

        from starlette.responses import Response

        from olano.config.system_settings import SpeechServiceSettings, load_speech_settings
        from olano.speech import (
            SpeechDisabledError,
            SpeechNotConfiguredError,
            audio_extension_for_format,
            resolve_tts,
            synthesize_speech,
        )

        body = body or {}
        updates = body.get("tts") or {}
        if not isinstance(updates, dict):
            raise HTTPException(status_code=400, detail="tts must be an object")
        merged = load_speech_settings().tts.model_dump()
        allowed = set(SpeechServiceSettings.model_fields)
        merged.update({k: str(v or "") for k, v in updates.items() if k in allowed})
        settings = SpeechServiceSettings.model_validate(merged)
        text = str(body.get("text") or "").strip()[:_PREVIEW_MAX_CHARS] or _PREVIEW_DEFAULT_TEXT

        def _err(status: int, code: str, exc: BaseException) -> HTTPException:
            return HTTPException(status_code=status, detail={"code": code, "message": str(exc)})

        try:
            resolved = resolve_tts(settings)
        except SpeechDisabledError as exc:
            raise _err(409, "tts_disabled", exc) from None
        except SpeechNotConfiguredError as exc:
            raise _err(409, "tts_not_configured", exc) from None
        audio_format = "mp3" if resolved.audio_format else ""

        with tempfile.TemporaryDirectory(prefix="olano-voice-preview-") as tmp:
            out = Path(tmp) / f"preview{audio_extension_for_format(audio_format)}"
            try:
                written = await asyncio.to_thread(
                    synthesize_speech, text, str(out), "", audio_format, settings
                )
                data = await asyncio.to_thread(Path(written).read_bytes)
            except (SpeechDisabledError, SpeechNotConfiguredError) as exc:
                raise _err(409, "tts_not_configured", exc) from None
            except Exception as exc:
                _api.logger.warning("voice preview failed: %s", exc)
                raise _err(502, "tts_provider_error", exc) from exc
        mime = _PREVIEW_MIME.get(Path(written).suffix.lower(), "application/octet-stream")
        return Response(content=data, media_type=mime, headers={"Cache-Control": "no-store"})
