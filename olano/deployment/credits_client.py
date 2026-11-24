"""Client for the deployment's olano-credit balance (cloud-authoritative).

``GET {cloud}/api/v1/credits/balance`` is the ONE number the instance trusts
for "how many credits are left": the per-turn charges this runtime computes
are display estimates, while the cloud's ledger is the account (the gateway
enforces it as the virtual key's budget). This client caches the balance for
:data:`TTL_SECONDS` and serves the last-known value (marked ``stale``) on any
fetch failure — a cloud blip must degrade the number's freshness, never blank
the dashboard card.

Unmanaged installs return ``None`` from :func:`get_balance` — there is no
account to read; the dashboard hides the card.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

TTL_SECONDS = 60.0
FETCH_TIMEOUT_SECONDS = 6.0

_lock = threading.Lock()
_cache: dict[str, Any] = {}  # {"data": payload, "at": monotonic, "stale": bool}


def invalidate_cache() -> None:
    with _lock:
        _cache.clear()


def _credential() -> tuple[str, str] | None:
    """``(bearer, path)``: the deployment token against the deployment route,
    or the pasted account key against its key-authenticated twin. ``None`` when
    the box holds neither (an unmanaged install with no key)."""
    from olano.deployment.sources import deployment_token

    token = deployment_token()
    if token:
        return token, "/api/v1/credits/balance"
    from olano.deployment.services_client import service_credential

    key = service_credential()
    return (key, "/api/v1/account/credits/balance") if key else None


def _fetch() -> dict[str, Any] | None:
    import httpx

    from olano.deployment.sources import cloud_api_url

    cred = _credential()
    if not cred:
        return None
    token, path = cred
    try:
        resp = httpx.get(
            f"{cloud_api_url()}{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=FETCH_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.debug("credits balance fetch failed: %s", exc)
        return None
    if not isinstance(data, dict) or "balance" not in data:
        return None
    return data


def get_balance(force: bool = False) -> dict[str, Any] | None:
    """The deployment's credit balance, or ``None`` (unmanaged / never fetched).

    Returns the cloud payload plus ``fetched_at_age_seconds`` and ``stale``
    (True when the last refresh attempt failed and this is the previous
    value). Never raises.
    """
    try:
        if not _credential():
            return None
    except Exception:
        return None

    now = time.monotonic()
    with _lock:
        cached = _cache.get("data")
        age = now - _cache.get("at", 0.0)
        if not force and cached is not None and age < TTL_SECONDS:
            return {**cached, "stale": bool(_cache.get("stale")), "age_seconds": round(age, 1)}

    fresh = _fetch()
    with _lock:
        if fresh is not None:
            _cache.update({"data": fresh, "at": time.monotonic(), "stale": False})
            return {**fresh, "stale": False, "age_seconds": 0.0}
        if cached is not None:
            # Serve stale rather than nothing; keep the old timestamp so the
            # age keeps growing and the UI can caption it honestly.
            _cache["stale"] = True
            return {**cached, "stale": True, "age_seconds": round(now - _cache.get("at", 0.0), 1)}
    return None
