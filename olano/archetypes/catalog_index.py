"""Compiled archetype catalog — the read path behind ``GET /api/archetypes``.

The dashboard's Team / Expert picker needs eight fields per archetype (name,
label, description, emoji, colour, category, model, subagent count). Producing
them from source means opening and parsing every archetype file: with the
canonical ``olano-agents`` pack installed that is 202 files / 8.7 MB, and
:func:`~olano.archetypes.installer.build_archetype_catalog` used to run inline
on the event loop for every request.

So the compiled projection is cached, and the read path is:

    in-process snapshot  →  <home>/catalog/archetypes.json  →  full rebuild

Freshness is a **fingerprint**, not a TTL: one directory walk collects
(path, mtime, size) for every archetype file and hashes it. A cached catalog is
served only while the files behind it are byte-for-byte the era they were built
from, so adding, editing, or deleting an archetype invalidates it on the next
read with no explicit cache-busting anywhere. The walk itself is throttled
(:data:`_FINGERPRINT_TTL`) so a burst of requests re-walks once, not once each.

The disk copy is what makes this survive restarts: a process that never runs
the boot warmer still answers the first request from ``<home>/catalog/`` in
milliseconds instead of paying the rebuild. It uses the same atomic,
never-raises primitives as the cloud-synced catalogs
(:mod:`olano.catalog.store`), so a crash mid-write cannot corrupt it and an
unreadable file just means "rebuild".

Nothing here reaches the network. The archetypes are local files; this module
only decides when re-reading them is worth it.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CATALOG_NAME = "archetypes"

# Re-walking the search dirs to re-check the fingerprint costs ~10ms with the
# canonical pack installed. Cheap, but not free, and the dashboard can fire
# several catalog reads in a burst — so a snapshot verified this recently is
# served without re-walking. The cost of the bound is that an archetype file
# edited on disk shows up within this many seconds rather than instantly;
# `invalidate()` covers every in-process write path, so the delay only ever
# applies to edits made behind the runtime's back.
_FINGERPRINT_TTL = 30.0

# Guards the module snapshot. Builds can be triggered concurrently by the boot
# warmer and a request, and rebuilding twice is wasteful but harmless — the
# lock is held only around the cheap snapshot swap, never around the build.
_LOCK = threading.Lock()

_snapshot: dict[str, Any] | None = None  # last compiled catalog (+ fingerprint)
_verified_at: float = 0.0  # monotonic time the snapshot's fingerprint was last confirmed


def _fingerprint(paths: list[Path]) -> str:
    """Content-era hash of the discovered archetype files.

    Keyed on (path, mtime_ns, size) rather than file contents: stat-ing 200
    files is ~10ms where hashing 8.7 MB is not, and any edit that matters moves
    at least one of the three. Discovery order is deterministic (see
    ``installer.discover_archetype_files``), so the hash is stable across runs.
    """
    h = hashlib.sha256()
    for p in paths:
        try:
            st = p.stat()
            h.update(f"{p}\0{st.st_mtime_ns}\0{st.st_size}\0".encode())
        except OSError:
            # Vanished between walk and stat — record the absence so the next
            # read (which won't discover it at all) hashes differently.
            h.update(f"{p}\0missing\0".encode())
    return h.hexdigest()


def _current_fingerprint() -> str:
    from olano.archetypes.installer import discover_archetype_files

    return _fingerprint(discover_archetype_files())


def _read_disk() -> dict[str, Any] | None:
    try:
        from olano.catalog import store

        payload = store.read_payload(CATALOG_NAME)
    except Exception:
        return None
    if not isinstance(payload, dict) or not payload.get("fingerprint"):
        return None
    if not isinstance(payload.get("teams"), list) or not isinstance(
        payload.get("specialists"), list
    ):
        return None
    return payload


def _write_disk(payload: dict[str, Any]) -> None:
    try:
        from olano.catalog import store

        # revision=None: this catalog is local, so there is no cloud revision to
        # diff against — freshness is the fingerprint inside the payload. The
        # state entry still records `fetched_at`, which `status()` reports.
        store.write_payload(CATALOG_NAME, payload, None)
    except Exception as exc:  # a read-only/full disk must not break the read path
        logger.debug("archetype catalog persist failed: %s", exc)


def _build(fingerprint: str) -> dict[str, Any]:
    from olano.archetypes.installer import build_archetype_catalog

    started = time.monotonic()
    catalog = build_archetype_catalog()
    payload: dict[str, Any] = {
        "fingerprint": fingerprint,
        "built_at": time.time(),
        "teams": catalog["teams"],
        "specialists": catalog["specialists"],
    }
    logger.info(
        "Archetype catalog compiled in %.2fs — %d teams, %d specialists",
        time.monotonic() - started,
        len(payload["teams"]),
        len(payload["specialists"]),
    )
    return payload


def invalidate() -> None:
    """Drop the in-process snapshot so the next read re-checks the fingerprint.

    Call after any write that changes the archetype files (pack update, a
    user-added pack dir, ``POST /api/catalog/refresh``). The disk copy is left
    alone on purpose — it is still valid if the fingerprint happens to match,
    and re-reading it is cheaper than rebuilding.
    """
    global _snapshot, _verified_at
    with _LOCK:
        _snapshot = None
        _verified_at = 0.0


def get_archetype_catalog(force: bool = False) -> dict[str, list[dict[str, Any]]]:
    """Return ``{"teams": [...], "specialists": [...]}``.

    Never raises and never blocks on the network. ``force=True`` skips both
    caches and rebuilds from source. This is sync and can take seconds on a
    cold cache with a large pack — call it from a thread, never on the event
    loop (``asyncio.to_thread``, or the boot warmer).
    """
    global _snapshot, _verified_at

    if not force:
        with _LOCK:
            snap = _snapshot
            fresh = snap is not None and (time.monotonic() - _verified_at) < _FINGERPRINT_TTL
        if fresh and snap is not None:
            return {"teams": snap["teams"], "specialists": snap["specialists"]}

    fingerprint = _current_fingerprint()

    if not force:
        # In-process snapshot still describes what is on disk.
        with _LOCK:
            snap = _snapshot
            if snap is not None and snap.get("fingerprint") == fingerprint:
                _verified_at = time.monotonic()
                return {"teams": snap["teams"], "specialists": snap["specialists"]}

        disk = _read_disk()
        if disk is not None and disk.get("fingerprint") == fingerprint:
            with _LOCK:
                _snapshot = disk
                _verified_at = time.monotonic()
            return {"teams": disk["teams"], "specialists": disk["specialists"]}

    payload = _build(fingerprint)
    with _LOCK:
        _snapshot = payload
        _verified_at = time.monotonic()
    _write_disk(payload)
    return {"teams": payload["teams"], "specialists": payload["specialists"]}


def status() -> dict[str, Any]:
    """Diagnostics for ``GET /api/catalog/status`` and ``olano catalog status``.

    Reports where the catalog would be served from and how big it is, so
    "the Expert list only shows five entries" is answerable without SSH: a box
    missing the canonical pack reports a small ``specialists`` count and a
    ``pack_installed: false``.
    """
    from olano.archetypes.installer import _archetype_search_dirs
    from olano.archetypes.sync import is_pack_installed

    with _LOCK:
        snap = _snapshot
        verified_at = _verified_at

    if snap is not None:
        source = "memory"
    elif _read_disk() is not None:
        source = "disk"
    else:
        source = "none"

    age: float | None = None
    try:
        from olano.catalog import store

        age = store.payload_age_seconds(CATALOG_NAME)
    except Exception:
        age = None

    out: dict[str, Any] = {
        "source": source,
        "age_seconds": age,
        "search_dirs": [str(d) for d in _archetype_search_dirs()],
        "pack_installed": bool(is_pack_installed()),
    }
    if snap is not None:
        out["teams"] = len(snap["teams"])
        out["specialists"] = len(snap["specialists"])
        out["verified_seconds_ago"] = round(max(0.0, time.monotonic() - verified_at), 1)
    return out
