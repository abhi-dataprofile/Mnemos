"""RQ job entry points.

Phase 1 ships dummy jobs that log and return. Real orchestration for
analysis and indexing lands in Phases 3-6.
"""

from __future__ import annotations

from typing import Any

from codereview.logging import bind_request_context, configure_logging, get_logger

_log = get_logger(__name__)


def run_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    """Dummy analysis job.

    Accepts the serialized ``AnalyzeRequest`` body and logs. Returns a
    trivial result dict so RQ stores something useful.
    """

    configure_logging()
    repo = payload.get("repository", {})
    pr = payload.get("pull_request", {})
    bind_request_context(
        repo=f"{repo.get('owner')}/{repo.get('name')}",
        pr_number=pr.get("number"),
    )
    _log.info("dummy_analysis_ran", head_sha=pr.get("head_sha"))
    return {"status": "ok", "stage": "dummy"}


def run_index(payload: dict[str, Any]) -> dict[str, Any]:
    """Dummy index job."""

    configure_logging()
    repo = payload.get("repository", {})
    bind_request_context(
        repo=f"{repo.get('owner')}/{repo.get('name')}",
        installation_id=payload.get("installation_id"),
    )
    _log.info("dummy_index_ran", depth=payload.get("depth"))
    return {"status": "ok", "stage": "dummy"}
