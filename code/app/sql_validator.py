"""
Treats every incoming SQL string -- whether produced by a real LLM or the
deterministic stand-in -- as untrusted input and puts it through a strict
allow-list pipeline before it ever touches the database.

Layers of defence (deliberately redundant -- any single layer failing
should not be enough to cause harm):

1. Single-statement check        -> reject anything containing a second
                                     statement (stacked-query injection).
2. Comment stripping check       -> reject SQL containing -- or /* */
                                     comments, which are commonly used to
                                     hide or truncate injected statements.
3. Keyword denylist (regex)      -> cheap, fast rejection of obviously
                                     unsafe statements before we even parse.
4. Parse + statement-type check  -> use a real SQL parser (sqlglot) and
                                     require the parsed statement to be a
                                     SELECT. This catches anything the
                                     regex denylist missed (e.g. unusual
                                     spacing/casing/encoding tricks).
5. Table allow-list check        -> every table referenced must exist in
                                     the live schema introspected from the
                                     database (or be a CTE defined within
                                     the same query); this also blocks
                                     attempts to reach sqlite_master,
                                     attached databases, etc.
6. Column allow-list check       -> every column reference must belong to
                                     the table it's qualified with (via
                                     alias resolution), or, if unqualified,
                                     to *some* table used in the query, or
                                     be a SELECT-list output alias (e.g. a
                                     GROUP BY/ORDER BY referencing `AS
                                     revenue`). Catches a hallucinated
                                     column name before it ever reaches
                                     SQLite.
7. Row cap enforcement           -> a LIMIT is injected/clamped so a single
                                     query can never return more than
                                     MAX_ROWS rows.
8. Read-only connection (db.py)  -> defence in depth: even if a write
                                     statement slipped through every layer
                                     above, the connection itself is opened
                                     read-only at the SQLite/OS level.
9. Execution timeout (db.py)     -> a watchdog interrupts long-running
                                     queries.

`tables_used` is derived here, from the parsed SQL itself -- never trusted
from the LLM -- per the assignment's requirement.
"""
import re

import sqlglot
from sqlglot import exp

from app.config import settings

FORBIDDEN_KEYWORDS = {
    "insert", "update", "delete", "drop", "alter", "create", "replace",
    "truncate", "attach", "detach", "pragma", "vacuum", "reindex", "grant",
    "revoke", "begin", "commit", "rollback", "savepoint", "into",
}


class SQLValidationError(Exception):
    """Raised whenever generated SQL fails any safety check."""


def _reject_if_multiple_statements(sql: str) -> str:
    stripped = sql.strip()
    if stripped.endswith(";"):
        stripped = stripped[:-1]
    if ";" in stripped:
        raise SQLValidationError(
            "Only a single SQL statement is allowed (extra ';' detected)."
        )
    return stripped


def _reject_comments(sql: str) -> None:
    if "--" in sql or "/*" in sql or "*/" in sql:
        raise SQLValidationError("SQL comments are not allowed.")


def _reject_forbidden_keywords(sql: str) -> None:
    lowered = sql.lower()
    for kw in FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{kw}\b", lowered):
            raise SQLValidationError(
                f"Query contains a disallowed keyword: '{kw}'. "
                "Only read-only SELECT queries are permitted."
            )


def _parse_select(sql: str) -> exp.Select:
    try:
        parsed = sqlglot.parse_one(sql, read="sqlite")
    except Exception as e:  # sqlglot raises its own ParseError subclasses
        raise SQLValidationError(f"Could not parse generated SQL: {e}") from e

    if not isinstance(parsed, exp.Select):
        raise SQLValidationError(
            "Only SELECT queries are permitted; "
            f"got statement of type '{type(parsed).__name__}'."
        )
    return parsed


def _extract_tables(parsed: exp.Select) -> list[str]:
    cte_names = {c.alias_or_name for c in parsed.find_all(exp.CTE)}
    return sorted({t.name for t in parsed.find_all(exp.Table)} - cte_names)


