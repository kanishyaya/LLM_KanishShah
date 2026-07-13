"""
Deterministic "stand-in" NL -> SQL translator.

By default (LLM_PROVIDER=auto) this engine only runs as the automatic
fallback behind Gemini -- see app/nl2sql/llm_provider.py and app/config.py.
It is also the engine used for every sample run in this submission's
README, because the development environment for this assignment had no
reachable LLM API key.
See README "Design decisions" / "Limitations" for the full discussion of
why this exists and exactly how `app/nl2sql/llm_provider.py` would replace
it given real credentials -- the rest of the service (auth, validation,
execution, response shaping) is identical either way.

Approach: lightweight, schema-aware pattern matching. It recognises a
fixed-but-fairly-broad set of common business-question shapes (top-N
rankings, totals/averages, group-by-region/segment/category/department/
month, simple lookups and counts) tailored to the four tables in
sparkline_demo.db, and renders each to a parameterised SQL template.
Anything it can't confidently classify raises UnanswerableQuestionError so
the API can fail gracefully instead of guessing.

This intentionally is NOT a general-purpose NL2SQL system -- a real LLM
generalises far better to questions outside the patterns below. That
trade-off, and how it was tested, is documented in the README.
"""
import re
from dataclasses import dataclass
from functools import lru_cache

from app.db import get_connection
from app.nl2sql.base import NL2SQLEngine, UnanswerableQuestionError

WRITE_INTENT_PATTERNS = [
    r"\bdelete\b", r"\bdrop\b", r"\btruncate\b", r"\bremove\b",
    r"\binsert\b", r"\bupdate\b", r"\balter\b", r"\bmodify\b",
    r"\bedit\b", r"\bset\s+\w+\s*=", r"\bcreate\s+table\b",
]

WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


class WriteIntentBlockedError(UnanswerableQuestionError):
    """The question itself is asking for a data-modifying operation."""


@lru_cache(maxsize=8)
def _distinct_values(table: str, column: str) -> tuple[str, ...]:
    con = get_connection()
    try:
        rows = con.execute(f"SELECT DISTINCT {column} FROM {table}").fetchall()
        return tuple(str(r[0]) for r in rows if r[0] is not None)
    finally:
        con.close()


def _find_value_mention(question_lower: str, values: tuple[str, ...]) -> str | None:
    """Return the longest value from `values` that appears in the question."""
    matches = [v for v in values if v.lower() in question_lower]
    if not matches:
        return None
    return max(matches, key=len)


def _find_name_mention(question: str, table: str, column: str = "name") -> str | None:
    """Best-effort fuzzy match of a proper-noun-ish entity (customer/product
    name) against the question text -- case-insensitive substring match."""
    names = _distinct_values(table, column)
    question_lower = question.lower()
    return _find_value_mention(question_lower, names)


def _extract_top_n(question_lower: str, default: int = 5, singular_word: str | None = None) -> int:
    m = re.search(r"\b(top|bottom|first|last)\s+(\d+)\b", question_lower)
    if m:
        return int(m.group(2))
    for word, n in WORD_NUMBERS.items():
        if re.search(rf"\b(top|bottom)\s+{word}\b", question_lower):
            return n
    # No explicit number given. If the question uses the singular form of
    # the noun (e.g. "the highest paid employee", not "employees"), the
    # person almost certainly wants exactly one result back.
    if singular_word and re.search(rf"\b{singular_word}\b", question_lower):
        if not re.search(rf"\b{singular_word}s\b", question_lower):
            return 1
    return default


@dataclass
class _Match:
    sql: str


def _check_write_intent(question_lower: str) -> None:
    for pattern in WRITE_INTENT_PATTERNS:
        if re.search(pattern, question_lower):
            raise WriteIntentBlockedError(
                "This service only answers read-only questions. Modifying "
                "or deleting data is not permitted."
            )


