"""A multi-user agent's end users: list them, block or unblock one.

The dashboard twin of ``olano customers list|block|unblock`` over
:mod:`olano.memory.customer_prefs`. Blocking is an operator switch (never a
model tool), so these routes are a management surface: the admin, or a member
who owns the agent (``auth.assert_agent_admin``), the same gate as the raw
config editor.
"""

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from olano.runtime import auth as auth_layer


def _require_agent(name: str) -> None:
    from olano.config.loader import find_agent_config_path

    if find_agent_config_path(name) is None:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")


def register_customers_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach the end-user (customer) routes to ``app``."""

    @app.get("/api/agents/{name}/customers")
    async def list_agent_customers(name: str) -> dict:
        """Every end user the agent has a private folder for, with their flags."""
        from olano.memory.customer_prefs import list_customers

        auth_layer.assert_agent_admin(name)
        _require_agent(name)
        rows = await asyncio.to_thread(list_customers, name)
        return {"agent": name, "customers": rows}

    @app.put("/api/agents/{name}/customers/{key}/blocked")
    async def set_agent_customer_blocked(name: str, key: str, request: Request) -> dict:
        """Block (``{"blocked": true}``) or unblock one end user by folder key."""
        from olano.memory.customer_prefs import list_customers, set_blocked

        auth_layer.assert_agent_admin(name)
        _require_agent(name)
        try:
            body = await request.json()
        except Exception:
            body = None
        blocked = body.get("blocked") if isinstance(body, dict) else None
        if not isinstance(blocked, bool):
            raise HTTPException(status_code=400, detail='Body must be {"blocked": true|false}')
        if not await asyncio.to_thread(set_blocked, name, key, blocked):
            raise HTTPException(status_code=404, detail=f"No end user '{key}' for agent '{name}'")
        rows = await asyncio.to_thread(list_customers, name)
        row = next((r for r in rows if r["key"] == key), None)
        return {"ok": True, "customer": row}
