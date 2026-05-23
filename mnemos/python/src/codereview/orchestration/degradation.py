"""Graceful-degradation helpers.

Three failure modes that should not crash a review:

1. **Indexing in progress.** A PR arrives before the initial graph
   ingest has finished. :func:`indexing_in_progress_payload` builds the
   minimal "not yet" payload the TS side can post verbatim. The footer
   still renders, so users see something reassuring instead of an
   in-progress check spinning forever.

2. **LLM rate limit.** Anthropic (or any OpenAI-compatible endpoint)
   occasionally hits the per-minute limit. :func:`with_jittered_retry`
   gives callers a one-shot retry with sleep of 1-3s; if the second
   attempt still raises, the caller should record the failure as a
   partial-results signal rather than propagating.

3. **Partial results.** If at least one agent produced output, the
   review is shippable. :func:`partial_footer` yields the
   ``failed_agents`` list the formatter already surfaces plus a
   ``partial`` flag on the payload. The orchestrator doesn't have to
   special-case this — agents that crashed just don't contribute
   their section.

None of these helpers talk to the network or the database. That keeps
them trivial to unit-test.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from codereview.logging import get_logger

__all__ = [
    "INDEXING_IN_PROGRESS_SUMMARY",
    "RATE_LIMIT_FOOTER",
    "indexing_in_progress_payload",
    "is_rate_limit_error",
    "with_jittered_retry",
]

_log = get_logger(__name__)

T = TypeVar("T")

INDEXING_IN_PROGRESS_SUMMARY = (
    "Mnemos is still indexing this repository. The next PR opened after "
    "indexing completes will receive a full review."
)

RATE_LIMIT_FOOTER = (
    "One or more checks did not complete because an LLM provider rate-limited "
    "the request. The review below reflects the agents that finished in time."
)


def indexing_in_progress_payload(
    *,
    commits_done: int | None = None,
    commits_total: int | None = None,
    mnemos_version: str | None = None,
) -> dict[str, object]:
    """Build the payload the TS side posts when the graph isn't ready.

    ``commits_done`` / ``commits_total`` are optional — when supplied we
    say "X of Y commits indexed" in the summary. Otherwise we fall back
    to a plain indexing-in-progress line.
    """

    if commits_done is not None and commits_total is not None and commits_total > 0:
        summary = (
            f"Mnemos is still indexing this repository ({commits_done} of "
            f"{commits_total} commits done). The next PR after indexing "
            "completes will receive a full review."
        )
    else:
        summary = INDEXING_IN_PROGRESS_SUMMARY

    payload: dict[str, object] = {
        "summary": summary,
        "conflicts": [],
    }
    if mnemos_version is not None:
        payload["mnemos_version"] = mnemos_version
    return payload


def is_rate_limit_error(exc: BaseException) -> bool:
    """Heuristic: does this exception look like an LLM rate-limit signal?

    Anthropic's SDK raises ``anthropic.RateLimitError`` with a 429
    status; OpenAI and openai-compatible endpoints do the same. We
    duck-type rather than import anthropic to keep this module
    dependency-free for downstream forks using a different provider.
    """

    name = exc.__class__.__name__.lower()
    if "ratelimit" in name or "rate_limit" in name:
        return True
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    return status == 429


async def with_jittered_retry(
    op: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 2,
    base_sleep_s: float = 1.0,
    max_sleep_s: float = 3.0,
    retry_on: Callable[[BaseException], bool] = is_rate_limit_error,
) -> T:
    """Run ``op`` with up to ``max_attempts`` tries.

    On a retryable failure, sleep ``base_sleep_s..max_sleep_s`` seconds
    (uniform) before retrying. The default predicate catches rate-limit
    shaped exceptions; callers can pass a different predicate for other
    transient classes.

    The caller is responsible for turning a final exception into a
    user-facing partial-results payload — this helper re-raises after
    the last attempt.
    """

    last: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await op()
        except Exception as exc:
            last = exc
            if attempt == max_attempts or not retry_on(exc):
                _log.warning(
                    "jittered_retry_giving_up",
                    attempt=attempt,
                    error=repr(exc),
                )
                raise
            sleep_s = random.uniform(base_sleep_s, max_sleep_s)
            _log.info(
                "jittered_retry_sleeping",
                attempt=attempt,
                sleep_s=round(sleep_s, 3),
                error=repr(exc),
            )
            await asyncio.sleep(sleep_s)
    # Unreachable — loop always returns or raises — but keep mypy happy.
    raise RuntimeError("jittered retry exhausted") from last
