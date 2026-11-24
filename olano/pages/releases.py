"""Immutable releases for static pages that have a build step.

A build writes into the agent's workspace (``dist/``, ``build/``…), and a
viewer must never see that directory half-written. After a successful build the
supervisor copies the output into a fresh release directory under the home,
then switches the page to it by rewriting one pointer file atomically. A failed
build switches nothing, so the previous release stays live; the last few
releases are kept for an instant rollback.

Copying is deliberate (not a rename of ``dist/``): the release must be outside
the sandbox's reach, and the build output is the agent's to keep rebuilding.
The copy never follows a symlink: a build output that links to ``/etc`` or to
another agent's workspace would otherwise be copied out of the sandbox's view
and served. It is also bounded in files and bytes.
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from olano.memory.markdown_memory import atomic_write_text
from olano.pages.paths import current_release_file, page_releases_dir

logger = logging.getLogger(__name__)

RELEASES_KEPT = 3
MAX_RELEASE_BYTES = 512 * 1024 * 1024
MAX_RELEASE_FILES = 20_000
_TMP_PREFIX = ".tmp-"


class ReleaseError(Exception):
    """A build output that cannot become a release, with the reason."""


@dataclass(frozen=True)
class ReleaseInfo:
    id: str
    files: int
    bytes: int
    skipped_symlinks: int
    has_index: bool


def _new_release_id() -> str:
    """Sorts chronologically by name, down to the nanosecond, then disambiguates.

    Pruning keeps "the newest N" by sorting names, so two builds in the same
    second must still sort in the order they were made.
    """
    now = time.time_ns()
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now // 1_000_000_000))
    return f"{stamp}.{now % 1_000_000_000:09d}Z-{secrets.token_hex(3)}"


def snapshot_release(slug: str, source: Path) -> ReleaseInfo:
    """Copy ``source`` into a new (not yet active) release. Blocking."""
    src = Path(source)
    if src.is_symlink() or not src.is_dir():
        raise ReleaseError(f"the build output '{src}' is not a directory")
    root = page_releases_dir(slug)
    root.mkdir(parents=True, exist_ok=True)
    rid = _new_release_id()
    tmp = root / f"{_TMP_PREFIX}{rid}"
    files = 0
    total = 0
    skipped = 0
    try:
        for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
            here = Path(dirpath)
            rel = here.relative_to(src)
            (tmp / rel).mkdir(parents=True, exist_ok=True)
            kept_dirs = []
            for name in dirnames:
                if (here / name).is_symlink():
                    skipped += 1
                else:
                    kept_dirs.append(name)
            dirnames[:] = kept_dirs
            for name in filenames:
                path = here / name
                if path.is_symlink():
                    skipped += 1
                    continue
                if not path.is_file():
                    continue  # sockets, fifos, devices
                files += 1
                total += path.stat().st_size
                if files > MAX_RELEASE_FILES:
                    raise ReleaseError(
                        f"the build output has more than {MAX_RELEASE_FILES} files; "
                        "point output at the built site, not the project"
                    )
                if total > MAX_RELEASE_BYTES:
                    raise ReleaseError(
                        f"the build output is larger than {MAX_RELEASE_BYTES // (1024 * 1024)} MB"
                    )
                shutil.copyfile(path, tmp / rel / name)
        if files == 0:
            raise ReleaseError(f"the build output '{src.name}' is empty")
        final = root / rid
        tmp.replace(final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if skipped:
        logger.info("page %s: release %s skipped %d symlink(s)", slug, rid, skipped)
    return ReleaseInfo(
        id=rid,
        files=files,
        bytes=total,
        skipped_symlinks=skipped,
        has_index=(final / "index.html").is_file(),
    )


def activate_release(slug: str, release_id: str) -> None:
    """Point the page at ``release_id``. Atomic: a viewer sees old or new, never neither."""
    if not (page_releases_dir(slug) / release_id).is_dir():
        raise ReleaseError(f"release {release_id} does not exist")
    atomic_write_text(current_release_file(slug), release_id + "\n")


def current_release_id(slug: str) -> str | None:
    try:
        rid = current_release_file(slug).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not rid or "/" in rid or rid.startswith("."):
        return None
    return rid


def current_release_dir(slug: str) -> Path | None:
    """The directory the page serves from now, or ``None`` before the first build."""
    rid = current_release_id(slug)
    if rid is None:
        return None
    path = page_releases_dir(slug) / rid
    return path if path.is_dir() else None


def list_releases(slug: str) -> list[str]:
    root = page_releases_dir(slug)
    try:
        names = [p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")]
    except OSError:
        return []
    return sorted(names)


def prune_releases(slug: str, keep: int = RELEASES_KEPT) -> list[str]:
    """Drop all but the newest ``keep`` releases (never the active one)."""
    active = current_release_id(slug)
    releases = list_releases(slug)
    survivors = set(releases[-keep:]) | ({active} if active else set())
    removed: list[str] = []
    root = page_releases_dir(slug)
    for rid in releases:
        if rid not in survivors:
            shutil.rmtree(root / rid, ignore_errors=True)
            removed.append(rid)
    # A crashed copy leaves a temp dir behind; nothing reads those.
    try:
        for leftover in root.glob(f"{_TMP_PREFIX}*"):
            shutil.rmtree(leftover, ignore_errors=True)
    except OSError:
        pass
    return removed


def delete_releases(slug: str) -> None:
    shutil.rmtree(page_releases_dir(slug), ignore_errors=True)
