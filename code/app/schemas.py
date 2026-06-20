from typing import Any

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=500, examples=[
        "Who are our top 5 customers by revenue?"
    ])


class AskResponse(BaseModel):
    question: str
    sql: str | None
    tables_used: list[str]
    result: list[dict[str, Any]]
    answer: str
