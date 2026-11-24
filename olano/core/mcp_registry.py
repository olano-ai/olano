"""Client for the cloud MCP server directory.

The catalog of ready-to-add MCP servers — the "Connect a service" presets and
the copy-paste example configs shown in the dashboard's *Add MCP server* modal
and by ``olano mcp search`` — used to be hardcoded in the app. It now lives
on the cloud side behind a public read API (``GET {CLOUD}/api/v1/mcp``), so the
list can be maintained centrally (new providers, fixed URLs, fresh logos)
without shipping a new build.

This module is the box/CLI side. Browsing never waits on the network:

    in-process cache  →  disk cache (<home>/catalog/mcp.json)  →  bundled snapshot

Scheduled refresh is owned by :class:`olano.catalog.sync.CatalogSyncService`
(the manifest poller), which calls :func:`refresh_from_cloud` only when the
cloud's per-catalog revision moved. The disk cache survives restarts, so cold
CLI processes (each ``olano mcp …`` invocation) read it instead of paying a
network round-trip; a throttled inline refresh self-heals standalone installs
where no sync service runs. The bundled snapshot is a known-good baseline, not
the live list: adding a new MCP is a cloud-side data change, no code edit here.

Config:
    LIONCLAW_CATALOG_API_URL / LIONCLAW_CLOUD_API_URL
        base URL of the cloud API (see olano/catalog/urls.py;
        default https://api.olano.ai)
    LIONCLAW_MCP_DIRECTORY_TTL   in-process cache TTL in seconds (default 600)
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CATALOG_NAME = "mcp"
DEFAULT_TTL_SECONDS = 600
DISK_REFRESH_SECONDS = 6 * 3600  # standalone self-heal: refresh disk older than this
_ATTEMPT_BACKOFF_SECONDS = 600  # min gap between failed inline cloud attempts
_FETCH_TIMEOUT = 4.0
_PAGE_LIMIT = 200  # server max for /api/v1/mcp

# Words that name the MECHANISM, not a service: "parallel mcp", "notion mcp
# server" and "parallel" all mean the same catalog row. Dropped from a query
# only when a content token survives, so a bare "mcp server" still searches for
# what the user actually typed instead of matching nothing.
DIRECTORY_NOISE: frozenset[str] = frozenset({"mcp", "server", "servers"})

# Columns a `?q=` term matches. Mirrors SEARCH_COLUMNS in the cloud's
# apps/web/lib/mcp-registry.ts — if the two lists disagree, a box searches
# differently online and offline. `url` is load-bearing: it is what carries the
# brand when slug and name diverge (api.you.com), and what makes the word "mcp"
# harmless rather than unmatchable (https://search.parallel.ai/mcp).
_SEARCH_FIELDS: tuple[str, ...] = ("slug", "name", "description", "category", "url", "notes")

# Full set of fields in a directory entry (camelCase, matching the cloud API's
# public projection). Bundled rows below only spell out the meaningful fields;
# `_normalize_entry` fills the rest with defaults.
_ENTRY_DEFAULTS: dict[str, Any] = {
    "slug": "",
    "name": "",
    "description": None,
    "kind": "preset",
    "category": None,
    "transport": None,
    "auth": "none",
    "oauthClient": None,
    "url": None,
    "command": None,
    "args": [],
    "envKeys": [],
    "headers": {},
    "logoUrl": None,
    "homepageUrl": None,
    "docsUrl": None,
    "appConsoleUrl": None,
    "appConsoleLabel": None,
    "badge": None,
    "runCommand": None,
    "exampleJson": None,
    "notes": None,
    "tags": [],
    "featured": False,
}


# ── Bundled offline snapshot ─────────────────────────────────────────────────
# The rows live in `bundled_mcp.json` beside this module, NOT as literals here.
# That file is the single source of truth for the offline baseline: the
# dashboard's own fallback lists are generated from it too
# (`dashboard_2.0/src/views/agents/mcpBundled.generated.ts`), so the two can no
# longer drift — they once disagreed on the id of the same server (`parallel`
# here, `parallel-search` there) and on which rows existed at all.
#
# Shape is a captured `GET /api/v1/mcp` payload filtered to the pinned slugs,
# written sparsely; `_normalize_entry` fills the defaults on load exactly as it
# does for a live cloud response. Regenerate with:
#     uv run python scripts/gen_bundled_mcp.py
BUNDLED_SNAPSHOT_PATH = Path(__file__).with_name("bundled_mcp.json")


def _load_bundled_servers() -> list[dict[str, Any]]:
    """The pinned rows, as a flat `servers` list in cloud wire shape.

    Never raises: a missing or unreadable snapshot degrades to an empty
    directory, which every caller already handles (`get_directory` promises
    only a usable dict, and the disk/cloud tiers sit above this one). A
    packaging mistake must not take the process down on import.
    """
    try:
        with BUNDLED_SNAPSHOT_PATH.open(encoding="utf-8") as fh:
            payload = json.load(fh)
        servers = payload.get("servers")
        return [s for s in servers if isinstance(s, dict)] if isinstance(servers, list) else []
    except Exception:  # pragma: no cover - packaging failure, not a code path
        logger.warning("bundled MCP snapshot unreadable at %s", BUNDLED_SNAPSHOT_PATH)
        return []


def _normalize_entry(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a full directory entry with every field present (defaults filled)."""
    entry = dict(_ENTRY_DEFAULTS)
    entry.update({k: v for k, v in raw.items() if k in _ENTRY_DEFAULTS})
    return entry


