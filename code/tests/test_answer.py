"""
Unit tests for app/answer.py's build_answer().

These exist specifically to cover a real bug found during manual testing:
build_answer() used to assume rows[0] was already the highest-value row
(true for the stand-in's templates, which always include ORDER BY ...
DESC, but NOT guaranteed for a real LLM-generated query). When Gemini
produced syntactically valid SQL with no ORDER BY, the sentence named
the wrong "leader" -- correct data, wrong English. See app/answer.py's
build_answer() docstring/comments for the fix: it now finds the actual
max across all rows instead of trusting row order.
"""
from app.answer import build_answer


def test_leader_is_correct_even_when_rows_are_not_sorted():
    # Reproduces the exact bug: Gemini-style SQL with no ORDER BY returned
    # rows in an arbitrary order where the true maximum is NOT first.
    columns = ["region", "total_sales_amount"]
    rows = [
        {"region": "East", "total_sales_amount": 376000},
        {"region": "North", "total_sales_amount": 228000},
        {"region": "South", "total_sales_amount": 755100},
        {"region": "West", "total_sales_amount": 899900},  # the actual max
    ]
    answer = build_answer("What is the total sales amount by region?", columns, rows)
    assert "West leads" in answer
    assert "899,900" in answer
    # The previous (buggy) behaviour would have said "East leads" here.
    assert "East leads" not in answer


def test_leader_is_correct_when_rows_are_already_sorted_descending():
    # The stand-in's templates (and well-formed LLM SQL) already sort
    # descending; the fix must not regress this common case.
    columns = ["region", "revenue"]
    rows = [
        {"region": "West", "revenue": 899900.0},
        {"region": "South", "revenue": 755100.0},
        {"region": "East", "revenue": 376000.0},
        {"region": "North", "revenue": 228000.0},
    ]
    answer = build_answer("What is the total sales amount by region?", columns, rows)
    assert "West leads with a revenue of 899,900." in answer
    assert "4 results are shown" in answer


def test_single_row_single_column_scalar_fact():
    answer = build_answer("What is the total revenue?", ["revenue"], [{"revenue": 1500000}])
    assert answer == "Revenue: 1,500,000."


def test_single_row_multiple_columns_describes_record():
    columns = ["name", "price"]
    rows = [{"name": "Laptop Pro 15", "price": 999.99}]
    answer = build_answer("What is the price of the Laptop Pro 15?", columns, rows)
    assert "Laptop Pro 15" in answer
    assert "999.99" in answer


def test_multi_row_non_numeric_metric_falls_back_to_example_listing():
    columns = ["id", "name"]
    rows = [{"id": 1, "name": "HCL"}, {"id": 2, "name": "Croma"}]
    answer = build_answer("List customers", columns, rows)
    assert "Found 2 matching results" in answer


def test_no_rows_returns_no_data_message():
    assert build_answer("anything", [], []) == "No matching data was found for that question."
