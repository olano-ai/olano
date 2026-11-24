"""Official-connector clients synced from the cloud (``GET {CLOUD}/api/v1/connectors``).

The catalog carries the **public half only** of each official Olano OAuth app:
provider id → client id (+ display name). Provider *definitions* — authorize
URLs, scopes, response hooks — stay in the wheel
(``olano/core/connectors/providers/``), and the client **secret** never leaves
Olano infrastructure (the central relay performs the exchange and refresh; see
``olano/core/connect_relay.py``). So this file is the answer to exactly one
question: *does provider X have an official client this box may use, and under
which client id?*

Read path mirrors :mod:`olano.catalog.credits_registry` — in-process cache →
disk (``<home>/catalog/connectors.json``) → bundled — with one deliberate
difference: **no inline cloud attempt, ever.** :func:`official_client` is
called from ``/api/auth/status`` (a dashboard poll running on the event loop)
and from every connect click, so a 4s network stall here would freeze the
runtime; the background :class:`~olano.catalog.sync.CatalogSyncService` owns
refresh. The bundled fallback is **empty** — a box that has never reached the
cloud simply has no official clients, which renders as today's BYO-only UI.

Poisoned-catalog defense: client ids pass the same conservative charset the
cloud route enforces before serving. A client id is public by nature, but it
is interpolated into authorize URLs box-side — a value that can't pass the
charset can't rewrite one.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

from olano.catalog import store
from olano.catalog.urls import catalog_api_url

logger = logging.getLogger(__name__)

CATALOG_NAME = "connectors"
DEFAULT_TTL_SECONDS = 600  # in-process cache: how often to re-check disk
_FETCH_TIMEOUT = 6.0

# Mirror of the cloud route's guard (apps/web/lib/connector-registry.ts
# CLIENT_ID_RE) — RFC 6749 appendix A charset, bounded.
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~:+/=-]{0,254}$")

BUNDLED: dict[str, Any] = {"revision": 0, "connectors": {}}


def _normalize_payload(raw: Any) -> dict[str, Any] | None:
    """Coerce a cloud/disk payload to the canonical shape; ``None`` if unusable.

    Entries that are disabled, empty, or fail the client-id charset are
    dropped (never kept as "maybe") — an entry that survives is offerable.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("connectors"), dict):
        return None
    connectors: dict[str, dict[str, Any]] = {}
    for provider_id, entry in raw["connectors"].items():
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue
        client_id = str(entry.get("client_id") or "")
        if not _CLIENT_ID_RE.match(client_id):
            if client_id:
                logger.warning(
                    "connectors catalog: dropping %s (client id fails charset)", provider_id
                )
            continue
        connectors[str(provider_id)] = {
            "client_id": client_id,
            "name": str(entry.get("name") or provider_id),
        }
    revision = raw.get("revision")
    return {
        "revision": int(revision) if isinstance(revision, int | float) else 0,
        "connectors": connectors,
    }


def _fetch_from_cloud() -> dict[str, Any] | None:
    try:
        import httpx  # lazy: httpx import is not free and this is an optional path
    except Exception:
        return None
    url = f"{catalog_api_url()}/api/v1/connectors"
    try:
        with httpx.Client(timeout=_FETCH_TIMEOUT) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.debug("connectors catalog fetch failed: %s", exc)
        return None
    return _normalize_payload(data)


_cache: dict[str, Any] = {}
_lock = threading.Lock()


def _set_cache(payload: dict[str, Any], source: str) -> None:
    with _lock:
        _cache["data"] = payload
        _cache["at"] = time.monotonic()
        _cache["source"] = source


def invalidate_cache() -> None:
    """Drop the in-process cache so the next read hits disk (sync refreshed it)."""
    with _lock:
        _cache.clear()


def refresh_from_cloud() -> dict[str, Any] | None:
    """Fetch, persist to disk, and cache the catalog. ``None`` on failure.

    Called by the sync service and explicit refresh — never from a read path.
    A failed fetch or an empty payload leaves the existing copy alone.
    """
    payload = _fetch_from_cloud()
    if payload is None:
        return None
    store.write_payload(CATALOG_NAME, payload, payload.get("revision"))
    _set_cache(payload, "cloud")
    return payload


