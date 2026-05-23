"""Liveness + readiness probes.

``/health`` and its alias ``/healthz`` answer without touching any
backing service — a 200 here just means "the process is up". ``/readyz``
checks the backing services (Postgres, Redis) and returns 503 if any of
them are unreachable, so load balancers can shed traffic from a node
that lost its database connection.

Both endpoints are allowlisted in ``api/auth.py`` so probes don't need
the bearer token.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from codereview import __version__
from codereview.api.schemas import HealthResponse
from codereview.config import get_settings
from codereview.db import get_session_factory
from codereview.logging import get_logger

router = APIRouter(tags=["health"])
_log = get_logger(__name__)


@router.get("/health", response_model=HealthResponse)
@router.get("/healthz", response_model=HealthResponse, include_in_schema=False)
async def health() -> HealthResponse:
    """Liveness probe. Does not touch the database."""

    return HealthResponse(version=__version__)


@router.get("/readyz", include_in_schema=False)
async def readyz(response: Response) -> dict[str, Any]:
    """Readiness probe.

    Returns 200 with component status when Postgres and Redis are
    reachable; 503 otherwise. Load balancers should route this to
    decide if a replica should take traffic.
    """

    checks: dict[str, str] = {}

    try:
        factory = get_session_factory()
        async with factory() as session:
            await session.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except SQLAlchemyError as exc:
        checks["postgres"] = f"error: {exc.__class__.__name__}"
        _log.warning("readyz_postgres_error", error=repr(exc))

    settings = get_settings()
    try:
        redis = Redis.from_url(str(settings.redis_url))
        try:
            await redis.ping()
            checks["redis"] = "ok"
        finally:
            await redis.aclose()
    except RedisError as exc:
        checks["redis"] = f"error: {exc.__class__.__name__}"
        _log.warning("readyz_redis_error", error=repr(exc))

    all_ok = all(v == "ok" for v in checks.values())
    response.status_code = (
        status.HTTP_200_OK if all_ok else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    return {"status": "ok" if all_ok else "degraded", "checks": checks, "version": __version__}
