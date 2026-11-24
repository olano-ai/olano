"""Pruned workspace traversal shared by the cartography phases.

Cartography used to enumerate the workspace with ``Path.rglob("*")`` and filter
afterwards — which walks into everything before deciding to ignore it. That is
fatal on a working agent's workspace: an agent that clones repositories to do
its job accumulates gigabytes of ``.git`` object stores, virtualenvs and tool
caches (an observed workspace: 11 GB, three Home Assistant checkouts, a 2 GB
venv), and enumerating + stat-ing those hundreds of thousands of inodes on
cold cloud block storage measured **130s of a 136s agent boot**. Name-based
skip lists can't fix that — the venvs were called ``.venv-social`` and
``.venv-google-verify``, and the clones can be called anything.

This walker prunes *before* descending, and recognises directories by what
they are, not what they're called:

* a directory (below the workspace root) containing ``.git`` is an **embedded
  repository** the agent cloned for its work — reported as a single entry and
  never entered;
* a directory containing ``pyvenv.cfg`` is a **virtualenv**, whatever its
  name — skipped entirely;
* a directory containing ``.package-lock.json`` (npm ≥7 writes one inside
  every ``node_modules``) or ``.modules.yaml`` (pnpm's equivalent) is an
  **npm installation tree**, even when renamed or vendored — skipped entirely;
* well-known state/cache directory names (:data:`SKIP_DIR_NAMES`) are skipped
  by name, including the workspace state folder (``.olano``, and the
  pre-rename ``.lionclaw`` that is a link to it on upgraded workspaces:
  cartography's own output, and indexing it would make the scan read its
  previous run) and the JS package-manager stores (``node_modules``,
  ``.pnpm-store``, ``.yarn``, ``.npm``, ``bower_components``);
* ``.olanoignore`` patterns (``.lionclawignore`` before the rename) prune
  matching directories at walk time instead of filtering their contents one
  file at a time afterwards.

The workspace root itself is never repo-pruned: a workspace that is a git repo
is still a workspace.
"""

from __future__ import annotations

import fnmatch
import logging
import os
from collections.abc import Iterator, Sequence
from pathlib import Path

from olano.config.workspace import LEGACY_WORKSPACE_STATE_DIRNAMES, WORKSPACE_STATE_DIRNAME

logger = logging.getLogger(__name__)

# Directory names never worth descending into. `customers` is the per-customer
# private memory of a customer-service agent (olano/memory/customer_memory.py)
# — indexing it would copy customer ids into `<state dir>/workspace_index.json`,
# which feeds prompt context. The state folder is listed under its current name
# and its pre-rename one (a link to the current folder on upgraded workspaces).
SKIP_DIR_NAMES = {
    ".git",
    "__pycache__",
    "node_modules",
    ".pnpm-store",
    ".yarn",
    ".npm",
    "bower_components",
    ".venv",
    "venv",
    "rag",
    "customers",
    WORKSPACE_STATE_DIRNAME,
    *LEGACY_WORKSPACE_STATE_DIRNAMES,
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".tox",
    ".cache",
}

# File extensions with no cartographic value (compiled artifacts, databases).
SKIP_EXTENSIONS = {".pyc", ".pyo", ".pyd", ".so", ".dll", ".db", ".sqlite"}

# Marker files that identify a directory by content rather than name.
_REPO_MARKER = ".git"
# `pyvenv.cfg` = Python virtualenv; `.package-lock.json` / `.modules.yaml` =
# an npm/pnpm installation tree (npm ≥7 and pnpm write these INSIDE
# node_modules, so a renamed or vendored one still carries its marker).
_INSTALL_TREE_MARKERS = frozenset({"pyvenv.cfg", ".package-lock.json", ".modules.yaml"})

# The label an embedded repository's single index entry carries. The map shows
# the directory exists; its tens of thousands of files stay out of the index.
EXTERNAL_REPO_LABEL = "external repository (contents not indexed)"


def _file_ignored(rel: str, name: str, patterns: Sequence[str]) -> bool:
    for pattern in patterns:
        if pattern.endswith("/"):
            continue
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern):
            return True
    return False


def _dir_ignored(rel: str, name: str, patterns: Sequence[str]) -> bool:
    for pattern in patterns:
        trimmed = pattern.rstrip("/")
        if not trimmed:
            continue
        if fnmatch.fnmatch(name, trimmed) or fnmatch.fnmatch(rel, trimmed):
            return True
    return False


def iter_workspace(
    root: Path, ignore_patterns: Sequence[str] = ()
) -> Iterator[tuple[str, str, os.stat_result | None]]:
    """Yield ``(kind, rel_path, stat)`` for the prunable view of a workspace.

    ``kind`` is ``"file"`` (``stat`` populated, from the directory read — no
    extra syscall per file on Linux) or ``"repo"`` (an embedded repository,
    ``rel_path`` ends with ``/``, ``stat`` is ``None``). Directories that are
    skipped are never descended into — that, not the filtering, is the point.
    Unreadable entries are skipped silently; a scan must never fail a boot.
    """
    stack: list[tuple[str, str]] = [(str(root), "")]
    while stack:
        directory, rel_prefix = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            continue
        names = {entry.name for entry in entries}
        if rel_prefix and _REPO_MARKER in names:
            yield ("repo", rel_prefix + "/", None)
            continue
        if not names.isdisjoint(_INSTALL_TREE_MARKERS):
            continue
        for entry in entries:
            rel = f"{rel_prefix}/{entry.name}" if rel_prefix else entry.name
            try:
                # follow_symlinks=False for the descend decision: a symlinked
                # directory could loop the walk (or pull a foreign tree in).
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in SKIP_DIR_NAMES:
                        continue
                    if ignore_patterns and _dir_ignored(rel, entry.name, ignore_patterns):
                        continue
                    stack.append((entry.path, rel))
                    continue
                if not entry.is_file():
                    continue
                dot = entry.name.rfind(".")
                if dot > 0 and entry.name[dot:] in SKIP_EXTENSIONS:
                    continue
                if ignore_patterns and _file_ignored(rel, entry.name, ignore_patterns):
                    continue
                yield ("file", rel, entry.stat())
            except OSError:
                continue
