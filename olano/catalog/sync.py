"""CatalogSyncService — the "what changed" poller for all cloud catalogs.

One background service keeps every synced catalog (models, MCP directory,
and whatever the cloud publishes next) fresh without per-browse network
calls. Each tick:

1. ``GET {CLOUD}/api/v1/catalog/manifest`` — a ~200-byte body of per-catalog
   revisions (bumped server-side by DB trigger on any write).
2. Diff against the local sync state (``<home>/catalog/state.json``).
3. Re-fetch **only** catalogs whose revision moved, atomically persist them,
   drop the owning module's in-process cache, and broadcast a
   ``catalog_updated`` SSE event so the dashboard refetches.

A cloud that predates the manifest endpoint (404) or is unreachable degrades
to a slow per-catalog TTL (24 h) — last-known-good data keeps serving either
way. Revisions are compared with ``!=`` rather than ``>``: the catalog is
public display data, and converging after a cloud-side reset matters more
than replay protection (the signed deployment-settings channel is the place
for monotonic enforcement).

Lifecycle mirrors :class:`olano.deployment.poller.SettingsPoller`
(``start()``/``stop()``, tracked task, jittered interval, never crashes the
runtime); settings are re-read every tick so config changes apply live.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from typing import Any

from olano.catalog import credits_registry, models_registry, store
from olano.catalog.urls import catalog_api_url

logger = logging.getLogger(__name__)

STARTUP_DELAY_SECONDS = 20.0
DEFAULT_INTERVAL_SECONDS = 3600.0
MIN_INTERVAL_SECONDS = 300.0
FALLBACK_TTL_SECONDS = 24 * 3600.0  # manifest unavailable → refresh this often
_FETCH_TIMEOUT = 4.0


def fetch_manifest() -> dict[str, Any] | None:
    """The per-catalog revision manifest, or None (older cloud / offline)."""
    try:
        import httpx  # lazy: httpx import is not free and this is an optional path
    except Exception:
        return None
    url = f"{catalog_api_url()}/api/v1/catalog/manifest"
    try:
        with httpx.Client(timeout=_FETCH_TIMEOUT) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.debug("catalog manifest fetch failed: %s", exc)
        return None
    catalogs = data.get("catalogs") if isinstance(data, dict) else None
    return catalogs if isinstance(catalogs, dict) else None


def _refresh_mcp() -> Any:
    from olano.core import mcp_registry

    return mcp_registry.refresh_from_cloud()


def _refresh_agents() -> Any:
    from olano.catalog import agents_registry

    return agents_registry.refresh_from_cloud()


def _refresh_connectors() -> Any:
    from olano.catalog import connectors_registry

    return connectors_registry.refresh_from_cloud()


def _refresh_skills() -> Any:
    from olano.catalog import skills_registry

    return skills_registry.refresh_from_cloud()


# Synced catalogs: name → refresh callable (fetch + persist + cache-drop,
# returns a truthy payload on success). Names match the cloud manifest keys.
_HANDLERS: dict[str, Callable[[], Any]] = {
    "models": models_registry.refresh_from_cloud,
    "mcp": _refresh_mcp,
    "credits": credits_registry.refresh_from_cloud,
    # The agent archetype pack. Unlike its siblings this one materializes to
    # disk (<home>/catalog/archetypes/*.json5) rather than being served from
    # memory — see agents_registry for why.
    "agents": _refresh_agents,
    # Official-connector clients (public client ids only; secrets stay on the
    # central relay — see connectors_registry / core/connect_relay.py).
    "connectors": _refresh_connectors,
    # The common skills pack (olano-ai/skills). Materializes into the skill-pack
    # dir and overlays <home>/skills, exactly where the git clone used to put
    # it. Token-gated on the cloud: an unmanaged box has no token and the
    # handler returns None, which the loop below reports and moves past.
    "skills": _refresh_skills,
}


def sync_once(force: bool = False, push_event: Any | None = None) -> dict[str, Any]:
    """One sync pass. Returns a per-catalog report; never raises.

    Safe to call from a worker thread (the service and the refresh API both
    do). ``force=True`` re-fetches every catalog regardless of revisions.
    """
    manifest = fetch_manifest()
    report: dict[str, Any] = {"manifest": manifest is not None, "catalogs": {}}
    for name, refresh in _HANDLERS.items():
        entry = store.state_entry(name)
        local_revision = entry.get("revision")
        remote_revision: int | None = None
        if manifest is not None:
            remote = manifest.get(name)
            raw = remote.get("revision") if isinstance(remote, dict) else None
            remote_revision = int(raw) if isinstance(raw, int | float) else None

        if force:
            should, reason = True, "forced"
        elif manifest is None:
            age = store.payload_age_seconds(name)
            should = age is None or age > FALLBACK_TTL_SECONDS
            reason = "fallback-ttl"
        elif remote_revision is None:
            # Catalog not in the manifest (not published there yet): leave the
            # local copy alone rather than guessing.
            should, reason = False, "not-in-manifest"
        else:
            should = remote_revision != local_revision
            reason = "revision"

        if not should:
            report["catalogs"][name] = {"status": "unchanged", "revision": local_revision}
            continue

        try:
            payload = refresh()
        except Exception as exc:  # a broken catalog must not break the others
            logger.debug("catalog %s refresh failed: %s", name, exc)
            payload = None
        if not payload:
            report["catalogs"][name] = {"status": "error", "revision": local_revision}
            continue

        new_revision = store.state_entry(name).get("revision")
        report["catalogs"][name] = {
            "status": "updated",
            "revision": new_revision,
            "reason": reason,
        }
        logger.info("catalog %s updated (revision %s → %s)", name, local_revision, new_revision)
        if push_event is not None:
            try:
                push_event("catalog_updated", {"catalog": name, "revision": new_revision})
            except Exception:
                logger.exception("broadcasting catalog update failed")
    return report


class CatalogSyncService:
    """Periodically syncs cloud catalogs; one instance per runtime process."""

    def __init__(self, push_event: Any | None = None) -> None:
        # push_event(event_type: str, payload: dict) — the runtime API's SSE
        # broadcaster; optional so the service is testable standalone.
        self._push_event = push_event
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="catalog_sync")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    @staticmethod
    def _interval_seconds() -> float:
        from olano.config.system_settings import load_catalog_sync_settings

        try:
            minutes = float(load_catalog_sync_settings().manifest_poll_minutes)
        except Exception:
            minutes = DEFAULT_INTERVAL_SECONDS / 60
        interval = max(MIN_INTERVAL_SECONDS, float(minutes) * 60)
        # Jitter so a fleet doesn't thundering-herd the control plane.
        return interval * random.uniform(0.9, 1.1)

    async def _loop(self) -> None:
        # Short boot delay: let the server settle, then do the first sync so a
        # fresh install has cloud data minutes after first start, not an hour.
        await asyncio.sleep(STARTUP_DELAY_SECONDS)
        while True:
            try:
                from olano.config.system_settings import load_catalog_sync_settings

                if load_catalog_sync_settings().enabled:
                    await asyncio.to_thread(sync_once, False, self._push_event)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("catalog sync tick failed: %s", exc)
            await asyncio.sleep(self._interval_seconds())
