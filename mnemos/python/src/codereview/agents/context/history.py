"""Recent-commit fetcher for the Context Packager.

For each file touched by the PR, pull the N most recent commits from the
graph. The result is a flat list keyed by SHA with ``file_path`` recorded
so the reviewer can see which commit touched which file. The same SHA
can appear once per touched file when a commit modifies several of the
PR's files; the caller dedupes if they want per-SHA uniqueness.

We cap:
- ``per_file_limit`` — commits to fetch for each file (default 5)
- ``total_limit`` — total commits returned across all files (default 15)

The fetch is serialised per file rather than gathered. PRs touching many
files would otherwise open dozens of DB connections in parallel, and the
query is cheap — serial is both kinder to the DB and easier to reason
about. If this becomes a bottleneck we can switch to ``asyncio.gather``
over ``recent_commits_touching`` calls.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from codereview.agents.context.types import RecentCommit
from codereview.logging import get_logger

__all__ = ["fetch_recent_history"]

_log = get_logger(__name__)

_DEFAULT_PER_FILE = 5
_DEFAULT_TOTAL = 15


async def fetch_recent_history(
    *,
    repo_id: UUID,
    file_paths: list[str],
    graph: Any,
    per_file_limit: int = _DEFAULT_PER_FILE,
    total_limit: int = _DEFAULT_TOTAL,
) -> list[RecentCommit]:
    """Return recent commits touching each file in ``file_paths``.

    Files missing from the graph (e.g. added in this PR) are skipped
    silently — it is not an error for a new file to lack history.
    """

    if not file_paths:
        return []

    file_by_path = getattr(graph, "file_by_path", None)
    recent_fn = getattr(graph, "recent_commits_touching", None)
    if file_by_path is None or recent_fn is None:
        return []

    out: list[RecentCommit] = []
    for path in file_paths:
        if len(out) >= total_limit:
            break
        try:
            file_id = await file_by_path(repo_id, path)
        except Exception as exc:
            _log.warning(
                "context.history.file_lookup_error", path=path, error=repr(exc)
            )
            continue
        if file_id is None:
            continue
        try:
            commits = await recent_fn(file_id, limit=per_file_limit)
        except Exception as exc:
            _log.warning(
                "context.history.commits_error", path=path, error=repr(exc)
            )
            continue
        for c in commits:
            if len(out) >= total_limit:
                break
            out.append(
                RecentCommit(
                    sha=getattr(c, "sha", ""),
                    title=_first_line(getattr(c, "message", "")),
                    url=None,
                    author_login=getattr(c, "author_login", None),
                    file_path=path,
                )
            )
    return out


def _first_line(message: str) -> str | None:
    """Return the first non-empty line of ``message`` or ``None``.

    Commit messages conventionally put the subject on the first line; we
    render only that in the packet to keep the comment short.
    """

    if not message:
        return None
    for line in message.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None
