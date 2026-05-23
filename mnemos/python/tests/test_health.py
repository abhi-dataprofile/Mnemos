"""Health endpoint smoke test."""

from __future__ import annotations

from fastapi.testclient import TestClient

from codereview import __version__
from codereview.api.main import app


def test_health_ok_and_unauthenticated() -> None:
    # /health is allowlisted from the bearer middleware so operators can
    # health-check without knowing the internal secret.
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}
