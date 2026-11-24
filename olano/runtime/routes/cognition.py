"""Cognition (Cortex) controls, status and items.

Split out of ``olano.runtime.api.create_api``; see ``olano.runtime.routes``.
"""

import asyncio
import re
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request

from olano.core.runs import REGISTRY as _RUNS
from olano.core.runs import AgentRun
from olano.core.runs import sse_run_view as _sse_run_view
from olano.runtime import api as _api
from olano.runtime import auth as auth_layer


def register_cognition_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach the cognition routes to ``app``."""
    # Closures defined by an earlier route module (see routes/__init__.py).
    _cortex_abs_path = closures["_cortex_abs_path"]
    _resolve_agent_for_pulse = closures["_resolve_agent_for_pulse"]

    # ── Cortex jobs: every background run, joinable and logged ────────────────

    from olano.cognition.jobs import REGISTRY as _JOBS
    from olano.cognition.jobs import CortexJob, status_for_outcome

    def _job_response(job: CortexJob, started: bool) -> dict:
        """What a ``background: true`` request answers: the job to follow."""
        return {"ok": True, "job": job.to_dict(), "already_running": not started}

    async def _await_job(job: CortexJob) -> dict[str, Any]:
        """Wait for a job on behalf of a caller that wants the old blocking
        contract. ``shield``: a client that disconnects stops waiting, it does
        not stop the run."""
        task = job.task
        if task is not None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # Two things raise this: the job was stopped (its task is
                # done, and the answer below says so), or THIS request was
                # cancelled by a client that went away, which must keep
                # propagating or the handler answers a socket nobody holds.
                if not task.done():
                    raise
        # A run that finished with a result answers with it even when that
        # result is a failure (the optimizers' contract: status "error" in a
        # 200); a run that produced none is a 500, as the inline route was.
        if job.status in ("done", "error") and job.result:
            return job.result
        if job.status != "done":
            raise HTTPException(status_code=500, detail=job.error or job.summary or job.status)
        return job.result

    # ── Cognition: autonomous optimization ────────────────────────────────────

    def _start_optimize(agent: Optional[str], which: str) -> tuple[CortexJob, bool]:
        """Start (or join) one optimizer run as a Cortex job."""
        from olano.cognition.audit import log_action
        from olano.cognition.backups import diff_against_backup
        from olano.cognition.orchestrator import phase_running
        from olano.config.workspace import get_workspace

        agent_name, cfg, _state_dir = _resolve_agent_for_pulse(agent)
        ws = get_workspace(cfg.workspace)
        phase_id = "workspace_optimize" if which == "workspace" else "skill_librarian"
        action = "optimize_workspace" if which == "workspace" else "optimize_skills"

        async def _run(scope: Any) -> None:
            if which == "workspace":
                from olano.cognition.workspace_optimizer import run_workspace_optimizer

                with phase_running(agent_name, phase_id):
                    outcome, backup_job = await run_workspace_optimizer(cfg, ws.root)
            else:
                from olano.cognition.skill_librarian import run_skill_librarian

                with phase_running(agent_name, phase_id):
                    outcome, backup_job = await run_skill_librarian(cfg, ws.root)
            changes = diff_against_backup(ws.root, backup_job) if backup_job else None
            detail: dict[str, Any] = {
                "status": outcome.status,
                "backup_job": backup_job,
                "phase": phase_id,  # so the per-phase Log finds it
                "files_changed": (changes or {}).get("files_changed"),
                "lines_delta": (changes or {}).get("lines_delta"),
            }
            if which == "skills":
                detail["files_removed"] = (changes or {}).get("files_removed")
            log_action(
                ws.root,
                action,
                summary=outcome.summary
                or ("Workspace optimized" if which == "workspace" else "Skills optimized"),
                detail=detail,
            )
            scope.complete(
                outcome=outcome.status,
                summary=outcome.summary or "",
                error=outcome.error or "",
                result={
                    "ok": True,
                    "agent": agent_name,
                    "status": outcome.status,
                    "summary": outcome.summary,
                    "artifacts": outcome.artifacts,
                    "backup_job": backup_job,
                    "changes": changes,
                },
                # "empty" (nothing to change) is a finished run; a timeout, a
                # budget block or a missing key is not, whatever the drawer
                # shows. Same rule as every other job (status_for_outcome).
            )

        return _JOBS.start(
            agent=agent_name,
            workspace_root=ws.root,
            kind=action,
            title="Optimize workspace" if which == "workspace" else "Optimize skills",
            run=_run,
        )

    @app.post("/api/cognition/optimize-workspace")
    async def optimize_workspace(body: dict) -> dict:
        """Run the workspace optimizer now (dedupe/reorg SOUL/AGENTS/IDENTITY/MEMORY).
        Applies changes in place (snapshotted) and posts a FINDING Pulse with Undo.

        ``background: true`` answers at once with the job (``GET
        /api/cortex/jobs/{id}`` follows it); otherwise the request waits for the
        result, as it always did. A second request while one runs joins it."""
        body = body or {}
        job, started = _start_optimize(body.get("agent"), "workspace")
        if body.get("background"):
            return _job_response(job, started)
        return await _await_job(job)

    @app.post("/api/cognition/optimize-skills")
    async def optimize_skills(body: dict) -> dict:
        """Run the skill librarian now (dedupe/merge/improve all skills).
        Applies changes in place (snapshotted) and posts a FINDING Pulse with Undo.
        ``background`` as for ``optimize-workspace``."""
        body = body or {}
        job, started = _start_optimize(body.get("agent"), "skills")
        if body.get("background"):
            return _job_response(job, started)
        return await _await_job(job)

    @app.post("/api/cognition/restore-backup")
    async def restore_cognition_backup(body: dict) -> dict:
        """Revert an optimization run by restoring its snapshot backup."""
        from olano.cognition.backups import restore
        from olano.config.workspace import get_workspace

        backup_job = (body or {}).get("backup_job") or ""
        agent = (body or {}).get("agent")
        if not backup_job:
            raise HTTPException(status_code=400, detail="backup_job is required")
        _agent_name, cfg, _state_dir = _resolve_agent_for_pulse(agent)
        ws = get_workspace(cfg.workspace)
        ok, message = restore(ws.root, backup_job)
        if not ok:
            raise HTTPException(status_code=400, detail=message)
        from olano.cognition.audit import log_action

        log_action(ws.root, "undo_optimization", summary=message, detail={"backup_job": backup_job})
        return {"ok": True, "message": message}

    @app.get("/api/cognition/actions")
    async def cognition_actions(agent: Optional[str] = None, limit: int = 100) -> dict:
        """Return the recent cognition/optimization audit log (newest first)."""
        from olano.cognition.audit import read_actions
        from olano.config.workspace import get_workspace

        agent_name, cfg, _state_dir = _resolve_agent_for_pulse(agent)
        ws = get_workspace(cfg.workspace)
        return {"agent": agent_name, "actions": read_actions(ws.root, limit=limit)}

    @app.post("/api/cognition/generate")
    async def cognition_generate(body: dict, request: Request) -> Any:
        """Magical Generate — run every cognition phase, streaming live progress.

        SSE frames (one JSON per ``data:`` line) mirror generate.generate_all:
            {"event": "phase_start", "id", "label", "running", "index", "total"}
            {"event": "phase_done",  "id", "label", "count", "summary", "status",
                                     "backup_job"}
            {"event": "all_done",    "total_artifacts"}
            {"event": "error",       "message"}
        """
        from starlette.responses import StreamingResponse as SR

        from olano.cognition.audit import log_action
        from olano.cognition.generate import generate_all
        from olano.config.workspace import get_workspace

        agent = (body or {}).get("agent")
        phases = (body or {}).get("phases")  # optional subset
        agent_name, cfg, _state_dir = _resolve_agent_for_pulse(agent)
        ws = get_workspace(cfg.workspace)

        # A Generate is minutes of work across every cognition phase, and it was
        # the response body — so closing the tab or letting a phone sleep threw
        # the whole run away mid-phase. It is a detached run now, exactly like a
        # chat turn (`olano/core/runs.py`), and the response is a view of it.
        async def _stream(run: AgentRun) -> None:
            # Also a Cortex job, so Think now is listed in the Runs panel with
            # every phase and worker step; each phase records into it (see
            # cognition/jobs.track) instead of appearing as its own row.
            async with _JOBS.track(
                agent=agent_name,
                workspace_root=ws.root,
                kind="generate",
                title="Think now",
                source="manual",
            ) as scope:
                try:
                    await _stream_phases(run, scope)
                except asyncio.CancelledError:
                    # The person pressed Stop (POST /api/cognition/... via the
                    # AgentRun): the job reads "stopped", not "interrupted".
                    scope.job.stop_requested = True
                    raise

        async def _stream_phases(run: AgentRun, scope: Any) -> None:
            done_counts: dict[str, int] = {}
            try:
                async for ev in generate_all(cfg, ws.root, phases=phases):
                    if ev.get("event") == "phase_done":
                        done_counts[ev.get("id", "")] = ev.get("count", 0)
                    run.emit(ev)
            except Exception as exc:  # pragma: no cover - defensive
                _api.logger.exception("cognition_generate failed for %s", agent_name)
                raw = str(exc)
                frame: dict[str, Any] = {"event": "error", "message": raw}
                # A locked/missing-credential failure (e.g. the cognition model
                # is deepseek but DEEPSEEK_API_KEY never reached os.environ
                # because the vault is locked) reads as a cryptic pydantic dump.
                # Turn it into an actionable "unlock vault & retry" signal.
                if _api._looks_like_secret_error(raw):
                    secret = _api._extract_secret_name(raw)
                    frame["code"] = "vault_locked"
                    frame["secret"] = secret
                    frame["message"] = (
                        f"Cognition needs the secret {secret}, which lives in your "
                        "locked vault. Unlock the vault and try again."
                        if secret
                        else "Cognition needs a credential from your locked vault. "
                        "Unlock the vault and try again."
                    )
                run.emit(frame)
                scope.complete(outcome="error", error=str(frame.get("message") or raw))
            else:
                total = sum(int(n or 0) for n in done_counts.values())
                scope.complete(
                    outcome="completed",
                    summary=f"Ran {len(done_counts)} phases, {total} new items.",
                    result={"counts": done_counts},
                )
                try:
                    log_action(
                        ws.root,
                        "generate_all",
                        summary="Ran all cognition phases",
                        detail={"counts": done_counts},
                    )
                except Exception:
                    pass

        run = _RUNS.begin("cortex", agent_name, "generate", "")
        if run is None:
            live = _RUNS.for_thread("cortex", agent_name, "generate")
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "run_in_progress",
                    "message": "A Generate is already running for this agent.",
                    "run_id": getattr(live, "run_id", ""),
                },
            )

        async def _drive() -> None:
            try:
                await _stream(run)
            except asyncio.CancelledError:
                run.emit({"event": "error", "message": "Stopped."})
                raise
            finally:
                _RUNS.finish(run)

        run.task = asyncio.get_running_loop().create_task(
            _drive(), name=f"cortex-run:{agent_name}:{run.run_id}"
        )
        return SR(_sse_run_view(run, 0, request), media_type="text/event-stream")

    @app.get("/api/cognition/generate/attach")
    async def cognition_generate_attach(request: Request, agent: str, cursor: int = 0) -> Any:
        """Rejoin a Generate already running for this agent.

        Same recovery as ``GET /api/chat/attach``: a Generate is minutes long,
        so the tab that started it is the one most likely to be closed, slept or
        navigated away before it ends.
        """
        from starlette.responses import StreamingResponse as SR

        agent_name, _cfg, _state_dir = _resolve_agent_for_pulse(agent)
        run = _RUNS.latest_for_thread("cortex", agent_name, "generate")
        if run is None:
            raise HTTPException(status_code=404, detail="no such run")
        return SR(_sse_run_view(run, max(0, int(cursor)), request), media_type="text/event-stream")

    # ── Cognition: autonomy control + status ──────────────────────────────────

    def _scheduler_next_runs(prefix: str) -> dict[str, str]:
        """Map cortex job_id → ISO next_run_time for jobs whose id starts with
        ``prefix`` (best-effort; empty when the scheduler isn't introspectable)."""
        out: dict[str, str] = {}
        try:
            sched = getattr(runtime, "scheduler", None)
            apsched = getattr(sched, "_scheduler", None)
            if apsched is None:
                return out
            for job in apsched.get_jobs():
                if job.id and job.id.startswith(prefix):
                    nrt = getattr(job, "next_run_time", None)
                    if nrt is not None:
                        out[job.id] = nrt.isoformat()
        except Exception:  # pragma: no cover - defensive
            pass
        return out

    @app.get("/api/cognition/status")
    async def cognition_status(agent: Optional[str] = None) -> dict:
        """Full Cortex autonomy snapshot: master switch, intensity, per-feature
        enablement, per-phase cadence/next-run/last-run, and the daily deep-run
        budget. Powers the dashboard Cortex header + per-feature cards."""
        from olano.cognition.audit import read_actions
        from olano.cognition.orchestrator import (
            PHASES,
            deep_cap,
            running_phases,
        )
        from olano.cognition.router import deep_runs_today
        from olano.config.system_settings import (
            FEATURES,
            INTENSITIES,
            auto_approve_enabled,
            autonomy_enabled,
            cortex_enabled,
            effective_intensity,
            effective_worker_budget,
            feature_enabled,
            load_system_settings,
            worker_budget_bounds,
        )
        from olano.config.workspace import get_workspace
        from olano.scheduler.cron import phase_cron_for

        agent_name, cfg, _state_dir = _resolve_agent_for_pulse(agent)

        def _snapshot() -> dict:
            # config.yaml read + audit-log scan + budget files — disk-bound.
            ws = get_workspace(cfg.workspace)
            settings = load_system_settings()
            intensity = effective_intensity(cfg, settings=settings)
            auto = autonomy_enabled(cfg, settings=settings)
            auto_approve = auto_approve_enabled(cfg, settings=settings)

            # Most-recent action per phase (for "last run" display).
            actions = read_actions(ws.root, limit=200)
            last_by_phase: dict[str, dict] = {}
            for a in actions:  # newest first
                pid = (a.get("detail") or {}).get("phase")
                if pid and pid not in last_by_phase:
                    last_by_phase[pid] = a

            next_runs = _scheduler_next_runs(f"{agent_name}_cortex_")

            phases_out = []
            for p in PHASES:
                last = last_by_phase.get(p.id)
                phases_out.append(
                    {
                        "id": p.id,
                        "label": p.label,
                        "feature": p.feature,
                        "policy": p.policy,
                        "in_think_cycle": p.in_think_cycle,
                        "enabled": feature_enabled(p.feature, cfg, settings=settings),
                        # The cron the job is registered with (explicit
                        # override, else the preset staggered per agent).
                        "cadence": phase_cron_for(cfg, p, intensity),
                        "next_run": next_runs.get(f"{agent_name}_cortex_{p.id}"),
                        "last_run": (last or {}).get("ts"),
                        "last_status": ((last or {}).get("detail") or {}).get("status"),
                        "last_summary": (last or {}).get("summary"),
                        "last_code": ((last or {}).get("detail") or {}).get("code", ""),
                        "last_remediation": ((last or {}).get("detail") or {}).get("remediation"),
                    }
                )

            cap = deep_cap(intensity)
            used = deep_runs_today(ws.root)
            # Three views of the same switches, because the panel shows all three
            # at once and resolving them client-side is how the two tiers drifted
            # apart in the first place:
            #
            #   defaults  — the home-wide value            ("All agents" column)
            #   overrides — this agent's raw tri-state     ("<agent>" column;
            #               None ⇒ inherit, which is what the Inherit chip means)
            #   resolved  — what this agent actually runs with, straight from the
            #               gate functions, so the UI never re-implements the rule
            cog = getattr(cfg, "cognition", None)
            budget_steps, budget_timeout = effective_worker_budget(cfg, settings=settings)
            overrides = {
                "enabled": getattr(cog, "enabled", None),
                "autonomous": getattr(cog, "autonomous", None),
                "auto_approve": getattr(cog, "auto_approve", None),
                "intensity": getattr(cog, "intensity", None),
                "worker_max_iterations": getattr(cog, "worker_max_iterations", None),
                "worker_timeout_seconds": getattr(cog, "worker_timeout_seconds", None),
                "features": {f: getattr(cog, f, None) for f in FEATURES},
            }
            defaults = {
                "enabled": settings.enabled,
                "autonomous": settings.autonomous,
                "auto_approve": getattr(settings, "auto_approve", True),
                "intensity": settings.intensity,
                "worker_max_iterations": settings.worker_max_iterations,
                "worker_timeout_seconds": settings.worker_timeout_seconds,
                "features": {f: getattr(settings.features, f, True) for f in FEATURES},
            }
            features_resolved = {f: feature_enabled(f, cfg, settings=settings) for f in FEATURES}
            resolved = {
                "enabled": cortex_enabled(cfg, settings=settings),
                "autonomous": auto,
                "auto_approve": auto_approve,
                "intensity": intensity,
                "worker_max_iterations": budget_steps,
                "worker_timeout_seconds": budget_timeout,
                "features": features_resolved,
            }
            return {
                "agent": agent_name,
                "autonomy": auto,
                "auto_approve": auto_approve,
                # The global master switch. NOT a reason to disable the per-agent
                # column: escaping a fleet-wide off is precisely what an override
                # is for (system_settings._resolve).
                "master_enabled": settings.enabled,
                "intensity": intensity,
                "intensities": list(INTENSITIES),
                "features": features_resolved,
                "overrides": overrides,
                "defaults": defaults,
                "resolved": resolved,
                # The legal range for the two budget fields. Served so the UI
                # clamps against the backend's numbers instead of keeping its
                # own copy to drift out of sync with delegate_guards.
                "worker_budget_bounds": worker_budget_bounds(),
                "deep_runs_today": used,
                "deep_run_cap": cap,
                "deep_runs_remaining": max(0, cap - used),
                "running": running_phases(agent_name),
                "phases": phases_out,
            }

        return await asyncio.to_thread(_snapshot)

    @app.post("/api/cognition/autonomy")
    async def set_cognition_autonomy(body: dict) -> dict:
        """Set the home-wide autonomy switch, master switch and/or intensity.

        Body: ``{"enabled": bool?, "master": bool?,
        "intensity": "relaxed|balanced|aggressive"?,
        "worker_max_iterations": int?, "worker_timeout_seconds": int?}``.

        ``enabled`` is the *autonomous* switch (turning it on also clears the
        master kill-switch, else it would be a no-op). ``master`` is the Cortex
        kill-switch on its own — needed because "off for every agent, on for the
        one I'm piloting" requires a way to say the first half, and agents with
        a ``cognition.enabled`` override keep running through it.

        The two ``worker_*`` keys are the fleet-wide half of the background-run
        budget — the value an agent inherits when its own
        ``cognition.worker_*`` is unset. Out-of-range values are rejected with
        the legal range rather than silently clamped: this is a person typing
        into a form, and a number that quietly becomes a different number is
        how a knob loses its user's trust.

        Runs off the event loop: each writer is a read-modify-write of
        config.yaml ending in an fsync, and on network block storage that is
        long enough to stall every other request on the loop.
        """
        from olano.config.system_settings import (
            INTENSITIES,
            load_system_settings,
            set_autonomy,
            set_cortex_master,
            set_intensity,
            set_worker_budget,
            worker_budget_bounds,
        )

        body = body or {}
        if "intensity" in body:
            value = str(body["intensity"])
            if value not in INTENSITIES:
                raise HTTPException(
                    status_code=400, detail=f"intensity must be one of {INTENSITIES}"
                )
            await asyncio.to_thread(set_intensity, value)

        bounds = worker_budget_bounds()
        budget: dict[str, int] = {}
        for key, lo_key, hi_key in (
            ("worker_max_iterations", "max_iterations_min", "max_iterations_max"),
            ("worker_timeout_seconds", "timeout_seconds_min", "timeout_seconds_max"),
        ):
            if key not in body:
                continue
            try:
                number = int(body[key])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"{key} must be a whole number")
            lo, hi = bounds[lo_key], bounds[hi_key]
            if not lo <= number <= hi:
                raise HTTPException(status_code=400, detail=f"{key} must be between {lo} and {hi}")
            budget[key] = number
        if budget:
            await asyncio.to_thread(
                set_worker_budget,
                budget.get("worker_max_iterations"),
                budget.get("worker_timeout_seconds"),
            )

        if "master" in body:
            await asyncio.to_thread(set_cortex_master, bool(body["master"]))
        if "enabled" in body:
            await asyncio.to_thread(
                lambda: set_autonomy(
                    bool(body["enabled"]), master=True if body["enabled"] else None
                )
            )
        s = await asyncio.to_thread(load_system_settings)
        return {
            "ok": True,
            "autonomy": s.enabled and s.autonomous,
            "master_enabled": s.enabled,
            "intensity": s.intensity,
            "worker_max_iterations": s.worker_max_iterations,
            "worker_timeout_seconds": s.worker_timeout_seconds,
        }

    @app.post("/api/cognition/auto-approve")
    async def set_cognition_auto_approve(body: dict) -> dict:
        """Flip the global "Fully Autonomous" auto-approve switch.

        Body: ``{"enabled": bool, "agent": str?}``. When enabling, the (optional)
        agent's existing pending approvals are swept and approved right away so the
        queue clears immediately rather than only on the next cognition run."""
        from olano.config.system_settings import load_system_settings, set_auto_approve

        body = body or {}
        enabled = bool(body.get("enabled", True))
        await asyncio.to_thread(set_auto_approve, enabled)
        swept = 0
        if enabled:
            try:
                from olano.cognition.orchestrator import auto_approve_cognition_items
                from olano.config.workspace import get_workspace

                _agent_name, cfg, _state_dir = _resolve_agent_for_pulse(body.get("agent"))
                ws = get_workspace(cfg.workspace)
                swept = auto_approve_cognition_items(cfg, ws.root)
            except Exception:
                _api.logger.debug("auto-approve sweep failed", exc_info=True)
        s = load_system_settings()
        return {"ok": True, "auto_approve": s.enabled and s.auto_approve, "swept": swept}

    @app.post("/api/cognition/auto-approve/sweep")
    async def sweep_cognition_auto_approve(body: dict) -> dict:
        """Approve one agent's pending cognition items right now, without changing
        any autonomy setting.

        Used by the per-agent Autonomy scope: turning on a per-agent
        ``cognition.auto_approve`` override (written via ``PUT /api/agents/{name}``)
        should clear that agent's queue immediately, mirroring the inline sweep the
        global ``/auto-approve`` toggle already performs."""
        from olano.cognition.orchestrator import auto_approve_cognition_items
        from olano.config.workspace import get_workspace

        body = body or {}
        swept = 0
        try:
            _agent_name, cfg, _state_dir = _resolve_agent_for_pulse(body.get("agent"))
            ws = get_workspace(cfg.workspace)
            swept = auto_approve_cognition_items(cfg, ws.root)
        except Exception:
            _api.logger.debug("auto-approve sweep failed", exc_info=True)
        return {"ok": True, "swept": swept}

    @app.post("/api/cognition/features/{feature}")
    async def set_cognition_feature_toggle(feature: str, body: dict) -> dict:
        """Enable/disable one Cortex feature group globally.

        Body: ``{"enabled": bool}``. Feature is one of system_settings.FEATURES
        (legacy aliases accepted). Off the loop — see the autonomy endpoint."""
        from olano.config.system_settings import (
            FEATURES,
            canonical_feature,
            load_system_settings,
            set_cognition_feature,
        )

        canonical = canonical_feature(feature)
        if canonical not in FEATURES:
            raise HTTPException(status_code=400, detail=f"unknown feature: {feature}")
        enabled = bool((body or {}).get("enabled", True))
        await asyncio.to_thread(set_cognition_feature, canonical, enabled)
        s = await asyncio.to_thread(load_system_settings)
        return {"ok": True, "feature": canonical, "enabled": getattr(s.features, canonical)}

    @app.post("/api/cognition/run-phase")
    async def run_cognition_phase(body: dict) -> dict:
        """Manually run a single Cortex phase now (override the schedule).

        Body: ``{"agent": str?, "phase": str, "background": bool?}``. Respects
        budget but bypasses the daily deep-run cap (it's an explicit human
        action). Runs as a Cortex job: a second request for the same phase
        while it runs joins the first, and ``background: true`` answers at once
        with the job instead of waiting for the result."""
        from olano.cognition.orchestrator import PHASE_BY_ID, run_phase
        from olano.config.workspace import get_workspace

        body = body or {}
        phase_id = body.get("phase") or ""
        if phase_id not in PHASE_BY_ID:
            raise HTTPException(status_code=400, detail=f"unknown phase: {phase_id}")
        agent_name, cfg, _state_dir = _resolve_agent_for_pulse(body.get("agent"))
        ws = get_workspace(cfg.workspace)

        async def _run(scope: Any) -> None:
            result = await run_phase(phase_id, cfg, ws.root, source="manual", force=True)
            status = str(result.get("status") or "")
            summary = str(result.get("summary") or "")
            # A phase that was disabled or skipped did not run; the Runs list
            # says why instead of a bare "Failed" (run_phase puts the reason
            # in `summary` for those, and the classified error for "error").
            failed = status_for_outcome(status) == "error"
            scope.complete(
                outcome=status,
                summary=summary,
                error=(summary or str(result.get("error") or "")) if failed else "",
                result={"ok": True, "agent": agent_name, **result},
            )

        job, started = _JOBS.start(
            agent=agent_name,
            workspace_root=ws.root,
            kind="phase",
            target=phase_id,
            title=PHASE_BY_ID[phase_id].label,
            run=_run,
        )
        if body.get("background"):
            return _job_response(job, started)
        return await _await_job(job)

    # ── Cortex items (ideas / brainstorms / prototypes / projects) ─────────────

    def _plan_progress(plan_text: str) -> dict:
        """Compute progress from a PLAN.md's markdown checkboxes."""
        done = len(re.findall(r"(?mi)^\s*[-*]\s*\[x\]", plan_text))
        todo = len(re.findall(r"(?mi)^\s*[-*]\s*\[ \]", plan_text))
        total = done + todo
        return {"done": done, "total": total, "pct": round(100 * done / total) if total else 0}

    def _cortex_visible_agent_names() -> list[str]:
        """Every agent whose Cortex items the caller may see, roster order.

        The index lives in each agent's workspace, so a paused or not-yet-booted
        agent still has items worth reviewing — hence the installed roster, not
        the booted runners (``/api/pulse`` walks runners because a Pulse queue
        only exists while its engine does). Booted agents missing from the
        roster (a test harness, an agent installed since the roster was read)
        are appended so nothing on screen can be lost to the list's source.
        """
        from olano.config.loader import list_agents

        try:
            names = list(list_agents())
        except Exception:
            names = []
        for name in runtime._runners:
            if name not in names:
                names.append(name)
        allowed = auth_layer.allowed_agent_names()
        if allowed is not None:
            names = [n for n in names if n in allowed]
        return names

    def _stamp_job(agent_name: str, item: dict) -> None:
        """Put the item's latest run on it: the running one (so the card reads
        *Creating project...* and its button follows that run instead of
        starting another), or one that ended in the last hour (so a failure
        is visible on the card it came from)."""
        job = _JOBS.latest_for_target(agent_name, str(item.get("id") or ""))
        if job is not None and job.kind in ("promote", "explore"):
            item["job"] = _job_ref(job)

    @app.get("/api/cortex/items")
    async def cortex_items_list(
        agent: Optional[str] = None,
        kind: Optional[str] = None,
        include_discarded: bool = False,
        all_agents: bool = False,
    ) -> dict:
        """List Cortex items (ideas/brainstorms/prototypes/projects) from the index.

        ``all_agents=true`` lists every agent the caller may see in one
        response — the dashboard's fleet-wide Cortex review — with each item
        stamped ``agent`` (the index itself never records an owner: the agent
        is implied by whose workspace the index was read from) and a
        ``counts_by_agent`` map beside the summed ``counts``. ``agent`` is
        ignored in that mode. Without it the response is one agent's, as it
        always was (``agent`` omitted ⇒ the default agent).
        """
        from olano.cognition import cortex_items as ci
        from olano.config.workspace import get_workspace

        if all_agents:
            names = _cortex_visible_agent_names()

            def _list_all() -> dict:
                items: list[dict] = []
                counts: dict[str, int] = {k: 0 for k in ci.KINDS}
                by_agent: dict[str, dict[str, int]] = {}
                # An agent whose index cannot be read is reported, never
                # silently left out: a fleet view that hides one broken index
                # reads exactly like that agent having no items.
                errors: dict[str, str] = {}
                for name in names:
                    try:
                        _, cfg, _state_dir = _resolve_agent_for_pulse(name)
                    except HTTPException:
                        continue
                    try:
                        ws = get_workspace(cfg.workspace)
                        rows = ci.list_items(
                            ws.root, kind=kind, include_discarded=include_discarded
                        )
                        agent_counts = ci.counts_by_kind(ws.root)
                    except ci.CortexIndexError as exc:
                        _api.logger.warning("cortex items: %s: %s", name, exc)
                        errors[name] = str(exc)
                        continue
                    except Exception:
                        _api.logger.debug("cortex items: skipping %s", name, exc_info=True)
                        continue
                    for it in rows:
                        it["agent"] = name
                        it["preview"] = ci.preview_for(ws.root, it)
                        it["abs_path"] = _cortex_abs_path(ws.root, it)
                        _stamp_job(name, it)
                    items.extend(rows)
                    by_agent[name] = agent_counts
                    for k, n in agent_counts.items():
                        counts[k] = counts.get(k, 0) + n
                # Newest first across the fleet, the same order one agent's list has.
                items.sort(key=lambda i: i.get("created_at", ""), reverse=True)
                return {
                    "agent": "",
                    "agents": list(by_agent.keys()),
                    "items": items,
                    "counts": counts,
                    "counts_by_agent": by_agent,
                    "errors": errors,
                }

            return await asyncio.to_thread(_list_all)

        agent_name, cfg, _state_dir = _resolve_agent_for_pulse(agent)

        def _list() -> dict:
            # Index read + one detail-file read per item — disk-bound.
            ws = get_workspace(cfg.workspace)
            try:
                items = ci.list_items(ws.root, kind=kind, include_discarded=include_discarded)
            except ci.CortexIndexError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            # Attach a card-ready `preview` (summary, or first paragraph of the detail
            # file) so the dashboard always has a description to show — not just a title.
            for it in items:
                it["agent"] = agent_name
                it["preview"] = ci.preview_for(ws.root, it)
                it["abs_path"] = _cortex_abs_path(ws.root, it)
                _stamp_job(agent_name, it)
            return {"agent": agent_name, "items": items, "counts": ci.counts_by_kind(ws.root)}

        return await asyncio.to_thread(_list)

    @app.get("/api/cortex/items/{item_id}")
    async def cortex_item_detail(item_id: str, agent: Optional[str] = None) -> dict:
        """Item metadata + its detail markdown + the files in its folder (and, for
        projects, progress computed from PLAN.md checkboxes)."""
        from olano.cognition import cortex_items as ci
        from olano.config.workspace import get_workspace

        agent_name, cfg, _state_dir = _resolve_agent_for_pulse(agent)

        def _detail() -> dict:
            ws = get_workspace(cfg.workspace)
            item = ci.get_item(ws.root, item_id)
            if item is None:
                raise HTTPException(status_code=404, detail=f"Cortex item {item_id} not found")
            folder = ws.root / item.get("path", "")
            item["abs_path"] = _cortex_abs_path(ws.root, item)
            # The item page acts on this one item, so it needs what the list
            # rows get: the promotion running on it (its button then reads
            # "Becoming an idea..." instead of offering the same tap again),
            # or the run that just failed.
            _stamp_job(agent_name, item)
            # Robust detail + files: falls back through folder markdown / the promoted
            # project / the stored summary so the drawer is never blank for an item
            # whose canonical detail file was never written.
            detail, files = ci.resolve_detail(ws.root, item)
            progress = None
            if item.get("kind") == "project":
                plan = folder / "PLAN.md"
                if plan.is_file():
                    try:
                        progress = _plan_progress(
                            plan.read_text(encoding="utf-8", errors="replace")
                        )
                    except OSError:
                        progress = None
            return {
                "agent": agent_name,
                "item": item,
                "detail": detail,
                "files": files,
                "progress": progress,
            }

        return await asyncio.to_thread(_detail)

    @app.post("/api/cortex/items/{item_id}/discard")
    async def cortex_item_discard(item_id: str, body: dict) -> dict:
        from olano.cognition import cortex_items as ci
        from olano.cognition.audit import log_action
        from olano.config.workspace import get_workspace

        agent = (body or {}).get("agent")
        agent_name, cfg, _state_dir = _resolve_agent_for_pulse(agent)
        ws = get_workspace(cfg.workspace)
        item = ci.set_status(ws.root, item_id, "discarded")
        if item is None:
            raise HTTPException(status_code=404, detail=f"Cortex item {item_id} not found")
        log_action(
            ws.root,
            "cortex_discard",
            summary=f"Discarded {item.get('kind')}: {item.get('title')}",
            detail={"item_id": item_id, "kind": item.get("kind")},
        )
        return {"ok": True, "item": item}

    @app.post("/api/cortex/items/{item_id}/explore")
    async def cortex_item_explore(item_id: str, body: dict) -> dict:
        """Run a deep research pass that enriches the item's folder.

        A Cortex job, like promote: a second request joins the running one and
        ``background: true`` answers at once with the job."""
        from olano.cognition import cortex_items as ci
        from olano.cognition.audit import log_action
        from olano.cognition.ideation import run_idea_explore
        from olano.config.workspace import get_workspace

        body = body or {}
        agent_name, cfg, _state_dir = _resolve_agent_for_pulse(body.get("agent"))
        ws = get_workspace(cfg.workspace)
        item = ci.get_item(ws.root, item_id)
        if item is None:
            raise HTTPException(status_code=404, detail=f"Cortex item {item_id} not found")

        async def _run(scope: Any) -> None:
            ci.set_status(ws.root, item_id, "exploring")
            try:
                outcome = await run_idea_explore(cfg, ws.root, item)
            finally:
                # Done (or failed, or stopped): back to active, never stuck
                # reading "exploring" after the run is gone.
                updated = ci.set_status(ws.root, item_id, "active")
            log_action(
                ws.root,
                "cortex_explore",
                summary=outcome.summary or "Explored",
                detail={
                    "item_id": item_id,
                    "status": outcome.status,
                    "artifacts": outcome.artifacts,
                },
            )
            scope.complete(
                outcome=outcome.status,
                summary=outcome.summary or "",
                error=outcome.error or "",
                result={
                    "ok": True,
                    "item": updated,
                    "status": outcome.status,
                    "summary": outcome.summary,
                    "artifacts": outcome.artifacts,
                },
            )

        job, started = _JOBS.start(
            agent=agent_name,
            workspace_root=ws.root,
            kind="explore",
            target=item_id,
            title=f"Explore: {item.get('title') or item_id}",
            detail={"item_kind": item.get("kind"), "item_title": item.get("title")},
            run=_run,
        )
        if body.get("background"):
            return _job_response(job, started)
        return await _await_job(job)

    @app.post("/api/cortex/items/{item_id}/promote")
    async def cortex_item_promote(item_id: str, body: dict) -> dict:
        """Promote an item. ``body.target`` names the stage (``idea`` /
        ``prototype`` / ``project``, or ``next`` for the funnel's next stage);
        absent, it is ``project``, which is what the dashboard's **Create
        project** button has always meant.

        The promotion is a Cortex job (``cognition/jobs.py``), so it outlives
        the page that asked for it. ``background: true`` answers at once with
        ``{job, already_running}`` and the dashboard follows it on
        ``GET /api/cortex/jobs/{id}``; without it the request waits for the
        result, as it always did. Either way a second request while one is
        running joins that run instead of creating a second project, and an
        item that is already promoted is refused (409)."""
        from olano.admin.cortex import start_promotion_job
        from olano.cognition import cortex_items as ci
        from olano.config.workspace import get_workspace

        body = body or {}
        target = str(body.get("target") or "project").strip().lower()
        agent_name, cfg, _state_dir = _resolve_agent_for_pulse(body.get("agent"))
        ws = get_workspace(cfg.workspace)
        item = ci.get_item(ws.root, item_id)
        if item is None:
            raise HTTPException(status_code=404, detail=f"Cortex item {item_id} not found")
        live = _JOBS.find_live(agent_name, "promote", item_id)
        if live is None and item.get("status") == "promoted":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "already_promoted",
                    "message": f"'{item.get('title')}' has already been promoted.",
                    "promoted_to": (item.get("links") or {}).get("promoted_to"),
                },
            )

        # Shared with AgentFather's olano_cortex_promote, so a promotion records
        # the same job, audit entry and result whichever surface started it.
        job, started = start_promotion_job(agent_name, cfg, ws.root, item, target)
        if body.get("background"):
            return _job_response(job, started)
        return await _await_job(job)

    # ── Cortex jobs: list / detail / stop ──────────────────────────────────────

    def _job_ref(job: Any) -> dict:
        """The slice of a job a card needs to say what is happening to it."""
        d = job.to_dict()
        return {
            k: d.get(k)
            for k in (
                "id",
                "kind",
                "status",
                "outcome",
                "title",
                "started_at",
                "ended_at",
                "duration_seconds",
                "error",
                "last_step",
                # `detail.to` is the stage a promotion is heading for, so a
                # list can show the item under that stage while it runs.
                "detail",
            )
        }

    @app.get("/api/cortex/jobs")
    async def cortex_jobs_list(
        agent: Optional[str] = None,
        all_agents: bool = False,
        active: bool = False,
        limit: int = 50,
        kind: Optional[str] = None,
        target: Optional[str] = None,
    ) -> dict:
        """Cortex jobs, newest first: what is running now, then the history.

        ``active=true`` answers from memory only (what is running right now),
        which is what a page polls; without it each agent's persisted history
        is read too. ``all_agents=true`` covers every agent the caller may see,
        each row stamped with its ``agent``."""
        from olano.config.workspace import get_workspace

        limit = max(1, min(int(limit or 50), 500))
        if all_agents:
            names = _cortex_visible_agent_names()
        else:
            names = [_resolve_agent_for_pulse(agent)[0]]

        if active:
            rows = [j.to_dict() for j in _JOBS.live_jobs(set(names))]
            if kind:
                rows = [r for r in rows if r.get("kind") == kind]
            if target:
                rows = [r for r in rows if r.get("target") == target]
            return {"jobs": rows[:limit], "running": len(rows)}

        def _collect() -> dict:
            rows: list[dict] = []
            for name in names:
                try:
                    _, cfg, _state_dir = _resolve_agent_for_pulse(name)
                    ws = get_workspace(cfg.workspace)
                    rows.extend(_JOBS.history(ws.root, name, limit=limit, kind=kind, target=target))
                except HTTPException:
                    continue
                except Exception:
                    _api.logger.debug("cortex jobs: skipping %s", name, exc_info=True)
            rows.sort(key=lambda r: str(r.get("started_at") or ""), reverse=True)
            running = sum(1 for r in rows if r.get("status") == "running")
            return {"jobs": rows[:limit], "running": running}

        return await asyncio.to_thread(_collect)

    @app.get("/api/cortex/jobs/{job_id}")
    async def cortex_job_detail(job_id: str, agent: Optional[str] = None) -> dict:
        """One job with its full step log, live or from the history."""
        from olano.config.workspace import get_workspace

        live = _JOBS.get_memory(job_id)
        agent_name, cfg, _state_dir = _resolve_agent_for_pulse(live.agent if live else agent)
        ws = get_workspace(cfg.workspace)
        job = await asyncio.to_thread(_JOBS.get, ws.root, job_id)
        if job is None or job.get("agent", agent_name) != agent_name:
            raise HTTPException(status_code=404, detail=f"Cortex job {job_id} not found")
        return {"job": job}

    @app.post("/api/cortex/jobs/{job_id}/stop")
    async def cortex_job_stop(job_id: str, body: dict) -> dict:
        """Stop a running job. Members may stop the runs they can start from a
        card (promote, explore); everything else is the operator's."""
        live = _JOBS.get_memory(job_id)
        if live is None or not live.live:
            raise HTTPException(status_code=404, detail="That run is not running.")
        _resolve_agent_for_pulse(live.agent)
        user = auth_layer.current_user()
        if (
            live.kind not in ("promote", "explore")
            and user is not None
            and not getattr(user, "is_admin", True)
        ):
            raise HTTPException(status_code=403, detail="Only an admin can stop this run.")
        return {"ok": _JOBS.stop(job_id), "job": live.to_dict()}

    @app.post("/api/cortex/items/{item_id}/rating")
    async def cortex_item_rate(item_id: str, body: dict) -> dict:
        """Write an item's rating by hand (the triage phase writes the same shape).

        Body: ``score`` (1-10), optional ``rationale``, ``recommendation``
        (promote | execute | explore | keep | discard), ``next_step``,
        ``dimensions`` ({name: number}), ``agent``.
        """
        from olano.cognition import cortex_items as ci
        from olano.cognition.audit import log_action
        from olano.config.workspace import get_workspace

        body = body or {}
        agent = body.get("agent")
        _agent_name, cfg, _state_dir = _resolve_agent_for_pulse(agent)
        ws = get_workspace(cfg.workspace)
        try:
            score = float(body.get("score") or "")
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="score must be a number 1-10") from exc
        updated = ci.set_rating(
            ws.root,
            item_id,
            score,
            rationale=str(body.get("rationale") or ""),
            dimensions=body.get("dimensions") if isinstance(body.get("dimensions"), dict) else None,
            recommendation=str(body.get("recommendation") or "keep"),
            next_step=str(body.get("next_step") or ""),
            rated_by="human",
        )
        if updated is None:
            raise HTTPException(status_code=404, detail=f"Cortex item {item_id} not found")
        log_action(
            ws.root,
            "cortex_rate",
            summary=f"Rated {updated.get('kind')} {updated.get('title')}: {updated['rating']['score']}/10",
            detail={"item_id": item_id, "rating": updated.get("rating"), "by": "human"},
        )
        return {"ok": True, "item": updated}