# ── Opportunistic freshness kick ────────────────────────────────────────────
#
# The sync service polls the cloud manifest hourly (catalog_sync settings), and
# on a closed box that is the ONLY automatic pickup: an operator enabling a new
# official client in the admin console otherwise stares at the old Connections
# UI for up to an hour with no SSH to force a refresh. The kick closes that
# gap: opening Connections (`GET /api/auth/status`) background-refreshes a
# stale connectors catalog, so the "Connect with Olano's app" button appears
# within seconds of the page being looked at.

# Re-fetch when the disk copy is older than this. Deliberately shorter than the
# manifest poll but long enough that a dashboard left open costs ~4 cloud hits
# per hour at worst (the throttle below bounds it harder).
KICK_MAX_AGE_SECONDS = 900.0
_KICK_THROTTLE_SECONDS = 600.0
_last_kick = float("-inf")


def kick_refresh_if_stale(max_age_seconds: float = KICK_MAX_AGE_SECONDS) -> bool:
    """Start a background refresh when the on-disk catalog is stale.

    Fire-and-forget: called from ``/api/auth/status`` **on the event loop**, so
    it must never block (the fetch runs in a daemon thread) and never raise.
    A monotonic throttle keeps repeated status polls from stacking fetches —
    the thread is also the only writer, and ``refresh_from_cloud`` keeps the
    last good copy on failure, so an unlucky kick can never lose data. Returns
    True when a refresh thread was actually started (the test seam).
    """
    global _last_kick
    try:
        age = store.payload_age_seconds(CATALOG_NAME)
        if age is not None and age <= max_age_seconds:
            return False
        now = time.monotonic()
        with _lock:
            if now - _last_kick < _KICK_THROTTLE_SECONDS:
                return False
            _last_kick = now

        def _run() -> None:
            try:
                refresh_from_cloud()
            except Exception as exc:  # network etc. — next kick retries
                logger.debug("connectors catalog kick failed: %s", exc)

        threading.Thread(target=_run, name="connectors-catalog-kick", daemon=True).start()
        return True
    except Exception:  # the caller is a hot API path — degrade to "no kick"
        return False


def _current() -> dict[str, Any]:
    """The catalog now: cache → disk → bundled. Never network, never raises."""
    now = time.monotonic()
    cached: dict[str, Any] | None = _cache.get("data")
    if cached is not None and (now - _cache.get("at", 0.0)) < DEFAULT_TTL_SECONDS:
        return cached
    try:
        payload = _normalize_payload(store.read_payload(CATALOG_NAME) or {})
    except Exception:
        payload = None
    if payload is not None:
        _set_cache(payload, "disk")
        return payload
    if cached is not None:
        return cached
    _set_cache(dict(BUNDLED), "bundled")
    return BUNDLED


def official_client(provider_id: str) -> str | None:
    """The official client id for *provider_id*, or ``None`` when there is none.

    Safe on the event loop: no network, no raising. This is the single
    predicate the broker's official floor and the runtime API both ask, so no
    surface can disagree about which providers have an official app.
    """
    try:
        entry = _current()["connectors"].get(provider_id)
    except Exception:  # pragma: no cover — _current is total, belt and braces
        return None
    if not isinstance(entry, dict):
        return None
    return str(entry.get("client_id")) or None


def official_ids() -> frozenset[str]:
    """Provider ids that currently have an official client."""
    try:
        return frozenset(_current()["connectors"])
    except Exception:  # pragma: no cover
        return frozenset()


def catalog_status() -> dict[str, Any]:
    """Sync/source status for dashboards: revision, source, count, fetched_at."""
    payload = _current()
    entry = store.state_entry(CATALOG_NAME)
    return {
        "revision": payload.get("revision", 0),
        "source": _cache.get("source", "bundled"),
        "official_count": len(payload.get("connectors") or {}),
        "fetched_at": entry.get("fetched_at"),
    }
