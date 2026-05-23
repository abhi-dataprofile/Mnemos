"""Bearer-token auth middleware for the internal HTTP API.

Only the TypeScript GitHub App service calls this API, and it does so with
``Authorization: Bearer <INTERNAL_SECRET>``. Health and metrics endpoints
are allowlisted.

Note: we use ``BaseHTTPMiddleware`` and return an explicit ``JSONResponse``
on failure rather than raising :class:`HTTPException`. Starlette's
``BaseHTTPMiddleware`` surrounds the app with a task group that does not
hand exceptions back to FastAPI's exception handlers, so raising here
produces a 500 instead of a 401.
"""

from __future__ import annotations

import hmac

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from codereview.config import get_settings

_ALLOWLISTED_PATHS = {"/health", "/healthz", "/metrics", "/docs", "/openapi.json", "/redoc"}


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Require ``Authorization: Bearer <INTERNAL_SECRET>`` on every request.

    Constant-time comparison avoids leaking the secret through timing.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in _ALLOWLISTED_PATHS:
            return await call_next(request)

        expected = get_settings().internal_secret
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")

        if scheme.lower() != "bearer" or not hmac.compare_digest(token, expected):
            return JSONResponse(
                {"detail": "invalid or missing bearer token"},
                status_code=401,
            )
        return await call_next(request)