def _bundled_directory() -> dict[str, Any]:
    """The offline baseline, split exactly like a live cloud payload."""
    return {**_split_servers(_load_bundled_servers()), "source": "bundled"}


def _categories_of(presets: list[dict[str, Any]]) -> list[str]:
    seen: dict[str, None] = {}
    for p in presets:
        cat = p.get("category")
        if cat:
            seen.setdefault(cat, None)
    return sorted(seen)


def cloud_api_url() -> str:
    """Base URL of the cloud API (no trailing slash).

    Delegates to the shared catalog resolver so every catalog consumer
    (models, MCP directory) targets the same host.
    """
    from olano.catalog.urls import catalog_api_url

    return catalog_api_url()


def _ttl_seconds() -> int:
    raw = os.environ.get("LIONCLAW_MCP_DIRECTORY_TTL")
    if not raw:
        return DEFAULT_TTL_SECONDS
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_TTL_SECONDS


def _split_servers(servers: list[dict[str, Any]]) -> dict[str, Any]:
    """Normalize + split a flat `servers` list into presets/examples/categories."""
    presets: list[dict[str, Any]] = []
    examples: list[dict[str, Any]] = []
    for raw in servers:
        entry = _normalize_entry(raw)
        (examples if entry.get("kind") == "example" else presets).append(entry)
    return {"presets": presets, "examples": examples, "categories": _categories_of(presets)}


def _fetch_from_cloud(
    params: dict[str, Any] | None = None, timeout: float = _FETCH_TIMEOUT
) -> list[dict[str, Any]] | None:
    """GET the flat server list from the cloud, or None on any failure.

    With no explicit ``params``, pages through the full directory (the server
    caps ``limit`` at 200 and the catalog outgrew it once — read ``total``).
    """
    full = _fetch_full(params=params, timeout=timeout)
    return None if full is None else full[0]


def _fetch_full(
    params: dict[str, Any] | None = None, timeout: float = _FETCH_TIMEOUT
) -> tuple[list[dict[str, Any]], int | None] | None:
    """(servers, catalog revision) from the cloud, or None on any failure.

    The revision (additive field on ``GET /api/v1/mcp``) is what the sync
    state records so the manifest diff can skip unchanged directories.
    """
    try:
        import httpx  # lazy: httpx import is not free and this is an optional path
    except Exception:
        return None
    url = f"{cloud_api_url()}/api/v1/mcp"

    def _revision_of(data: dict[str, Any]) -> int | None:
        raw = data.get("revision")
        return int(raw) if isinstance(raw, int | float) else None

    try:
        with httpx.Client(timeout=timeout) as client:
            if params is not None:
                resp = client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
                servers = data.get("servers") if isinstance(data, dict) else None
                if not isinstance(servers, list):
                    return None
                return servers, _revision_of(data)
            servers = []
            revision: int | None = None
            offset = 0
            while True:
                resp = client.get(url, params={"limit": _PAGE_LIMIT, "offset": offset})
                resp.raise_for_status()
                data = resp.json()
                page = data.get("servers") if isinstance(data, dict) else None
                if not isinstance(page, list):
                    return None
                if offset == 0:
                    revision = _revision_of(data)
                servers.extend(s for s in page if isinstance(s, dict))
                total = data.get("total")
                if not isinstance(total, int) or len(servers) >= total or not page:
                    return servers, revision
                offset = len(servers)
    except Exception as exc:
        logger.debug("mcp directory fetch failed: %s", exc)
        return None


