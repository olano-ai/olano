"""System status, seal gate and diagnostics endpoints.

Split out of ``olano.runtime.api.create_api``; see ``olano.runtime.routes``.
"""

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from olano.deployment import enforcement as dep_enforcement
from olano.runtime import api as _api
from olano.runtime import auth as auth_layer


def _open_posture() -> bool:
    """True on a self-hosted (open) box; never raises."""
    try:
        return dep_enforcement.deployment_posture().posture == "open"
    except Exception:  # pragma: no cover - defensive
        return False


def register_status_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach the status routes to ``app``."""
    # Closures defined by an earlier route module (see routes/__init__.py).
    _resolve_api_key = closures["_resolve_api_key"]

    # ── Status ────────────────────────────────────────────────────────────────

    # Cache for the user-independent half of /api/status capabilities.
    # ``(expires_at, payload)``; see _shared_capabilities below.
    _caps_cache: dict[str, Any] = {"expires": 0.0, "value": {}}
    _CAPS_TTL_SECONDS = 5.0

    def _shared_capabilities() -> dict:
        """Feature/entitlement flags that don't depend on who is asking.

        Every one of these costs real I/O — a vault lookup for the OpenAI key,
        config.yaml reads for the Playground and terminal switches, the
        deployment posture resolve — and they run **synchronously on the event
        loop**. That is affordable for an occasional call and not for this one:
        /api/status is the readiness probe every installer polls and every open
        dashboard tab hits every 2.5s, and it is the endpoint that must answer
        fastest precisely when the box is at its busiest (first boot, where the
        agent-build loop already owns the loop between awaits). A probe that
        can't answer inside the browser's budget is read as "the server is
        offline" — so the flags are computed at most once every few seconds and
        shared across callers, while the live per-request state (agents,
        channels, booting) stays uncached.
        """
        now = time.monotonic()
        if now < _caps_cache["expires"]:
            return dict(_caps_cache["value"])

        from olano.config.system_settings import (
            group_threads_enabled,
            playground_enabled,
            self_serve_agents_enabled,
        )
        from olano.runtime import terminal as terminal_mod
        from olano.speech import stt_available, tts_available
        from olano.tools.image_tools import olano_managed_media_available

        # Can this box generate a cartoon character at all? Either route counts:
        # the Olano image service (credits, no key — the default on a managed
        # deployment) or the customer's own OpenAI account. Gating this on the
        # OpenAI key alone, as it did before the Olano image service existed,
        # left the "Generate cartoon" button greyed out on exactly the boxes
        # where generation is free. `olano_managed_media_available` is the cheap
        # check (posture + a key/token already on disk); it never enrolls, which
        # matters because this runs synchronously on the event loop.
        _image_gen = bool(_resolve_api_key("OPENAI_API_KEY")) or olano_managed_media_available()

        shared: dict[str, Any] = {
            "image_gen": _image_gen,
            # Deprecated alias of `image_gen`, kept so an older dashboard build
            # served from cache doesn't lose the button. No longer OpenAI-only.
            "openai_image_gen": _image_gen,
            # Feature-flag half of the terminal gate; the per-human half
            # (can_use_terminal) is ANDed in by the caller, uncached.
            "terminal_feature": terminal_mod.terminal_enabled(),
            # Where a terminal session's shell runs — inside the agent's sandbox
            # container (managed posture) or as a host shell in the agent's
            # workspace. Posture-only, so the shared cache is safe; the
            # dashboard's Terminal header words its hint from it.
            "terminal_sandbox": terminal_mod.terminal_uses_sandbox(),
            # Home-wide Playground switch (config.yaml playground.enabled). When
            # off, the dashboard hides the Playground and character generation
            # skips the animation sprite sheets (portrait still generated).
            "playground_enabled": playground_enabled(),
            # Home-wide self-serve agent creation (config.yaml
            # self_serve_agents.enabled, default ON). A deployment setting,
            # never a per-user one: it says whether this box lets non-admins
            # create their own agent at all, which is what decides whether a
            # member sees the New-bot button (admins see it regardless — the
            # server never gated them). The caller's own remaining quota is
            # deliberately NOT here — /api/status is a public path and this
            # payload is cached process-wide for seconds, so anything
            # per-user would be served to the wrong human.
            "self_serve_agents": self_serve_agents_enabled(),
            # Home-wide multi-bot group threads (config.yaml
            # groups.enabled). Off by default; while off, every
            # /api/groups* route answers 404.
            "groups_enabled": group_threads_enabled(),
            # Server-side transcription for the dashboard composer's mic
            # (POST /api/transcribe). This is the *fallback* half of voice
            # input: browsers with the Web Speech API dictate locally for free
            # and never ask, so the flag only decides whether a browser without
            # it (Firefox) gets a mic at all. Managed boxes resolve STT to the
            # olano gateway by default, so it is on out of the box; the probe
            # is a config read, no network (speech.stt_available).
            "stt_enabled": stt_available(),
            # Text-to-speech is set up (Config -> Speech; off by default on
            # every install). The mobile app reads replies aloud in this voice
            # (POST /api/speech/speak) when it is on, and in the phone's own
            # voice when it is not. Same cheap config read as stt_enabled.
            "tts_enabled": tts_available(),
            # Static code capability, not a feature flag: this runtime can sign
            # MCP relay requests with an Ed25519 private key
            # (core/mcp_relay.py:sign_ed25519), and re-keys itself on boot if it
            # is still on the legacy shared secret (core/mcp_relay_enroll.py).
            # Diagnostic only — the enroller runs in-process, so nothing gates on
            # this flag; it just lets the console/operator tell at a glance whether
            # an instance's image is new enough to have migrated.
            "mcp_relay_ed25519": True,
        }
        # Deployment entitlements: per-tab visibility (tab_<name>, default-on —
        # the dashboard hides only on explicit False) + posture flags + the raw
        # config editor toggle. Never raises; open posture leaves everything on.
        try:
            from olano.deployment.schema import KNOWN_TABS

            dep_status = dep_enforcement.deployment_posture()
            for tab in KNOWN_TABS:
                shared[f"tab_{tab}"] = dep_status.settings.tab_enabled(tab)
            shared["deployment_enforced"] = dep_status.enforced
            shared["deployment_quarantined"] = dep_status.quarantined
            shared["agent_config_raw_enabled"] = dep_status.settings.feature_enabled(
                "agent_config_raw"
            )
        except Exception:
            _api.logger.exception("deployment capabilities failed; omitting flags")

        _caps_cache["value"] = shared
        _caps_cache["expires"] = now + _CAPS_TTL_SECONDS
        return dict(shared)

    # Cache for the box-identity half of /api/status. Same rationale as
    # _shared_capabilities: the probe is polled every ~2.5s and must never pay
    # I/O per call — gateway_key() can touch the vault (sqlite) and the version
    # env reads are cheap but there is no reason to repeat any of it per poll.
    _identity_cache: dict[str, Any] = {"expires": 0.0, "value": {}}

    def _runtime_identity() -> dict:
        """Build identity + gateway health, safe for the public status probe.

        ``runtime_version`` is the same value the box reports to the control
        plane (git-<sha> / image ref / src-<pkg>, or null when genuinely
        unknown — never a placeholder). ``gateway`` is
        ``enrolled | unenrolled | unattached``. Both exist here because a
        managed box has no SSH: after an Update, "did the new image actually
        boot, and does it hold a gateway key" must be answerable from outside
        the login wall — the 2026-08-15 gateway incident was debugged blind for
        want of exactly these two fields.
        """
        now_m = time.monotonic()
        if now_m < _identity_cache["expires"]:
            return dict(_identity_cache["value"])

        from olano.deployment.gateway_key import enrollment_state
        from olano.deployment.version import runtime_version

        try:
            gateway = enrollment_state()
        except Exception:  # pragma: no cover - identity must never break status
            _api.logger.exception("gateway state for /api/status failed")
            gateway = None
        try:
            version = runtime_version()
        except Exception:  # pragma: no cover - identity must never break status
            _api.logger.exception("runtime version for /api/status failed")
            version = None

        value: dict[str, Any] = {"runtime_version": version, "gateway": gateway}
        _identity_cache["value"] = value
        _identity_cache["expires"] = now_m + _CAPS_TTL_SECONDS
        return dict(value)

    # Cache for the data-custody half of /api/status. Same rationale as the two
    # above: the seal state is one small file on the root disk that changes
    # only at provisioning time, so the probe must not stat + parse it per poll.
    _custody_cache: dict[str, Any] = {"expires": 0.0, "value": {}}

    def _custody() -> dict:
        """Which volume this box keeps its data on — ``{"mode": "none"}`` on
        every box not provisioned with a sealed volume (runtime/seal_guard.py)."""
        now_m = time.monotonic()
        if now_m < _custody_cache["expires"]:
            return dict(_custody_cache["value"])

        from olano.config.home import get_olano_home
        from olano.runtime import seal_guard

        try:
            value: dict[str, Any] = seal_guard.custody_status(get_olano_home())
        except Exception:  # pragma: no cover - custody must never break status
            _api.logger.exception("custody status for /api/status failed")
            value = {"mode": "none"}
        _custody_cache["value"] = value
        _custody_cache["expires"] = now_m + _CAPS_TTL_SECONDS
        return dict(value)

    @app.get("/api/system/seal-gate", status_code=204)
    async def seal_gate():
        """The auth decision for the sealed volume's ceremony API.

        On a managed box with a sealed data volume the recovery-key ceremony
        is served by the sealer itself (``olano-sealer serve``) on a unix
        socket, and Caddy proxies ``/api/seal/*`` to it only after asking this
        route (``forward_auth`` in ``deploy/Caddyfile.allinone``): the original
        request's headers arrive here, so the session cookie resolves the
        caller exactly as it does for every other dashboard request. A 204
        means "let it through"; its two headers are copied onto the proxied
        request and are what the sealer trusts, so Caddy strips any copy the
        client sent first. Anything but a 2xx is returned to the client and
        the socket is never reached.

        Who passes: a signed-in admin, which is the owner role in this runtime
        (``auth.context_kwargs_for_request``: ``is_owner`` is ``is_admin``).
        ``/api/system`` sits under ``_ADMIN_ONLY_PREFIXES``, so in multi-user
        mode the middleware already answers 401 (no session) and 403 (a
        member) before this body runs; ``assert_admin`` is the belt for
        single-user mode and for any later edit of that prefix list. 404
        ``no_sealed_volume`` on every box without seal state (legacy and
        self-hosted installs): the dashboard hides the card there, and a
        request that reaches this anyway must not be forwarded to a socket
        that does not exist. Deliberately uncached (one stat and parse per
        ceremony request, never per poll) and ``no-store``.
        """
        from starlette.responses import Response

        from olano.runtime import seal_guard

        user = auth_layer.assert_admin()
        state = seal_guard.read_seal_state()
        if not state or not str(state.get("luks_uuid") or "").strip():
            raise HTTPException(status_code=404, detail="no_sealed_volume")
        return Response(
            status_code=204,
            headers={
                "X-Olano-Seal-Gate": "ok",
                # Who ran the ceremony, for the sealer's own log. The id, not
                # the email: header values must be ASCII and an id always is.
                "X-Olano-Seal-User": str(user.id),
                "Cache-Control": "no-store",
            },
        )

    @app.get("/api/status")
    async def get_status() -> dict:
        # Hidden agents stay out even when booted (LIONCLAW_BOOT_HIDDEN_AGENTS
        # or an explicit `olano serve --agent`).
        agents = [
            name
            for name, runner in list(runtime._runners.items())
            if not _api._agent_is_hidden(getattr(runner, "config", None))
        ]
        scheduler_jobs = runtime.scheduler.list_jobs() if runtime.scheduler else []

        capabilities = _shared_capabilities()
        # On = feature flag is on AND this human may use it (admin or granted).
        capabilities["terminal_enabled"] = (
            bool(capabilities.pop("terminal_feature", False)) and auth_layer.can_use_terminal()
        )
        # /api/status is public (it doubles as the pre-login readiness probe),
        # so the roster must not leak: anonymous callers get an empty list and
        # members only the agents they were granted. Admins/single-user: all.
        allowed = auth_layer.allowed_agent_names()
        if allowed is not None:
            agents = [a for a in agents if a in allowed]
        # Connectors ride on the same scope as the roster above, by
        # construction: an anonymous caller's `allowed` is the empty set, so
        # the pre-login readiness probe sees no connector either.
        channels = _api._live_connectors(runtime, allowed)
        channels_configured = _api._configured_connectors(runtime, allowed)
        # How many agents this caller will end up with once the boot loop is
        # done, so the dashboard can render "6 of 15 ready" instead of an
        # open-ended spinner. Counts the set the runtime actually intends to
        # boot (paused / hidden / deployment-blocked agents already removed),
        # scoped exactly like `agents` above. None = not resolved yet.
        expected = getattr(runtime, "_agents_expected", None)
        agents_total: int | None = None
        if isinstance(expected, list) and expected:
            agents_total = len(
                [n for n in expected if allowed is None or n in allowed]
                if allowed is not None
                else expected
            )
        # Provisioning-readiness signal for the cloud console: True once the
        # login gate is armed (multi-user on) and an admin sign-in exists —
        # i.e. first-boot provisioning (quick-setup) has finished and a
        # visitor gets a login screen, never the implicit owner's powers.
        # The console's deployment card must not offer "Open dashboard" while
        # this is False. Public route (pre-login readiness probe), and it
        # leaks nothing but a boolean. Fails closed: an error reads as "not
        # ready" so the console keeps the gate shut rather than opening early.
        try:
            from olano.users.store import get_user_store as _get_user_store

            setup_complete = (
                auth_layer.multi_user_enabled() and _get_user_store().db.count_users() > 0
            )
        except Exception:  # pragma: no cover - status must never break
            setup_complete = False

        return {
            # The API binds before the agent-boot loop, so "running" here means
            # the runtime process is up and serving — agents may still be
            # booting (see "booting"). Kept as "running" for compatibility.
            "status": "running",
            "booting": bool(getattr(runtime, "_agents_booting", False)),
            "setup_complete": setup_complete,
            "agents": agents,
            "agents_total": agents_total,
            "channels": channels,
            # Declared vs. attached — the Overview headline is what the human
            # configured, the "N active" sub-line is what actually came up.
            "channels_configured": channels_configured,
            "scheduler_jobs": len(scheduler_jobs),
            # The runtime's START instant, not "now" — the dashboard renders
            # this as (now - value). Emitting now() here pinned the Overview
            # uptime stat at 0s forever.
            "uptime": (
                getattr(runtime, "_started_at", None) or datetime.now(timezone.utc)
            ).isoformat(),
            "capabilities": capabilities,
            # Data custody: the volume the home lives on (cached, see _custody).
            "custody": _custody(),
            # Build identity + gateway health (cached, see _runtime_identity).
            **_runtime_identity(),
        }

    # ── Memory diagnostics ──────────────────────────────────────────────────────

    @app.get("/api/diagnostics/memory")
    async def get_memory_diagnostics(gc: bool = True, tracemalloc: bool = True) -> dict:
        """Process memory (RSS/VmSize/Pss/threads/uptime) + a breakdown of the
        in-process olano structures that accumulate — the leak-vs-baseline
        report. The gc census is O(objects), so it runs off the event loop."""
        from olano.admin.memory import memory_report

        try:
            return await asyncio.to_thread(
                memory_report, include_gc=gc, include_tracemalloc=tracemalloc
            )
        except Exception as exc:
            _api.logger.debug("memory report failed: %s", exc)
            raise HTTPException(status_code=500, detail="Failed to build memory report") from exc

    @app.post("/api/diagnostics/memory/monitor")
    async def set_memory_monitor_endpoint(request: Request) -> dict:
        """Toggle the background memory monitor / tracemalloc tracing at runtime.

        Body ``{"action": "on"|"off"|"status"|"tracemalloc-on"|"tracemalloc-off"}``.
        ``off`` also stops tracemalloc so the monitor's own footprint drops to
        zero without restarting the daemon."""
        from olano.admin.memory import apply_monitor_action

        try:
            payload = await request.json()
        except Exception:
            payload = {}
        action = str((payload or {}).get("action", "status"))
        try:
            return apply_monitor_action(runtime, action)
        except Exception as exc:
            _api.logger.debug("memory monitor toggle failed: %s", exc)
            raise HTTPException(status_code=400, detail="Failed to toggle memory monitor") from exc

    # ── Checkpoint history diagnostics ───────────────────────────────────────────

    @app.get("/api/diagnostics/checkpoints")
    async def get_checkpoint_diagnostics() -> dict:
        """Size of the checkpoint tables, the settings of the history sweep
        and its last report (``core/checkpoint_gc``). Postgres only: on the
        sqlite backend ``backend`` says so and the rest is empty."""
        from olano.config.system_settings import load_checkpoint_gc_settings
        from olano.core.checkpoints import aget_postgres_pool

        auth_layer.assert_admin()
        settings = await asyncio.to_thread(load_checkpoint_gc_settings)
        out: dict[str, Any] = {
            "backend": "sqlite",
            "settings": settings.model_dump(),
            "last_run": getattr(runtime, "_checkpoint_gc_last", None),
            "tables": None,
        }
        try:
            pool = await aget_postgres_pool()
        except Exception as exc:
            _api.logger.debug("checkpoint diagnostics: pool unavailable: %s", exc)
            pool = None
        if pool is None:
            return out
        out["backend"] = "postgres"
        try:
            async with pool.connection() as conn, conn.cursor() as cur:
                tables = {}
                for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                    await cur.execute("SELECT pg_total_relation_size(%s)", (table,))
                    row = await cur.fetchone()
                    value = row[0] if not isinstance(row, dict) else next(iter(row.values()))
                    tables[table] = int(value or 0)
                out["tables"] = tables
        except Exception as exc:
            _api.logger.debug("checkpoint diagnostics: size query failed: %s", exc)
        return out

    @app.post("/api/diagnostics/checkpoints/gc")
    async def run_checkpoint_gc_endpoint(request: Request) -> dict:
        """Run one checkpoint history sweep now. Body ``{"dry_run": bool}``;
        a dry run plans every namespace and removes nothing. Admin only."""
        auth_layer.assert_admin()
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        dry_run = bool((payload or {}).get("dry_run", False))
        run = getattr(runtime, "run_checkpoint_gc", None)
        if run is None:
            raise HTTPException(status_code=404, detail="checkpoint gc unavailable")
        try:
            report = await run(dry_run=dry_run)
        except Exception as exc:
            _api.logger.warning("checkpoint gc via API failed: %s", exc)
            raise HTTPException(status_code=500, detail="checkpoint gc failed") from exc
        if report is None:
            return {"backend": "sqlite", "ran": False}
        return {"backend": "postgres", "ran": True, "report": report}

    # ── Disk diagnostics ────────────────────────────────────────────────────────

    @app.get("/api/diagnostics/disk")
    async def get_disk_diagnostics() -> dict:
        """Disk usage on the monitored path + monitor settings, persisted state
        (last cleanup report) and per-action availability. Takes a fresh
        measurement, so it runs off the event loop."""
        from olano.admin.disk import disk_report

        try:
            return await asyncio.to_thread(disk_report)
        except Exception as exc:
            _api.logger.debug("disk report failed: %s", exc)
            raise HTTPException(status_code=500, detail="Failed to build disk report") from exc

    @app.get("/api/diagnostics/boot")
    async def get_boot_diagnostics(agent: str | None = None) -> dict:
        """Per-agent boot timing trees, newest last.

        Collected on every boot regardless of ``LIONCLAW_BOOT_PROFILE`` (that env
        var only adds the log output), so a slow startup can be investigated after
        the fact instead of only on the next restart. Pure in-memory read.
        """
        from olano.core.boot_profile import all_profiles, fleet_summary, report_enabled

        profiles = all_profiles()
        if agent:
            profiles = [p for p in profiles if p.agent == agent]
        return {
            "ok": True,
            "logging_enabled": report_enabled(),
            "summary": fleet_summary(profiles),
            "profiles": [p.to_dict() for p in profiles],
        }

    @app.get("/api/diagnostics/version")
    async def get_version_diagnostics() -> dict:
        """What image/commit this runtime is actually running.

        Exists because the control plane could not answer it: nothing on the box
        reported a version, so `deployments.runtime_version` stayed NULL and the
        console rendered that NULL as "latest (as deployed)" — a placeholder
        indistinguishable from a real answer, which made a deploy that pulled a
        stale image look exactly like one that worked (2026-08-14).

        `known: false` is a real answer here, never a value: it means this build
        carries no identity (a local/source build, or an image built before the
        build args were baked in), and the honest response is to say so.
        """
        from olano.deployment.version import version_report

        report = version_report()
        return {
            "ok": True,
            "remediation": (
                None
                if report.get("known")
                else (
                    "This build carries no version identity. Managed images bake "
                    "OLANO_BUILD_SHA at build time; a source install reports its package "
                    "version instead. Nothing is broken, but this box cannot tell the "
                    "console what it is running."
                )
            ),
            **report,
        }

    @app.get("/api/diagnostics/gateway")
    async def get_gateway_diagnostics(probe: bool = False) -> dict:
        """Olano model-gateway health: does this box hold a key, and does the
        gateway serve the managed groups this deployment depends on.

        Exists because the failures it describes were previously invisible. A box
        that could not enroll showed a green "connected" provider card, with the
        only evidence a WARNING scrolling past in the process log while every
        managed model 401'd (incident 2026-08-14). Separately, a box whose
        gateway served no `olano-vision` group reported managed vision as
        available everywhere while every image_describe call 400'd — a routing
        gap no local surface could see.

        The enrollment half is a pure in-memory read: that answer must be
        available precisely when the control plane is not. The `served` half is
        the cached probe reading (warmed at boot, network-free here); pass
        `?probe=1` to force a fresh look at the gateway's model list.
        """
        from olano.deployment import gateway_probe
        from olano.deployment.gateway_key import enrollment_status

        status = await asyncio.to_thread(enrollment_status)
        remedy = {
            "enrolled": None,
            "unenrolled": (
                "Retrying automatically with exponential backoff. If this persists, check "
                "that cloud.olano.ai is reachable from this box and that the deployment "
                "token is still valid; a key can also be set directly as "
                "OLANO_GATEWAY_KEY in /opt/olano/olano.env, which takes precedence. "
                "Full detail: olano gateway"
            ),
            "unattached": (
                "This box has no deployment token, so it cannot enroll. Attach it to "
                "cloud.olano.ai, or configure a provider key and use a provider:model "
                "instead of an olano: tier."
            ),
        }[str(status.get("state"))]
        if status.get("state") == "unattached" and _open_posture():
            # Only a self-hosted box can take an Olano API key; a quarantined
            # managed box without its token must not be told to paste one.
            remedy = (
                "This box has no deployment token, so it cannot enroll. Paste an Olano "
                "API key (cloud.olano.ai → API keys) on Providers → Olano, attach the box "
                "to cloud.olano.ai, or configure a provider key and use a provider:model "
                "instead of an olano: tier."
            )
        if status.get("source") == "account":
            remedy = (
                (
                    "The Olano API key this box holds was refused by the gateway (revoked "
                    "on cloud.olano.ai, or pasted incorrectly). Paste a valid key on "
                    "Providers → Olano."
                )
                if status.get("account_key_rejected")
                else None
            )
        reading = (
            await asyncio.to_thread(gateway_probe.probe, force=True)
            if probe
            else gateway_probe.cached()
        )
        payload: dict = {"ok": True, "remediation": remedy, **status}
        if reading is not None:
            payload["served"] = reading.to_dict()
        return payload

    @app.get("/api/diagnostics/mcp")
    async def get_mcp_diagnostics(agent: str | None = None) -> dict:
        """Last MCP health sweep: per-agent, per-server connection + token state.

        Answers "why did this connection drop, and is anything fixing it?"
        without touching the network — it reports what the sweep last observed
        and did (``refreshed`` / ``reload_requested`` / ``backoff`` /
        ``needs_authorization``). ``checked_at`` is null until the first tick.
        """
        from olano.config.system_settings import load_mcp_health_settings

        snapshot = runtime.mcp_health_snapshot()
        agents = snapshot.get("agents", {})
        if agent:
            auth_layer.assert_agent_access(agent)
            agents = {agent: agents[agent]} if agent in agents else {}
        else:
            allowed = auth_layer.allowed_agent_names()
            if allowed is not None:
                agents = {name: row for name, row in agents.items() if name in allowed}
        settings = await asyncio.to_thread(load_mcp_health_settings)
        from olano.core import mcp_relay_selfhost

        return {
            "ok": True,
            "checked_at": snapshot.get("checked_at"),
            "settings": settings.model_dump(),
            "agents": agents,
            # Fleet-wide, not per agent: a leaked stdio-MCP container carries
            # the agent's slug as a label, but the bridge it fills is the box's.
            "ephemeral_containers": snapshot.get("ephemeral_containers") or {},
            # Whether a self-hosted box rides the central MCP sign-in relay on
            # its Olano API key, and if not, the last reason (never the key).
            "selfhost_relay": await asyncio.to_thread(mcp_relay_selfhost.status),
        }

    @app.post("/api/diagnostics/disk/monitor")
    async def set_disk_monitor_endpoint(request: Request) -> dict:
        """Control automatic disk cleanup at runtime.

        Body ``{"action": "on"|"off"|"status"|"run"|"scan"}``. ``on``/``off``
        flip the persisted ``disk_monitor.cleanup.enabled`` opt-in — the
        monitor loop re-reads settings each tick, so it applies within one
        check interval, no restart. ``run`` starts a manual cleanup in the
        background and returns immediately (docker prune can take minutes);
        poll ``status`` / ``GET /api/diagnostics/disk`` until
        ``cleanup_running`` clears. ``scan`` starts the directory-size scan
        now (same background posture; watch ``scan_running`` / the
        ``disk_scan`` SSE frame)."""
        from olano.admin.disk import apply_monitor_action

        try:
            payload = await request.json()
        except Exception:
            payload = {}
        action = str((payload or {}).get("action", "status"))
        try:
            return await asyncio.to_thread(apply_monitor_action, runtime, action)
        except Exception as exc:
            _api.logger.debug("disk monitor action failed: %s", exc)
            raise HTTPException(
                status_code=400, detail="Failed to apply disk monitor action"
            ) from exc

    # ── Logs ──────────────────────────────────────────────────────────────────

    @app.get("/api/logs")
    async def get_logs(lines: int = 100) -> dict:
        recent = _api._log_buffer[-min(lines, _api._MAX_LOG_LINES) :]
        return {"logs": recent, "total": len(_api._log_buffer)}

    # Closures that later route modules bind (see routes/__init__.py).
    closures["_caps_cache"] = _caps_cache
