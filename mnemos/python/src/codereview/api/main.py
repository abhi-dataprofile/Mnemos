"""FastAPI application entry point.

Run with::

    python -m uvicorn codereview.api.main:app --host 0.0.0.0 --port 8000

or via the module's ``__main__`` shortcut (``python -m codereview.api.main``).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from codereview import __version__
from codereview.api.auth import BearerAuthMiddleware
from codereview.api.routes import analyze, health, incremental, index, metrics
from codereview.db import dispose_engine
from codereview.logging import bind_request_context, clear_request_context, configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    try:
        yield
    finally:
        await dispose_engine()


app = FastAPI(
    title="Mnemos orchestrator",
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(BearerAuthMiddleware)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Thread a per-request id through structlog context."""

    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    bind_request_context(request_id=request_id, path=request.url.path)
    try:
        response = await call_next(request)
    finally:
        clear_request_context()
    response.headers["x-request-id"] = request_id
    return response


app.include_router(health.router)
app.include_router(metrics.router)
app.include_router(analyze.router)
app.include_router(index.router)
app.include_router(incremental.router)


def main() -> None:  # pragma: no cover - entry point
    import uvicorn

    uvicorn.run(
        "codereview.api.main:app",
        host="0.0.0.0",
        port=8000,
        log_config=None,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
