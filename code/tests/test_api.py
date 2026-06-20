"""
Integration tests for the /ask and /health endpoints, run against the
deterministic stand-in engine (no network access required).
"""
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

client = TestClient(app)
HEADERS = {"X-API-Key": settings.API_KEY}


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_missing_api_key_is_rejected():
    r = client.post("/ask", json={"question": "Total revenue?"})
    assert r.status_code == 401


def test_wrong_api_key_is_rejected():
    r = client.post(
        "/ask", json={"question": "Total revenue?"},
        headers={"X-API-Key": "wrong-key"},
    )
    assert r.status_code == 401


def test_normal_question():
    r = client.post(
        "/ask",
        json={"question": "Who are our top 5 customers by revenue?"},
        headers=HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["sql"] is not None
    assert "customers" in body["tables_used"]
    assert "sales" in body["tables_used"]
    assert len(body["result"]) == 5
    assert body["answer"]


def test_grouping_question():
    r = client.post(
        "/ask",
        json={"question": "What is the total sales amount by region?"},
        headers=HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["sql"] is not None
    assert len(body["result"]) > 0
    assert "region" in body["result"][0]


def test_write_attempt_is_refused_gracefully():
    r = client.post(
        "/ask",
        json={"question": "Delete all sales records for Croma Retail"},
        headers=HEADERS,
    )
    assert r.status_code == 200  # graceful, not a crash
    body = r.json()
    assert body["sql"] is None
    assert body["result"] == []
    assert "read-only" in body["answer"].lower() or "permitted" in body["answer"].lower()


def test_unanswerable_question_is_handled_gracefully():
    r = client.post(
        "/ask",
        json={"question": "asdkj completely unrelated gibberish"},
        headers=HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["sql"] is None
    assert body["result"] == []


def test_empty_question_returns_422():
    r = client.post("/ask", json={"question": ""}, headers=HEADERS)
    assert r.status_code == 422


@pytest.mark.parametrize(
    "question,expected_table",
    [
        ("What is the total revenue?", "sales"),
        ("How many customers do we have in the West region?", "customers"),
        ("Who is the highest paid employee?", "employees"),
        ("What is the price of the Laptop Pro 15?", "products"),
    ],
)
def test_various_supported_questions(question, expected_table):
    r = client.post("/ask", json={"question": question}, headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert expected_table in body["tables_used"]
