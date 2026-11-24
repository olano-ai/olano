"""
Migration runner for Olano home state.

Each migration is a module at olano/migrations/NNNN_<description>.py
exposing:
  ID: str = "NNNN_<description>"
  def apply(home: Path) -> None: ...

apply() must be idempotent (safe to call twice).

A migration may also expose:
  def applicable() -> bool: ...

returning False when the migration has nothing to do *yet* on this box — not
"done", but "does not apply here at this time". Such a migration is neither run
nor recorded as applied, so it stays eligible for the boot where the condition
flips. The case this exists for: 0011 moves archetype agents onto the Olano
tiers only on a managed box, and a first boot that raced the deployment
manifest resolved as unmanaged — recording the skip as "applied" stranded those
agents on the BYOK default forever. An `applicable` gate must be CHEAP (it runs
on every boot's pending check) and total (an exception counts as applicable, so
the migration's own guards get the final say).
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path


def _load_migrations() -> list:
    """Discover and return all migration modules in ascending ID order."""
    migrations = []
    for info in pkgutil.iter_modules(__path__):  # type: ignore[name-defined]
        mod = importlib.import_module(f"olano.migrations.{info.name}")
        if hasattr(mod, "ID") and hasattr(mod, "apply"):
            migrations.append(mod)
    return sorted(migrations, key=lambda m: m.ID)


def _is_applicable(migration) -> bool:
    """The module's `applicable()` verdict; missing or raising means True."""
    gate = getattr(migration, "applicable", None)
    if gate is None:
        return True
    try:
        return bool(gate())
    except Exception:  # pragma: no cover - defensive: apply()'s guards decide
        return True


def pending_migration_ids(applied: list[str]) -> list[str]:
    """IDs of discovered migrations not yet in ``applied`` (discovery only).

    Excludes migrations whose ``applicable()`` is currently False: they are not
    runnable work, and counting them as pending would defeat the boot-time
    fast path forever on boxes where the condition never flips.
    """
    return [m.ID for m in _load_migrations() if m.ID not in applied and _is_applicable(m)]


def run_pending_migrations(home: Path, applied: list[str]) -> list[str]:
    """
    Run all migrations whose ID is not in `applied`.

    Returns the list of newly applied IDs. A migration whose ``applicable()``
    is False is skipped WITHOUT being recorded — it runs on a later boot when
    it starts applying to this box.
    """
    newly_applied: list[str] = []
    for migration in _load_migrations():
        if migration.ID not in applied and _is_applicable(migration):
            migration.apply(home)
            newly_applied.append(migration.ID)
    return newly_applied
