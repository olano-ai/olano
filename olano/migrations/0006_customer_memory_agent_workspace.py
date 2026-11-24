"""Relocate per-customer memory into the agent's own workspace.

Customer-service agents used to keep each customer's private memory under a
home-level tree keyed by agent name::

    <home>/customers/<agent>/<user_id>/

which left the data orphaned whenever the agent was deleted (``delete_agent``
rmtree's ``<home>/agents/<name>/`` and never knew about the sibling tree), and
meant backing up or moving an agent silently dropped its customer history. The
store now lives with the agent it belongs to::

    <home>/agents/<agent>/workspace/customers/<user_id>/

This migration moves every legacy customer directory to the new location on the
first boot after the upgrade, so an existing support bot keeps recognising the
people it has already talked to instead of starting from an empty memory.

Idempotent and never destructive:

* A customer directory whose destination already exists is left where it is and
  logged — two stores for one customer are an operator decision (a partial
  hand-migration, a restored backup), not something to silently merge or
  clobber.
* Only directories are moved; a stray *file* under ``<home>/customers/<agent>/``
  was never written by this feature, so it is reported and left alone.
* Empty legacy directories are pruned afterwards, so a fully migrated home ends
  up with no ``<home>/customers/`` at all and a re-run returns immediately.
* Per-directory failures (e.g. a cross-device move that dies half way) are
  logged with both paths and do not abort the rest of the pass: a boot must not
  be blocked by one unreadable customer folder, and the legacy copy stays
  readable at its old path either way.

Agents that no longer exist are migrated too — the destination workspace is
created as needed. A bare ``<home>/agents/<name>/`` directory carries no
``agent.json5``, so it stays invisible to agent discovery and does not block
creating an agent by that name later.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

ID = "0006_customer_memory_agent_workspace"

_CUSTOMERS_DIR = "customers"


def _new_base(home: Path, agent: str) -> Path:
    """``<home>/agents/<agent>/workspace/customers`` — mirrors
    :func:`olano.memory.customer_memory.customers_base`, but derived from the
    ``home`` the runner passes in rather than the ambient env."""
    return home / "agents" / agent / "workspace" / _CUSTOMERS_DIR


def _prune_if_empty(directory: Path) -> None:
    try:
        next(directory.iterdir())
    except StopIteration:
        directory.rmdir()
    except OSError:
        pass


def apply(home: Path) -> None:
    legacy_root = home / _CUSTOMERS_DIR
    if not legacy_root.is_dir():
        return

    try:
        agent_dirs = sorted(p for p in legacy_root.iterdir() if p.is_dir())
    except OSError as exc:
        logger.warning("%s: cannot list %s: %s", ID, legacy_root, exc)
        return

    for legacy_agent_dir in agent_dirs:
        agent = legacy_agent_dir.name
        dest_base = _new_base(home, agent)
        moved = 0
        try:
            entries = sorted(legacy_agent_dir.iterdir())
        except OSError as exc:
            logger.warning("%s: cannot list %s: %s", ID, legacy_agent_dir, exc)
            continue
        for entry in entries:
            if not entry.is_dir():
                logger.warning(
                    "%s: leaving unexpected file %s in place (not a customer store)", ID, entry
                )
                continue
            dest = dest_base / entry.name
            if dest.exists():
                logger.warning(
                    "%s: %s already exists — leaving the legacy copy at %s for review",
                    ID,
                    dest,
                    entry,
                )
                continue
            try:
                dest_base.mkdir(parents=True, exist_ok=True)
                shutil.move(str(entry), str(dest))
            except Exception as exc:
                logger.error("%s: could not move %s → %s: %s", ID, entry, dest, exc)
                continue
            moved += 1
        if moved:
            logger.info(
                "%s: moved %d customer store(s) for %s into %s", ID, moved, agent, dest_base
            )
        _prune_if_empty(legacy_agent_dir)

    _prune_if_empty(legacy_root)