# Module-level cache: {"data": <directory dict>, "at": <monotonic ts>}.
_cache: dict[str, Any] = {}

# "No cloud attempt yet." NOT 0.0 — see models_registry.NEVER_ATTEMPTED: the
# backoff compares against time.monotonic() (time since boot), so 0.0 would
# suppress the first inline fetch for the machine's first
# _ATTEMPT_BACKOFF_SECONDS of uptime.
NEVER_ATTEMPTED = float("-inf")
_last_attempt = NEVER_ATTEMPTED


def _may_attempt_cloud() -> bool:
    global _last_attempt
    now = time.monotonic()
    if now - _last_attempt < _ATTEMPT_BACKOFF_SECONDS:
        return False
    _last_attempt = now
    return True


def _set_cache(directory: dict[str, Any]) -> None:
    _cache["data"] = {k: v for k, v in directory.items() if k != "source"}
    _cache["at"] = time.monotonic()


def invalidate_cache() -> None:
    """Drop the in-process cache so the next read hits disk (sync refreshed it)."""
    _cache.clear()


def refresh_from_cloud() -> dict[str, Any] | None:
    """Fetch the full directory, persist it to disk, and cache it. None on failure.

    Called by the catalog sync service (when the cloud manifest says the mcp
    revision moved), by ``get_directory(force=True)``, and by the stale-disk
    self-heal. Safe to call from a worker thread.
    """
    full = _fetch_full()
    if full is None:
        return None
    servers, revision = full
    directory = _split_servers(servers)
    try:
        from olano.catalog import store

        # Persist the raw server list (small, forward-compatible) — the split/
        # normalize happens on read so bundled and disk paths share one shape.
        # Recording the revision lets the sync manifest diff skip unchanged
        # directories instead of re-fetching every tick.
        store.write_payload(CATALOG_NAME, {"servers": servers}, revision)
    except Exception as exc:
        logger.debug("mcp directory disk write failed: %s", exc)
    _set_cache(directory)
    return directory


def _read_disk() -> dict[str, Any] | None:
    try:
        from olano.catalog import store

        payload = store.read_payload(CATALOG_NAME)
    except Exception:
        return None
    servers = payload.get("servers") if isinstance(payload, dict) else None
    if not isinstance(servers, list) or not servers:
        return None
    return _split_servers([s for s in servers if isinstance(s, dict)])


def get_directory(force: bool = False) -> dict[str, Any]:
    """Return the MCP directory: in-process cache → disk → bundled snapshot.

    Always returns a usable dict with keys ``presets``, ``examples``,
    ``categories`` and ``source`` (``"cloud"``/``"cache"``/``"disk"``/
    ``"bundled"``). Never raises and never returns empty. ``force=True``
    fetches from the cloud inline (explicit refresh), falling back down the
    chain on failure. Scheduled refresh is the sync service's job — a normal
    browse only touches the network as a throttled self-heal when there is no
    usable disk cache (standalone CLI installs).
    """
    now = time.monotonic()
    cached = _cache.get("data")
    if not force and cached is not None and (now - _cache.get("at", 0.0)) < _ttl_seconds():
        return {**cached, "source": "cache"}

    if force:
        fetched = refresh_from_cloud()
        if fetched is not None:
            return {**fetched, "source": "cloud"}

    directory = _read_disk()
    if directory is not None:
        # Standalone self-heal (no sync service running, e.g. bare CLI): if the
        # disk copy is very stale, try one throttled refresh; stale beats none.
        try:
            from olano.catalog import store

            age = store.payload_age_seconds(CATALOG_NAME)
        except Exception:
            age = None
        if age is not None and age > DISK_REFRESH_SECONDS and _may_attempt_cloud():
            fetched = refresh_from_cloud()
            if fetched is not None:
                return {**fetched, "source": "cloud"}
        _set_cache(directory)
        return {**directory, "source": "disk"}

    if cached is not None:
        return {**cached, "source": "cache"}

    if _may_attempt_cloud():
        fetched = refresh_from_cloud()
        if fetched is not None:
            return {**fetched, "source": "cloud"}

    return _bundled_directory()


