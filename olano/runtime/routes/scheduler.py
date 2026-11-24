"""Scheduled tasks.

Split out of ``olano.runtime.api.create_api``; see ``olano.runtime.routes``.
"""

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException

from olano.admin.scheduler import CONFIG_JOB_PREFIX as _CONFIG_JOB_PREFIX
from olano.admin.scheduler import config_schedule_tasks as _config_schedule_tasks
from olano.admin.scheduler import validate_task_effort, validate_task_model
from olano.runtime import api as _api
from olano.runtime import auth as auth_layer


def _task_model_or_422(model: str | None) -> str:
    """The validated task model, or a 422 the form can show next to the field."""
    try:
        return validate_task_model(model)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _task_effort_or_422(effort: str | None, *, model: str, agent_name: str | None) -> str:
    """The validated task effort (checked against the model the task runs on:
    its own, else the agent's), or a 422 naming the ladder that model accepts."""
    try:
        return validate_task_effort(effort, model=model, agent_name=agent_name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def register_scheduler_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach the scheduler routes to ``app``."""
    # ── Scheduler ─────────────────────────────────────────────────────────────

    @app.get("/api/scheduler/tasks")
    async def list_tasks() -> list:
        # Everything here is blocking disk work — SchedulerDB() itself runs the
        # CREATE/ALTER migration in _init_db, list_tasks() reads sqlite, and
        # _config_schedule_tasks() JSON5-parses every agent.json5 on disk — so
        # the whole collect step runs off the event loop in one thread hop.
        # Otherwise one slow/large config (or a swap-thrashing host) stalls
        # every other request, including /api/status.
        def _collect() -> list:
            db = _api.SchedulerDB()
            # DB-backed tasks (created via the scheduler tool / this API) …
            db_tasks = db.list_tasks()
            # The live next fire time, read off the scheduler's own job. The
            # `tasks.next_run` column is never written, so without this every
            # saved task showed a dash whether or not it was registered;
            # `live` says whether a job exists for it at all.
            from olano.scheduler.cron import get_active_scheduler

            live_sched = get_active_scheduler()
            for t in db_tasks:
                t["source"] = "db"
                if live_sched is not None and t.get("enabled", True):
                    try:
                        t["next_run"] = live_sched.next_run_for_task(str(t["id"]))
                    except Exception:
                        t["next_run"] = None
                    t["live"] = t["next_run"] is not None
            # … merged with agent.json5 `schedule` jobs, which used to be
            # invisible here. Both carry agent_name, so the multi-user
            # visibility filter scopes them uniformly (admins/single-user see
            # all; members see their agents').
            return db_tasks + _config_schedule_tasks()

        merged = await asyncio.to_thread(_collect)
        return auth_layer.filter_tasks_for_current_user(merged)

    def _assert_task_agent_access(task_id: str) -> None:
        """Gate a by-id scheduler operation on the owning agent's grant.

        Resolves the agent from a ``cfg:<agent>:<index>`` config-job id or the
        DB task row, then requires chat access. Members get a 404 (not 403)
        for non-granted/unknown tasks so task existence isn't disclosed —
        config-job ids are guessable (they embed the agent name).
        """
        allowed = auth_layer.allowed_agent_names()
        if allowed is None:
            return
        agent: str | None
        if task_id.startswith(_CONFIG_JOB_PREFIX):
            try:
                agent, _idx = _api._parse_config_job_id(task_id)
            except HTTPException:
                raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        else:
            task = _api.SchedulerDB().get_task(task_id)
            agent = (task or {}).get("agent_name")
        if not agent or agent not in allowed:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    @app.post("/api/scheduler/tasks")
    async def create_task(body: _api.TaskCreateRequest) -> dict:
        # The task will run autonomously AS this agent — creating one requires
        # the same grant as chatting with it (members must name a granted
        # agent; the no-agent form is reserved for admins/single-user).
        if body.agent_name.strip():
            auth_layer.assert_agent_access(body.agent_name.strip())
        elif auth_layer.allowed_agent_names() is not None:
            raise HTTPException(status_code=403, detail="agent_name is required")
        visibility = body.visibility if body.visibility in ("private", "shared") else "private"
        # The agent selected in the dashboard form is both executor and owner.
        created_by = body.agent_name.strip() or None
        # Refused before anything is written: a task on a model this box cannot
        # run would fail every fire with the same error, and an effort the model
        # rejects would be dropped on every run.
        task_model = await asyncio.to_thread(_task_model_or_422, body.model)
        task_effort = await asyncio.to_thread(
            lambda: _task_effort_or_422(
                body.reasoning_effort, model=task_model, agent_name=created_by
            )
        )

        # SchedulerDB() runs sqlite DDL/IO. sync_db_task is thread-safe from a
        # worker: it holds the scheduler's RLock and APScheduler's add/remove_job
        # marshal their wakeup onto the loop via call_soon_threadsafe.
        def _create() -> str:
            task_id = _api.SchedulerDB().create_task(
                name=body.name,
                prompt=body.prompt,
                schedule=body.schedule.strip() or None,
                run_at=body.run_at.strip() or None,
                timezone=body.timezone or "UTC",
                agent_name=body.agent_name.strip() or None,
                visibility=visibility,
                created_by=created_by,
                model=task_model or None,
                reasoning_effort=task_effort or None,
            )
            try:
                from olano.scheduler.cron import get_active_scheduler

                scheduler = get_active_scheduler()
                if scheduler is not None:
                    scheduler.sync_db_task(task_id)
            except Exception:
                pass
            return task_id

        task_id = await asyncio.to_thread(_create)
        _api._push_event("scheduler_update", {"action": "created", "id": task_id})
        return {"result": f"Task created! ID: {task_id}", "id": task_id}

    @app.put("/api/scheduler/tasks/{task_id}")
    async def update_task(task_id: str, body: _api.TaskUpdateRequest) -> dict:
        # The access check reads sqlite (task → owning agent); contextvars —
        # and so the request's auth context — propagate into to_thread.
        await asyncio.to_thread(_assert_task_agent_access, task_id)
        # Validated once, up front, for both stores: None keeps, "" clears.
        task_model = (
            await asyncio.to_thread(_task_model_or_422, body.model)
            if body.model is not None
            else None
        )
        # The effort is validated against the pair as it will be after the edit
        # (a new model against the stored effort, a new effort against the
        # stored model), which needs the current row: done per store below.
        overrides_touched = body.model is not None or body.reasoning_effort is not None
        # Config-schedule job → edit the agent.json5 entry in place, then
        # live-reregister. Only the fields that make sense for a config job apply
        # (name, message, cron, timezone, enabled, model, reasoning_effort);
        # visibility/run_at are ignored.
        if task_id.startswith(_CONFIG_JOB_PREFIX):
            agent, index = _api._parse_config_job_id(task_id)

            # config parse + deployment-locked write + APScheduler resync (RLock
            # + call_soon_threadsafe wakeup) — one thread hop for all of it.
            def _apply_config_update() -> None:
                try:
                    data = _api._read_agent_config(agent)
                except FileNotFoundError:
                    raise HTTPException(status_code=404, detail=f"Agent '{agent}' not found")
                entries = data.get("schedule")
                if not isinstance(entries, list) or index >= len(entries):
                    raise HTTPException(status_code=404, detail=f"Config job {task_id} not found")
                entry = entries[index]
                if not isinstance(entry, dict):
                    raise HTTPException(
                        status_code=422, detail=f"Malformed schedule entry {task_id}"
                    )
                if body.name is not None:
                    entry["name"] = body.name
                if body.prompt is not None:
                    entry["message"] = body.prompt
                if body.schedule is not None and body.schedule.strip():
                    # Canonicalize onto `cron` and drop the split minute/hour/… fields
                    # so there's a single source of truth (and no every-minute trap if
                    # someone later clears `cron`). An empty value is ignored rather
                    # than blanking the schedule into an all-"*" every-minute job.
                    entry["cron"] = body.schedule.strip()
                    for stale in ("minute", "hour", "day", "month", "day_of_week"):
                        entry.pop(stale, None)
                if body.timezone is not None:
                    entry["timezone"] = body.timezone
                if body.enabled is not None:
                    entry["enabled"] = bool(body.enabled)
                if overrides_touched:
                    next_model = (
                        task_model
                        if task_model is not None
                        else str(entry.get("model") or "").strip()
                    )
                    next_effort = _task_effort_or_422(
                        entry.get("reasoning_effort")
                        if body.reasoning_effort is None
                        else body.reasoning_effort,
                        model=next_model,
                        agent_name=agent,
                    )
                    # "" = back to the agent's own value: drop the key rather
                    # than writing an empty string into agent.json5.
                    for key, value in (("model", next_model), ("reasoning_effort", next_effort)):
                        if value:
                            entry[key] = value
                        else:
                            entry.pop(key, None)
                data["schedule"] = entries
                _api._write_and_resync_schedule(agent, data)

            await asyncio.to_thread(_apply_config_update)
            _api._push_event("scheduler_update", {"action": "updated", "id": task_id})
            return {"ok": True}

        updates: dict = {}
        if body.name is not None:
            updates["name"] = body.name
        if body.prompt is not None:
            updates["prompt"] = body.prompt
        if body.schedule is not None:
            updates["schedule"] = body.schedule
        if body.run_at is not None:
            updates["run_at"] = body.run_at
        if body.enabled is not None:
            updates["enabled"] = 1 if body.enabled else 0
        if body.timezone is not None:
            updates["timezone"] = body.timezone
        if body.visibility in ("private", "shared"):
            updates["visibility"] = body.visibility

        # Same thread-safety grounds as _create in create_task above.
        def _apply_db_update() -> bool:
            if overrides_touched:
                current = _api.SchedulerDB().get_task(task_id) or {}
                next_model = (
                    task_model
                    if task_model is not None
                    else str(current.get("model") or "").strip()
                )
                next_effort = _task_effort_or_422(
                    current.get("reasoning_effort")
                    if body.reasoning_effort is None
                    else body.reasoning_effort,
                    model=next_model,
                    agent_name=current.get("agent_name") or None,
                )
                if task_model is not None:
                    updates["model"] = next_model or None
                if body.reasoning_effort is not None:
                    updates["reasoning_effort"] = next_effort or None
            ok = _api.SchedulerDB().update_task(task_id, **updates)
            if not ok:
                return False
            try:
                from olano.scheduler.cron import get_active_scheduler

                scheduler = get_active_scheduler()
                if scheduler is not None:
                    scheduler.sync_db_task(task_id)
            except Exception:
                pass
            return True

        if not await asyncio.to_thread(_apply_db_update):
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        # Broadcast so live views (and the sidebar active-jobs badge) reflect a
        # pause / resume / edit immediately — the active count can change here.
        _api._push_event("scheduler_update", {"action": "updated", "id": task_id})
        return {"ok": True}

    @app.delete("/api/scheduler/tasks/{task_id}")
    async def delete_task(task_id: str) -> dict:
        await asyncio.to_thread(_assert_task_agent_access, task_id)
        # Config-schedule job → remove the entry from agent.json5 and reregister.
        if task_id.startswith(_CONFIG_JOB_PREFIX):
            agent, index = _api._parse_config_job_id(task_id)

            # Same thread hop as update_task's _apply_config_update.
            def _apply_config_delete() -> None:
                try:
                    data = _api._read_agent_config(agent)
                except FileNotFoundError:
                    raise HTTPException(status_code=404, detail=f"Agent '{agent}' not found")
                entries = data.get("schedule")
                if not isinstance(entries, list) or index >= len(entries):
                    raise HTTPException(status_code=404, detail=f"Config job {task_id} not found")
                entries.pop(index)
                data["schedule"] = entries
                _api._write_and_resync_schedule(agent, data)

            await asyncio.to_thread(_apply_config_delete)
            _api._push_event("scheduler_update", {"action": "deleted", "id": task_id})
            return {"ok": True}

        ok = await asyncio.to_thread(lambda: _api.SchedulerDB().delete_task(task_id))
        if not ok:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        _api._push_event("scheduler_update", {"action": "deleted", "id": task_id})
        return {"ok": True}

    @app.get("/api/scheduler/history/{task_id}")
    async def task_history(task_id: str, limit: int = 10) -> list:
        await asyncio.to_thread(_assert_task_agent_access, task_id)
        return await asyncio.to_thread(lambda: _api.SchedulerDB().get_history(task_id, limit=limit))

    @app.post("/api/scheduler/tasks/{task_id}/run")
    async def run_task_now(task_id: str) -> dict:
        """Fire a task immediately, ignoring its schedule. Works for both DB
        tasks and config-schedule jobs (cfg: ids); needs the runtime scheduler
        live (and, for config jobs, the owning agent booted)."""
        from olano.scheduler.cron import get_active_scheduler

        await asyncio.to_thread(_assert_task_agent_access, task_id)
        scheduler = get_active_scheduler()
        if scheduler is None:
            raise HTTPException(status_code=409, detail="Scheduler is not active in this runtime.")

        # Both run-now paths are worker-thread-safe: descriptors are read under
        # the scheduler's RLock and _dispatch_now hands the coroutine back to
        # the runtime loop via run_coroutine_threadsafe when called off-loop.
        def _run() -> tuple[bool, str]:
            if task_id.startswith(_CONFIG_JOB_PREFIX):
                return scheduler.run_config_job_now(task_id)
            if _api.SchedulerDB().get_task(task_id) is None:
                raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
            return scheduler.run_db_task_now(task_id)

        ok, message = await asyncio.to_thread(_run)
        if not ok:
            raise HTTPException(status_code=409, detail=message)
        _api._push_event("scheduler_update", {"action": "ran", "id": task_id})
        return {"ok": True, "message": message}
