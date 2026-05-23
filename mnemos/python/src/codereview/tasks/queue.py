"""RQ connection and enqueue helper.

Phase 1 uses a single default queue. If queue segmentation becomes useful
(index vs analyze, priority bands), add named queues here and switch
``enqueue`` on a ``queue_name`` argument.
"""

from __future__ import annotations

from typing import Any

from redis import Redis
from rq import Queue
from rq.job import Job

from codereview.config import get_settings

_redis: Redis | None = None
_queue: Queue | None = None


def get_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(get_settings().redis_url)
    return _redis


def get_queue() -> Queue:
    global _queue
    if _queue is None:
        _queue = Queue("default", connection=get_redis())
    return _queue


def enqueue(func_path: str, *, payload: dict[str, Any]) -> Job:
    """Enqueue a job referenced by dotted path with a single payload kwarg.

    The worker resolves ``func_path`` at execution time so the API process
    does not need to import job modules at startup.
    """

    return get_queue().enqueue(func_path, payload=payload, job_timeout=600)
