"""Derived SQLite cache of per-thread token/cost aggregates.

Mining the multi-GB checkpoint store on every Insights request is too slow, so we
keep a small ``threadcost.db`` of per-thread rollups keyed by ``thread_id`` and
invalidated by the thread's checkpoint ``version`` (count + max checkpoint_id). This
is regenerable/derived data — safe to delete; it rebuilds lazily.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from olano.config.home import get_olano_paths

logger = logging.getLogger(__name__)

# Bump when the row OR model_json shape changes — `connect` drops & rebuilds on
# mismatch (the table is a regenerable cache, so a rebuild just re-warms from
# threads.db). Tracked via PRAGMA user_version so content-shape bumps trigger too.
_SCHEMA_VERSION = 4

_SCHEMA = """
CREATE TABLE IF NOT EXISTS thread_cost (
    thread_id            TEXT PRIMARY KEY,
    agent                TEXT,
    origin               TEXT,
    day                  TEXT,
    last_ts              TEXT,
    input_tokens         INTEGER,
    output_tokens        INTEGER,
    cache_read_tokens    INTEGER,
    total_tokens         INTEGER,
    usd                  REAL,
    call_count           INTEGER,
    subagent_call_count  INTEGER,
    namespaces           INTEGER,
    pricing_complete     INTEGER,
    model_json           TEXT,
    version              TEXT,
    updated_at           TEXT
);
CREATE INDEX IF NOT EXISTS idx_thread_cost_usd   ON thread_cost(usd DESC);
CREATE INDEX IF NOT EXISTS idx_thread_cost_agent ON thread_cost(agent, usd DESC);
CREATE INDEX IF NOT EXISTS idx_thread_cost_origin ON thread_cost(origin);
CREATE INDEX IF NOT EXISTS idx_thread_cost_day ON thread_cost(day);
CREATE INDEX IF NOT EXISTS idx_thread_cost_last_ts ON thread_cost(last_ts);
"""


@dataclass
class CachedThread:
    thread_id: str
    agent: str
    origin: str
    day: str
    # Full last-active ISO timestamp behind `day`. `day` buckets the usage
    # rollup's series; this is what the drill-down's windows compare against, so
    # its "1 day" can be the same rolling 24 h the Overview's is.
    last_ts: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    total_tokens: int
    usd: float
    call_count: int
    subagent_call_count: int
    namespaces: int
    pricing_complete: int
    model_json: str  # JSON {model: {input_tokens, output_tokens, cache_read_tokens, usd}}
    version: str
    updated_at: str = ""

    def as_dict(self) -> dict:
        d = asdict(self)
        d["usd"] = round(self.usd, 6)
        return d


def _migrate_in_place(conn: sqlite3.Connection, from_version: int) -> bool:
    """Upgrade an existing ``thread_cost`` table without losing it, or False.

    Dropping is always *correct* — the table is derived — but it refills at
    ``service._WARM_CAP`` threads per request, so on a fleet with thousands of
    threads the drill-down reads near-zero until someone browses it back. A
    purely additive bump is worth migrating instead.

    **3 → 4**: ``last_ts`` (the full last-active timestamp, so the drill-down's
    "1 day" can be a rolling 24 h). Backfilled from ``day`` at noon UTC — the
    precision those rows were already ranked at — and replaced with the real
    checkpoint timestamp on each thread's next re-mine.
    """
    if from_version != 3:
        return False
    try:
        conn.execute("ALTER TABLE thread_cost ADD COLUMN last_ts TEXT")
        conn.execute(
            "UPDATE thread_cost SET last_ts = day || 'T12:00:00+00:00' "
            "WHERE day IS NOT NULL AND day != ''"
        )
        conn.commit()
    except sqlite3.Error:
        # No table yet, or a shape we can't reason about — fall back to the drop.
        logger.debug("threadcost cache: in-place migration failed, rebuilding", exc_info=True)
        return False
    return True


def connect(home: str | Path | None = None) -> sqlite3.Connection:
    path = get_olano_paths(home).threadcost_db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Drop & rebuild on any schema/content-shape bump the migration above can't
    # absorb (the cache is derived, so a drop just forces a re-warm).
    # user_version also avoids the failure mode where _SCHEMA's new indexes
    # reference columns an old table lacks.
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    if ver != _SCHEMA_VERSION and not _migrate_in_place(conn, ver):
        conn.executescript("DROP TABLE IF EXISTS thread_cost;")
    conn.executescript(_SCHEMA)
    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
    conn.commit()
    return conn


def cached_versions(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT thread_id, version FROM thread_cost").fetchall()
    return {r["thread_id"]: r["version"] for r in rows}


def get_many(conn: sqlite3.Connection, thread_ids: list[str]) -> dict[str, dict]:
    if not thread_ids:
        return {}
    out: dict[str, dict] = {}
    # Chunk to stay under SQLite's variable limit.
    for i in range(0, len(thread_ids), 500):
        chunk = thread_ids[i : i + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT * FROM thread_cost WHERE thread_id IN ({placeholders})", chunk
        ).fetchall()
        for r in rows:
            out[r["thread_id"]] = dict(r)
    return out


def upsert(conn: sqlite3.Connection, row: CachedThread) -> None:
    row.updated_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO thread_cost (
            thread_id, agent, origin, day, last_ts, input_tokens, output_tokens, cache_read_tokens,
            total_tokens, usd, call_count, subagent_call_count, namespaces,
            pricing_complete, model_json, version, updated_at
        ) VALUES (
            :thread_id, :agent, :origin, :day, :last_ts, :input_tokens, :output_tokens,
            :cache_read_tokens,
            :total_tokens, :usd, :call_count, :subagent_call_count, :namespaces,
            :pricing_complete, :model_json, :version, :updated_at
        )
        ON CONFLICT(thread_id) DO UPDATE SET
            agent=excluded.agent, origin=excluded.origin, day=excluded.day,
            last_ts=excluded.last_ts,
            input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens,
            cache_read_tokens=excluded.cache_read_tokens, total_tokens=excluded.total_tokens,
            usd=excluded.usd, call_count=excluded.call_count,
            subagent_call_count=excluded.subagent_call_count, namespaces=excluded.namespaces,
            pricing_complete=excluded.pricing_complete, model_json=excluded.model_json,
            version=excluded.version, updated_at=excluded.updated_at
        """,
        asdict(row),
    )
    conn.commit()


