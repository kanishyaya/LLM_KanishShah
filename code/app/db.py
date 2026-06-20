"""
All access to sparkline_demo.db goes through this module.

Two safety measures live here, on top of whatever the SQL validator does:

1. The connection is opened with `mode=ro` in the SQLite URI, which makes the
   OS-level file handle itself read-only -- SQLite will refuse any write
   even if a malicious statement somehow slipped past validation.
2. `PRAGMA query_only = ON` is set on every connection as a second,
   independent enforcement of read-only-ness at the SQLite engine level.

Schema introspection (`get_schema()`) is the single source of truth for:
- the whitelist of tables/columns the SQL validator checks generated SQL against
- the schema description embedded in the LLM prompt
- the table/column hints used by the deterministic stand-in engine
"""
import sqlite3
import threading
from functools import lru_cache
from typing import Any

from app.config import settings


def get_connection() -> sqlite3.Connection:
    """Open a fresh, read-only connection to the demo database."""
    uri = f"file:{settings.DB_PATH}?mode=ro"
    con = sqlite3.connect(uri, uri=True, check_same_thread=False)
    con.execute("PRAGMA query_only = ON;")
    return con


@lru_cache(maxsize=1)
def get_schema() -> dict[str, list[str]]:
    """Return {table_name: [column_name, ...]} for every user table.

    Cached for the lifetime of the process -- the schema of a demo SQLite
    file is not expected to change while the service is running.
    """
    con = get_connection()
    try:
        cur = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
        tables = [r[0] for r in cur.fetchall()]
        schema: dict[str, list[str]] = {}
        for t in tables:
            cols = con.execute(f"PRAGMA table_info('{t}')").fetchall()
            schema[t] = [c[1] for c in cols]
        return schema
    finally:
        con.close()


def get_schema_context() -> str:
    """Render the schema as plain text, suitable for an LLM prompt."""
    schema = get_schema()
    lines = []
    con = get_connection()
    try:
        for table, cols in schema.items():
            col_types = {
                row[1]: row[2]
                for row in con.execute(f"PRAGMA table_info('{table}')").fetchall()
            }
            col_desc = ", ".join(f"{c} {col_types.get(c, '')}".strip() for c in cols)
            lines.append(f"- {table}({col_desc})")

        fks = []
        for table in schema:
            for fk in con.execute(f"PRAGMA foreign_key_list('{table}')").fetchall():
                # fk columns: (id, seq, table, from, to, on_update, on_delete, match)
                fks.append(f"{table}.{fk[3]} -> {fk[2]}.{fk[4]}")
        fk_block = "\nForeign keys:\n" + "\n".join(f"- {f}" for f in fks) if fks else ""
    finally:
        con.close()

    return "Tables:\n" + "\n".join(lines) + fk_block


def execute_query(sql: str) -> tuple[list[str], list[dict[str, Any]]]:
    """Execute an already-validated, read-only SQL string.

    A watchdog timer interrupts the connection if the query runs longer than
    QUERY_TIMEOUT_SECONDS, which protects against accidentally-expensive
    queries (e.g. unintended cartesian-product joins) tying up a worker.
    """
    con = get_connection()
    timer = threading.Timer(settings.QUERY_TIMEOUT_SECONDS, con.interrupt)
    timer.start()
    try:
        cur = con.execute(sql)
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
    finally:
        timer.cancel()
        con.close()

    result = [dict(zip(columns, row)) for row in rows]
    return columns, result
