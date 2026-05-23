"""Tests for the /metrics scrape endpoint.

We exercise the endpoint via the real FastAPI app using TestClient,
bumping a counter before we scrape to prove the endpoint renders the
current registry state in the text exposition format.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from codereview.api.main import app
from codereview.metrics import AGENT_FAILURES


def test_metrics_endpoint_exposes_documented_names() -> None:
    with TestClient(app) as client:
        response = client.get("/metrics")
    assert response.status_code == 200
    # Prometheus text exposition format carries TYPE / HELP lines per
    # metric family. Checking the names here doubles as a drift test:
    # a rename without a CHANGELOG entry would yank one of these.
    body = response.text
    for name in (
        "mnemos_review_duration_seconds",
        "mnemos_agent_failures_total",
        "mnemos_llm_tokens_total",
        "mnemos_graph_query_duration_seconds",
        "mnemos_index_progress_ratio",
    ):
        assert name in body, f"missing metric family: {name}"


def test_metrics_endpoint_reflects_counter_increments() -> None:
    # Bump a known counter, scrape, prove the post-scrape value appears.
    AGENT_FAILURES.labels(agent="probe_agent", reason="timeout").inc(3)
    with TestClient(app) as client:
        body = client.get("/metrics").text
    assert 'mnemos_agent_failures_total{agent="probe_agent",reason="timeout"} 3.0' in body


def test_metrics_endpoint_does_not_require_auth() -> None:
    # /metrics is allowlisted in the bearer-auth middleware; calling it
    # without a token must still succeed. Regression guard against an
    # accidental allowlist edit.
    with TestClient(app) as client:
        response = client.get("/metrics", headers={})
    assert response.status_code == 200