def prune(conn: sqlite3.Connection, keep_ids: set[str]) -> int:
    """Drop cached rows for threads that are no longer candidates (e.g. deleted).

    Keeps the cache aligned with the live session-file set so ranking never shows
    threads that no longer exist.
    """
    existing = [r["thread_id"] for r in conn.execute("SELECT thread_id FROM thread_cost")]
    drop = [t for t in existing if t not in keep_ids]
    for i in range(0, len(drop), 500):
        chunk = drop[i : i + 500]
        placeholders = ",".join("?" * len(chunk))
        conn.execute(f"DELETE FROM thread_cost WHERE thread_id IN ({placeholders})", chunk)
    if drop:
        conn.commit()
    return len(drop)


def since_clause(since: str | None) -> tuple[str, list]:
    """SQL fragment + params restricting rows to threads last active on/after ``since``.

    ``since`` is either a ``YYYY-MM-DD`` day or a full ISO-8601 UTC timestamp;
    both compare correctly against ``last_ts`` lexicographically, which is how
    the drill-down's "1 day" can mean the same rolling 24 h the Overview's does
    while the longer buttons stay whole calendar days. Empty fragment when
    ``since`` is falsy (all history).

    A blank ``last_ts`` — a thread whose checkpoints carry no mineable
    timestamp — is treated as *now*, so such a thread stays visible in every
    window instead of silently dropping out of all of them.
    """
    if not since:
        return "", []
    now = datetime.now(timezone.utc).isoformat()
    return " AND (CASE WHEN last_ts IS NULL OR last_ts = '' THEN ? ELSE last_ts END) >= ?", [
        now,
        since,
    ]


