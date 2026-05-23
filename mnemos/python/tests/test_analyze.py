"""Shape validation on /v1/pull-requests/analyze."""

from __future__ import annotations

from fastapi.testclient import TestClient

from codereview.api.main import app

HEADERS = {"Authorization": "Bearer test-secret"}


def test_rejects_missing_fields(fake_queue) -> None:  # noqa: ANN001
    with TestClient(app) as client:
        response = client.post(
            "/v1/pull-requests/analyze",
            json={"installation_id": 1},
            headers=HEADERS,
        )
    assert response.status_code == 422
    fake_queue.assert_not_called()


def test_rejects_short_callback_secret(fake_queue) -> None:  # noqa: ANN001
    body = {
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
        "callback_secret": "short",
    }
    with TestClient(app) as client:
        response = client.post("/v1/pull-requests/analyze", json=body, headers=HEADERS)
    assert response.status_code == 422
    fake_queue.assert_not_called()


def test_index_route_accepts_valid_body(fake_queue) -> None:  # noqa: ANN001
    body = {
        "installation_id": 1,
        "repository": {"owner": "acme", "name": "monolith", "github_id": 42},
        "depth": 500,
    }
    with TestClient(app) as client:
        response = client.post("/v1/repositories/index", json=body, headers=HEADERS)
    assert response.status_code == 202, response.text
    fake_queue.assert_called_once()
