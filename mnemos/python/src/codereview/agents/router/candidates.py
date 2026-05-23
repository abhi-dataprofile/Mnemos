"""Candidate pool assembly + signal gathering for the Reviewer Router.

Two logical steps live in this file:

1. **Pool** — union CODEOWNERS owners with recent authors and
   reviewers of the PR's touched files, then strip the PR author,
   bots, and anyone who hasn't been active in 90 days.
2. **Signals** — for each surviving candidate, compute the six signals
   the scoring function reads.

Both steps are tolerant of missing graph surface: any :class:`GraphClient`
method that the caller happens to stub can be missing and we degrade
the corresponding signal to its neutral default rather than raising.
That keeps unit tests ergonomic — a fake graph only has to implement
the methods the test exercises — and matches the duck-typing pattern
already established by the Context Packager (see
``agents/context/packager.py``).
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from codereview.agents.base import AgentContext, ChangedFile
from codereview.agents.router.codeowners import (
    CODEOWNERS_PATHS,
    CodeOwnersMap,
    parse_codeowners,
)
from codereview.agents.router.types import Candidate, Signals
from codereview.logging import get_logger

__all__ = [
    "WINDOW_DAYS",
    "assemble_candidates",
    "gather_signals",
    "is_bot_login",
    "load_codeowners_from_workspace",
]

_log = get_logger(__name__)

# Scoring window for "recent" authorship, review acceptance, and the
# call-graph overlap signal. The plan doc calls for 6 months.
WINDOW_DAYS = 180

# Inactivity cutoff for the candidate pool. Anyone whose last commit /
# review is older than this gets dropped, even if they would have
# matched a signal.
INACTIVE_DAYS = 90

# Bot detection. Any login matching any of these patterns is excluded.
_BOT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r".*-bot$"),
    re.compile(r".*-actions$"),
    re.compile(r".*\[bot\]$"),
)


def is_bot_login(login: str) -> bool:
    """True if ``login`` looks like a GitHub bot or CI account.

    Exported so tests can pin down the exact heuristic without
    re-importing a private.
    """

    return any(p.match(login) is not None for p in _BOT_PATTERNS)


# -- Candidate pool --------------------------------------------------------


async def assemble_candidates(
    ctx: AgentContext,
    *,
    codeowners: CodeOwnersMap | None = None,
    now: dt.datetime | None = None,
) -> list[Candidate]:
    """Build the pool of people who might be asked to review.

    Parameters
    ----------
    ctx:
        The agent context. Only ``pr`` (author + changed files),
        ``repo_id``, and ``graph`` are consulted.
    codeowners:
        Optional pre-parsed :class:`CodeOwnersMap`. In production the
        router loads it from :attr:`AgentContext.workspace_root` via
        :func:`load_codeowners_from_workspace`; tests can pass a
        synthetic map directly.
    now:
        Override for the "current time" used to compute the 90-day
        activity window. Defaults to UTC now.
    """

    now = now or _now_utc()
    activity_cutoff = now - dt.timedelta(days=INACTIVE_DAYS)
    window_start = now - dt.timedelta(days=WINDOW_DAYS)

    paths = [f.path for f in ctx.pr.changed_files]
    file_ids = await _resolve_file_ids(ctx, paths)

    # Walk through each source of candidate logins + person IDs.
    pool: dict[str, Candidate] = {}

    # 1. CODEOWNERS.
    if codeowners is not None:
        for path in paths:
            for owner in codeowners.owners_for(path):
                is_team = "/" in owner
                pool.setdefault(owner, Candidate(login=owner, is_team=is_team))

    # 2. Recent authors per touched file.
    authors = await _collect_authors(ctx, file_ids, window_start)
    for person_id, login in authors:
        _merge_person(pool, login, person_id)

    # 3. Recent reviewers per touched file.
    reviewers = await _collect_reviewers(ctx, file_ids, window_start)
    for person_id, login in reviewers:
        _merge_person(pool, login, person_id)

    # Filter: PR author, bots, inactive humans.
    out: list[Candidate] = []
    for candidate in pool.values():
        if candidate.login == ctx.pr.author:
            continue
        if is_bot_login(candidate.login):
            continue
        if candidate.is_team:
            # Teams aren't "active" the way humans are — skip the
            # activity check for them. They ride through unless the
            # caller explicitly excludes them.
            out.append(candidate)
            continue
        if candidate.person_id is None:
            # Unknown-to-graph CODEOWNERS entry. We can't verify
            # activity; let them through so the CODEOWNERS signal
            # isn't silently lost when the graph is behind.
            out.append(candidate)
            continue
        if not await _is_active(ctx, candidate.person_id, activity_cutoff):
            continue
        out.append(candidate)

    return out


# -- Signal gathering ------------------------------------------------------


@dataclass(slots=True)
class _SignalContext:
    """Pre-computed graph data shared across all candidates.

    Gathering one query's worth of data per candidate would be O(N)
    round-trips; instead we pre-compute per-file authorship and the
    call-graph overlap map once and look each candidate up from those
    dicts.
    """

    file_ids: list[UUID]
    authors_per_file: dict[UUID, dict[UUID, int]]
    """file_id → {person_id: commit_count} over the scoring window."""

    reviewers_per_file: dict[UUID, dict[UUID, int]]
    """file_id → {person_id: review_count} over the scoring window."""

    call_graph_overlap: dict[UUID, int] = field(default_factory=dict)
    """person_id → overlap count. Missing entries score 0."""

    touched_file_count: int = 0
    path_by_file_id: dict[UUID, str] = field(default_factory=dict)


async def gather_signals(
    ctx: AgentContext,
    candidates: list[Candidate],
    *,
    codeowners: CodeOwnersMap | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Signals]:
    """Produce a ``login → Signals`` map for ``candidates``.

    Signals are computed in bulk where possible to avoid O(N) round
    trips. The returned dict key is the candidate ``login`` so callers
    can look up by the same handle they passed in.
    """

    now = now or _now_utc()
    window_start = now - dt.timedelta(days=WINDOW_DAYS)
    paths = [f.path for f in ctx.pr.changed_files]
    file_ids = await _resolve_file_ids(ctx, paths)

    sig_ctx = _SignalContext(
        file_ids=file_ids,
        authors_per_file={},
        reviewers_per_file={},
        touched_file_count=len(paths),
    )
    sig_ctx.path_by_file_id = await _resolve_file_id_to_path(ctx, paths)

    # Pre-compute per-file authorship and reviewer counts.
    for fid in file_ids:
        sig_ctx.authors_per_file[fid] = await _safe_authors_of_file(
            ctx, fid, window_start
        )
        sig_ctx.reviewers_per_file[fid] = await _safe_reviewers_of_file(
            ctx, fid, window_start
        )

    # One shot at the call-graph overlap map for every candidate.
    sig_ctx.call_graph_overlap = await _safe_call_graph_overlap_counts(
        ctx, file_ids, window_start
    )

    out: dict[str, Signals] = {}
    for cand in candidates:
        out[cand.login] = await _signals_for(ctx, cand, sig_ctx, codeowners, window_start)
    return out


# -- Workspace bridge ------------------------------------------------------


def load_codeowners_from_workspace(workspace_root: Any | None) -> CodeOwnersMap | None:
    """Probe the standard CODEOWNERS locations under ``workspace_root``.

    Returns ``None`` when the workspace is unset (e.g. Phase 7
    orchestration hasn't plumbed the checkout yet) or when no
    CODEOWNERS file is present. Never raises; unreadable files log a
    warning and return ``None`` so a broken CODEOWNERS doesn't kill
    the agent.
    """

    if workspace_root is None:
        return None
    from pathlib import Path

    root = Path(workspace_root)
    for rel in CODEOWNERS_PATHS:
        candidate = root / rel
        if candidate.exists():
            try:
                return parse_codeowners(candidate.read_text(encoding="utf-8"))
            except OSError as exc:
                _log.warning(
                    "router.codeowners_read_error",
                    path=str(candidate),
                    error=repr(exc),
                )
                return None
    return None


# -- Internals: pool -------------------------------------------------------


async def _resolve_file_ids(ctx: AgentContext, paths: list[str]) -> list[UUID]:
    file_by_path = getattr(ctx.graph, "file_by_path", None)
    if file_by_path is None:
        return []
    out: list[UUID] = []
    for path in paths:
        try:
            fid = await file_by_path(ctx.repo_id, path)
        except Exception as exc:
            _log.warning(
                "router.file_by_path_error", path=path, error=repr(exc)
            )
            continue
        if fid is not None:
            out.append(fid)
    return out


async def _resolve_file_id_to_path(
    ctx: AgentContext, paths: list[str]
) -> dict[UUID, str]:
    file_by_path = getattr(ctx.graph, "file_by_path", None)
    if file_by_path is None:
        return {}
    out: dict[UUID, str] = {}
    for path in paths:
        try:
            fid = await file_by_path(ctx.repo_id, path)
        except Exception:
            continue
        if fid is not None:
            out[fid] = path
    return out


async def _collect_authors(
    ctx: AgentContext,
    file_ids: list[UUID],
    window_start: dt.datetime,
) -> list[tuple[UUID, str]]:
    authors_of_file = getattr(ctx.graph, "authors_of_file", None)
    if authors_of_file is None:
        return []
    seen: dict[UUID, str] = {}
    for fid in file_ids:
        try:
            rows = await authors_of_file(fid, window_start)
        except Exception as exc:
            _log.warning("router.authors_of_file_error", error=repr(exc))
            continue
        for row in rows:
            seen.setdefault(row.person.id, row.person.github_login)
    return [(pid, login) for pid, login in seen.items()]


async def _collect_reviewers(
    ctx: AgentContext,
    file_ids: list[UUID],
    window_start: dt.datetime,
) -> list[tuple[UUID, str]]:
    reviewers_of_file = getattr(ctx.graph, "reviewers_of_file", None)
    if reviewers_of_file is None:
        return []
    seen: dict[UUID, str] = {}
    for fid in file_ids:
        try:
            rows = await reviewers_of_file(fid, window_start)
        except Exception as exc:
            _log.warning("router.reviewers_of_file_error", error=repr(exc))
            continue
        for row in rows:
            seen.setdefault(row.person.id, row.person.github_login)
    return [(pid, login) for pid, login in seen.items()]


def _merge_person(
    pool: dict[str, Candidate], login: str, person_id: UUID
) -> None:
    existing = pool.get(login)
    if existing is None:
        pool[login] = Candidate(login=login, person_id=person_id)
    elif existing.person_id is None:
        # CODEOWNERS found this login first without an ID; backfill.
        pool[login] = Candidate(
            login=login, person_id=person_id, is_team=existing.is_team
        )


async def _is_active(
    ctx: AgentContext,
    person_id: UUID,
    activity_cutoff: dt.datetime,
) -> bool:
    last_activity_at = getattr(ctx.graph, "last_activity_at", None)
    if last_activity_at is None:
        # Without the graph primitive we can't confirm inactivity; err
        # on the side of including the candidate.
        return True
    try:
        last = await last_activity_at(person_id)
    except Exception as exc:
        _log.warning("router.last_activity_error", error=repr(exc))
        return True
    if last is None:
        return False
    # The graph sometimes returns naive datetimes (SQLite backend);
    # treat those as UTC so comparison with the tz-aware cutoff works.
    if last.tzinfo is None:
        last = last.replace(tzinfo=dt.timezone.utc)  # noqa: UP017
    return last >= activity_cutoff


# -- Internals: signals ----------------------------------------------------


async def _signals_for(
    ctx: AgentContext,
    cand: Candidate,
    sig_ctx: _SignalContext,
    codeowners: CodeOwnersMap | None,
    window_start: dt.datetime,
) -> Signals:
    s = Signals()

    # 1. CODEOWNERS match.
    if codeowners is not None:
        for path in sig_ctx.path_by_file_id.values():
            if cand.login in codeowners.owners_for(path):
                s.is_codeowner = True
                break

    # 2. Authorship share + authored file list.
    authored_files: list[str] = []
    if cand.person_id is not None and sig_ctx.touched_file_count:
        for fid, per_person in sig_ctx.authors_per_file.items():
            if cand.person_id in per_person:
                path = sig_ctx.path_by_file_id.get(fid)
                if path is not None:
                    authored_files.append(path)
    s.authored_files = tuple(authored_files)
    if sig_ctx.touched_file_count:
        s.authorship_share = len(authored_files) / sig_ctx.touched_file_count

    # 3. Recent review count across touched files.
    review_count = 0
    if cand.person_id is not None:
        for per_person in sig_ctx.reviewers_per_file.values():
            review_count += per_person.get(cand.person_id, 0)
    s.recent_review_count = review_count

    # 4. Call-graph overlap.
    if cand.person_id is not None:
        s.call_graph_overlap = sig_ctx.call_graph_overlap.get(cand.person_id, 0)

    # 5. Review acceptance rate (global, not touched-file scoped).
    if cand.person_id is not None:
        approved, total = await _safe_review_acceptance_rate(
            ctx, cand.person_id, window_start
        )
        s.total_reviews = total
        s.review_acceptance_rate = (approved / total) if total else 0.0

    # 6. Open PR load.
    if cand.person_id is not None:
        s.open_pr_load = await _safe_open_prs_assigned_to(ctx, cand.person_id)

    return s


async def _safe_authors_of_file(
    ctx: AgentContext, file_id: UUID, since: dt.datetime
) -> dict[UUID, int]:
    authors_of_file = getattr(ctx.graph, "authors_of_file", None)
    if authors_of_file is None:
        return {}
    try:
        rows = await authors_of_file(file_id, since)
    except Exception:
        return {}
    return {row.person.id: row.count for row in rows}


async def _safe_reviewers_of_file(
    ctx: AgentContext, file_id: UUID, since: dt.datetime
) -> dict[UUID, int]:
    reviewers_of_file = getattr(ctx.graph, "reviewers_of_file", None)
    if reviewers_of_file is None:
        return {}
    try:
        rows = await reviewers_of_file(file_id, since)
    except Exception:
        return {}
    return {row.person.id: row.count for row in rows}


async def _safe_call_graph_overlap_counts(
    ctx: AgentContext, file_ids: list[UUID], since: dt.datetime
) -> dict[UUID, int]:
    fn = getattr(ctx.graph, "call_graph_overlap_counts", None)
    if fn is None:
        return {}
    try:
        return dict(await fn(file_ids, since))
    except Exception as exc:
        _log.warning("router.call_graph_overlap_error", error=repr(exc))
        return {}


async def _safe_review_acceptance_rate(
    ctx: AgentContext, person_id: UUID, since: dt.datetime
) -> tuple[int, int]:
    fn = getattr(ctx.graph, "review_acceptance_rate", None)
    if fn is None:
        return (0, 0)
    try:
        approved, total = await fn(person_id, since)
    except Exception:
        return (0, 0)
    return (int(approved), int(total))


async def _safe_open_prs_assigned_to(
    ctx: AgentContext, person_id: UUID
) -> int:
    fn = getattr(ctx.graph, "open_prs_assigned_to", None)
    if fn is None:
        return 0
    try:
        return int(await fn(person_id))
    except Exception:
        return 0


def _now_utc() -> dt.datetime:
    # 3.10-compatible spelling mirrors agents/context/packager.py.
    return dt.datetime.now(tz=dt.timezone.utc)  # noqa: UP017


_ = ChangedFile  # keep the import stable for future type hints
