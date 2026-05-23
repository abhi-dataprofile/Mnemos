"""Structured logging setup.

Every log line is JSON. Every line carries ``job_id``, ``repo_id``, and
``pr_number`` when those have been bound via :func:`bind_request_context`.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars

from codereview.config import get_settings


def configure_logging() -> None:
    """Configure structlog + stdlib logging to emit JSON to stdout.

    Safe to call more than once; the stdlib basicConfig call is idempotent.
    """

    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def bind_request_context(**kwargs: Any) -> None:
    """Bind request-scoped fields so every subsequent log line carries them."""

    bind_contextvars(**{k: v for k, v in kwargs.items() if v is not None})


def clear_request_context() -> None:
    """Clear request-scoped fields at request end."""

    clear_contextvars()


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger for ``name``."""

    return structlog.get_logger(name)
