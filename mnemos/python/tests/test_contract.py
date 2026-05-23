"""Cross-service contract tests.

Each fixture under ``fixtures/contract/`` is a request body that the TS
``OrchestratorClient`` posts and this service parses. If either side
drifts, the corresponding test here or in ``typescript/test/contract.test.ts``
will break — that is the intended failure mode.

We also POST each fixture through the full FastAPI stack so middleware,
dependency injection, and response shape are covered in one place.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codereview.api.main import app
from codereview.api.schemas import (
    AnalyzeRequest,
    IncrementalUpdateRequest,
    IndexRequest,
)

HEADERS = {"Authorization": "Bearer test-secret"}

CONTRACT_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "contract"


def _load(name: str) -> dict:
    """Load a contract fixture by filename."""

    path = CONTRACT_DIR / name
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def test_contract_dir_exists() -> None:
    """Fixture directory must live at repo root so TS and Python see the same files."""

    assert CONTRACT_DIR.is_dir(), f"missing contract fixtures at {CONTRACT_DIR}"


# -- Schema validation (no HTTP stack) --------------------------------------


def test_analyze_fixture_matches_schema() -> None:
    body = _load("analyze-request.json")
    parsed = AnalyzeRequest.model_validate(body)
    # Round-trip must be byte-stable except for URL normalisation — serialise
    # and re-parse to catch silently-dropped fields.
    reparsed = AnalyzeRequest.model_validate(parsed.model_dump(mode="json"))
    assert reparsed == parsed


def test_index_fixture_matches_schema() -> None:
    body = _load("index-request.json")
    parsed = IndexRequest.model_validate(body)
    assert parsed.repository.owner == "acme"


def test_incremental_update_fixture_matches_schema() -> None:
    body = _load("incremental-update-request.json")
    parsed = IncrementalUpdateRequest.model_validate(body)
    assert parsed.base_sha != parsed.head_sha


# -- End-to-end through the HTTP stack --------------------------------------


@pytest.mark.parametrize(
    ("fixture_name", "path"),
    [
        ("analyze-request.json", "/v1/pull-requests/analyze"),
        ("index-request.json", "/v1/repositories/index"),
        ("incremental-update-request.json", "/v1/repositories/incremental-update"),
    ],
)
def test_fixture_is_accepted_by_route(
    fake_queue,  # noqa: ANN001
    fixture_name: str,
    path: str,
) -> None:
    body = _load(fixture_name)
    with TestClient(app) as client:
        response = client.post(path, json=body, headers=HEADERS)
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["status"] == "queued"
    assert payload["job_id"].startswith("test-job-")
    fake_queue.assert_called_once()


# -- Guardrails: the fixtures must exercise the validators ------------------


def test_short_callback_secret_still_rejected() -> None:
    """If someone edits the fixture with a too-short secret, the contract
    test still catches drift *away* from validation, not just towards it."""

    body = _load("analyze-request.json")
    body["callback_secret"] = "short"
    with pytest.raises(ValueError):
        AnalyzeRequest.model_validate(body)
