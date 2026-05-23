"""Unit tests for :mod:`codereview.orchestration.degradation`.

Rate-limit detection is heuristic; these tests pin the exact shapes we
recognise so a rename in an upstream SDK becomes a loud test failure
rather than silently breaking the retry.
"""

from __future__ import annotations

import pytest

from codereview.orchestration.degradation import (
    INDEXING_IN_PROGRESS_SUMMARY,
    indexing_in_progress_payload,
    is_rate_limit_error,
    with_jittered_retry,
)

# -- indexing_in_progress_payload ------------------------------------------


def test_indexing_payload_without_counts_uses_default_summary() -> None:
    payload = indexing_in_progress_payload()
    assert payload["summary"] == INDEXING_IN_PROGRESS_SUMMARY
    assert payload["conflicts"] == []


def test_indexing_payload_with_counts_includes_progress() -> None:
    payload = indexing_in_progress_payload(commits_done=12, commits_total=100)
    assert "12 of 100 commits done" in str(payload["summary"])


def test_indexing_payload_ignores_zero_total() -> None:
    # total=0 would divide-by-zero in a naive implementation; make sure
    # the helper falls back to the plain phrasing.
    payload = indexing_in_progress_payload(commits_done=0, commits_total=0)
    assert payload["summary"] == INDEXING_IN_PROGRESS_SUMMARY


def test_indexing_payload_stamps_version_when_passed() -> None:
    payload = indexing_in_progress_payload(mnemos_version="0.1.0-alpha.0")
    assert payload["mnemos_version"] == "0.1.0-alpha.0"


# -- is_rate_limit_error ---------------------------------------------------


class _FakeAnthropicRateLimitError(Exception):
    pass


class _FakeOpenAIError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def test_detects_error_by_class_name_suffix() -> None:
    assert is_rate_limit_error(_FakeAnthropicRateLimitError("slow down"))


def test_detects_error_by_status_code_429() -> None:
    assert is_rate_limit_error(_FakeOpenAIError(429))


def test_does_not_false_positive_on_unrelated_errors() -> None:
    assert not is_rate_limit_error(ValueError("bad input"))
    assert not is_rate_limit_error(_FakeOpenAIError(500))


# -- with_jittered_retry ---------------------------------------------------


async def test_retry_returns_first_success_without_sleeping() -> None:
    calls = 0

    async def op() -> str:
        nonlocal calls
        calls += 1
        return "ok"

    assert await with_jittered_retry(op) == "ok"
    assert calls == 1


async def test_retry_swallows_one_rate_limit_then_succeeds() -> None:
    calls = 0

    async def op() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _FakeAnthropicRateLimitError("backoff")
        return "ok"

    result = await with_jittered_retry(
        op, base_sleep_s=0.0, max_sleep_s=0.0, max_attempts=2
    )
    assert result == "ok"
    assert calls == 2


async def test_retry_reraises_on_second_failure() -> None:
    async def op() -> str:
        raise _FakeAnthropicRateLimitError("still slow")

    with pytest.raises(_FakeAnthropicRateLimitError):
        await with_jittered_retry(
            op, base_sleep_s=0.0, max_sleep_s=0.0, max_attempts=2
        )


async def test_retry_does_not_retry_non_retryable_errors() -> None:
    calls = 0

    async def op() -> str:
        nonlocal calls
        calls += 1
        raise ValueError("not a rate limit")

    with pytest.raises(ValueError):
        await with_jittered_retry(
            op, base_sleep_s=0.0, max_sleep_s=0.0, max_attempts=5
        )
    # Should have tried exactly once — the predicate says "don't retry".
    assert calls == 1
