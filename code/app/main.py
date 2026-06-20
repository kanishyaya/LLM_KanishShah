"""
FastAPI entrypoint. The actual NL -> SQL -> validate -> execute -> answer
flow lives in app/pipeline.py; this file is just the HTTP layer (auth,
routing, top-level error shape) so the request flow is independently
testable. See app/pipeline.py's docstring for the full flow diagram.
"""
import logging

from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse

from app.auth import require_api_key
from app.config import settings
from app.nl2sql import get_engine
from app.pipeline import answer_question
from app.schemas import AskRequest, AskResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sparkline_ask")

app = FastAPI(
    title="Sparkline NL-to-SQL Q&A Service",
    description=(
        "Ask plain-English business questions about the demo customers / "
        "products / sales / employees database and get back validated SQL "
        "plus a plain-English answer."
    ),
    version="2.0.0",
)

# Built once at process start. For the LLM-backed engine this just stores
# the API key/base url/model -- no network call happens until the first
# real question comes in.
engine = get_engine()


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "llm_provider": settings.LLM_PROVIDER,
        "engine": type(engine).__name__,
        "model": settings.LLM_MODEL if hasattr(engine, "primary") else "stand-in",
    }


@app.post("/ask", response_model=AskResponse, dependencies=[Depends(require_api_key)])
async def ask(payload: AskRequest) -> AskResponse:
    return answer_question(payload.question, engine)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    # e.g. empty / missing "question" field -- still a graceful JSON error,
    # not a raw 500.
    return JSONResponse(status_code=422, content={"detail": exc.errors()})
