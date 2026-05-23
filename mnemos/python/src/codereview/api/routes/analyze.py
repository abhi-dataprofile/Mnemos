"""``POST /v1/pull-requests/analyze`` — enqueue a PR analysis job.

Phase 1 enqueues a dummy RQ job that the worker logs and returns from.
Phases 3-6 replace the dummy with the real coordinator.
"""

from __future__ import annotations

from fastapi import APIRouter, status
from rq.job import Job

from codereview.api.schemas import AnalyzeRequest, JobAccepted
from codereview.logging import bind_request_context, get_logger
from codereview.tasks.queue import enqueue

router = APIRouter(prefix="/v1/pull-requests", tags=["analyze"])
_log = get_logger(__name__)


@router.post(
    "/analyze",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def analyze(request: AnalyzeRequest) -> JobAccepted:
    bind_request_context(
        repo=f"{request.repository.owner}/{request.repository.name}",
        pr_number=request.pull_request.number,
    )
    job: Job = enqueue(
        "codereview.tasks.jobs.run_analysis",
        payload=request.model_dump(mode="json"),
    )
    _log.info(
        "analyze_enqueued",
        job_id=job.id,
        head_sha=request.pull_request.head_sha,
        callback_url=str(request.callback_url),
    )
    return JobAccepted(job_id=job.id, status="queued")
