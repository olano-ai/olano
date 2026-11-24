"""Self-improvement triggers and self-learning status.

Split out of ``olano.runtime.api.create_api``; see ``olano.runtime.routes``.
"""

from typing import Any, Optional

from fastapi import FastAPI, HTTPException

from olano.runtime import api as _api
from olano.runtime import auth as auth_layer


def register_self_learning_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach the self learning routes to ``app``."""
    # Closures defined by an earlier route module (see routes/__init__.py).
    _resolve_agent_for_pulse = closures["_resolve_agent_for_pulse"]

    # ── Self-improvement ──────────────────────────────────────────────────────

    @app.post("/api/self-improve")
    async def trigger_self_improve(body: dict) -> dict:
        """Run a one-shot self-improvement cycle for an agent. Mirrors the
        `/self-improve` chat command so the dashboard can launch it on demand."""
        from olano.config.workspace import get_workspace
        from olano.memory.markdown_memory import MarkdownMemory
        from olano.memory.self_learning import SelfLearningService

        agent = (body or {}).get("agent")
        agent_name, cfg, _state_dir = _resolve_agent_for_pulse(agent)
        ws = get_workspace(cfg.workspace)
        md_mem = MarkdownMemory(ws.root)
        svc = SelfLearningService(
            agent_config=cfg,
            workspace_root=ws.root,
            markdown_memory=md_mem,
        )
        try:
            result = await svc.self_improve()
        except Exception as exc:
            _api.logger.exception("Self-improve failed for %s", agent_name)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"ok": True, "agent": agent_name, "result": result}

    @app.get("/api/self-learning/status")
    async def self_learning_status(agent: Optional[str] = None) -> dict:
        """Status snapshot of the background self-learning service. Used as the
        signal source for the dashboard's "infrastructure / errors" panel —
        any non-empty `failures` is surfaced as an issue row."""
        services = list(getattr(runtime, "_self_learning", []) or [])
        if agent:
            auth_layer.assert_agent_access(agent)
            services = [s for s in services if getattr(s.config, "name", None) == agent]
        allowed = auth_layer.allowed_agent_names()
        if allowed is not None:
            services = [s for s in services if getattr(s.config, "name", None) in allowed]
        return {
            "services": [s.status() for s in services],
            "count": len(services),
        }

    @app.post("/api/self-learning/reset-failures")
    async def self_learning_reset_failures(body: dict) -> dict:
        """Clear the recorded failure counters for an agent's self-learning
        service. Backs the Self-Checks "Dismiss" action so a transient/stale
        failure row can be acknowledged and removed from the dashboard. With a
        ``stage`` given, only that stage is cleared (so dismissing one finding
        leaves sibling-stage findings intact)."""
        agent = (body or {}).get("agent")
        stage = (body or {}).get("stage") or None
        services = list(getattr(runtime, "_self_learning", []) or [])
        if agent:
            auth_layer.assert_agent_access(agent)
            services = [s for s in services if getattr(s.config, "name", None) == agent]
        # Members can never sweep other owners' diagnostics via the no-agent form.
        allowed = auth_layer.allowed_agent_names()
        if allowed is not None:
            services = [s for s in services if getattr(s.config, "name", None) in allowed]
        cleared = 0
        for svc in services:
            if hasattr(svc, "reset_failures"):
                cleared += svc.reset_failures(stage)
        return {"ok": True, "agent": agent, "stage": stage, "cleared": cleared}
