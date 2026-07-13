"""
Tests for the primary-LLM-with-fallback architecture (app/nl2sql and
app/pipeline.py). These use fake engines instead of a real network call to
Gemini, so they run offline and deterministically, while still proving the
exact failure-handling behaviour the architecture is supposed to provide:

  question -> primary (Gemini)
                 |  network/timeout/bad-response error -> fallback (stand-in)
                 |  explicit "I can't answer this"      -> propagated as-is
                 v
              SQL -> validator
                 |  validation failure (e.g. hallucinated column), and the
                 |  engine that produced it was the composite/LLM path
                 |     -> retried once against the stand-in
                 v
              final answer
"""
import pytest

from app.nl2sql import CompositeEngine
from app.nl2sql.base import NL2SQLEngine, UnanswerableQuestionError
from app.nl2sql.standin import StandInEngine
from app.pipeline import answer_question


class _FailingEngine(NL2SQLEngine):
    """Simulates Gemini being unreachable (network error, timeout, etc)."""

    def generate_sql(self, question: str) -> str:
        raise RuntimeError("simulated network failure")


class _RefusingEngine(NL2SQLEngine):
    """Simulates Gemini explicitly judging the question unanswerable."""

    def generate_sql(self, question: str) -> str:
        raise UnanswerableQuestionError("simulated LLM refusal")


class _HallucinatingEngine(NL2SQLEngine):
    """Simulates Gemini generating syntactically valid but schema-invalid SQL."""

    def generate_sql(self, question: str) -> str:
        return "SELECT made_up_column FROM customers"


def test_composite_falls_back_on_technical_failure():
    engine = CompositeEngine(_FailingEngine(), StandInEngine(), primary_name="fake-llm")
    sql = engine.generate_sql("What is the total revenue?")
    assert "SUM(amount)" in sql.upper().replace(" ", "") or "sales" in sql.lower()
    assert engine.last_engine_used == "stand-in"


def test_composite_propagates_explicit_refusal_without_fallback():
    engine = CompositeEngine(_RefusingEngine(), StandInEngine(), primary_name="fake-llm")
    with pytest.raises(UnanswerableQuestionError):
        engine.generate_sql("anything")
    # The primary's own judgement is trusted -- no silent override.
    assert engine.last_engine_used == "fake-llm"


def test_pipeline_retries_with_standin_on_validation_failure(caplog):
    engine = CompositeEngine(_HallucinatingEngine(), StandInEngine(), primary_name="fake-llm")
    with caplog.at_level("INFO", logger="pipeline"):
        response = answer_question("What is the total revenue?", engine)
    assert response.sql is not None
    assert "made_up_column" not in response.sql
    # engine_used is no longer part of the API response (see README); the
    # retry behaviour is instead verified via the pipeline's own log line,
    # which still records that the stand-in ultimately produced the answer.
    assert "engine=stand-in" in caplog.text
    assert response.result  # the retried stand-in query actually returned data


def test_pipeline_answers_directly_with_standin():
    response = answer_question(
        "Who are our top 5 customers by revenue?", StandInEngine()
    )
    # engine_used is no longer part of the API response (see README); what
    # matters functionally is that calling the pipeline with a bare
    # StandInEngine (no LLM involved at all) still produces a correct answer.
    assert len(response.result) == 5
