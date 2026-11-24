"""Client for the cloud LLM provider/model catalog (``GET {CLOUD}/api/v1/models``).

Providers, their models, per-model capabilities (vision, image generation,
TTS, STT, reasoning), context windows, and list pricing used to be hardcoded
across the app (``PROVIDERS`` default_models, ``_MODEL_COSTS``, two
context-window ladders). They now live in the cloud catalog so new models and
price changes ship without a new build.

Read path — never a synchronous cloud call on a browse (the background
:class:`~olano.catalog.sync.CatalogSyncService` owns scheduled refresh):

    in-process cache  →  disk cache (<home>/catalog/models.json)  →  bundled

The only inline cloud fetches are ``force=True`` (explicit refresh) and a
throttled self-heal when there is no usable disk cache or it is very stale
(standalone CLI use, where no sync service runs).

Lookup helpers (:func:`model_entry`, :func:`model_pricing`,
:func:`model_context_window`) are index-backed and never touch the network —
safe for hot paths like per-call cost accounting.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from typing import Any

from olano.catalog import store
from olano.catalog.off_peak import active_off_peak
from olano.catalog.urls import catalog_api_url

logger = logging.getLogger(__name__)

CATALOG_NAME = "models"
DEFAULT_TTL_SECONDS = 600  # in-process cache: how often to re-check disk
DISK_REFRESH_SECONDS = 6 * 3600  # standalone self-heal: refresh disk older than this
_ATTEMPT_BACKOFF_SECONDS = 600  # min gap between failed inline cloud attempts
_FETCH_TIMEOUT = 4.0
_PAGE_LIMIT = 2000  # server max; catalog is a few hundred rows

_PROVIDER_DEFAULTS: dict[str, Any] = {
    "key": "",
    "name": "",
    "package": None,
    "envVar": None,
    "docsUrl": None,
    "consoleUrl": None,
    "notes": None,
}

_MODEL_DEFAULTS: dict[str, Any] = {
    "id": "",
    "provider": "",
    "model": "",
    "displayName": None,
    "description": None,
    "family": None,
    "modality": "chat",
    "vision": False,
    "audioInput": False,
    "imageGen": False,
    "tts": False,
    "stt": False,
    "reasoning": False,
    "reasoningEfforts": [],
    "reasoningDefault": None,
    "contextWindow": None,
    "maxOutputTokens": None,
    "priceInput": None,
    "priceOutput": None,
    "priceCacheRead": None,
    "priceCacheWrite": None,
    "pricePerImage": None,
    "pricePerMinute": None,
    "tier": None,
    "recommended": False,
    "defaultForProvider": False,
    "deprecated": False,
    "eolDate": None,
    "metadata": {},
}


def _normalize_provider(raw: dict[str, Any]) -> dict[str, Any]:
    entry = dict(_PROVIDER_DEFAULTS)
    entry.update({k: v for k, v in raw.items() if k in _PROVIDER_DEFAULTS})
    return entry


def _normalize_model(raw: dict[str, Any]) -> dict[str, Any]:
    entry = dict(_MODEL_DEFAULTS)
    entry.update({k: v for k, v in raw.items() if k in _MODEL_DEFAULTS})
    if not entry["id"] and entry["provider"] and entry["model"]:
        entry["id"] = f"{entry['provider']}:{entry['model']}"
    return entry


def _normalize_payload(raw: dict[str, Any]) -> dict[str, Any] | None:
    providers = raw.get("providers")
    models = raw.get("models")
    if not isinstance(providers, list) or not isinstance(models, list) or not models:
        return None
    revision = raw.get("revision")
    return {
        "revision": int(revision) if isinstance(revision, int | float) else 0,
        "providers": [_normalize_provider(p) for p in providers if isinstance(p, dict)],
        "models": [_normalize_model(m) for m in models if isinstance(m, dict)],
    }


def _bundled_catalog() -> dict[str, Any]:
    from olano.catalog.bundled import BUNDLED_MODELS, BUNDLED_PROVIDERS

    return {
        "revision": 0,
        "providers": [_normalize_provider(p) for p in BUNDLED_PROVIDERS],
        "models": [_normalize_model(m) for m in BUNDLED_MODELS],
    }


def _ttl_seconds() -> int:
    raw = os.environ.get("LIONCLAW_MODEL_CATALOG_TTL")
    if not raw:
        return DEFAULT_TTL_SECONDS
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_TTL_SECONDS


def _fetch_from_cloud() -> dict[str, Any] | None:
    """GET the full catalog (paginating if needed), or None on any failure."""
    try:
        import httpx  # lazy: httpx import is not free and this is an optional path
    except Exception:
        return None
    url = f"{catalog_api_url()}/api/v1/models"
    providers: list[dict[str, Any]] = []
    models: list[dict[str, Any]] = []
    revision = 0
    try:
        with httpx.Client(timeout=_FETCH_TIMEOUT) as client:
            offset = 0
            while True:
                resp = client.get(url, params={"limit": _PAGE_LIMIT, "offset": offset})
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, dict):
                    return None
                page = data.get("models")
                if not isinstance(page, list):
                    return None
                if offset == 0:
                    raw_providers = data.get("providers")
                    providers = raw_providers if isinstance(raw_providers, list) else []
                    raw_rev = data.get("revision")
                    revision = int(raw_rev) if isinstance(raw_rev, int | float) else 0
                models.extend(m for m in page if isinstance(m, dict))
                total = data.get("total")
                # Read `total` and page until complete — the MCP directory
                # client silently truncated at the server default; never again.
                if not isinstance(total, int) or len(models) >= total or not page:
                    break
                offset = len(models)
    except Exception as exc:
        logger.debug("model catalog fetch failed: %s", exc)
        return None
    return _normalize_payload({"revision": revision, "providers": providers, "models": models})


# Module state: cached catalog + id index. `_cache` holds {"data", "at"};
# the lock guards rebuilds (FastAPI thread pool + sync-service thread).
_cache: dict[str, Any] = {}
_index: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()

# "No cloud attempt yet." NOT 0.0: the backoff compares against
# time.monotonic(), which counts from BOOT, so a 0.0 sentinel reads as "last
# attempted at boot" and suppresses the very first inline refresh on any machine
# whose uptime is under _ATTEMPT_BACKOFF_SECONDS, freshly booted boxes and
# CI runners alike. -inf is never within the backoff window.
NEVER_ATTEMPTED = float("-inf")
_last_attempt = NEVER_ATTEMPTED


def _set_cache(catalog: dict[str, Any], source: str) -> None:
    with _lock:
        _cache["data"] = catalog
        _cache["at"] = time.monotonic()
        _cache["source"] = source
        _index.clear()
        for m in catalog.get("models", []):
            mid = str(m.get("id") or "")
            if mid:
                _index[mid.lower()] = m


def invalidate_cache() -> None:
    """Drop the in-process cache so the next read hits disk (sync refreshed it)."""
    with _lock:
        _cache.clear()
        _index.clear()


def _may_attempt_cloud() -> bool:
    global _last_attempt
    now = time.monotonic()
    if now - _last_attempt < _ATTEMPT_BACKOFF_SECONDS:
        return False
    _last_attempt = now
    return True


def refresh_from_cloud() -> dict[str, Any] | None:
    """Fetch, persist to disk, and cache the catalog. None on failure.

    Called by the sync service (scheduled), ``force=True`` reads, and the
    stale-disk self-heal. Safe to call from a worker thread.
    """
    payload = _fetch_from_cloud()
    if payload is None:
        return None
    store.write_payload(CATALOG_NAME, payload, payload.get("revision"))
    _set_cache(payload, "cloud")
    return payload


def get_model_catalog(force: bool = False) -> dict[str, Any]:
    """Return the model catalog: in-process cache → disk → bundled.

    Always returns a usable dict with ``providers``, ``models``, ``revision``
    and ``source`` (``"cloud"``/``"cache"``/``"disk"``/``"bundled"``). Never
    raises and never returns empty. ``force=True`` fetches from the cloud
    inline (explicit refresh), falling back down the chain on failure.
    """
    now = time.monotonic()
    cached = _cache.get("data")
    if not force and cached is not None and (now - _cache.get("at", 0.0)) < _ttl_seconds():
        return {**cached, "source": "cache"}

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
        return {**cached, "source": "cache"}

    if _may_attempt_cloud():
        fetched = refresh_from_cloud()
        if fetched is not None:
            return {**fetched, "source": "cloud"}

    bundled = _bundled_catalog()
    _set_cache(bundled, "bundled")
    return {**bundled, "source": "bundled"}


def catalog_status() -> dict[str, Any]:
    """Sync/source status for dashboards: revision, source, fetched_at."""
    catalog = get_model_catalog()
    entry = store.state_entry(CATALOG_NAME)
    return {
        "revision": catalog.get("revision", 0),
        "source": catalog.get("source"),
        "providers": len(catalog.get("providers", [])),
        "models": len(catalog.get("models", [])),
        "fetched_at": entry.get("fetched_at"),
    }


# ── index-backed lookups (no network, hot-path safe) ─────────────────────────


def _ensure_index() -> dict[str, dict[str, Any]]:
    if not _index:
        get_model_catalog()
    return _index


def model_entry(model_str: str | None) -> dict[str, Any] | None:
    """Catalog entry for a ``provider:model`` (or bare model) string, or None."""
    if not model_str:
        return None
    key = model_str.strip().lower()
    index = _ensure_index()
    hit = index.get(key)
    if hit is not None:
        return hit
    if ":" not in key:
        # Bare model name (cost records sometimes carry no provider prefix):
        # first catalog entry whose model segment matches.
        for entry in index.values():
            if str(entry.get("model", "")).lower() == key:
                return entry
    return None


def _positive(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value > 0


def model_pricing(
    model_str: str | None, at: datetime | None = None
) -> dict[str, float | None] | None:
    """Per-1M-token USD prices for the model, or None when the catalog has no
    price (unknown ≠ free: callers fall back to their legacy tables).

    ``at`` is when the call ran (default: now). The price columns are the
    provider's standard (peak) rate; a model whose ``metadata.off_peak`` block
    covers ``at`` (DeepSeek's half-price hours) prices input, cache reads and
    output at that block's rates instead.
    """
    entry = model_entry(model_str)
    if entry is None:
        return None
    price_input = entry.get("priceInput")
    price_output = entry.get("priceOutput")
    if price_input is None or price_output is None:
        return None
    cache_read = entry.get("priceCacheRead")
    meta = entry.get("metadata")
    off = active_off_peak(meta.get("off_peak") if isinstance(meta, dict) else None, at)
    if off is not None and _positive(off.get("price_input")) and _positive(off.get("price_output")):
        price_input, price_output = off["price_input"], off["price_output"]
        if _positive(off.get("price_cache_read")):
            cache_read = off["price_cache_read"]
    return {
        "input": float(price_input),
        "output": float(price_output),
        "cache_read": float(cache_read) if cache_read is not None else None,
        "cache_write": (
            float(entry["priceCacheWrite"]) if entry.get("priceCacheWrite") is not None else None
        ),
    }


def model_context_window(model_str: str | None) -> int | None:
    """Per-model context window from the catalog, or None if unknown."""
    entry = model_entry(model_str)
    if entry is None:
        return None
    window = entry.get("contextWindow")
    return int(window) if isinstance(window, int | float) and window > 0 else None
