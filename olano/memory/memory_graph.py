"""Entity-relationship memory graph backed by SQLite."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import cast

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,          -- person | place | project | concept | tool | org
    description TEXT DEFAULT '',
    confidence REAL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata TEXT DEFAULT '{}'   -- JSON blob
);

CREATE TABLE IF NOT EXISTS relationships (
    id TEXT PRIMARY KEY,
    from_entity TEXT NOT NULL,
    to_entity TEXT NOT NULL,
    relation TEXT NOT NULL,      -- e.g. "works_on", "knows", "uses", "is_a"
    confidence REAL DEFAULT 1.0,
    source TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    metadata TEXT DEFAULT '{}',
    FOREIGN KEY (from_entity) REFERENCES entities(id),
    FOREIGN KEY (to_entity) REFERENCES entities(id)
);

CREATE INDEX IF NOT EXISTS idx_rel_from ON relationships(from_entity);
CREATE INDEX IF NOT EXISTS idx_rel_to ON relationships(to_entity);
CREATE INDEX IF NOT EXISTS idx_entity_name ON entities(name);
CREATE INDEX IF NOT EXISTS idx_entity_type ON entities(type);
"""


@dataclass
class Entity:
    id: str
    name: str
    type: str
    description: str = ""
    confidence: float = 1.0
    created_at: str = ""
    updated_at: str = ""
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.utcnow().isoformat()
        if not self.updated_at:
            self.updated_at = self.created_at


@dataclass
class Relationship:
    id: str
    from_entity: str
    to_entity: str
    relation: str
    confidence: float = 1.0
    source: str = ""
    created_at: str = ""
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.utcnow().isoformat()


class MemoryGraph:
    """SQLite-backed entity-relationship graph for relational memory."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    # ──────────────────────────────────── Entities ────────────────────────────

    def upsert_entity(
        self,
        name: str,
        entity_type: str,
        description: str = "",
        confidence: float = 1.0,
        metadata: dict | None = None,
    ) -> str:
        """Upsert an entity by name+type. Returns entity ID."""
        import uuid

        with self._conn() as conn:
            row = conn.execute(
                "SELECT id FROM entities WHERE name = ? AND type = ?", (name, entity_type)
            ).fetchone()

            now = datetime.utcnow().isoformat()
            if row:
                entity_id = row["id"]
                conn.execute(
                    "UPDATE entities SET description=?, confidence=?, updated_at=?, metadata=? WHERE id=?",
                    (description, confidence, now, json.dumps(metadata or {}), entity_id),
                )
            else:
                entity_id = str(uuid.uuid4())[:12]
                conn.execute(
                    "INSERT INTO entities VALUES (?,?,?,?,?,?,?,?)",
                    (
                        entity_id,
                        name,
                        entity_type,
                        description,
                        confidence,
                        now,
                        now,
                        json.dumps(metadata or {}),
                    ),
                )
        return cast(str, entity_id)

    def get_entity(self, entity_id: str) -> Entity | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
            return self._row_to_entity(row) if row else None

    def find_entities(
        self, name: str | None = None, entity_type: str | None = None
    ) -> list[Entity]:
        parts = []
        params = []
        if name:
            parts.append("name LIKE ?")
            params.append(f"%{name}%")
        if entity_type:
            parts.append("type = ?")
            params.append(entity_type)
        where = ("WHERE " + " AND ".join(parts)) if parts else ""
        with self._conn() as conn:
            rows = conn.execute(f"SELECT * FROM entities {where} LIMIT 50", params).fetchall()
            return [self._row_to_entity(r) for r in rows]

    # ──────────────────────────────────── Relationships ──────────────────────

    def add_relationship(
        self,
        from_entity_id: str,
        to_entity_id: str,
        relation: str,
        confidence: float = 1.0,
        source: str = "",
        metadata: dict | None = None,
    ) -> str:
        import uuid

        with self._conn() as conn:
            # Avoid duplicate relationships
            row = conn.execute(
                "SELECT id FROM relationships WHERE from_entity=? AND to_entity=? AND relation=?",
                (from_entity_id, to_entity_id, relation),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE relationships SET confidence=? WHERE id=?", (confidence, row["id"])
                )
                return cast(str, row["id"])

            rel_id = str(uuid.uuid4())[:12]
            conn.execute(
                "INSERT INTO relationships VALUES (?,?,?,?,?,?,?,?)",
                (
                    rel_id,
                    from_entity_id,
                    to_entity_id,
                    relation,
                    confidence,
                    source,
                    datetime.utcnow().isoformat(),
                    json.dumps(metadata or {}),
                ),
            )
        return rel_id

    def get_neighbors(
        self, entity_id: str, relation: str | None = None, direction: str = "both"
    ) -> list[dict]:
        """Return neighboring entities and relationships."""
        results: list[dict] = []
        with self._conn() as conn:
            if direction in ("out", "both"):
                q = "SELECT * FROM relationships WHERE from_entity=?"
                params = [entity_id]
                if relation:
                    q += " AND relation=?"
                    params.append(relation)
                for row in conn.execute(q, params).fetchall():
                    entity = self.get_entity(row["to_entity"])
                    if entity:
                        results.append(
                            {"direction": "out", "relation": row["relation"], "entity": entity}
                        )

            if direction in ("in", "both"):
                q = "SELECT * FROM relationships WHERE to_entity=?"
                params = [entity_id]
                if relation:
                    q += " AND relation=?"
                    params.append(relation)
                for row in conn.execute(q, params).fetchall():
                    entity = self.get_entity(row["from_entity"])
                    if entity:
                        results.append(
                            {"direction": "in", "relation": row["relation"], "entity": entity}
                        )

        return results

    def entity_count(self) -> int:
        with self._conn() as conn:
            return cast(int, conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0])

    def relationship_count(self) -> int:
        with self._conn() as conn:
            return cast(int, conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[0])

    def all_entities(self, limit: int = 200) -> list[Entity]:
        """Return all entities, ordered by confidence descending."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM entities ORDER BY confidence DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._row_to_entity(r) for r in rows]

    def to_memory_md_section(self, max_entities: int = 50) -> str:
        """Render graph entities as a MEMORY.md section string.

        Returns the section including the H2 heading so callers can splice
        it directly into MEMORY.md.  If the graph is empty, returns an empty
        string so callers can skip the section entirely.
        """
        entities = self.all_entities(limit=max_entities)
        if not entities:
            return ""

        by_type: dict[str, list[Entity]] = {}
        for entity in entities:
            by_type.setdefault(entity.type, []).append(entity)

        lines = ["## Knowledge Graph\n"]
        lines.append("<!-- auto-generated by olano graph sync — do not edit this section -->\n")
        for etype, group in sorted(by_type.items()):
            lines.append(f"\n### {etype.capitalize()}s\n")
            for e in group:
                desc = f" — {e.description}" if e.description else ""
                lines.append(f"- **{e.name}**{desc}\n")
        return "".join(lines)

    @staticmethod
    def _row_to_entity(row: sqlite3.Row) -> Entity:
        return Entity(
            id=row["id"],
            name=row["name"],
            type=row["type"],
            description=row["description"],
            confidence=row["confidence"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            metadata=json.loads(row["metadata"] or "{}"),
        )