def _try_total_overall(q: str) -> str | None:
    if re.search(r"\btotal\s+(revenue|sales|amount)\b", q) and not re.search(
        r"\bby\s+(region|segment|category|month|customer|product|department)\b", q
    ):
        return "SELECT SUM(amount) AS total_revenue FROM sales"
    if re.search(r"\b(average|avg)\b.*\b(sale|order|amount|revenue)\b", q):
        return "SELECT AVG(amount) AS average_order_value FROM sales"
    if re.search(r"\bhow many (sales|orders|transactions)\b", q) or re.search(
        r"\bnumber of (sales|orders|transactions)\b", q
    ):
        return "SELECT COUNT(*) AS sales_count FROM sales"
    return None


def _try_group_by(q: str) -> str | None:
    wants_total = bool(re.search(r"\b(total|sum)\b", q)) or "revenue" in q or "sales" in q
    if "region" in q and ("revenue" in q or "sales" in q or "amount" in q):
        return (
            "SELECT c.region, SUM(s.amount) AS revenue "
            "FROM customers c JOIN sales s ON s.customer_id = c.id "
            "GROUP BY c.region ORDER BY revenue DESC"
        )
    if "segment" in q and ("revenue" in q or "sales" in q or "amount" in q):
        return (
            "SELECT c.segment, SUM(s.amount) AS revenue "
            "FROM customers c JOIN sales s ON s.customer_id = c.id "
            "GROUP BY c.segment ORDER BY revenue DESC"
        )
    if "category" in q and ("revenue" in q or "sales" in q or "amount" in q):
        return (
            "SELECT p.category, SUM(s.amount) AS revenue "
            "FROM products p JOIN sales s ON s.product_id = p.id "
            "GROUP BY p.category ORDER BY revenue DESC"
        )
    if ("month" in q or "monthly" in q) and ("revenue" in q or "sales" in q or "amount" in q):
        return (
            "SELECT strftime('%Y-%m', s.sale_date) AS month, "
            "SUM(s.amount) AS revenue FROM sales s "
            "GROUP BY month ORDER BY month"
        )
    if "department" in q and ("employee" in q or "salary" in q or "headcount" in q or wants_total):
        return (
            "SELECT department, COUNT(*) AS headcount, "
            "SUM(salary) AS total_salary FROM employees "
            "GROUP BY department ORDER BY total_salary DESC"
        )
    return None


def _try_top_n_customers(q: str) -> str | None:
    if "customer" not in q:
        return None
    is_bottom = bool(re.search(r"\b(bottom|lowest|least|worst)\b", q))
    is_top = bool(re.search(r"\b(top|highest|best|most)\b", q))
    if not (is_top or is_bottom):
        return None
    metric_quantity = "quantity" in q or "units" in q
    n = _extract_top_n(q, default=5, singular_word="customer")
    if metric_quantity:
        metric_sql, alias = "SUM(s.quantity)", "units_purchased"
    else:
        metric_sql, alias = "SUM(s.amount)", "revenue"
    direction = "ASC" if is_bottom else "DESC"
    return (
        f"SELECT c.name, {metric_sql} AS {alias} "
        "FROM customers c JOIN sales s ON s.customer_id = c.id "
        f"GROUP BY c.id, c.name ORDER BY {alias} {direction} LIMIT {n}"
    )


def _try_top_n_products(q: str) -> str | None:
    if "product" not in q:
        return None
    is_bottom = bool(re.search(r"\b(bottom|lowest|least|worst)\b", q))
    is_top = bool(re.search(r"\b(top|highest|best|most|popular)\b", q))
    if not (is_top or is_bottom):
        return None
    metric_quantity = "quantity" in q or "units" in q or "sold" in q
    n = _extract_top_n(q, default=5, singular_word="product")
    if metric_quantity:
        metric_sql, alias = "SUM(s.quantity)", "units_sold"
    else:
        metric_sql, alias = "SUM(s.amount)", "revenue"
    direction = "ASC" if is_bottom else "DESC"
    return (
        f"SELECT p.name, {metric_sql} AS {alias} "
        "FROM products p JOIN sales s ON s.product_id = p.id "
        f"GROUP BY p.id, p.name ORDER BY {alias} {direction} LIMIT {n}"
    )