def query_tokens(query: str) -> list[str]:
    """Content tokens of a directory query.

    Lowercased and whitespace-split, with :data:`DIRECTORY_NOISE` dropped —
    unless that would empty the query, in which case the user's own words are
    kept. A bare ``"mcp server"`` must still search for something rather than
    match everything.
    """
    tokens = str(query or "").strip().lower().split()
    content = [t for t in tokens if t not in DIRECTORY_NOISE]
    return content or tokens


def search(
    query: str, *, kind: str | None = None, category: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """Search the directory. Hits the cloud ``?q=`` search, falling back to a
    local filter over the bundled/cached directory on failure.

    Both paths apply the same rule: **every** token of the query must appear
    somewhere in :data:`_SEARCH_FIELDS` (or ``tags``), order-independent, after
    :func:`query_tokens` drops the mechanism words. A whole-phrase substring
    match was the old rule, and it made one extra word unmatchable — a search
    for ``"parallel mcp"`` missed the row whose slug is exactly ``parallel``.

    Sending the cleaned tokens as ``?q=`` is also what keeps a box correct
    against a cloud that has not been redeployed: the noise is gone before the
    remote filter — whatever its own matching rule — ever sees the query.
    """
    terms = query_tokens(query)
    params: dict[str, Any] = {"limit": max(1, min(limit, 200))}
    if terms:
        params["q"] = " ".join(terms)
    if kind:
        params["kind"] = kind
    if category:
        params["category"] = category

    servers = _fetch_from_cloud(params=params)
    if servers is not None:
        return [_normalize_entry(s) for s in servers][:limit]

    # Offline fallback: filter whatever directory we can assemble locally.
    directory = get_directory()
    pool: list[dict[str, Any]] = list(directory["presets"]) + list(directory["examples"])

    def matches(entry: dict[str, Any]) -> bool:
        if kind and entry.get("kind") != kind:
            return False
        if category and (entry.get("category") or "") != category:
            return False
        if not terms:
            return True
        hay = " ".join(
            [str(entry.get(f) or "") for f in _SEARCH_FIELDS]
            + [" ".join(str(t) for t in (entry.get("tags") or []))]
        ).lower()
        return all(t in hay for t in terms)

    return [e for e in pool if matches(e)][:limit]


def get_server(slug: str) -> dict[str, Any] | None:
    """Return one entry by slug (from the current directory), or None."""
    directory = get_directory()
    entries: list[dict[str, Any]] = list(directory["presets"]) + list(directory["examples"])
    for entry in entries:
        if entry.get("slug") == slug:
            return entry
    return None


def _url_key(url: str) -> str:
    return str(url or "").strip().lower().rstrip("/")


def _host_of(url: str) -> str:
    from urllib.parse import urlparse

    try:
        return (urlparse(str(url or "").strip()).hostname or "").lower()
    except ValueError:
        return ""


def find_by_url(url: str) -> dict[str, Any] | None:
    """The preset a remote server URL belongs to, or ``None``.

    Local only (cache → disk → bundled) — this runs on the magic-link mint path
    and on every page render. Exact match first (trailing slash and case
    ignored: ``https://mcp.hubspot.com/`` is the HubSpot row), then the host
    when exactly one preset lives on it — an agent that typed a path variant
    still lands on the right console link and auth mode, while two presets on
    one host (Parallel Search vs Parallel Task) stay ambiguous rather than
    guessed.
    """
    key = _url_key(url)
    if not key.startswith(("http://", "https://")):
        return None
    presets: list[dict[str, Any]] = list(get_directory()["presets"])
    for entry in presets:
        if _url_key(str(entry.get("url") or "")) == key:
            return entry
    host = _host_of(key)
    if not host:
        return None
    on_host = [e for e in presets if _host_of(str(e.get("url") or "")) == host]
    return on_host[0] if len(on_host) == 1 else None
