"""
Unit tests for the safety layer in app/sql_validator.py.

These feed hand-crafted malicious / unsafe SQL strings directly into the
validator -- bypassing whichever NL2SQL engine is configured -- to prove
the safety gate itself holds regardless of where the SQL came from. This
is the layer that matters most if a real LLM is ever swapped in and
occasionally ignores its system prompt.
"""
import pytest

from app.db import get_schema
from app.sql_validator import SQLValidationError, validate_and_prepare


@pytest.fixture(scope="module")
def schema():
    return get_schema()


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM sales",
        "DROP TABLE customers",
        "UPDATE customers SET name = 'x'",
        "INSERT INTO customers (name) VALUES ('x')",
        "SELECT * FROM customers; DROP TABLE customers",
        "SELECT * FROM customers WHERE 1=1; -- comment",
        "SELECT * FROM customers /* sneaky */",
        "ATTACH DATABASE '/etc/passwd' AS x",
        "PRAGMA table_info(customers)",
        "SELECT * FROM sqlite_master",
        "VACUUM",
        "",
        "   ",
    ],
)
def test_unsafe_sql_is_rejected(sql, schema):
    with pytest.raises(SQLValidationError):
        validate_and_prepare(sql, schema)


def test_legitimate_query_passes(schema):
    sql = (
        "SELECT c.name, SUM(s.amount) AS revenue FROM customers c "
        "JOIN sales s ON s.customer_id = c.id "
        "GROUP BY c.id, c.name ORDER BY revenue DESC LIMIT 5"
    )
    safe_sql, tables = validate_and_prepare(sql, schema)
    assert "customers" in tables
    assert "sales" in tables
    assert "LIMIT 5" in safe_sql.upper()


def test_row_limit_is_clamped(schema):
    sql = "SELECT * FROM sales LIMIT 999999"
    safe_sql, _ = validate_and_prepare(sql, schema)
    assert "LIMIT 200" in safe_sql.upper()


def test_missing_limit_is_added(schema):
    sql = "SELECT * FROM sales"
    safe_sql, _ = validate_and_prepare(sql, schema)
    assert "LIMIT" in safe_sql.upper()


def test_unknown_table_is_rejected(schema):
    with pytest.raises(SQLValidationError):
        validate_and_prepare("SELECT * FROM employees_secret", schema)


# --- Column-level allow-list (added per reviewer feedback) -----------------

def test_hallucinated_column_is_rejected(schema):
    with pytest.raises(SQLValidationError, match="fake_column"):
        validate_and_prepare("SELECT fake_column FROM customers", schema)


def test_hallucinated_column_via_join_is_rejected(schema):
    # 'region' is a real column, but not on `sales` -- a real LLM mistake
    # this check is specifically designed to catch.
    with pytest.raises(SQLValidationError):
        validate_and_prepare(
            "SELECT s.region FROM sales s JOIN customers c ON c.id = s.customer_id",
            schema,
        )


def test_order_by_select_alias_is_allowed(schema):
    # 'revenue' isn't a real column anywhere -- it's a SELECT-list alias
    # (`SUM(s.amount) AS revenue`) referenced again in ORDER BY. Must NOT
    # be rejected as an unknown column.
    sql = (
        "SELECT c.region, SUM(s.amount) AS revenue FROM customers c "
        "JOIN sales s ON s.customer_id = c.id "
        "GROUP BY c.region ORDER BY revenue DESC"
    )
    safe_sql, tables = validate_and_prepare(sql, schema)
    assert "region" in safe_sql.lower()


def test_cte_with_renamed_column_is_allowed(schema):
    # The CTE's own body is checked against the real schema; its renamed
    # output column ('amount_with_tax') is then trusted in the outer query.
    sql = (
        "WITH recent AS ("
        "SELECT customer_id, amount * 1.18 AS amount_with_tax FROM sales"
        ") SELECT customer_id, SUM(amount_with_tax) AS total FROM recent "
        "GROUP BY customer_id"
    )
    safe_sql, tables = validate_and_prepare(sql, schema)
    assert tables == ["sales"]


def test_cte_referencing_real_hallucinated_column_in_body_is_rejected(schema):
    # The CTE body itself must still be valid against the real schema.
    sql = (
        "WITH recent AS (SELECT customer_id, made_up_col FROM sales) "
        "SELECT customer_id FROM recent"
    )
    with pytest.raises(SQLValidationError):
        validate_and_prepare(sql, schema)
