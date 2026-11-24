"""Best-effort LangGraph checkpoint migrations.

Two independent migrations live here (both opt-in, both dry-run-able):

1. :func:`migrate_sqlite_to_postgres` — copy the latest checkpoint of every
   ``(thread_id, checkpoint_ns)`` partition from a legacy ``threads.db`` into the
   configured Postgres store, so conversations stay resumable after switching
   ``LIONCLAW_CHECKPOINT_BACKEND`` to ``postgres``. Both savers take their serde
   from ``core/checkpoints.checkpoint_serde_kwargs()``: ``JsonPlusSerializer``,
   wrapped in AES when ``LANGGRAPH_AES_KEY`` is set. The two ends differ in one
   respect: the source is built permissive (``strict=False``), because a legacy
   ``threads.db`` holds plaintext rows and the strict serde of a box that sets
   ``LIONCLAW_CHECKPOINT_REQUIRE_ENCRYPTED`` would refuse the very rows being
   moved, while the destination follows the box's mode. Every row is decoded by
   the source saver and re-encoded by the destination, never copied as raw
   bytes, so a plaintext legacy row lands encrypted when a key is set.

2. :func:`scope_checkpoints_by_agent` — rewrite legacy *unscoped* checkpoint keys to
   the agent-scoped ``<agent>::<thread_id>`` form the runtime now uses, so two agents
   the same human talked to never share a lineage (see the section comment above the
   function for the ownership → action mapping).

Scope (deliberately "best-effort", per the design): only the *latest* checkpoint
per partition is copied — enough to resume the model's context. Intermediate
checkpoint history (used for fine-grained per-call cost attribution in threadcost)
is NOT replayed; that history stays readable by pointing the backend back at the
legacy SQLite file. The project is pre-release, so no online/auto migration is run.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class MigrationResult:
    threads: int = 0
    checkpoints: int = 0
    failed_threads: int = 0
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False

    def summary(self) -> str:
        verb = "Would copy" if self.dry_run else "Copied"
        line = (
            f"{verb} {self.checkpoints} checkpoint(s) across {self.threads} thread(s)"
            f" ({self.failed_threads} failed)."
        )
        return line


def migrate_sqlite_to_postgres(
    *,
    source_db: Path,
    dest_url: str,
    dry_run: bool = False,
    log: Callable[[str], None] = logger.info,
) -> MigrationResult:
    """Copy latest checkpoints from ``source_db`` (SQLite) into ``dest_url`` (Postgres).

    Idempotent: re-running upserts by ``(thread_id, checkpoint_ns, checkpoint_id)``.
    A thread that fails is logged and skipped — never aborts the whole run.
    """
    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.checkpoint.sqlite import SqliteSaver

    from olano.core.checkpoints import checkpoint_serde_kwargs

    result = MigrationResult(dry_run=dry_run)
    # The SOURCE reads permissively on purpose: a legacy threads.db predates the
    # key, so its rows are plaintext, and the strict serde a sealed box runs
    # (LIONCLAW_CHECKPOINT_REQUIRE_ENCRYPTED) would refuse the very rows this
    # exists to move. The DESTINATION follows the box's mode, so every row it
    # writes is encrypted where the box requires that, and a box that claims
    # strictness with no key fails here, before anything is copied.
    src_serde_kwargs = checkpoint_serde_kwargs(strict=False)
    dest_serde_kwargs = checkpoint_serde_kwargs()

    if not source_db.exists():
        log(f"Source checkpoint DB not found: {source_db} — nothing to migrate.")
        return result

    src_conn = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True, check_same_thread=False)
    src = SqliteSaver(src_conn, **src_serde_kwargs)
    try:
        thread_ids = [
            r[0]
            for r in src_conn.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()
            if r[0]
        ]
        log(f"Found {len(thread_ids)} thread(s) in {source_db}.")

        dest_conn = psycopg.connect(dest_url, autocommit=True)
        dest = PostgresSaver(dest_conn, **dest_serde_kwargs)  # type: ignore[arg-type]
        try:
            if not dry_run:
                dest.setup()
            for tid in thread_ids:
                try:
                    result.checkpoints += _copy_thread(src, src_conn, dest, tid, dry_run)
                    result.threads += 1
                except Exception as exc:  # never let one thread abort the run
                    result.failed_threads += 1
                    msg = f"thread {tid}: {exc}"
                    result.errors.append(msg)
                    log(f"  ! skipped {msg}")
        finally:
            dest_conn.close()
    finally:
        src_conn.close()

    log(result.summary())
    return result


def _copy_thread(
    src: Any, src_conn: sqlite3.Connection, dest: Any, thread_id: str, dry_run: bool
) -> int:
    """Copy the latest checkpoint of each namespace for one thread. Returns count."""
    namespaces = [
        r[0]
        for r in src_conn.execute(
            "SELECT DISTINCT checkpoint_ns FROM checkpoints WHERE thread_id = ?",
            (thread_id,),
        ).fetchall()
    ]
    copied = 0
    for ns in namespaces:
        cfg: Any = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns}}
        tup = src.get_tuple(cfg)
        if not tup or not tup.checkpoint:
            continue
        copied += 1
        if dry_run:
            continue
        new_versions = tup.checkpoint.get("channel_versions") or {}
        dest.put(tup.config, tup.checkpoint, tup.metadata or {}, new_versions)
        # Copy any pending (uncommitted) writes, grouped by task, best-effort.
        if tup.pending_writes:
            by_task: dict[str, list[tuple[str, Any]]] = {}
            for task_id, channel, value in tup.pending_writes:
                by_task.setdefault(task_id, []).append((channel, value))
            for task_id, writes in by_task.items():
                try:
                    dest.put_writes(tup.config, writes, task_id)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug("put_writes failed for %s/%s: %s", thread_id, ns, exc)
    return copied


# ───────────────────── agent-scoping migration (SQLite, in place) ─────────────
#
# Legacy ``threads.db`` files key checkpoint rows by a *human-derived* thread id
# (Telegram chat id, dashboard user, …) with no agent component, so two agents the
# same human talked to shared one lineage and bled identity into each other. The
# runtime now scopes every checkpoint key as ``<agent>::<thread_id>`` (see
# ``core/checkpoints.scope_thread``); this rewrites existing rows to the scoped
# form so legitimate history stays resumable. Ownership is recovered from the
# per-agent transcript dirs ``sessions/<agent>/<stem>.jsonl``:
#   • stem owned by exactly ONE agent  → rename rows to ``<agent>::<stem>``;
#   • stem owned by ≥2 agents (contaminated, e.g. a shared chat id) → RESET
#     (drop rows) — the single blob interleaves both agents and cannot be split,
#     so each agent then rehydrates cleanly from its own JSONL;
#   • already-scoped (``<known-agent>::…``) and cognition (``cog::``/``sub::``)
#     ids are left untouched (idempotent re-runs, cognition owns its own scheme);
#   • orphan (no owning transcript) is left alone unless ``drop_orphans=True``.

_COGNITION_PREFIXES = ("cog::", "sub::")


@dataclass
class ScopeMigrationResult:
    renamed: int = 0
    reset_contaminated: int = 0
    dropped_orphans: int = 0
    superseded: int = 0  # legacy dropped because a fresh scoped row already exists
    left_scoped: int = 0
    left_cognition: int = 0
    left_orphans: int = 0
    dry_run: bool = False
    details: list[str] = field(default_factory=list)

    def summary(self) -> str:
        verb = "Would" if self.dry_run else "Did"
        return (
            f"{verb}: rename {self.renamed}, reset {self.reset_contaminated} contaminated, "
            f"drop {self.dropped_orphans} orphan(s), supersede {self.superseded} legacy; "
            f"left {self.left_scoped} scoped + {self.left_cognition} cognition + "
            f"{self.left_orphans} orphan(s) untouched."
        )


def _thread_keyed_tables(conn: sqlite3.Connection) -> list[str]:
    """Every table in the checkpoint DB that has a ``thread_id`` column.

    Enumerated (rather than hardcoded) so the rewrite stays correct across
    langgraph sqlite-saver schema versions (``checkpoints`` + ``writes`` today;
    older/newer versions may add ``checkpoint_blobs``).
    """
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    keyed: list[str] = []
    for t in tables:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
        if "thread_id" in cols:
            keyed.append(t)
    return keyed


def _build_stem_owners(sessions_dir: Path) -> tuple[dict[str, set[str]], set[str]]:
    """``{jsonl_stem: {owning agents}}`` plus the set of known agent names."""
    stem_owners: dict[str, set[str]] = {}
    agents: set[str] = set()
    if not sessions_dir.exists():
        return stem_owners, agents
    for agent_dir in sorted(sessions_dir.iterdir()):
        if not agent_dir.is_dir():
            continue
        agent = agent_dir.name
        agents.add(agent)
        for f in agent_dir.glob("*.jsonl"):
            stem_owners.setdefault(f.stem, set()).add(agent)
    return stem_owners, agents


def scope_checkpoints_by_agent(
    *,
    db_path: Path,
    sessions_dir: Path,
    dry_run: bool = False,
    drop_orphans: bool = False,
    log: Callable[[str], None] = logger.info,
) -> ScopeMigrationResult:
    """Rewrite legacy unscoped checkpoint keys to ``<agent>::<thread_id>`` in place.

    Idempotent: already-scoped and cognition threads are skipped, so re-running is
    safe. See the module comment above for the full ownership → action mapping.
    """
    from olano.core.checkpoints import scope_thread

    result = ScopeMigrationResult(dry_run=dry_run)
    if not db_path.exists():
        log(f"Checkpoint DB not found: {db_path} — nothing to scope.")
        return result

    stem_owners, agents = _build_stem_owners(sessions_dir)
    conn = sqlite3.connect(db_path)
    try:
        tables = _thread_keyed_tables(conn)
        if "checkpoints" not in tables:
            log("No 'checkpoints' table — not a langgraph checkpoint DB; skipping.")
            return result
        tids = [
            r[0]
            for r in conn.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()
            if r[0]
        ]

        def _delete(tid: str) -> None:
            if dry_run:
                return
            for t in tables:
                conn.execute(f"DELETE FROM {t} WHERE thread_id = ?", (tid,))

        def _rename(tid: str, target: str) -> None:
            if dry_run:
                return
            for t in tables:
                conn.execute(
                    f"UPDATE {t} SET thread_id = ? WHERE thread_id = ?",
                    (target, tid),
                )

        for tid in tids:
            # Already scoped by a known agent, or a cognition-owned thread → leave.
            if any(tid.startswith(f"{a}{'::'}") for a in agents):
                result.left_scoped += 1
                continue
            if tid.startswith(_COGNITION_PREFIXES):
                result.left_cognition += 1
                continue

            owners = stem_owners.get(tid, set())
            if len(owners) == 1:
                agent = next(iter(owners))
                target = scope_thread(agent, tid)
                already = conn.execute(
                    "SELECT 1 FROM checkpoints WHERE thread_id = ? LIMIT 1", (target,)
                ).fetchone()
                if already:
                    # A fresh scoped lineage already exists (fix deployed and ran a
                    # turn) — it supersedes the legacy rows; drop the stragglers.
                    _delete(tid)
                    result.superseded += 1
                    result.details.append(f"supersede legacy {tid} (→ {target} exists)")
                else:
                    _rename(tid, target)
                    result.renamed += 1
                    result.details.append(f"rename {tid} → {target}")
            elif len(owners) >= 2:
                _delete(tid)
                result.reset_contaminated += 1
                result.details.append(f"reset contaminated {tid} (agents: {sorted(owners)})")
            elif drop_orphans:
                _delete(tid)
                result.dropped_orphans += 1
                result.details.append(f"drop orphan {tid}")
            else:
                result.left_orphans += 1

        if not dry_run:
            conn.commit()
            # Reclaim space from deletes/renames so the shared DB doesn't bloat.
            try:
                conn.execute("VACUUM")
            except sqlite3.OperationalError as exc:  # pragma: no cover - defensive
                logger.debug("VACUUM skipped: %s", exc)
    finally:
        conn.close()

    log(result.summary())
    return result
