"""HTTP middleware: request_id generation and rate limiting."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.core.ratelimit import SlidingWindowRateLimiter
from app.core.security import hash_identifier
from app.observability.logging import get_logger

logger = get_logger("api.middleware")

REQUEST_ID_HEADER = "X-Request-ID"

CallNext = Callable[[Request], Awaitable[Response]]


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attaches a request_id to every request and logs it safely."""

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Rejects requests exceeding the configured rate with 429."""

    def __init__(self, app: ASGIApp, limiter: SlidingWindowRateLimiter) -> None:
        super().__init__(app)
        self._limiter = limiter

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        if request.url.path == "/process":
            key = request.client.host if request.client else "unknown"
            if not self._limiter.allow(key):
                logger.warning(
                    "rate_limited client_hash=%s",
                    hash_identifier(key),
                )
                return JSONResponse(
                    status_code=429,
                    content={
                        "error": "SERVICE_OVERLOADED",
                        "message": "too many requests",
                    },
                    headers={"Retry-After": "1"},
                )
        return await call_next(request)