"""Generic SQL Database toolkit via SQLAlchemy.

Auth: LIONCLAW_SQL_DSN env var. Examples:
  - postgresql+psycopg://user:pass@host:5432/db
  - mysql+pymysql://user:pass@host:3306/db
  - sqlite:///path/to/file.db
  - snowflake://user:pass@account/db/schema?warehouse=wh

All tools are READ-ONLY (only SELECT/EXPLAIN allowed). Writes deliberately
blocked because olano agents share the same DSN and one bad UPDATE could
corrupt the dataset.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated, Any

from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._install_hint import optional_import
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)

_SQL_DSN_MSG = (
    "[missing-credentials] LIONCLAW_SQL_DSN not set in vault. Provide a SQLAlchemy "
    "DSN, e.g. 'postgresql+psycopg://user:pass@host/db' or 'sqlite:///path.db'."
)

_WRITE_KEYWORDS = re.compile(
    r"\b(insert|update|delete|drop|truncate|alter|create|grant|revoke|merge|replace|"
    r"call|copy|attach|detach)\b",
    re.IGNORECASE,
)


def _engine() -> tuple[Any, str | None]:
    """Return (engine, error_string). One is always None."""
    sa, err = optional_import("sqlalchemy", purpose="SQL database access")
    if err:
        return None, err
    dsn = get_credential("LIONCLAW_SQL_DSN")
    if not dsn:
        return None, _SQL_DSN_MSG
    assert sa is not None  # narrowed: err is None ⇒ import succeeded
    try:
        engine = sa.create_engine(dsn, pool_pre_ping=True, future=True)
        return engine, None
    except Exception as exc:
        return None, f"[tool-error] sql: engine init failed: {exc}"


def _is_read_only(stmt: str) -> bool:
    s = stmt.strip().lower().rstrip(";")
    if not s:
        return False
    # Multiple statements blocked
    if ";" in s:
        return False
    # Allow SELECT, WITH (CTE), EXPLAIN, SHOW, DESCRIBE
    if not re.match(r"^(select|with|explain|show|describe|desc|pragma)\b", s):
        return False
    return not _WRITE_KEYWORDS.search(s)


@tool
def sql_list_tables(
    schema_name: Annotated[str, "Schema name (empty = default)"] = "",
) -> str:
    """List all tables/views in the configured database."""
    engine, err = _engine()
    if err:
        return err
    try:
        sa, _ = optional_import("sqlalchemy", purpose="SQL database access")
        assert sa is not None  # reached only after _engine() imported sqlalchemy
        inspector = sa.inspect(engine)
        schema_arg = schema_name or None
        tables = sorted(inspector.get_table_names(schema=schema_arg))
        views = sorted(inspector.get_view_names(schema=schema_arg))
        lines = [
            f"Database: {engine.url.database}  driver: {engine.url.drivername}",
            f"Tables ({len(tables)}):",
        ]
        lines.extend(f"  - {t}" for t in tables)
        if views:
            lines.append(f"Views ({len(views)}):")
            lines.extend(f"  - {v}" for v in views)
        return "\n".join(lines) or "(empty database)"
    except Exception as exc:
        return f"[tool-error] sql_list_tables: {exc}"


@tool
def sql_describe_table(
    table_name: Annotated[str, "Table name"],
    schema_name: Annotated[str, "Schema name (empty = default)"] = "",
) -> str:
    """Describe a table's columns, types, primary key, and indexes."""
    engine, err = _engine()
    if err:
        return err
    try:
        sa, _ = optional_import("sqlalchemy", purpose="SQL database access")
        assert sa is not None  # reached only after _engine() imported sqlalchemy
        inspector = sa.inspect(engine)
        schema_arg = schema_name or None
        cols = inspector.get_columns(table_name, schema=schema_arg)
        pk = inspector.get_pk_constraint(table_name, schema=schema_arg)
        indexes = inspector.get_indexes(table_name, schema=schema_arg)
        fks = inspector.get_foreign_keys(table_name, schema=schema_arg)

        lines = [f"Table: {table_name}"]
        lines.append(f"  {'Column':<30} {'Type':<20} {'Null':<6} Default")
        for c in cols:
            nullable = "yes" if c.get("nullable") else "no"
            default = c.get("default")
            lines.append(
                f"  {c.get('name', '?'):<30} {c.get('type', '?')!s:<20} "
                f"{nullable:<6} {default if default is not None else ''}"
            )
        pk_cols = (pk or {}).get("constrained_columns") or []
        if pk_cols:
            lines.append(f"Primary key: {', '.join(pk_cols)}")
        if indexes:
            lines.append("Indexes:")
            for i in indexes:
                cols_str = ", ".join(i.get("column_names") or [])
                uniq = " UNIQUE" if i.get("unique") else ""
                lines.append(f"  - {i.get('name', '?')}({cols_str}){uniq}")
        if fks:
            lines.append("Foreign keys:")
            for fk in fks:
                lc = ", ".join(fk.get("constrained_columns") or [])
                ref = fk.get("referred_table", "?")
                rc = ", ".join(fk.get("referred_columns") or [])
                lines.append(f"  - ({lc}) -> {ref}({rc})")
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] sql_describe_table({table_name}): {exc}"


