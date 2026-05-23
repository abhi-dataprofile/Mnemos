"""``POST /v1/repositories/index`` — enqueue an initial or re-index job."""

from __future__ import annotations

from fastapi import APIRouter, status

from codereview.api.schemas import IndexRequest, JobAccepted
from codereview.logging import bind_request_context, get_logger
from codereview.tasks.queue import enqueue

router = APIRouter(prefix="/v1/repositories", tags=["index"])
_log = get_logger(__name__)


@router.post(
    "/index",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def index(request: IndexRequest) -> JobAccepted:
    bind_request_context(
        repo=f"{request.repository.owner}/{request.repository.name}",
        installation_id=request.installation_id,
    )
    job = enqueue(
        "codereview.tasks.jobs.run_index",
        payload=request.model_dump(mode="json"),
    )
    _log.info("index_enqueued", job_id=job.id, depth=request.depth)
    return JobAccepted(job_id=job.id, status="queued")
