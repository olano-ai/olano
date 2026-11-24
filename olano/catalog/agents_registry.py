"""Client for the cloud agent archetype catalog (``GET {CLOUD}/api/v1/agents``).

The Team/Expert catalog used to arrive by ``git clone`` of olano-ai/olano-agents
into ``<home>/archetypes/``, run once at provisioning time, best-effort, with no
retry anywhere in the runtime. A network blip or a GitHub rate limit during
bootstrap left the box permanently on the ~11 in-package templates instead of
the pack's 202 — and nothing reported it, so the picker looked healthy with the
wrong contents.

This module replaces that transport with the catalog machinery every other
synced catalog already uses: revision-diffed against the cloud manifest, cached
on disk, refreshed on a schedule by :class:`olano.catalog.sync.CatalogSyncService`.

**It materializes to disk rather than serving from memory.** The whole archetype
layer — ``find_archetype_path``, ``_load_archetype_file``,
``build_archetype_catalog``, ``install_archetype``, and ``from_archetype``
resolution inside ``AgentConfig`` — is defined over files in a search dir. Adding
a second, in-memory resolution source would mean touching every one of those and
keeping two lookup paths in agreement forever. Writing the fetched archetypes as
``<home>/catalog/archetypes/<slug>.json5`` instead means the cloud is simply
another way of populating a search dir, and every consumer works unchanged.

The filename is the **slug**, which is the archetype's original filename stem —
that is what ``from_archetype:`` references resolve against, so a team's
specialists resolve exactly as they did from a git checkout.

Config:
    LIONCLAW_CATALOG_API_URL / LIONCLAW_CLOUD_API_URL
        base URL of the cloud API (see olano/catalog/urls.py)
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CATALOG_NAME = "agents"
_FETCH_TIMEOUT = 10.0
# The API caps ?include=body at 50 rows/page — bodies are the whole archetype
# document, so a bigger page is a multi-megabyte response.
_PAGE_LIMIT = 50
_MAX_PAGES = 40  # 2000 archetypes; a runaway-pagination backstop, not a real bound


def materialized_dir() -> Path:
    """Where fetched archetypes are written. One flat dir of ``<slug>.json5``."""
    from olano.config.home import get_olano_paths

    return get_olano_paths().catalog_dir / "archetypes"


def _fetch_all() -> tuple[list[dict[str, Any]], int | None] | None:
    """(archetypes, catalog revision) from the cloud, or None on any failure."""
    try:
        import httpx  # lazy: httpx import is not free and this is an optional path
    except Exception:
        return None

    from olano.catalog.urls import catalog_api_url

    url = f"{catalog_api_url()}/api/v1/agents"
    agents: list[dict[str, Any]] = []
    revision: int | None = None
    try:
        with httpx.Client(timeout=_FETCH_TIMEOUT) as client:
            for page in range(_MAX_PAGES):
                resp = client.get(
                    url,
                    params={
                        "include": "body",
                        "limit": _PAGE_LIMIT,
                        "offset": len(agents),
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                rows = data.get("agents") if isinstance(data, dict) else None
                if not isinstance(rows, list):
                    return None
                if page == 0:
                    raw = data.get("revision")
                    revision = int(raw) if isinstance(raw, int | float) else None
                agents.extend(r for r in rows if isinstance(r, dict))
                total = data.get("total")
                if not rows or not isinstance(total, int) or len(agents) >= total:
                    break
    except Exception as exc:
        logger.debug("agent catalog fetch failed: %s", exc)
        return None
    return agents, revision


def _materialize(agents: list[dict[str, Any]]) -> int:
    """Write each archetype body to ``<home>/catalog/archetypes/<slug>.json5``.

    Writes are atomic per file (mkstemp → replace, the ``catalog/store.py``
    idiom) so a crash mid-write never leaves a half-written archetype that the
    JSON5 loader would then log-and-skip. Slugs no longer in the catalog are
    removed, so a withdrawn archetype stops being offered.
    """
    directory = materialized_dir()
    directory.mkdir(parents=True, exist_ok=True)

    written: set[str] = set()
    for entry in agents:
        slug = str(entry.get("slug") or "").strip()
        body = entry.get("body")
        # Slug becomes a filename: refuse anything that could escape the dir.
        if not slug or not isinstance(body, dict) or "/" in slug or slug.startswith("."):
            logger.debug("skipping malformed catalog archetype: %r", slug)
            continue
        target = directory / f"{slug}.json5"
        payload = json.dumps(body, indent=2, ensure_ascii=False) + "\n"
        try:
            fd, tmp = tempfile.mkstemp(dir=str(directory), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                Path(tmp).replace(target)
            except Exception:
                Path(tmp).unlink(missing_ok=True)
                raise
        except Exception as exc:
            logger.debug("materializing archetype %s failed: %s", slug, exc)
            continue
        written.add(target.name)

    for stale in directory.glob("*.json5"):
        if stale.name not in written:
            stale.unlink(missing_ok=True)
    return len(written)


def refresh_from_cloud() -> dict[str, Any] | None:
    """Fetch the catalog, persist it, and materialize it. None on failure.

    Called by the catalog sync service when the manifest says the ``agents``
    revision moved, and by an explicit refresh. Safe to call from a worker
    thread. Returns a small summary (never the bodies) so the sync service's
    truthiness check stays cheap.
    """
    fetched = _fetch_all()
    if fetched is None:
        return None
    agents, revision = fetched
    if not agents:
        # An empty catalog is far more likely to be a cloud-side accident than a
        # deliberate withdrawal of every archetype. Refuse rather than delete
        # the box's whole materialized set.
        logger.debug("agent catalog returned no rows; keeping the existing copy")
        return None

    count = _materialize(agents)
    try:
        from olano.catalog import store

        # The payload records only the index, not the bodies — those already
        # live on disk as files, and duplicating ~1.3 MB into catalog/agents.json
        # would double the write for no reader.
        store.write_payload(
            CATALOG_NAME,
            {
                "count": count,
                "slugs": sorted(str(a.get("slug")) for a in agents if a.get("slug")),
            },
            revision,
        )
    except Exception as exc:
        logger.debug("agent catalog state write failed: %s", exc)

    # The materialized dir is an archetype search dir, so its contents changing
    # is exactly what the compiled catalog's fingerprint is watching for. Drop
    # it here so the dashboard reflects the new archetypes immediately rather
    # than at the next fingerprint re-check.
    try:
        from olano.archetypes.catalog_index import invalidate

        invalidate()
    except Exception as exc:
        logger.debug("archetype catalog invalidation skipped: %s", exc)

    logger.info("Agent catalog synced — %d archetypes (revision %s)", count, revision)
    return {"count": count, "revision": revision}


def catalog_status() -> dict[str, Any]:
    """Diagnostics for ``GET /api/catalog/status`` and ``olano catalog status``."""
    from olano.catalog import store

    directory = materialized_dir()
    on_disk = len(list(directory.glob("*.json5"))) if directory.exists() else 0
    entry = store.state_entry(CATALOG_NAME)
    return {
        "source": "cloud" if on_disk else "none",
        "archetypes": on_disk,
        "revision": entry.get("revision"),
        "fetched_at": entry.get("fetched_at"),
        "dir": str(directory),
    }
