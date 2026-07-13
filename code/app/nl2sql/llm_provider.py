"""
A thin client for any OpenAI Chat-Completions-compatible HTTP API.

This is the PRIMARY NL->SQL engine for this submission (see app/config.py
and README "Architecture"): LLM_BASE_URL/LLM_MODEL default to Google
Gemini's OpenAI-compatible endpoint, since Gemini has a generous free tier
(https://aistudio.google.com/apikey). Pointing those two settings at a
different host/model lets the *same* code talk to:
- Google Gemini (OpenAI-compat)  https://generativelanguage.googleapis.com/v1beta/openai/  [default]
- OpenAI                         https://api.openai.com/v1
- Groq                           https://api.groq.com/openai/v1
- A local Ollama / vLLM server   http://localhost:11434/v1  (model-dependent)

No provider-specific SDK is required, which keeps the dependency list small
and the integration point obvious.

Any exception raised here (network error, timeout, bad HTTP status,
malformed response body) is intentionally left uncaught -- it propagates
to app/nl2sql/CompositeEngine, which treats it as "the LLM is unavailable
right now" and retries the same question against the deterministic
stand-in engine. See that class's docstring for the full fallback policy.
"""
import re

import requests

from app.config import settings
from app.db import get_schema_context
from app.nl2sql.base import NL2SQLEngine, UnanswerableQuestionError

SYSTEM_PROMPT = """You are a careful SQLite query-writing assistant for a \
small business database. You translate a plain-English business question \
into exactly one read-only SQL SELECT statement that SQLite can execute.

Rules you must always follow:
1. Use ONLY the tables and columns given in the schema below. Never invent \
table or column names.
2. Output ONE SQL statement only: a single SELECT. Never use INSERT, \
UPDATE, DELETE, DROP, ALTER, PRAGMA, ATTACH, or multiple statements \
separated by ';'.
3. Do not include comments, markdown fences, or any explanation -- output \
raw SQL only, nothing else.
4. Prefer explicit column lists and meaningful aliases (e.g. SUM(amount) \
AS revenue) over SELECT *.
5. If the question cannot be answered with the given schema -- because it \
is ambiguous, refers to data that doesn't exist here, or asks for a \
write/delete operation -- respond with exactly: NO_QUERY: <short reason>
   Do not guess in that case.

Schema:
{schema}
"""


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"```$", "", text)
    return text.strip()


class OpenAICompatibleEngine(NL2SQLEngine):
    def __init__(self) -> None:
        if not settings.LLM_API_KEY:
            raise RuntimeError(
                "LLM_PROVIDER=openai requires LLM_API_KEY to be set."
            )
        self.api_key = settings.LLM_API_KEY
        self.base_url = settings.LLM_BASE_URL.rstrip("/")
        self.model = settings.LLM_MODEL

    def generate_sql(self, question: str) -> str:
        schema = get_schema_context()
        system_prompt = SYSTEM_PROMPT.format(schema=schema)

        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": question},
                ],
            },
            timeout=settings.LLM_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        content = _strip_code_fences(content)

        if content.upper().startswith("NO_QUERY"):
            reason = content.split(":", 1)[1].strip() if ":" in content else (
                "The question could not be mapped to the available data."
            )
            raise UnanswerableQuestionError(reason)

        return content
