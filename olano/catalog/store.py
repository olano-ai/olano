"""On-disk cache for synced cloud catalogs: ``<home>/catalog/``.

Layout:
    <home>/catalog/models.json   last-fetched provider/model catalog payload
    <home>/catalog/mcp.json      last-fetched MCP directory payload
    <home>/catalog/state.json    per-catalog sync state (revision, fetched_at)

Payload files hold the raw cloud response (already normalized by the owning
registry module); ``state.json`` is the source of truth for "what revision do
I have and when did I get it", which the sync service diffs against the cloud
manifest. Writes are atomic (mkstemp → replace, the deployment/sources.py
idiom) so a crash mid-write never corrupts the cache; reads never raise — a
corrupt file just means "no cache" and the caller falls back to bundled data.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from olano.config.home import OlanoHomePaths, get_olano_paths

logger = logging.getLogger(__name__)

STATE_VERSION = 1
STATE_FILE = "state.json"

# Payload files are catalog JSON, not secrets, but keep the 0600 posture of the
# sibling deployment cache — one less thing to reason about.
_MAX_PAYLOAD_BYTES = 8 * 1024 * 1024


def catalog_dir(paths: OlanoHomePaths | None = None) -> Path:
    return (paths or get_olano_paths()).catalog_dir


def default_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "catalogs": {}}


def read_state(paths: OlanoHomePaths | None = None) -> dict[str, Any]:
    """The sync state, merged onto defaults (tolerates old/new/corrupt files)."""
    state = default_state()
    p = catalog_dir(paths) / STATE_FILE
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(raw.get("catalogs"), dict):
            state["catalogs"] = raw["catalogs"]
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.debug("catalog state unreadable (%s); starting fresh", exc)
    return state


def write_state(state: dict[str, Any], paths: OlanoHomePaths | None = None) -> None:
    """Persist sync state; never raises (a full disk must not break the tick)."""
    try:
        directory = catalog_dir(paths)
        directory.mkdir(parents=True, exist_ok=True)
        _atomic_write(directory / STATE_FILE, json.dumps(state, indent=2) + "\n")
    except Exception as exc:
        logger.debug("catalog state write failed: %s", exc)


def state_entry(name: str, paths: OlanoHomePaths | None = None) -> dict[str, Any]:
    entry = read_state(paths)["catalogs"].get(name)
    return entry if isinstance(entry, dict) else {}


def update_state_entry(
    name: str, revision: int | None, paths: OlanoHomePaths | None = None
) -> None:
    """Record a successful fetch of catalog ``name`` at ``revision`` (now)."""
    state = read_state(paths)
    state["catalogs"][name] = {"revision": revision, "fetched_at": int(time.time())}
    write_state(state, paths)


def payload_age_seconds(name: str, paths: OlanoHomePaths | None = None) -> float | None:
    """Seconds since catalog ``name`` was last fetched, or None if never."""
    fetched_at = state_entry(name, paths).get("fetched_at")
    if not isinstance(fetched_at, int | float) or fetched_at <= 0:
        return None
    return max(0.0, time.time() - float(fetched_at))


def read_payload(name: str, paths: OlanoHomePaths | None = None) -> dict[str, Any] | None:
    """The cached payload for catalog ``name``, or None (missing/corrupt)."""
    p = catalog_dir(paths) / f"{name}.json"
    try:
        if p.stat().st_size > _MAX_PAYLOAD_BYTES:
            logger.warning("catalog payload %s implausibly large; ignoring", p)
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.debug("catalog payload %s unreadable: %s", name, exc)
        return None


def write_payload(
    name: str,
    payload: dict[str, Any],
    revision: int | None,
    paths: OlanoHomePaths | None = None,
) -> None:
    """Persist a fetched payload + record it in state. Never raises."""
    try:
        directory = catalog_dir(paths)
        directory.mkdir(parents=True, exist_ok=True)
        _atomic_write(directory / f"{name}.json", json.dumps(payload) + "\n")
        update_state_entry(name, revision, paths)
    except Exception as exc:
        logger.debug("catalog payload %s write failed: %s", name, exc)


def _atomic_write(path: Path, content: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        tmp_path.chmod(0o600)
        tmp_path.replace(path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
