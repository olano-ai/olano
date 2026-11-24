"""Client for the deployment's per-service olano-credit usage (cloud-authoritative).

``GET {cloud}/api/v1/usage/services`` returns how many olano credits each
managed service (the chat tiers, olano-vision, olano-image, olano-tts,
olano-stt, olano-web-search) has consumed over a window. The cloud's
``gateway_calls`` fact table is the single source of truth, so the box dashboard
shows the EXACT same per-service credit numbers as the cloud deployment view —
they cannot drift.

CREDITS ONLY by contract: the payload carries a service label + credits (+ a
call count), never a backend model, token count, unit, or COGS. Mirrors
:mod:`olano.deployment.credits_client` — 60s TTL, stale-on-error, ``None`` on an
unmanaged box (no account to read; the dashboard hides the card).
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
# One entry per requested window: {days_key: {"data": payload, "at": monotonic,
# "stale": bool}}. Key 0 = the cloud default window (no ?days param) — what the
# services card asks for; the Insights overview asks per its own window.
_cache: dict[int, dict[str, Any]] = {}


def invalidate_cache() -> None:
    with _lock:
        _cache.clear()


def _credential() -> tuple[str, str] | None:
    """``(bearer, path)``: the deployment token against the deployment route,
    or the pasted account key against its key-authenticated twin."""
    from olano.deployment.sources import deployment_token

    token = deployment_token()
    if token:
        return token, "/api/v1/usage/services"
    from olano.deployment.services_client import service_credential

    key = service_credential()
    return (key, "/api/v1/account/usage/services") if key else None


def _fetch(days: int | None) -> dict[str, Any] | None:
    import httpx

    from olano.deployment.sources import cloud_api_url

    cred = _credential()
    if not cred:
        return None
    token, path = cred
    kwargs: dict[str, Any] = {
        "headers": {"Authorization": f"Bearer {token}"},
        "timeout": FETCH_TIMEOUT_SECONDS,
    }
    # Only pass the param when a window was asked for — the default request
    # stays byte-identical to the pre-`days` client (the route defaults to 30).
    if days:
        kwargs["params"] = {"days": int(days)}
    try:
        resp = httpx.get(f"{cloud_api_url()}{path}", **kwargs)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.debug("service usage fetch failed: %s", exc)
        return None
    if not isinstance(data, dict) or "services" not in data:
        return None
    return data


def get_usage(force: bool = False, *, days: int | None = None) -> dict[str, Any] | None:
    """Per-service credit usage, or ``None`` (unmanaged / never fetched).

    ``days`` selects the cloud's rolling window (1–365; the route defaults to
    30 when omitted); each window caches independently under the same TTL +
    stale-on-error semantics. Returns the cloud payload plus
    ``stale``/``age_seconds``. Never raises.
    """
    try:
        if not _credential():
            return None
    except Exception:
        return None

    key = int(days or 0)
    now = time.monotonic()
    with _lock:
        entry = _cache.get(key) or {}
        cached = entry.get("data")
        age = now - entry.get("at", 0.0)
        if not force and cached is not None and age < TTL_SECONDS:
            return {**cached, "stale": bool(entry.get("stale")), "age_seconds": round(age, 1)}

    fresh = _fetch(days)
    with _lock:
        if fresh is not None:
            _cache[key] = {"data": fresh, "at": time.monotonic(), "stale": False}
            return {**fresh, "stale": False, "age_seconds": 0.0}
        stale_entry = _cache.get(key)
        if stale_entry is not None and stale_entry.get("data") is not None:
            stale_entry["stale"] = True
            return {
                **stale_entry["data"],
                "stale": True,
                "age_seconds": round(time.monotonic() - stale_entry.get("at", 0.0), 1),
            }
    return None
