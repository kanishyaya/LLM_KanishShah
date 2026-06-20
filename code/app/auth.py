"""
Simple API-key authentication.

A real product would use short-lived tokens / OAuth, but for this scope an
API key in a custom header is explicit, easy to test with curl, and easy to
explain -- which the assignment asks for ("simple mechanism").
"""
from fastapi import Header, HTTPException, status

from app.config import settings


async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if not x_api_key or x_api_key != settings.API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key. Pass it in the 'X-API-Key' header.",
        )
