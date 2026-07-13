"""
The actual request-handling pipeline, kept separate from app/main.py so the
flow is independently testable and main.py stays a thin HTTP layer.

    question
        |
        v
    NL2SQLEngine.generate_sql()   (Gemini primary, stand-in fallback)
        |  raises UnanswerableQuestionError -> graceful response
        v
    sql_validator.validate_and_prepare()
        |  raises SQLValidationError
        |     -> if the engine that answered was a real LLM, retry ONCE
        |        against the deterministic stand-in (an LLM occasionally
        |        hallucinates a column/table; the stand-in's templates are
        |        always schema-valid by construction)
        |     -> otherwise: graceful response
        v
    db.execute_query()
        |  raises sqlite3.Error -> graceful response
        v
    answer.build_answer()
        |
        v
    AskResponse
"""
import logging
import sqlite3

from app.answer import build_answer
from app.db import execute_query, get_schema
from app.nl2sql import CompositeEngine
from app.nl2sql.base import NL2SQLEngine, UnanswerableQuestionError
from app.nl2sql.standin import StandInEngine
from app.schemas import AskResponse
from app.sql_validator import SQLValidationError, validate_and_prepare

logger = logging.getLogger("pipeline")


def answer_question(question: str, engine: NL2SQLEngine) -> AskResponse:
    question = question.strip()
    schema = get_schema()

    # 1. NL -> SQL
    try:
        raw_sql = engine.generate_sql(question)
        engine_used = getattr(engine, "last_engine_used", "stand-in")
    except UnanswerableQuestionError as e:
        logger.info("Unanswerable question: %r (%s)", question, e)
        return AskResponse(
            question=question, sql=None, tables_used=[], result=[],
            answer=f"I can't answer that: {e}",
        )

    # 2. Validate & safety-check the generated SQL (untrusted input)
    try:
        safe_sql, tables_used = validate_and_prepare(raw_sql, schema)
    except SQLValidationError as e:
        # If a real LLM produced SQL that failed validation (e.g. a
        # hallucinated column), give the deterministic stand-in one shot
        # at the same question before giving up -- its templates are
        # always schema-valid by construction, so this materially
        # improves reliability without weakening the validator itself.
        if isinstance(engine, CompositeEngine):
            logger.warning(
                "Validation failed for %s-generated SQL (%s); retrying "
                "with stand-in.", engine_used, e,
            )
            try:
                fallback_sql = StandInEngine().generate_sql(question)
                safe_sql, tables_used = validate_and_prepare(fallback_sql, schema)
                raw_sql = fallback_sql
                engine_used = "stand-in"
            except (UnanswerableQuestionError, SQLValidationError):
                return AskResponse(
                    question=question, sql=None, tables_used=[], result=[],
                    answer=f"I can't run that query because it failed a safety check: {e}",
                )
        else:
            logger.warning(
                "Rejected unsafe SQL for %r (engine=%s): %s | sql=%s",
                question, engine_used, e, raw_sql,
            )
            return AskResponse(
                question=question, sql=None, tables_used=[], result=[],
                answer=f"I can't run that query because it failed a safety check: {e}",
            )

    # 3. Execute
    try:
        columns, result = execute_query(safe_sql)
    except sqlite3.Error as e:
        logger.error("DB error executing %s (engine=%s): %s", safe_sql, engine_used, e)
        return AskResponse(
            question=question, sql=safe_sql, tables_used=tables_used, result=[],
            answer=f"The query could not be executed: {e}",
        )

    # 4. Plain-English summary
    logger.info("Answered %r using engine=%s", question, engine_used)
    return AskResponse(
        question=question,
        sql=safe_sql,
        tables_used=tables_used,
        result=result,
        answer=build_answer(question, columns, result),
    )
