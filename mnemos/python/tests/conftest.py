"""Shared test fixtures.

The Phase 1 smoke suite does not need Postgres or a real Redis. Tests patch
the queue-enqueue seam so the API layer can be exercised in isolation.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Provide deterministic config for every test."""

    monkeypatch.setenv("INTERNAL_SECRET", "test-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@localhost/x")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")

    # Reset the settings cache so env changes take effect.
    from codereview.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fake_queue(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace ``codereview.tasks.queue.enqueue`` with a MagicMock.

    Routes import ``enqueue`` from the queue module; patch there so both
    import paths see the same mock.
    """

    fake = MagicMock()

    def _enqueue(func_path: str, *, payload: dict) -> MagicMock:  # type: ignore[type-arg]
        job = MagicMock()
        job.id = "test-job-" + os.urandom(4).hex()
        fake(func_path=func_path, payload=payload)
        return job

    monkeypatch.setattr("codereview.tasks.queue.enqueue", _enqueue)
    monkeypatch.setattr("codereview.api.routes.analyze.enqueue", _enqueue)
    monkeypatch.setattr("codereview.api.routes.index.enqueue", _enqueue)
    monkeypatch.setattr("codereview.api.routes.incremental.enqueue", _enqueue)
    return fake
