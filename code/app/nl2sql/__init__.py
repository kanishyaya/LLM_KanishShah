import logging

from app.config import settings
from app.nl2sql.base import NL2SQLEngine, UnanswerableQuestionError  # noqa: F401
from app.nl2sql.standin import StandInEngine

logger = logging.getLogger("nl2sql")


class CompositeEngine(NL2SQLEngine):
    """Primary engine (a real LLM) with automatic fallback.

    Architecture:  question -> primary.generate_sql() -> SQL
                       | (network error / timeout / bad response)
                       v
                   fallback.generate_sql() -> SQL

    A deliberate "I can't answer this" from the primary
    (UnanswerableQuestionError) is NOT a failure -- it's propagated as-is,
    since the stand-in re-trying is unlikely to do better on a question the
    LLM already judged unanswerable, and silently overriding the LLM's
    explicit judgement would be confusing.

    Anything else (requests.RequestException, timeouts, HTTP errors,
    malformed responses, ...) is treated as the primary being unavailable
    right now, and the request is retried against the deterministic
    stand-in so a flaky/unreachable LLM provider never takes the whole
    service down. `last_engine_used` records which one actually answered,
    so callers (see app/pipeline.py) can report it for transparency.
    """

    def __init__(self, primary: NL2SQLEngine, fallback: NL2SQLEngine, primary_name: str):
        self.primary = primary
        self.fallback = fallback
        self.primary_name = primary_name
        self.last_engine_used = primary_name

    def generate_sql(self, question: str) -> str:
        try:
            sql = self.primary.generate_sql(question)
            self.last_engine_used = self.primary_name
            return sql
        except UnanswerableQuestionError:
            self.last_engine_used = self.primary_name
            raise
        except Exception as e:
            logger.warning(
                "Primary LLM engine (%s) failed (%s); falling back to stand-in.",
                self.primary_name, e,
            )
            self.last_engine_used = "stand-in"
            return self.fallback.generate_sql(question)


def get_engine() -> NL2SQLEngine:
    provider = settings.LLM_PROVIDER.lower()

    if provider == "stand-in":
        return StandInEngine()

    if provider in ("auto", "openai", "gemini"):
        if not settings.LLM_API_KEY:
            if provider in ("openai", "gemini"):
                raise RuntimeError(
                    f"LLM_PROVIDER={provider!r} requires LLM_API_KEY (or "
                    "GEMINI_API_KEY) to be set."
                )
            logger.warning(
                "No LLM_API_KEY/GEMINI_API_KEY configured; using the "
                "stand-in engine. Set GEMINI_API_KEY to enable the real "
                "LLM path (see README 'Setup')."
            )
            return StandInEngine()

        from app.nl2sql.llm_provider import OpenAICompatibleEngine

        primary = OpenAICompatibleEngine()
        return CompositeEngine(primary, StandInEngine(), primary_name=settings.LLM_MODEL)

    logger.warning("Unknown LLM_PROVIDER=%r; falling back to stand-in.", provider)
    return StandInEngine()