def _check_table_allowlist(parsed: exp.Select, schema: dict[str, list[str]]) -> list[str]:
    cte_names = {c.alias_or_name for c in parsed.find_all(exp.CTE)}
    all_tables = {t.name for t in parsed.find_all(exp.Table)}
    real_tables = sorted(all_tables - cte_names)

    allowed = set(schema.keys())
    unknown = [t for t in real_tables if t not in allowed]
    if unknown:
        raise SQLValidationError(
            f"Query references unknown/disallowed table(s): {unknown}. "
            f"Allowed tables are: {sorted(allowed)}."
        )
    if not real_tables:
        raise SQLValidationError("Query does not reference any table.")
    return real_tables


def _check_column_allowlist(parsed: exp.Select, schema: dict[str, list[str]]) -> None:
    """Verify every column reference resolves to a real column.

    Builds an alias -> table map from the FROM/JOIN clauses (covering both
    `sales s` and unaliased `sales`), then checks every Column node:
    - qualified (e.g. `s.amount`)   -> must exist on the resolved table.
    - unqualified (e.g. `amount`)   -> must exist on *some* table used in
      the query, OR be a SELECT-list output alias (`SUM(...) AS revenue`
      referenced again in GROUP BY/ORDER BY), OR be produced by a CTE
      (whose own body was already validated against the real schema).
    """
    cte_names = {c.alias_or_name for c in parsed.find_all(exp.CTE)}

    alias_map: dict[str, str] = {}
    for t in parsed.find_all(exp.Table):
        alias_map[t.alias_or_name] = t.name

    cte_output_names: set[str] = set()
    for cte in parsed.find_all(exp.CTE):
        inner = cte.this
        if isinstance(inner, exp.Select):
            cte_output_names |= {
                e.alias for e in inner.selects if isinstance(e, exp.Alias)
            }

    # Only a genuine `... AS alias` introduces a new name. A bare selected
    # column (e.g. `SELECT fake_column FROM customers`) must NOT whitelist
    # itself -- `output_name` falls back to the column's own name when
    # there's no alias, which would otherwise defeat this entire check.
    output_aliases = {e.alias for e in parsed.selects if isinstance(e, exp.Alias)}

    real_tables_used = {t for t in alias_map.values() if t not in cte_names}
    unqualified_allowed = set(output_aliases) | cte_output_names
    for t in real_tables_used:
        unqualified_allowed |= set(schema.get(t, []))

    for col in parsed.find_all(exp.Column):
        col_name = col.name
        if not col_name or col_name == "*":
            continue

        table_ref = col.table
        if table_ref:
            real_table = alias_map.get(table_ref)
            if real_table is None:
                raise SQLValidationError(
                    f"Query references an unknown table alias '{table_ref}'."
                )
            if real_table in cte_names:
                continue  # CTE's own body was already checked against the real schema.
            if col_name not in schema.get(real_table, []):
                raise SQLValidationError(
                    f"Column '{col_name}' does not exist on table "
                    f"'{real_table}'. Allowed columns: {schema.get(real_table, [])}."
                )
        else:
            if col_name not in unqualified_allowed:
                raise SQLValidationError(
                    f"Column '{col_name}' is not a recognised column or "
                    "output alias for this query."
                )


def _enforce_row_limit(parsed: exp.Select) -> exp.Select:
    existing = parsed.args.get("limit")
    if existing is None:
        parsed = parsed.limit(settings.MAX_ROWS)
    else:
        try:
            n = int(existing.expression.this)
            if n > settings.MAX_ROWS:
                parsed = parsed.limit(settings.MAX_ROWS)
        except (AttributeError, ValueError, TypeError):
            # Couldn't confidently read the existing limit -- clamp it to be safe.
            parsed = parsed.limit(settings.MAX_ROWS)
    return parsed


def validate_and_prepare(sql: str, schema: dict[str, list[str]]) -> tuple[str, list[str]]:
    """Run the full validation pipeline.

    Returns (safe_sql_string, tables_used) or raises SQLValidationError.
    """
    if not sql or not sql.strip():
        raise SQLValidationError("Generated SQL was empty.")

    single = _reject_if_multiple_statements(sql)
    _reject_comments(single)
    _reject_forbidden_keywords(single)

    parsed = _parse_select(single)
    tables = _check_table_allowlist(parsed, schema)
    _check_column_allowlist(parsed, schema)

    parsed = _enforce_row_limit(parsed)
    safe_sql = parsed.sql(dialect="sqlite")

    return safe_sql, tables
