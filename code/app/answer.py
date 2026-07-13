"""
Turns a query result into a short, plain-English sentence.

Kept deterministic and template-based (no extra LLM round-trip) so it works
identically regardless of which NL2SQL engine produced the SQL, and so the
service has one fewer external dependency on the critical path.
"""
from typing import Any


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        if value.is_integer():
            return f"{value:,.0f}"
        return f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def build_answer(question: str, columns: list[str], rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No matching data was found for that question."

    # Single row, single column -> a single scalar fact (totals, counts, averages).
    if len(rows) == 1 and len(columns) == 1:
        col = columns[0]
        return f"{col.replace('_', ' ').capitalize()}: {_fmt(rows[0][col])}."

    # Single row, multiple columns -> describe that one record.
    if len(rows) == 1:
        parts = [f"{c.replace('_', ' ')} = {_fmt(rows[0][c])}" for c in columns]
        return "Result: " + ", ".join(parts) + "."

    # Multiple rows -> summarise the top entry plus a count, which reads
    # naturally for both "top N" rankings and "group by" breakdowns.
    # The *last* column is used as "the metric" because every SQL template
    # in this service puts its ORDER BY column last (see nl2sql/standin.py);
    # for a real LLM-generated query this column-position heuristic can
    # still be wrong, which is why it only kicks in when that column is
    # actually numeric below.
    #
    # Importantly, "leads with" is never said about rows[0] on the
    # assumption the SQL is already sorted descending -- a real LLM-written
    # query isn't guaranteed to include an ORDER BY for ranking-style
    # questions. Instead the actual max is found across all rows, so the
    # sentence is correct regardless of what order the database returned
    # them in.
    first = rows[0]
    label_col = columns[0]
    metric_col = columns[-1] if len(columns) > 1 else None
    row_word = "result" if len(rows) == 1 else "results"

    if metric_col is not None and isinstance(first[metric_col], (int, float)):
        metric_desc = metric_col.replace("_", " ")
        top_row = max(rows, key=lambda r: r[metric_col])
        lead_sentence = (
            f"{top_row[label_col]} leads with a {metric_desc} of "
            f"{_fmt(top_row[metric_col])}."
        )
        # len(rows) > 1 is always true here: the len(rows) == 1 cases
        # already returned earlier in this function.
        return f"{lead_sentence} {len(rows)} {row_word} are shown."

    label = first[label_col]
    if len(columns) > 1:
        example = ", ".join(
            f"{c.replace('_', ' ')}: {_fmt(first[c])}" for c in columns[1:]
        )
        return (
            f"Found {len(rows)} matching {row_word}. For example, "
            f"{label} has {example}."
        )

    return f"Found {len(rows)} matching {row_word}, including {label}."
