"""Client for the cloud skills-pack catalog (``GET {CLOUD}/api/v1/skills``).

The common skills pack (the private ``olano-ai/skills`` repo) used to reach a
box by ``git clone``, run at bootstrap and on every update. A managed box holds
no GitHub credential, so on every one of them the clone failed the same way
(``could not read Username for 'https://github.com'``) and the box stayed on
the package-bundled skills — silently, because the failure was deliberately
non-fatal. The archetype pack had the identical defect and got the identical
cure (``agents_registry``); this is the skills half.

Like ``agents_registry`` it **materializes to disk** rather than serving from
memory: each skill's files are written under the same directory the git clone
used (``<home>/skill-packs/olano-skills/skills/<slug>/``), and the existing
overlay (``skills.sync._copy_pack_skills``) copies them into the common skills
dir ``<home>/skills/`` with the same ``.olano-skillpack`` sentinel. Every
consumer of the pack — the loader, the librarian, pinning, pruning — is
unchanged; the cloud is simply another way of populating the clone directory.

Unlike its siblings this catalog is **token-gated**: the pack is the
company's own work, and the box fetches it with its deployment token, the one
credential a managed box does hold. An unmanaged box (no token) gets nothing
from here and keeps the git path, which works wherever a GitHub credential
exists.

A poisoned catalog row is the threat model of the writer: slugs and relative
paths are validated the way the cloud validates them on ingest
(``apps/web/lib/skill-registry.ts``), and every write lands inside the pack
directory or not at all.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CATALOG_NAME = "skills"
_FETCH_TIMEOUT = 30.0  # pages carry whole skill directories
_PAGE_LIMIT = 25  # the API's cap for ?include=files
_MAX_PAGES = 80  # 2000 skills; a runaway-pagination backstop, not a real bound
_MAX_FILE_BYTES = 512 * 1024
_MAX_SKILL_BYTES = 4 * 1024 * 1024

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")


def pack_skills_dir(home: str | Path | None = None) -> Path:
    """Where the pack's skills are materialized — the git clone's ``skills/`` dir."""
    from olano.skills.sync import olano_skills_pack_dir

    return olano_skills_pack_dir(home) / "skills"


def _auth_headers() -> dict[str, str] | None:
    """Bearer header for the deployment token, or None when the box has none."""
    from olano.deployment.sources import deployment_token

    token = deployment_token()
    return {"Authorization": f"Bearer {token}"} if token else None


def _fetch_all() -> tuple[list[dict[str, Any]], int | None] | None:
    """(skills, catalog revision) from the cloud, or None on any failure.

    None also when the box has no deployment token: the endpoint is
    token-gated and an unauthenticated request would only earn a 401.
    """
    headers = _auth_headers()
    if headers is None:
        logger.debug("skills catalog: no deployment token, skipping (token-gated)")
        return None
    try:
        import httpx  # lazy: httpx import is not free and this is an optional path
    except Exception:
        return None

    from olano.catalog.urls import catalog_api_url

    url = f"{catalog_api_url()}/api/v1/skills"
    skills: list[dict[str, Any]] = []
    revision: int | None = None
    try:
        with httpx.Client(timeout=_FETCH_TIMEOUT, headers=headers) as client:
            for page in range(_MAX_PAGES):
                resp = client.get(
                    url,
                    params={"include": "files", "limit": _PAGE_LIMIT, "offset": len(skills)},
                )
                resp.raise_for_status()
                data = resp.json()
                rows = data.get("skills") if isinstance(data, dict) else None
                if not isinstance(rows, list):
                    return None
                if page == 0:
                    raw = data.get("revision")
                    revision = int(raw) if isinstance(raw, int | float) else None
                skills.extend(r for r in rows if isinstance(r, dict))
                total = data.get("total")
                if not rows or not isinstance(total, int) or len(skills) >= total:
                    break
    except Exception as exc:
        logger.debug("skills catalog fetch failed: %s", exc)
        return None
    return skills, revision


def _safe_relpath(path: str) -> bool:
    """A file path the box may write under a skill dir: relative, no escapes."""
    if not path or len(path) > 512:
        return False
    if "\0" in path or path.startswith("/") or "\\" in path:
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def _decode(entry: Any) -> bytes | None:
    if not isinstance(entry, dict):
        return None
    content = entry.get("content")
    encoding = entry.get("encoding")
    if not isinstance(content, str):
        return None
    try:
        if encoding == "utf8":
            return content.encode("utf-8")
        if encoding == "base64":
            return base64.b64decode(content, validate=True)
    except Exception:
        return None
    return None


def normalize_skill_perms(path: Path) -> None:
    """Make a skill tree readable by every agent lane: dirs 0755, files 0644
    (0755 when already executable).

    Skills are read by three different identities — the runtime in-process
    (root inside the container), the sandbox agent (uid 1000) through the
    bind mount, and the ``filesystem`` backend's own reads — and a skill only
    the first of those can open is a skill the second silently cannot use.
    That is exactly what shipped: ``tempfile.mkdtemp`` creates its directory
    ``0700``, the atomic swap below renamed it into place as-is, and
    ``shutil.copytree`` carried the mode into ``<home>/skills/<name>``. Agent
    Father (in-process, root) read the charts skill fine; a sandboxed agent
    got ``permission_denied`` on the same path and hand-built a chart instead.

    Idempotent and best-effort: an unwritable entry is skipped, never raised
    on — a permissions repair must not fail the sync that carries it.
    """
    try:
        path.chmod(0o755)
    except OSError:
        return
    for dirpath, dirnames, filenames in os.walk(path):
        for d in dirnames:
            try:
                (Path(dirpath) / d).chmod(0o755)
            except OSError:
                pass
        for f in filenames:
            p = Path(dirpath) / f
            try:
                mode = p.stat().st_mode
                # Keep an executable executable; make everything else 0644.
                p.chmod(0o755 if mode & 0o111 else 0o644)
            except OSError:
                pass


def _write_skill(root: Path, slug: str, files: dict[str, Any]) -> bool:
    """Materialize one skill atomically: build in a temp dir, then swap it in."""
    tmp = Path(tempfile.mkdtemp(prefix=f".{slug}.", dir=str(root)))
    try:
        total = 0
        for rel, entry in files.items():
            if not _safe_relpath(rel):
                raise ValueError(f"unsafe path {rel!r}")
            blob = _decode(entry)
            if blob is None:
                raise ValueError(f"undecodable file {rel!r}")
            if len(blob) > _MAX_FILE_BYTES:
                raise ValueError(f"{rel!r} too large")
            total += len(blob)
            if total > _MAX_SKILL_BYTES:
                raise ValueError("skill too large")
            target = tmp / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
            if rel.endswith((".sh", ".py")) or "/scripts/" in f"/{rel}":
                target.chmod(0o755)
        if not (tmp / "SKILL.md").exists():
            raise ValueError("no SKILL.md")
        # mkdtemp made `tmp` 0700; the rename below would ship that mode.
        normalize_skill_perms(tmp)
        final = root / slug
        old = root / f".{slug}.old"
        if old.exists():
            shutil.rmtree(old, ignore_errors=True)
        if final.exists():
            final.rename(old)
        tmp.rename(final)
        shutil.rmtree(old, ignore_errors=True)
        return True
    except Exception as exc:
        logger.debug("materializing skill %s failed: %s", slug, exc)
        shutil.rmtree(tmp, ignore_errors=True)
        return False


def _materialize(
    skills: list[dict[str, Any]], home: str | Path | None = None
) -> tuple[int, dict[str, str]]:
    """Write each skill under the pack dir. Returns (count, {slug: content hash}).

    A skill whose ``contentHash`` matches the one recorded at the previous
    sync is left untouched (nothing to write); slugs no longer in the catalog
    are removed, so a withdrawn skill stops being offered — the overlay then
    prunes it from ``<home>/skills`` on the same pass.
    """
    from olano.catalog import store

    root = pack_skills_dir(home)
    root.mkdir(parents=True, exist_ok=True)
    previous = (store.read_payload(CATALOG_NAME) or {}).get("hashes")
    previous = previous if isinstance(previous, dict) else {}

    kept: dict[str, str] = {}
    for entry in skills:
        slug = str(entry.get("slug") or "").strip()
        files = entry.get("files")
        if not _SLUG_RE.match(slug) or not isinstance(files, dict) or not files:
            logger.debug("skipping malformed catalog skill: %r", slug)
            continue
        digest = str(entry.get("contentHash") or "")
        if digest and previous.get(slug) == digest and (root / slug / "SKILL.md").exists():
            # Unchanged content is not rewritten -- but its permissions are
            # re-checked, so a box that materialized this skill 0700 before
            # the fix is repaired on its next sync rather than never.
            normalize_skill_perms(root / slug)
            kept[slug] = digest
            continue
        if _write_skill(root, slug, files):
            kept[slug] = digest

    for stale in root.iterdir():
        if stale.is_dir() and stale.name not in kept and not stale.name.startswith("."):
            shutil.rmtree(stale, ignore_errors=True)
    return len(kept), kept


def refresh_from_cloud(home: str | Path | None = None) -> dict[str, Any] | None:
    """Fetch the pack, materialize it, overlay it into the common skills dir.

    None on failure (including "no deployment token"). Called by the catalog
    sync service when the manifest says the ``skills`` revision moved, by an
    explicit refresh, and by ``olano skills update`` on a managed box. Safe to
    call from a worker thread. Returns a small summary, never the files.
    """
    fetched = _fetch_all()
    if fetched is None:
        return None
    skills, revision = fetched
    if not skills:
        # An empty catalog is far likelier to be a cloud-side accident than a
        # deliberate withdrawal of every skill. Refuse rather than wipe the box.
        logger.debug("skills catalog returned no rows; keeping the existing copy")
        return None

    count, hashes = _materialize(skills, home)
    if count == 0:
        return None

    from olano.skills.sync import _copy_pack_skills, common_skills_dir

    report = _copy_pack_skills(pack_skills_dir(home), common_skills_dir(home))
    try:
        from olano.catalog import store

        store.write_payload(CATALOG_NAME, {"count": count, "hashes": hashes}, revision)
    except Exception as exc:
        logger.debug("skills catalog state write failed: %s", exc)

    logger.info("Skills catalog synced — %d skills (revision %s); %s", count, revision, report)
    return {"count": count, "revision": revision, "overlay": str(report)}


def catalog_status(home: str | Path | None = None) -> dict[str, Any]:
    """Diagnostics for ``GET /api/catalog/status`` and ``olano catalog status``."""
    from olano.catalog import store
    from olano.skills.sync import olano_skills_pack_dir

    directory = pack_skills_dir(home)
    on_disk = (
        sum(1 for d in directory.iterdir() if d.is_dir() and (d / "SKILL.md").exists())
        if directory.exists()
        else 0
    )
    entry = store.state_entry(CATALOG_NAME)
    if entry.get("fetched_at"):
        source = "cloud"
    elif (olano_skills_pack_dir(home) / ".git").exists():
        source = "git"
    else:
        source = "none"
    return {
        "source": source,
        "skills": on_disk,
        "revision": entry.get("revision"),
        "fetched_at": entry.get("fetched_at"),
        "token": _auth_headers() is not None,
        "dir": str(directory),
    }


__all__ = [
    "CATALOG_NAME",
    "catalog_status",
    "pack_skills_dir",
    "refresh_from_cloud",
]

# Keep ``os`` referenced for callers that monkeypatch environment-driven
# behaviour through this module in tests.
_ = os