@tool
def sql_query(
    statement: Annotated[str, "Read-only SQL (SELECT/WITH/EXPLAIN/SHOW/DESCRIBE only)"],
    max_rows: Annotated[int, "Hard cap on rows returned (default 100)"] = 100,
) -> str:
    """Execute a read-only SQL statement and return rows as a formatted table.

    Blocks any statement that contains INSERT/UPDATE/DELETE/DDL or multiple
    statements separated by semicolons.
    """
    if not _is_read_only(statement):
        return (
            "[tool-error] sql_query: only single read-only statements are permitted "
            "(SELECT, WITH, EXPLAIN, SHOW, DESCRIBE)."
        )
    engine, err = _engine()
    if err:
        return err
    sa, _ = optional_import("sqlalchemy", purpose="SQL database access")
    assert sa is not None  # reached only after _engine() imported sqlalchemy
    try:
        max_rows = max(1, min(max_rows, 10000))
        with engine.connect() as conn:
            result = conn.execute(sa.text(statement))
            cols = list(result.keys())
            rows = result.fetchmany(max_rows)
            extra = result.fetchone()
        if not rows:
            return "(no rows)"
        # Format
        widths = [len(c) for c in cols]
        for r in rows:
            for i, v in enumerate(r):
                widths[i] = max(widths[i], len(str(v)) if v is not None else 4)
        widths = [min(w, 60) for w in widths]
        sep = "  ".join("-" * w for w in widths)
        header = "  ".join(c.ljust(w) for c, w in zip(cols, widths, strict=False))
        body = "\n".join(
            "  ".join(
                (str(v) if v is not None else "NULL")[:w].ljust(w)
                for v, w in zip(row, widths, strict=False)
            )
            for row in rows
        )
        suffix = ""
        if extra is not None:
            suffix = f"\n... truncated at {max_rows} rows."
        return f"{header}\n{sep}\n{body}\n({len(rows)} row{'s' if len(rows) != 1 else ''}){suffix}"
    except Exception as exc:
        return f"[tool-error] sql_query: {exc}"


@tool
def sql_explain(
    statement: Annotated[str, "SQL statement to explain (SELECT/WITH only)"],
) -> str:
    """Get the database engine's execution plan for a SELECT/WITH statement."""
    s = statement.strip().lower()
    if not re.match(r"^(select|with)\b", s):
        return "[tool-error] sql_explain: only SELECT / WITH statements supported."
    if not _is_read_only(statement):
        return "[tool-error] sql_explain: statement is not read-only."
    engine, err = _engine()
    if err:
        return err
    sa, _ = optional_import("sqlalchemy", purpose="SQL database access")
    assert sa is not None  # reached only after _engine() imported sqlalchemy
    try:
        with engine.connect() as conn:
            result = conn.execute(sa.text(f"EXPLAIN {statement}"))
            rows = result.fetchall()
        if not rows:
            return "(empty plan)"
        return "\n".join(" ".join(str(c) for c in row) for row in rows)
    except Exception as exc:
        return f"[tool-error] sql_explain: {exc}"


get_tools, get_tool = make_toolkit_api(globals())
