"""RQ worker entry point.

Started inside the docker-compose ``python-worker`` service with::

    python -m codereview.tasks.worker
"""

from __future__ import annotations

from rq import Worker

from codereview.logging import configure_logging, get_logger
from codereview.tasks.queue import get_queue, get_redis

_log = get_logger(__name__)


def main() -> None:  # pragma: no cover - runtime entry point
    configure_logging()
    queue = get_queue()
    worker = Worker([queue], connection=get_redis())
    _log.info("worker_starting", queue=queue.name)
    worker.work(with_scheduler=False)


if __name__ == "__main__":  # pragma: no cover
    main()
