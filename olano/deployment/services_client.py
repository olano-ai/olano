"""Client for the cloud's managed service APIs (web search / web fetch).

Managed deployments get web search and page extraction as **Olano-metered
services**: the instance calls ``POST {cloud}/api/v1/services/search|fetch``
with its deployment token, the control plane holds the provider (Parallel)
key, meters the call into ``gateway_calls``, and debits the deployment's
olano-credit balance. The tools keep their local fallbacks (Tavily key,
plain httpx fetch) — an unmanaged box, a disabled service, or an exhausted
balance degrades to those instead of erroring the turn.

Never used implicitly on unmanaged installs: :func:`services_available` gates
on the managed posture + a deployment token, the same signals the cost-display
lock and the speech default use.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 45.0


class ServiceUnavailable(RuntimeError):
    """The managed service cannot serve this call (disabled, out of credits,
    provider down, not configured). Carries ``code`` so tools can tell 'fall
    back quietly' from 'tell the user to buy credits'."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def services_available() -> bool:
    """True when this box may use the managed service APIs (web search / fetch).

    Managed posture + a deployment token AND the ``olano_web_search`` entitlement
    (missing ⇒ enabled), or a self-hosted box holding a pasted Olano account key.
    A withheld service reads as unavailable so the search tool falls through to
    BYOK Tavily instead of leading with a rung that 402s.
    """
    try:
        from olano.deployment import enforcement
        from olano.deployment import resolver as dep_resolver
        from olano.deployment.sources import deployment_token

        if dep_resolver.deployment_status().posture != "open":
            return bool(deployment_token()) and enforcement.service_entitled("web_search")
        # A self-hosted box reaches the same routes with its pasted account
        # key (the cloud dispatches on the bearer's prefix). The entitlement
        # check is kept for shape; every feature reads enabled on open posture.
        from olano.deployment.gateway_key import account_key_if_unmanaged

        return bool(account_key_if_unmanaged()) and enforcement.service_entitled("web_search")
    except Exception:  # pragma: no cover - defensive
        return False


def service_credential_pair() -> tuple[str, str] | None:
    """``(family, bearer)`` for the managed-service routes: ``("deployment",
    token)`` on a managed box, ``("account", key)`` on a self-hosted one holding
    an Olano API key, else ``None``. The cloud tells the two apart by prefix
    (``olano_dep_`` / ``sk-olano-``); the box keeps the family beside the token
    so a refusal is explained in the caller's own terms without re-deriving it
    from the token's spelling."""
    from olano.deployment.sources import deployment_token

    token = deployment_token()
    if token:
        return ("deployment", token)
    try:
        from olano.deployment import resolver as dep_resolver

        if dep_resolver.deployment_status().posture != "open":
            return None
        from olano.deployment.gateway_key import account_key_if_unmanaged

        key = account_key_if_unmanaged()
        return ("account", key) if key else None
    except Exception:  # pragma: no cover - defensive
        return None


def service_credential() -> str | None:
    """The bearer alone; see :func:`service_credential_pair`."""
    pair = service_credential_pair()
    return pair[1] if pair else None


def credits_exhausted_message(family: str, body: dict[str, Any] | None = None) -> str:
    """The out-of-credits sentence for a 402, in the wallet owner's terms. The
    cloud's body names the wallet it refused (``scope``) when it knows; the
    key family is the fallback. The deployment sentence is the pre-account-key
    text with its dash written as a colon (the repository's output style);
    nothing else about a managed box's message changed."""
    scope = (body or {}).get("scope") if isinstance(body, dict) else None
    owner = scope if scope in ("deployment", "account") else family
    if owner == "deployment":
        return (
            "This deployment is out of olano credits: buy more on your "
            "deployment card at cloud.olano.ai."
        )
    return (
        "Your Olano account is out of credits; buy more on cloud.olano.ai "
        "(Billing → Account credits)."
    )


def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    import httpx

    from olano.deployment.sources import cloud_api_url

    pair = service_credential_pair()
    if not pair:
        raise ServiceUnavailable("no_token", "no deployment token or Olano API key")
    family, token = pair
    try:
        resp = httpx.post(
            f"{cloud_api_url()}{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise ServiceUnavailable("network", f"service call failed: {exc}") from exc

    if resp.status_code == 402:
        try:
            body = resp.json()
        except ValueError:
            body = None
        raise ServiceUnavailable("credits_exhausted", credits_exhausted_message(family, body))
    if resp.status_code in (403, 503):
        raise ServiceUnavailable(
            "disabled", f"managed service unavailable (HTTP {resp.status_code})"
        )
    if resp.status_code != 200:
        raise ServiceUnavailable("error", f"managed service error (HTTP {resp.status_code})")
    try:
        data = resp.json()
    except ValueError as exc:
        raise ServiceUnavailable("error", "managed service returned non-JSON") from exc
    return data if isinstance(data, dict) else {}


def service_search(
    *,
    query: str | None = None,
    queries: list[str] | None = None,
    objective: str | None = None,
    max_results: int = 5,
    agent: str | None = None,
) -> dict[str, Any]:
    """Managed web search. Returns ``{results: [{url,title,excerpts}], credits_charged}``."""
    payload: dict[str, Any] = {"max_results": max(1, min(20, max_results))}
    if query:
        payload["query"] = query
    if queries:
        payload["queries"] = queries[:5]
    if objective:
        payload["objective"] = objective
    if agent:
        payload["agent"] = agent
    return _post("/api/v1/services/search", payload)


def service_fetch(
    url: str, *, objective: str | None = None, agent: str | None = None
) -> dict[str, Any]:
    """Managed page extraction. Returns ``{pages: [{url,title,content}], credits_charged}``."""
    payload: dict[str, Any] = {"url": url}
    if objective:
        payload["objective"] = objective
    if agent:
        payload["agent"] = agent
    return _post("/api/v1/services/fetch", payload)
