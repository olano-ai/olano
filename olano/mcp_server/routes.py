"""Admin + consent API for external access (inbound MCP / A2A / OAuth).

Everything under ``/api/mcp-server`` and ``/api/oauth/consent`` is admin-only
(listed in ``runtime/auth._ADMIN_ONLY_PREFIXES`` and re-checked with
``assert_admin`` in every handler) because each of these calls decides who
outside the deployment may drive an agent. The raw bearer token appears exactly
once, in the response that minted it.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class MintTokenRequest(BaseModel):
    label: str = Field(
        description=(
            "Which integration this token is for, e.g. `Claude desktop — Ana` or "
            "`Zapier orders flow`. Shown in listings and in the Agent to Agent view as the "
            "caller's name, and how a leaked token is traced and revoked alone."
        ),
    )
    agents: list[str] = Field(
        description=(
            'Agent names this token may address, e.g. `["merli", "support"]`, or `["*"]` '
            "for every agent that is (now or later) exposed. Only agents with External access "
            "switched on are reachable regardless of what the token lists."
        ),
    )
    ttl_days: int | None = Field(
        default=None,
        ge=0,
        le=3650,
        description=(
            "Lifetime in days. Empty uses the home-wide default (Config → External access → "
            "token lifetime, 90 unless changed); `0` means no expiry."
        ),
    )


class ConsentDecision(BaseModel):
    approve: bool = Field(description="True to grant, false to decline.")
    agents: list[str] = Field(
        default_factory=list,
        description=(
            "Agents the client may reach, chosen by the admin: names from the offered list, "
            'or `["*"]` for every agent exposed now or later. Ignored when declining.'
        ),
    )


def register_mcp_server_admin_routes(app: Any, server: Any, a2a_server: Any = None) -> None:
    from olano.runtime import auth as auth_layer

    def _exposed_all() -> set[str]:
        names = set(server.exposed_names())
        if a2a_server is not None:
            names.update(a2a_server.exposed_names())
        return names

    @app.get("/api/mcp-server/status")
    async def mcp_server_status() -> dict[str, Any]:
        """Whether the inbound surfaces are on, where they answer, which agents are
        exposed on each and how many tokens / OAuth clients exist."""
        auth_layer.assert_admin()
        status = dict(server.status())
        status["a2a"] = a2a_server.status() if a2a_server is not None else None
        return status

    @app.get("/api/mcp-server/tokens")
    async def mcp_server_tokens() -> list[dict[str, Any]]:
        """Every minted token (hash never included), newest first — operator-minted
        and OAuth-issued alike."""
        auth_layer.assert_admin()
        return [t.public() for t in server.token_store.list()]

    @app.post("/api/mcp-server/tokens")
    async def mcp_server_mint_token(body: MintTokenRequest) -> dict[str, Any]:
        """Mint a bearer token. The `token` field in the response is shown once."""
        user = auth_layer.assert_admin()
        settings = server.settings()
        ttl = settings.token_ttl_days if body.ttl_days is None else body.ttl_days
        try:
            raw, record = server.token_store.mint(
                label=body.label,
                agents=body.agents,
                ttl_days=ttl,
                created_by=getattr(user, "email", "") or getattr(user, "id", "") or "admin",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        unexposed = [a for a in record.agents if a != "*" and a not in _exposed_all()]
        status = server.status()
        return {
            "token": raw,
            "record": record.public(),
            "endpoint": status.get("endpoint"),
            "a2a_endpoint_template": (
                a2a_server.status().get("endpoint_template") if a2a_server else None
            ),
            "warning": (
                "These agents are not exposed yet (turn on External access → MCP or A2A on "
                "the agent and restart it): " + ", ".join(unexposed)
                if unexposed
                else None
            ),
        }

    @app.delete("/api/mcp-server/tokens/{token_id}")
    async def mcp_server_revoke_token(token_id: str) -> dict[str, Any]:
        """Revoke a token. Takes effect on the next request that presents it."""
        auth_layer.assert_admin()
        if not server.token_store.revoke(token_id):
            raise HTTPException(status_code=404, detail="token not found or already revoked")
        return {"status": "revoked", "id": token_id}

    # ── OAuth clients (registered via DCR) ─────────────────────────────────
    @app.get("/api/mcp-server/oauth/clients")
    async def mcp_server_oauth_clients() -> list[dict[str, Any]]:
        """Clients that registered with Olano's authorization server (secrets omitted),
        with the number of live tokens each holds."""
        auth_layer.assert_admin()
        tokens = server.token_store.list()
        refresh = server.token_store.list_refresh()
        rows: list[dict[str, Any]] = list(server.oauth.client_store.list())
        for row in rows:
            cid = row.get("client_id")
            row["active_tokens"] = sum(1 for t in tokens if t.client_id == cid and t.active)
            row["active_refresh_tokens"] = sum(
                1 for r in refresh if r.client_id == cid and r.active
            )
        return rows

    @app.delete("/api/mcp-server/oauth/clients/{client_id}")
    async def mcp_server_delete_oauth_client(client_id: str) -> dict[str, Any]:
        """Forget a registered client and revoke every token it holds."""
        auth_layer.assert_admin()
        revoked = server.token_store.revoke_client(client_id)
        if not server.oauth.client_store.delete(client_id) and not revoked:
            raise HTTPException(status_code=404, detail="client not found")
        return {"status": "deleted", "client_id": client_id, "revoked_tokens": revoked}

    # ── Consent (the dashboard page an OAuth sign-in lands on) ─────────────
    @app.get("/api/oauth/consent/{request_id}")
    async def oauth_consent_pending(request_id: str) -> dict[str, Any]:
        """Details of a pending OAuth authorization: who is asking, which agents
        they requested, which agents may be granted."""
        auth_layer.assert_admin()
        pending = server.oauth.pending(request_id)
        if pending is None:
            raise HTTPException(status_code=404, detail="consent_request_unknown_or_expired")
        return dict(pending)

    @app.post("/api/oauth/consent/{request_id}")
    async def oauth_consent_decide(request_id: str, body: ConsentDecision) -> dict[str, Any]:
        """Approve (with the granted agents) or decline; returns the URL the browser
        must be sent to so the client receives its code or the refusal."""
        user = auth_layer.assert_admin()
        subject = getattr(user, "email", "") or getattr(user, "id", "") or "admin"
        try:
            if body.approve:
                redirect = server.oauth.approve(request_id, agents=body.agents, subject=subject)
            else:
                redirect = server.oauth.deny(request_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="consent_request_unknown_or_expired"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"redirect_to": redirect, "approved": body.approve}
