"""``POST /v1/repositories/incremental-update`` — enqueue an incremental update.

Fires when the TS push handler observes a default-branch advance. The
orchestrator re-ingests the slice of history between ``base_sha`` and
``head_sha`` rather than re-indexing from scratch.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from codereview.api.schemas import IncrementalUpdateRequest, JobAccepted
from codereview.logging import bind_request_context, get_logger
from codereview.tasks.queue import enqueue

router = APIRouter(prefix="/v1/repositories", tags=["incremental-update"])
_log = get_logger(__name__)


@router.post(
    "/incremental-update",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def incremental_update(request: IncrementalUpdateRequest) -> JobAccepted:
    bind_request_context(
        repo=f"{request.repository.owner}/{request.repository.name}",
        installation_id=request.installation_id,
    )
    job = enqueue(
        "codereview.tasks.jobs.run_incremental_update",
        payload=request.model_dump(mode="json"),
    )
    _log.info(
        "incremental_update_enqueued",
        job_id=job.id,
        base_sha=request.base_sha[:7],
        head_sha=request.head_sha[:7],
    )
    return JobAccepted(job_id=job.id, status="queued")
