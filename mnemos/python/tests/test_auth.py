"""Bearer auth middleware behavior."""

from __future__ import annotations

from fastapi.testclient import TestClient

from codereview.api.main import app


def _analyze_body() -> dict:
    return {
        "installation_id": 1,
        "repository": {"owner": "acme", "name": "monolith", "github_id": 42},
        "pull_request": {
            "number": 1,
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "title": "t",
            "body": "",
            "author": "abhi",
        },
        "callback_url": "http://localhost:3000/cb",
        "callback_secret": "callback-secret-000",
    }


def test_missing_bearer_rejected() -> None:
    with TestClient(app) as client:
        response = client.post("/v1/pull-requests/analyze", json=_analyze_body())
    assert response.status_code == 401


def test_wrong_bearer_rejected() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/v1/pull-requests/analyze",
            json=_analyze_body(),
            headers={"Authorization": "Bearer nope"},
        )
    assert response.status_code == 401


def test_correct_bearer_accepted(fake_queue) -> None:  # noqa: ANN001
    with TestClient(app) as client:
        response = client.post(
            "/v1/pull-requests/analyze",
            json=_analyze_body(),
            headers={"Authorization": "Bearer test-secret"},
        )
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "queued"
    assert response.json()["job_id"].startswith("test-job-")
    fake_queue.assert_called_once()