def _try_top_n_employees(q: str) -> str | None:
    if "employee" not in q and "salary" not in q and "paid" not in q:
        return None
    is_bottom = bool(re.search(r"\b(bottom|lowest|least|worst)\b", q))
    is_top = bool(re.search(r"\b(top|highest|best|most)\b", q))
    if not (is_top or is_bottom):
        return None
    n = _extract_top_n(q, default=5, singular_word="employee")
    direction = "ASC" if is_bottom else "DESC"
    return (
        "SELECT name, department, role, salary FROM employees "
        f"ORDER BY salary {direction} LIMIT {n}"
    )


def _try_counts(q: str) -> str | None:
    if not re.search(r"\bhow many\b|\bnumber of\b|\bcount\b", q):
        return None
    if "customer" in q:
        region = _find_value_mention(q, _distinct_values("customers", "region"))
        segment = _find_value_mention(q, _distinct_values("customers", "segment"))
        where = []
        if region:
            where.append(f"region = '{region}'")
        if segment:
            where.append(f"segment = '{segment}'")
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        return f"SELECT COUNT(*) AS customer_count FROM customers{clause}"
    if "product" in q:
        category = _find_value_mention(q, _distinct_values("products", "category"))
        clause = f" WHERE category = '{category}'" if category else ""
        return f"SELECT COUNT(*) AS product_count FROM products{clause}"
    if "employee" in q:
        department = _find_value_mention(q, _distinct_values("employees", "department"))
        clause = f" WHERE department = '{department}'" if department else ""
        return f"SELECT COUNT(*) AS employee_count FROM employees{clause}"
    return None


def _try_list_with_filter(q: str) -> str | None:
    if "customer" in q and ("list" in q or "which" in q or "show" in q or "who are" in q):
        region = _find_value_mention(q, _distinct_values("customers", "region"))
        segment = _find_value_mention(q, _distinct_values("customers", "segment"))
        where = []
        if region:
            where.append(f"region = '{region}'")
        if segment:
            where.append(f"segment = '{segment}'")
        if where:
            clause = " WHERE " + " AND ".join(where)
            return f"SELECT name, region, segment FROM customers{clause}"
    if "product" in q and ("list" in q or "which" in q or "show" in q):
        category = _find_value_mention(q, _distinct_values("products", "category"))
        if category:
            return (
                "SELECT name, category, unit_price FROM products "
                f"WHERE category = '{category}'"
            )
    return None


def _try_named_entity_lookup(q: str, original_question: str) -> str | None:
    customer = _find_name_mention(original_question, "customers")
    if customer and ("revenue" in q or "sales" in q or "spent" in q or "bought" in q):
        return (
            "SELECT c.name, SUM(s.amount) AS revenue, SUM(s.quantity) AS units "
            "FROM customers c JOIN sales s ON s.customer_id = c.id "
            f"WHERE c.name = '{customer}' GROUP BY c.name"
        )
    product = _find_name_mention(original_question, "products")
    if product:
        if "price" in q or "cost" in q:
            return f"SELECT name, category, unit_price FROM products WHERE name = '{product}'"
        if "sold" in q or "units" in q or "revenue" in q or "sales" in q:
            return (
                "SELECT p.name, SUM(s.quantity) AS units_sold, SUM(s.amount) AS revenue "
                "FROM products p JOIN sales s ON s.product_id = p.id "
                f"WHERE p.name = '{product}' GROUP BY p.name"
            )
    return None


# Handlers are tried in order; the first one that returns SQL wins.
_HANDLERS = [
    _try_total_overall,
    _try_top_n_customers,
    _try_top_n_products,
    _try_top_n_employees,
    _try_group_by,
    _try_counts,
    _try_list_with_filter,
]


class StandInEngine(NL2SQLEngine):
    """Rule-based fallback used when no real LLM is configured."""

    def generate_sql(self, question: str) -> str:
        q = question.strip().lower()
        if not q:
            raise UnanswerableQuestionError("The question was empty.")

        _check_write_intent(q)

        for handler in _HANDLERS:
            sql = handler(q)
            if sql:
                return sql

        sql = _try_named_entity_lookup(q, question)
        if sql:
            return sql

        raise UnanswerableQuestionError(
            "I couldn't confidently map this question to the available "
            "data (customers, products, sales, employees). Try asking "
            "about revenue/sales by customer, product, region, segment, "
            "category, month, or department -- e.g. 'top 5 customers by "
            "revenue' or 'total sales by region'."
        )
