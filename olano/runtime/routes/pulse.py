"""PULSE items: list, approve, reject, defer, generate, undo.

Split out of ``olano.runtime.api.create_api``; see ``olano.runtime.routes``.
"""

import asyncio
from pathlib import Path
from typing import Any, Optional, cast

from fastapi import FastAPI, HTTPException

from olano.runtime import api as _api
from olano.runtime import auth as auth_layer


def register_pulse_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach the pulse routes to ``app``."""
    # ── PULSE / Approvals / Ops ──────────────────────────────────────────────

    def _resolve_agent_for_pulse(name: str | None) -> tuple[str, Any, Path]:
        """Pick an agent and return (name, AgentConfig, state_dir).

        Falls back to the default agent if `name` is omitted. Raises 404 if the
        target agent isn't installed. Access-checked: every Pulse / Cortex /
        self-improve handler funnels through here, and each one reads or
        mutates the target agent's workspace — so the caller needs that
        agent's grant (the default-agent fallback is checked too).
        """
        from olano.config.workspace import get_workspace

        target = name or _api._default_agent_name()
        auth_layer.assert_agent_access(target)
        runner = runtime._runners.get(target)
        if runner is not None:
            cfg = runner.config
        else:
            try:
                # Cached (read-only display path) — several handlers call this
                # on the event loop before offloading their blocking body.
                cfg = _api._load_agent_config_cached(target)
            except FileNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        ws = get_workspace(cfg.workspace)
        return target, cfg, ws.state_dir

    def _cortex_abs_path(root: Path, item: dict) -> str:
        """Absolute folder of a Cortex item, or "" when it has none.

        The index stores `path` relative to the workspace so it survives a
        move; the dashboard's chat hand-off drafts the item's full path into
        the composer so the agent opens the artifacts instead of hunting for
        them. Empty stays empty — never resolve to the workspace ROOT.
        """
        rel = str(item.get("path") or "").strip()
        return str(root / rel) if rel else ""

    def _pulse_engine_for(name: str | None):
        from olano.pulse.engine import PulseEngine

        agent_name, cfg, state_dir = _resolve_agent_for_pulse(name)
        return agent_name, PulseEngine(cfg, state_dir), cfg

    def _serialize_pulse_item(item: Any) -> dict:
        try:
            return cast(dict, item.to_dict())
        except Exception:
            return {
                "id": getattr(item, "id", ""),
                "title": getattr(item, "title", ""),
                "content": getattr(item, "content", ""),
            }

    @app.get("/api/pulse")
    async def list_pulse(
        agent: Optional[str] = None, lane: str = "pulse", status: str = "pending"
    ) -> dict:
        from olano.config.workspace import get_workspace
        from olano.pulse.models import PulseLane, PulseStatus

        try:
            lane_value = PulseLane.from_value(lane)
        except Exception:
            raise HTTPException(status_code=400, detail=f"Unknown lane '{lane}'")

        # status="pending" → the live actionable queue (default, back-compat).
        # Any other status (approved/rejected/…) → resolved history for review.
        status_norm = (status or "pending").strip().lower()
        resolved_status: PulseStatus | None = None
        if status_norm not in ("pending", "", "active"):
            try:
                resolved_status = PulseStatus(status_norm)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Unknown status '{status}'")

        # Aggregate when no agent specified — surfaces every booted agent's
        # queue so the dashboard can show a single "Pulse" inbox. Members'
        # inboxes only aggregate their granted agents. The explicit ?agent=
        # form is checked HERE (not just inside _pulse_engine_for) because the
        # loop below swallows per-agent HTTPExceptions by design.
        if agent:
            auth_layer.assert_agent_access(agent)
        agent_names: list[str] = [agent] if agent else list(runtime._runners.keys())
        if not agent:
            allowed = auth_layer.allowed_agent_names()
            if allowed is not None:
                agent_names = [n for n in agent_names if n in allowed]

        def _collect() -> list[dict]:
            # Per-agent engine construction + state-file reads are disk-bound.
            results: list[dict] = []
            for name in agent_names:
                try:
                    _, engine, cfg = _pulse_engine_for(name)
                except HTTPException:
                    continue
                if resolved_status is None:
                    items = engine.list_pending(lane=lane_value)
                else:
                    items = engine.list_by_status(resolved_status, lane=lane_value)
                # A card's `references` are workspace-relative; the chat
                # hand-off needs the agent's root to draft them as full paths.
                workspace_root = str(get_workspace(cfg.workspace).root)
                for item in items:
                    payload = _serialize_pulse_item(item)
                    payload["agent"] = name
                    payload["workspace_root"] = workspace_root
                    results.append(payload)
            return results

        results = await asyncio.to_thread(_collect)

        # Highest priority first, then most recent.
        def _sort_key(p: dict) -> tuple:
            score = {"high": 3, "normal": 2, "low": 1}.get(str(p.get("priority", "normal")), 2)
            return (-score, p.get("created_at") or "")

        results.sort(key=_sort_key)
        return {"items": results, "lane": lane_value.value, "count": len(results)}

    @app.post("/api/pulse/approve")
    async def approve_pulse(body: dict) -> dict:
        item_id = (body or {}).get("item_id") or ""
        agent = (body or {}).get("agent")
        if not item_id:
            raise HTTPException(status_code=400, detail="item_id is required")
        agent_name, engine, _cfg = _pulse_engine_for(agent)

        # Capture the item's shape *before* approving so the dashboard can
        # describe exactly what happened (file updated / skill kept / task
        # started) — the action runs during approve() and may mutate metadata.
        item = engine.get_item(item_id)
        action_type = str((item.action or {}).get("type") or "noop") if item else "noop"
        references = list(item.references or []) if item else []
        kind = str((item.metadata or {}).get("kind") or "") if item else ""
        title = (item.title or "") if item else ""

        ok, message = engine.approve(item_id)
        if not ok:
            raise HTTPException(status_code=404, detail=message)

        # After approval a run_worker action records its background job id.
        job_id = ""
        if item is not None:
            job_id = str((item.metadata or {}).get("worker_job_id") or "")
        if job_id:
            # Start it now, not on the drainer's next tick: it shows in the
            # Cortex Runs panel the moment it starts.
            wake = getattr(runtime, "wake_cognition_drainer", None)
            if callable(wake):
                wake()

        result = {
            "ok": True,
            "message": message,
            "action_type": action_type,
            "references": references,
            "kind": kind,
            "title": title,
            "job_id": job_id,
            "agent": agent_name,
        }
        _api._push_event(
            "pulse_resolved",
            {"item_id": item_id, "action": "approved", **result},
        )
        return result

    @app.post("/api/pulse/reject")
    async def reject_pulse(body: dict) -> dict:
        item_id = (body or {}).get("item_id") or ""
        agent = (body or {}).get("agent")
        if not item_id:
            raise HTTPException(status_code=400, detail="item_id is required")
        _, engine, _cfg = _pulse_engine_for(agent)
        if not engine.reject(item_id):
            raise HTTPException(status_code=404, detail=f"Item '{item_id}' not found")
        return {"ok": True}

    @app.post("/api/pulse/defer")
    async def defer_pulse(body: dict) -> dict:
        item_id = (body or {}).get("item_id") or ""
        agent = (body or {}).get("agent")
        try:
            hours = float((body or {}).get("hours") or 24.0)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="hours must be a number")
        if not item_id:
            raise HTTPException(status_code=400, detail="item_id is required")
        _, engine, _cfg = _pulse_engine_for(agent)
        if not engine.defer(item_id, hours=hours):
            raise HTTPException(status_code=404, detail=f"Item '{item_id}' not found")
        return {"ok": True, "deferred_hours": hours}

    @app.post("/api/pulse/generate")
    async def generate_pulse(body: dict) -> dict:
        from olano.pulse.generators import PulseGenerator

        agent = (body or {}).get("agent")
        agent_name, cfg, state_dir = _resolve_agent_for_pulse(agent)
        from olano.config.workspace import get_workspace

        ws = get_workspace(cfg.workspace)
        # DEEP, web-research-grounded curation (live web_search/web_fetch), with an
        # automatic FAST fallback inside curate_pulse when over budget / on failure.
        result = await PulseGenerator(cfg, ws.root).capture_pulse_agent(force=True)
        out: dict[str, Any] = {
            "ok": result.status != "error",
            "agent": agent_name,
            "status": result.status,
            "generated": len(result.items),
            "items": [_serialize_pulse_item(i) for i in result.items],
        }
        # When nothing surfaced, tell the user WHY (skipped/empty/budget/error) and,
        # for a real failure, how to fix it — classified the same way as Cortex.
        if not result.items:
            out["reason"] = result.reason
            if result.status == "error":
                from olano.cognition.orchestrator import classify_cognition_error_full

                code, message, remediation = classify_cognition_error_full(
                    result.error or result.reason, cfg
                )
                out["code"] = code
                out["message"] = message
                out["remediation"] = remediation
        return out

    @app.post("/api/pulse/undo")
    async def undo_pulse(body: dict) -> dict:
        """Undo an autonomous artifact by firing the item's restore_backup action."""
        item_id = (body or {}).get("item_id") or ""
        agent = (body or {}).get("agent")
        if not item_id:
            raise HTTPException(status_code=400, detail="item_id is required")
        _, engine, _cfg = _pulse_engine_for(agent)
        ok, message = engine.undo(item_id)
        if not ok:
            raise HTTPException(status_code=400, detail=message)
        return {"ok": True, "message": message}

    # Closures that later route modules bind (see routes/__init__.py).
    closures["_cortex_abs_path"] = _cortex_abs_path
    closures["_resolve_agent_for_pulse"] = _resolve_agent_for_pulse
