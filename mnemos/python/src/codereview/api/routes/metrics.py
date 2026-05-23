"""Prometheus scrape endpoint.

Serves the default CollectorRegistry's current state in the text
exposition format. No auth on this route — same convention every
prometheus_client example follows. In production, rely on your
reverse proxy / network policy to scope who can reach /metrics.
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

router = APIRouter(tags=["ops"])


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
