"""Client for the cloud's managed LinkedIn outreach service.

LinkedIn's official API has no invitation or follow endpoint for ordinary apps,
so visiting a profile, following it and sending connection requests run on the
member's own LinkedIn account through an account-based provider. The provider
bills per connected account, so its key lives in the control plane only: the
box calls ``POST {cloud}/api/v1/services/linkedin/{connect,status,action}``
with its deployment token, the cloud acts on the account bound to
``(deployment, agent)``, meters the action and debits olano credits. Same
posture as :mod:`olano.deployment.services_client` (web search / fetch).

Each connected account is one seat of the **LinkedIn outreach add-on**, bought
on cloud.olano.ai, not in credits: on the deployment's subscription (the
instance page, or the deploy flow) for a managed box, and on the account's own
add-on subscription (Billing → LinkedIn outreach) for a self-hosted box that
runs on an Olano API key, which reaches the same routes with that key. The
cloud checks the seats on every request and says which of the two places the
fix is in (``scope`` on its refusals).

**Bring your own Unipile.** When the agent's credentials resolve
``UNIPILE_API_KEY`` and ``UNIPILE_DSN`` (its own Unipile workspace), the three
calls below go to :mod:`olano.deployment.linkedin_direct` instead: the box talks
to that workspace directly, on any install, managed or not, with no add-on seat
and no credits. Owner-supplied keys win over the managed service, because
those two names exist for nothing else, so setting them is the choice.

Every refusal carries a code
the tool turns into an answer: ``addon_required`` (the owner switches the
add-on on in the Olano Cloud console), ``seat_limit_reached`` (every seat serves
another agent), ``not_connected`` / ``needs_reconnect`` (offer a sign-in link),
``daily_cap_reached`` (stop for today), ``provider_refused`` (LinkedIn's own
reason, relayed), ``credits_exhausted`` (buy credits). The codes are the cloud
routes' error contract.
"""

from __future__ import annotations

import logging
from typing import Any

from olano.deployment.services_client import ServiceUnavailable

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 45.0

CONNECT_PATH = "/api/v1/services/linkedin/connect"
STATUS_PATH = "/api/v1/services/linkedin/status"
ACTION_PATH = "/api/v1/services/linkedin/action"


class LinkedinServiceError(ServiceUnavailable):
    """The service answered with a refusal. ``data`` is the cloud's JSON body
    (``{error, detail?, cap?, type?, ...}``), so a tool can quote the cap or
    LinkedIn's own reason instead of a status code."""

    def __init__(self, code: str, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(code, message)
        self.data = data or {}


def linkedin_service_available() -> bool:
    """True when the LinkedIn tools have somewhere to go: the agent's own
    Unipile workspace (BYOK), or the managed service (a managed posture with a
    deployment token, or a self-hosted box holding an Olano API key).

    Deliberately NOT gated on the ``olano_linkedin_outreach`` entitlement the
    signed settings carry. That flag follows the add-on's seat count, but the
    box re-reads it only every few minutes, so gating on it would keep refusing
    for a while after the owner switched the add-on on. The cloud checks the
    seats on every request and answers ``addon_required``, which the tools turn
    into the same explanation, immediately and always current."""
    if _byok():
        return True
    try:
        from olano.deployment import resolver as dep_resolver
        from olano.deployment.sources import deployment_token

        if dep_resolver.deployment_status().posture != "open":
            return bool(deployment_token())
        # A self-hosted box reaches the same routes with its Olano API key; the
        # cloud bills the account wallet and checks the account's own seats.
        from olano.deployment.gateway_key import account_key_if_unmanaged

        return bool(account_key_if_unmanaged())
    except Exception:  # pragma: no cover - defensive
        return False


# HTTP status → code used when the body names none (an old cloud, a proxy page).
_STATUS_CODES = {
    401: "unauthorized",
    402: "credits_exhausted",
    403: "service_disabled",
    409: "not_connected",
    422: "invalid_request",
    429: "rate_limited",
    502: "provider_error",
    503: "service_unavailable",
}


def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    import httpx

    from olano.deployment.services_client import service_credential_pair
    from olano.deployment.sources import cloud_api_url

    pair = service_credential_pair()
    if not pair:
        raise LinkedinServiceError("no_token", "no deployment token or Olano API key")
    family, token = pair
    try:
        resp = httpx.post(
            f"{cloud_api_url()}{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise LinkedinServiceError("network", f"service call failed: {exc}") from exc

    try:
        data = resp.json()
    except ValueError:
        data = None
    body: dict[str, Any] = data if isinstance(data, dict) else {}

    if resp.status_code == 200:
        if not isinstance(data, dict):
            raise LinkedinServiceError("error", "managed service returned non-JSON")
        return body

    code = body.get("error") if isinstance(body.get("error"), str) else None
    code = code or _STATUS_CODES.get(resp.status_code, "error")
    if code == "credits_exhausted":
        from olano.deployment.services_client import credits_exhausted_message

        message = credits_exhausted_message(family, body)
    else:
        detail = body.get("detail")
        message = str(detail) if isinstance(detail, str) and detail else code
    raise LinkedinServiceError(code, message, body)


def _byok() -> bool:
    """Whether the agent's own Unipile workspace is configured (lazy import:
    linkedin_direct imports this module for the error type)."""
    from olano.deployment import linkedin_direct

    return linkedin_direct.byok_configured()


def linkedin_connect(agent: str) -> dict[str, Any]:
    """A hosted sign-in link for ``agent``: ``{url, intent_id, expires_at, ...}``."""
    if _byok():
        from olano.deployment import linkedin_direct

        return linkedin_direct.connect(agent)
    return _post(CONNECT_PATH, {"agent": agent})


def linkedin_status(agent: str, intent_id: str | None = None) -> dict[str, Any]:
    """``{status, account, intent, limits, prices, addon}`` for ``agent``'s account."""
    if _byok():
        from olano.deployment import linkedin_direct

        return linkedin_direct.status(agent, intent_id)
    payload: dict[str, Any] = {"agent": agent}
    if intent_id:
        payload["intent_id"] = intent_id
    return _post(STATUS_PATH, payload)


def linkedin_action(agent: str, action: str, **fields: Any) -> dict[str, Any]:
    """Run one action (``visit`` / ``follow`` / ``invite`` / ``list_invitations``
    / ``withdraw_invitation`` / ``disconnect``). ``None`` fields are dropped."""
    if _byok():
        from olano.deployment import linkedin_direct

        return linkedin_direct.action(
            agent, action, **{k: v for k, v in fields.items() if v is not None}
        )
    payload: dict[str, Any] = {"agent": agent, "action": action}
    payload.update({k: v for k, v in fields.items() if v is not None})
    return _post(ACTION_PATH, payload)
