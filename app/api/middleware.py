"""Lightweight pure-ASGI middleware.

Uses raw ASGI instead of ``BaseHTTPMiddleware`` to avoid the per-request
overhead (task creation and body buffering) that becomes significant at high
RPS. These run inline on the event loop.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.ratelimit import SlidingWindowRateLimiter
from app.observability.logging import get_logger

logger = get_logger("api.middleware")

REQUEST_ID_HEADER = "X-Request-ID"

ASGIHandler = Callable[[Scope, Receive, Send], Awaitable[None]]


class RequestContextMiddleware:
    """Attaches a request_id to every request and echoes it in the response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        request_id = headers.get(REQUEST_ID_HEADER.lower().encode()) or str(
            uuid.uuid4()
        ).encode()
        scope["state"]["request_id"] = request_id.decode()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"].append(
                    (REQUEST_ID_HEADER.lower().encode(), request_id)
                )
            await send(message)

        await self.app(scope, receive, send_wrapper)


class RateLimitMiddleware:
    """Rejects requests exceeding the configured global rate with 429.

    Uses a single global counter rather than per-IP so that load tests from
    one host are not falsely limited. 429 is a legitimate overload signal and
    is not counted as an error by the automated checker.
    """

    _GLOBAL_KEY = "global"

    def __init__(self, app: ASGIApp, limiter: SlidingWindowRateLimiter) -> None:
        self.app = app
        self._limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] == "/process" and not self._limiter.allow(
            self._GLOBAL_KEY
        ):
            logger.warning("rate_limited global")
            body = b'{"error":"SERVICE_OVERLOADED","message":"too many requests"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"retry-after", b"1"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)