def rank_agents(conn: sqlite3.Connection, *, since: str | None = None) -> list[dict]:
    frag, params = since_clause(since)
    rows = conn.execute(
        f"""
        SELECT agent,
               SUM(usd) AS usd,
               SUM(input_tokens) AS input_tokens,
               SUM(output_tokens) AS output_tokens,
               SUM(cache_read_tokens) AS cache_read_tokens,
               SUM(total_tokens) AS total_tokens,
               SUM(call_count) AS call_count,
               COUNT(*) AS thread_count,
               MIN(pricing_complete) AS pricing_complete
        FROM thread_cost WHERE call_count > 0{frag} GROUP BY agent ORDER BY usd DESC
        """,
        params,
    ).fetchall()
    return [_round_row(dict(r)) for r in rows]


def rank_threads(
    conn: sqlite3.Connection,
    *,
    agent: str | None = None,
    agents: set[str] | None = None,
    origin: str | None = None,
    since: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    where: list[str] = ["call_count > 0"]
    params: list = []
    frag, frag_params = since_clause(since)
    if frag:
        where.append(frag.removeprefix(" AND "))
        params.extend(frag_params)
    if agent:
        where.append("agent = ?")
        params.append(agent)
    # Multi-agent scope (member visibility): constrain the ranking BEFORE the
    # LIMIT so a member's threads aren't dropped by a fleet-wide top-N cap.
    if agents is not None:
        placeholders = ",".join("?" * len(agents)) or "''"
        where.append(f"agent IN ({placeholders})")
        params.extend(sorted(agents))
    if origin == "non_interactive":
        where.append("origin != 'interactive'")
    elif origin and origin != "all":
        where.append("origin = ?")
        params.append(origin)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM thread_cost {clause}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM thread_cost {clause} ORDER BY usd DESC LIMIT ? OFFSET ?",
        [*params, int(limit), int(offset)],
    ).fetchall()
    return [_round_row(dict(r)) for r in rows], int(total)


def origin_split(
    conn: sqlite3.Connection, agents: set[str] | None = None, *, since: str | None = None
) -> list[dict]:
    """Spend split by origin; ``agents`` restricts to those agents' threads
    (multi-user member scoping), ``since`` to a trailing day window."""
    where = "WHERE call_count > 0"
    params: list = []
    if agents is not None:
        placeholders = ",".join("?" * len(agents)) or "''"
        where += f" AND agent IN ({placeholders})"
        params = sorted(agents)
    frag, frag_params = since_clause(since)
    where += frag
    params.extend(frag_params)
    rows = conn.execute(
        f"""
        SELECT origin,
               SUM(usd) AS usd,
               SUM(input_tokens) AS input_tokens,
               SUM(output_tokens) AS output_tokens,
               SUM(total_tokens) AS total_tokens,
               SUM(call_count) AS call_count,
               COUNT(*) AS thread_count
        FROM thread_cost {where} GROUP BY origin ORDER BY usd DESC
        """,
        params,
    ).fetchall()
    return [_round_row(dict(r)) for r in rows]


def agent_origin_split(
    conn: sqlite3.Connection, *, since: str | None = None
) -> dict[str, dict[str, dict]]:
    """Per-agent interactive-vs-non-interactive breakdown → {agent: {origin: {...}}}."""
    frag, params = since_clause(since)
    rows = conn.execute(
        f"""
        SELECT agent, origin, SUM(usd) AS usd, SUM(total_tokens) AS total_tokens,
               SUM(call_count) AS call_count
        FROM thread_cost WHERE call_count > 0{frag} GROUP BY agent, origin
        """,
        params,
    ).fetchall()
    out: dict[str, dict[str, dict]] = {}
    for r in rows:
        out.setdefault(r["agent"], {})[r["origin"]] = _round_row(dict(r))
    return out


def all_rows(conn: sqlite3.Connection) -> list[dict]:
    """Every cached thread row with activity — source for the usage rollup."""
    rows = conn.execute("SELECT * FROM thread_cost WHERE call_count > 0").fetchall()
    return [dict(r) for r in rows]


def _round_row(row: dict) -> dict:
    if row.get("usd") is not None:
        row["usd"] = round(float(row["usd"]), 6)
    return row
