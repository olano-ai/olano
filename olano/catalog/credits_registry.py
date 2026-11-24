"""Client for the cloud olano-credit rate (``GET {CLOUD}/api/v1/credits``).

BYOK cost is *accounted* in USD (the usage ledger, the threadcost service,
the per-call records), and this module's rate converts those USD numbers into
credits **for display**. The rate comes from the cloud rather than the
instance build: repricing a credit must never require a redeploy.
The v1 rate is ``$1 → 1,000 credits`` (1 credit = $0.001).

**Scope — BYOK rows only.** Olano-managed calls (``olano:*`` tiers) carry the
gateway's own authoritative charge in ``UsageEvent.credits`` and their USD is
0 by design; converting *that* USD through this rate would fabricate a price
(0 × rate, or worse, a default-table estimate × rate). Two different books:
managed rows render their credits verbatim, BYOK rows render converted USD.

Read path mirrors :mod:`olano.catalog.models_registry` — never a
synchronous cloud call on a render (the background
:class:`~olano.catalog.sync.CatalogSyncService` owns scheduled refresh):

    in-process cache  →  disk cache (<home>/catalog/credits.json)  →  bundled

The bundled fallback is the launch rate, so an instance that has never reached
the cloud still renders credits consistently rather than refusing to.

:func:`credits_for_usd` is the one conversion helper; it is index-free and
never touches the network, so it is safe on hot paths.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from olano.catalog import store
from olano.catalog.urls import catalog_api_url

logger = logging.getLogger(__name__)

CATALOG_NAME = "credits"
DEFAULT_TTL_SECONDS = 600  # in-process cache: how often to re-check disk
DISK_REFRESH_SECONDS = 6 * 3600  # standalone self-heal: refresh disk older than this
_ATTEMPT_BACKOFF_SECONDS = 600  # min gap between failed inline cloud attempts
_FETCH_TIMEOUT = 4.0

# The v1 rate. Kept here rather than in catalog/bundled.py (which is
# generated from the model catalog) because it is a handful of fields, not a
# table worth regenerating. Only used until the first catalog sync — the cloud
# is authoritative, so a redenomination never needs a redeploy.
BUNDLED_RATE: dict[str, Any] = {
    "revision": 0,
    "creditsPerUsd": 1000.0,
    "unitName": "credit",
    "unitNamePlural": "credits",
    "decimals": 0,
    "updatedAt": None,
}


def _normalize_payload(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Coerce a cloud/disk payload into the canonical shape, or None if the
    one field that matters (a positive rate) is missing or nonsense."""
    if not isinstance(raw, dict):
        return None
    try:
        rate = float(raw.get("creditsPerUsd"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not (rate > 0) or rate == float("inf"):
        return None

    def _text(key: str) -> str:
        value = raw.get(key)
        return value.strip() if isinstance(value, str) and value.strip() else str(BUNDLED_RATE[key])

    decimals = raw.get("decimals")
    revision = raw.get("revision")
    return {
        "revision": int(revision) if isinstance(revision, int | float) else 0,
        "creditsPerUsd": rate,
        "unitName": _text("unitName"),
        "unitNamePlural": _text("unitNamePlural"),
        "decimals": (max(0, min(6, int(decimals))) if isinstance(decimals, int | float) else 0),
        "updatedAt": raw.get("updatedAt") if isinstance(raw.get("updatedAt"), str) else None,
    }


def _fetch_from_cloud() -> dict[str, Any] | None:
    """GET the published rate, or None on any failure."""
    try:
        import httpx  # lazy: httpx import is not free and this is an optional path
    except Exception:
        return None
    url = f"{catalog_api_url()}/api/v1/credits"
    try:
        with httpx.Client(timeout=_FETCH_TIMEOUT) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.debug("credit rate fetch failed: %s", exc)
        return None
    return _normalize_payload(data if isinstance(data, dict) else {})


# Module state: cached rate + the "last inline cloud attempt" clock. See
# models_registry for why NEVER_ATTEMPTED is -inf rather than 0.0.
_cache: dict[str, Any] = {}
_lock = threading.Lock()
NEVER_ATTEMPTED = float("-inf")
_last_attempt = NEVER_ATTEMPTED


def _set_cache(rate: dict[str, Any], source: str) -> None:
    with _lock:
        _cache["data"] = rate
        _cache["at"] = time.monotonic()
        _cache["source"] = source


def invalidate_cache() -> None:
    """Drop the in-process cache so the next read hits disk (sync refreshed it)."""
    with _lock:
        _cache.clear()


def _may_attempt_cloud() -> bool:
    global _last_attempt
    now = time.monotonic()
    if now - _last_attempt < _ATTEMPT_BACKOFF_SECONDS:
        return False
    _last_attempt = now
    return True


def refresh_from_cloud() -> dict[str, Any] | None:
    """Fetch, persist to disk, and cache the rate. None on failure.

    Called by the sync service (scheduled), ``force=True`` reads, and the
    stale-disk self-heal. Safe to call from a worker thread.
    """
    payload = _fetch_from_cloud()
    if payload is None:
        return None
    store.write_payload(CATALOG_NAME, payload, payload.get("revision"))
    _set_cache(payload, "cloud")
    return payload


def get_credit_rate(force: bool = False) -> dict[str, Any]:
    """Return the credit rate: in-process cache → disk → bundled.

    Always returns a usable dict with ``creditsPerUsd``, the display vocabulary,
    ``revision`` and ``source`` (``"cloud"``/``"disk"``/``"bundled"``). Never
    raises. ``force=True`` fetches from the cloud inline (explicit refresh),
    falling back down the chain on failure.

    Unlike :mod:`~olano.catalog.models_registry`, a cache hit reports the
    **origin** it was cached from rather than the literal ``"cache"``. The only
    question anyone asks this field is "did the cloud actually confirm this
    rate, or are we showing the bundled launch value?" — and ``"cache"`` answers
    neither, which is how the Config screen came to caption a bundled rate
    "Synced from cloud.olano.ai".
    """
    now = time.monotonic()
    cached = _cache.get("data")
    if not force and cached is not None and (now - _cache.get("at", 0.0)) < DEFAULT_TTL_SECONDS:
        return {**cached, "source": _cache.get("source", "bundled")}

    if force:
        fetched = refresh_from_cloud()
        if fetched is not None:
            return {**fetched, "source": "cloud"}

    payload = _normalize_payload(store.read_payload(CATALOG_NAME) or {})
    if payload is not None:
        # Standalone self-heal (no sync service running, e.g. bare CLI): if the
        # disk copy is very stale, try one throttled refresh; stale beats none.
        age = store.payload_age_seconds(CATALOG_NAME)
        if age is not None and age > DISK_REFRESH_SECONDS and _may_attempt_cloud():
            fetched = refresh_from_cloud()
            if fetched is not None:
                return {**fetched, "source": "cloud"}
        _set_cache(payload, "disk")
        return {**payload, "source": "disk"}

    if cached is not None:
        return {**cached, "source": _cache.get("source", "bundled")}

    if _may_attempt_cloud():
        fetched = refresh_from_cloud()
        if fetched is not None:
            return {**fetched, "source": "cloud"}

    _set_cache(dict(BUNDLED_RATE), "bundled")
    return {**BUNDLED_RATE, "source": "bundled"}


def catalog_status() -> dict[str, Any]:
    """Sync/source status for dashboards: revision, source, rate, fetched_at."""
    rate = get_credit_rate()
    entry = store.state_entry(CATALOG_NAME)
    return {
        "revision": rate.get("revision", 0),
        "source": rate.get("source"),
        "credits_per_usd": rate.get("creditsPerUsd"),
        "fetched_at": entry.get("fetched_at"),
    }


def credits_per_usd() -> float:
    """How many olano credits one USD is worth (never zero, never raises)."""
    try:
        value = float(get_credit_rate().get("creditsPerUsd") or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    return value if value > 0 else float(BUNDLED_RATE["creditsPerUsd"])


def credits_for_usd(usd: float | int | None) -> float:
    """Convert a USD amount to olano credits at the published rate."""
    if usd is None:
        return 0.0
    try:
        return float(usd) * credits_per_usd()
    except (TypeError, ValueError):
        return 0.0
