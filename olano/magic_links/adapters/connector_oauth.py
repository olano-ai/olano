"""Connector-broker OAuth — GitHub, Microsoft 365, Shopify, Salesforce, ….

Wraps :mod:`olano.core.connectors` so every provider registered there becomes
reachable from a chat, not just from the dashboard's Connections tab. The broker
already builds the authorization URL without opening a browser and already
exchanges the code on ``GET /api/auth/{service}/callback`` (a public,
state-matched route); this adapter only hands the user over and reflects status.

**Token scope is decided at mint time**, not by the person holding the link:
``agent`` selects the per-agent token path and ``shared`` the sharing flag, both
frozen into the link's params by whoever was authorized to mint it. So a link
forwarded to a colleague still writes exactly the token the owner intended.

The redirect URI is the deployment's public origin — the same origin the link
itself is served from, which is by definition browser-reachable — so it must be
registered with the provider's OAuth app. ``GET /api/mcp/oauth/redirect-uri``
and the Connections panel already tell operators what to register.
"""

from __future__ import annotations

from typing import Any

from olano.magic_links.flows import (
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_IN_PROGRESS,
    BaseFlow,
    register,
)
from olano.magic_links.store import LinkRecord

_STATUS_MAP = {
    "awaiting_authorization": STATUS_IN_PROGRESS,
    "exchanging": STATUS_IN_PROGRESS,
    "authorized": STATUS_DONE,
    "error": STATUS_ERROR,
}


def _provider_or_raise(service: str) -> Any:
    from olano.core.connectors import get_provider

    provider = get_provider(service)
    if provider is None:
        from olano.core.connectors import provider_ids

        raise ValueError(
            f"Unknown service '{service}'. Available: {', '.join(provider_ids())}. "
            "(Google uses the 'google' flow; a sign-in code uses 'connector-device'.)"
        )
    return provider


class ConnectorOAuthFlow(BaseFlow):
    id = "connector-oauth"
    label = "Service sign-in"
    kind = "redirect"
    instructions = "You'll be handed to the provider to approve access, then sent back here."

    def validate(self, params: dict[str, Any]) -> dict[str, Any]:
        service = str(params.get("service") or "").strip().lower()
        if not service:
            raise ValueError("A service is required (e.g. 'microsoft365', 'shopify').")
        _provider_or_raise(service)
        out: dict[str, Any] = {"service": service, "shared": bool(params.get("shared"))}
        agent = str(params.get("agent") or "").strip()
        if agent:
            out["agent"] = agent
        # Connect-time provider params (Shopify's shop, Microsoft's tenant …).
        # The broker validates these against its own charset rules on start.
        creds = params.get("creds")
        if isinstance(creds, dict):
            out["creds"] = {str(k): str(v) for k, v in creds.items()}
        return out

    def _provider(self, link: LinkRecord) -> Any:
        return _provider_or_raise(str(link.params.get("service") or ""))

    def descriptor(self, link: LinkRecord) -> dict[str, Any]:
        provider = self._provider(link)
        base = super().descriptor(link)
        agent = link.params.get("agent")
        base["service"] = {"id": provider.id, "name": provider.name}
        base["title"] = f"Connect {provider.name}"
        if agent:
            base["instructions"] = (
                f"You'll approve access at {provider.name}. The account you pick "
                f"is stored for the agent '{agent}'."
            )
        return base

    def start(self, link: LinkRecord) -> dict[str, Any]:
        from olano.core.connectors import start_connector_auth
        from olano.magic_links.service import public_base_url

        provider = self._provider(link)
        origin = public_base_url()
        if not origin:
            raise ValueError("This deployment has no public origin for the OAuth callback.")
        session = start_connector_auth(
            provider,
            creds=link.params.get("creds") if isinstance(link.params.get("creds"), dict) else None,
            agent=str(link.params.get("agent") or "") or None,
            shared=bool(link.params.get("shared")),
            redirect_uri=f"{origin}/api/auth/{provider.id}/callback",
        )
        return {"session_id": session.session_id}

    def state(self, link: LinkRecord) -> dict[str, Any]:
        from olano.core.connectors import poll_connector_auth

        if not link.session_id:
            return {"status": link.status}
        session = poll_connector_auth(link.session_id)
        if session is None:
            return {"status": STATUS_ERROR, "error": "This sign-in attempt expired."}
        status = _STATUS_MAP.get(session.status, STATUS_IN_PROGRESS)
        out: dict[str, Any] = {
            "status": status,
            "authorization_url": session.authorization_url or "",
        }
        if status == STATUS_DONE:
            provider = self._provider(link)
            scope = f" for {link.params['agent']}" if link.params.get("agent") else ""
            out["result"] = f"{provider.name} connected{scope}."
        elif status == STATUS_ERROR:
            out["error"] = session.error or ""
        return out


register(ConnectorOAuthFlow())
