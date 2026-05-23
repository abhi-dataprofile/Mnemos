"""Risk-note heuristics for the Context Packager.

Three cheap signals, no LLM:

1. **Recently reverted** — any touched file appears in a commit whose
   message starts with ``revert`` in the last 30 days.
2. **High churn** — any touched file has more than ``threshold`` commits
   in the last 30 days (default 20).
3. **Large PR** — total diff lines across all files exceed a threshold
   (default 500).

Tone is deliberately neutral per the plan: "this file had 25 commits in
the last 30 days", not "this is a scary file". The risk notes are hints
for a human reviewer, not judgements.

Windows are measured from a caller-supplied ``now`` for testability.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from codereview.agents.base import ChangedFile
from codereview.logging import get_logger

__all__ = ["compute_risk_notes"]

_log = get_logger(__name__)

_DEFAULT_WINDOW_DAYS = 30
_DEFAULT_CHURN_THRESHOLD = 20
_DEFAULT_LARGE_PR_LINES = 500


async def compute_risk_notes(
    *,
    repo_id: UUID,
    changed_files: list[ChangedFile],
    graph: Any,
    now: dt.datetime,
    churn_threshold: int = _DEFAULT_CHURN_THRESHOLD,
    large_pr_threshold: int = _DEFAULT_LARGE_PR_LINES,
    window_days: int = _DEFAULT_WINDOW_DAYS,
) -> list[str]:
    """Return a list of neutral, one-line risk notes."""

    notes: list[str] = []

    # Heuristic 3 is pure arithmetic — emit it first so the packet has
    # *something* even if graph calls fail below.
    total_lines = _total_diff_lines(changed_files)
    if total_lines > large_pr_threshold:
        notes.append(
            f"Large PR: the diff touches {total_lines} lines across "
            f"{len(changed_files)} file(s)."
        )

    if not changed_files:
        return notes

    file_by_path = getattr(graph, "file_by_path", None)
    revert_fn = getattr(graph, "revert_commits_touching", None)
    churn_fn = getattr(graph, "commit_count_for_file_since", None)
    if file_by_path is None:
        return notes

    since = now - dt.timedelta(days=window_days)

    for cf in changed_files:
        try:
            file_id = await file_by_path(repo_id, cf.path)
        except Exception as exc:
            _log.warning(
                "context.risk.file_lookup_error", path=cf.path, error=repr(exc)
            )
            continue
        if file_id is None:
            continue

        if revert_fn is not None:
            try:
                reverts = await revert_fn(file_id, since)
            except Exception as exc:
                _log.warning(
                    "context.risk.revert_error", path=cf.path, error=repr(exc)
                )
                reverts = []
            if reverts:
                shas = ", ".join(
                    getattr(c, "sha", "?")[:7] for c in reverts[:3]
                )
                notes.append(
                    f"Recently reverted: {cf.path} was touched by "
                    f"{len(reverts)} revert commit(s) in the last "
                    f"{window_days} days ({shas})."
                )

        if churn_fn is not None:
            try:
                count = await churn_fn(file_id, since)
            except Exception as exc:
                _log.warning(
                    "context.risk.churn_error", path=cf.path, error=repr(exc)
                )
                count = 0
            if count > churn_threshold:
                notes.append(
                    f"High churn: {cf.path} had {count} commits in the "
                    f"last {window_days} days."
                )

    return notes


def _total_diff_lines(changed_files: list[ChangedFile]) -> int:
    """Count added + removed lines across every patch.

    Lines are ``+``/``-`` prefixed bodies in the unified-diff patch;
    hunk headers (``@@``) and context lines are not counted.
    """

    total = 0
    for cf in changed_files:
        patch = cf.patch or ""
        for line in patch.splitlines():
            # Unified-diff convention: +++ and --- are file headers, not
            # content. Skip those but count every other +/- line.
            if line.startswith(("+++", "---")):
                continue
            if line.startswith(("+", "-")):
                total += 1
    return total
